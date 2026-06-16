"""
Integration tests for M4 failure scenarios — TASK 4-1C.

10 scenarios, each verifying:
  1. 신규 주문 차단 여부 (new-order blocking)
  2. Kill Switch 동작 여부 (kill switch activation)
  3. Recovery 가능 여부 (recovery possible)
  4. 상태 일관성 유지 여부 (state consistency)
  5. Audit Log 생성 여부 (audit log creation)
"""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.worker.runner as runner
from backend.brokers.models import Balance, Order as BOrder, OrderStatus, Position as BPosition
from backend.database.models import (
    AuditLog, Base, DailyRiskState,
    Fill as DBFill, Order as DBOrder, Position as DBPosition,
    ReconciliationLog, StrategyRun,
)
from backend.execution.circuit_breaker import ConsecutiveFailureBreaker
from backend.execution.idempotency import IdempotencyKey, IdempotencyStore
from backend.execution.order_machine import OrderStateMachine
from backend.execution.order_poller import OrderFillPoller
from backend.execution.position_tracker import Fill, PositionTracker
from backend.execution.reconciler import PositionReconciler
from backend.data.stale_detector import (
    StaleDataDetectionService, StaleFeedError, StaleState, TradingGate,
)
from backend.risk.kill_switch import (
    KillReasonLog, KillSwitch, KillTriggerEngine, NotificationHook,
    OrderIntent, RecoveryManager, Severity, TradingState,
)
from backend.worker.recovery import SAFE_MODE, StartupRecovery


_FIXED_NOW = datetime(2026, 6, 6, 9, 37, 0, tzinfo=timezone.utc)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def patched_runner_factory(db_factory, monkeypatch):
    monkeypatch.setattr(runner, "_SessionFactory", db_factory)
    return db_factory


@pytest.fixture(autouse=True)
def _reset_safe_mode():
    """SAFE_MODE is a process-level singleton — reset before/after each test."""
    SAFE_MODE._can_trade = False
    SAFE_MODE._reason = "초기화 중"
    yield
    SAFE_MODE._can_trade = False
    SAFE_MODE._reason = "초기화 중"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _count_audit(factory, event_type: str) -> int:
    sess = factory()
    try:
        return sess.query(AuditLog).filter(AuditLog.event_type == event_type).count()
    finally:
        sess.close()


def _bare_worker(poller=None):
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w._poller = poller
    w._loss_tracker = None
    return w


def _tracker():
    return PositionTracker(OrderStateMachine())


def _insert_order(factory, broker_order_id, symbol, broker="kis", status="submitted",
                  side="buy", qty=10, price=70000.0, filled_qty=0, avg_fill_price=None,
                  created_at=None):
    sess = factory()
    row = DBOrder(
        broker_order_id=broker_order_id, symbol=symbol, side=side, qty=qty, price=price,
        filled_qty=filled_qty, avg_fill_price=avg_fill_price,
        status=status, market="KR", broker=broker,
        created_at=created_at or datetime.utcnow(),
    )
    sess.add(row)
    sess.commit()
    oid = row.id
    sess.close()
    return oid


def _broker_order(status: OrderStatus, filled_qty=0, avg_fill_price=70000.0,
                  broker_order_id="ORD001", symbol="005930", qty=10, side="buy",
                  price=70000.0) -> BOrder:
    return BOrder(id=broker_order_id, symbol=symbol, side=side, qty=qty, price=price,
                  status=status, filled_qty=filled_qty, avg_fill_price=avg_fill_price)


def _kill_switch(*, trigger_engine=None, db_factory=None, _now=None,
                 cooldown_seconds=300.0, validation_checks=None):
    """Mirrors backend/risk/tests/test_kill_switch.py::_kill_switch."""
    reason_log = KillReasonLog(db_factory=db_factory)
    notify = NotificationHook()
    recovery = RecoveryManager(cooldown_seconds=cooldown_seconds,
                               validation_checks=validation_checks)
    ks = KillSwitch(trigger_engine=trigger_engine, reason_log=reason_log,
                    recovery_manager=recovery, notification_hook=notify,
                    db_factory=db_factory, _now=_now)
    return ks, reason_log, notify, recovery


def _healthy_broker():
    """MagicMock broker that passes startup recovery steps cleanly."""
    from unittest.mock import MagicMock
    broker = MagicMock()
    broker.get_balance.return_value = Balance(cash_krw=0.0, cash_usd=0.0, total_eval_krw=1_000_000.0)
    broker.get_positions.return_value = []
    broker.get_order_status.return_value = None
    return broker


class FakeRedis:
    """Thread-safe dict-backed Redis stub."""

    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def setex(self, key, ttl, value):
        with self._lock:
            self._data[key] = str(value)

    def get(self, key):
        with self._lock:
            val = self._data.get(key)
            return val.encode() if isinstance(val, str) else val

    def set(self, key, value, nx=False, ex=None):
        with self._lock:
            if nx and key in self._data:
                return False
            self._data[key] = str(value)
            return True

    def delete(self, key):
        with self._lock:
            self._data.pop(key, None)

    def ping(self):
        return True


class BrokenRedis:
    """Every call raises ConnectionError — simulates Redis Down."""

    def setex(self, *a, **kw):
        raise ConnectionError("Redis down")

    def get(self, *a, **kw):
        raise ConnectionError("Redis down")

    def set(self, *a, **kw):
        raise ConnectionError("Redis down")

    def delete(self, *a, **kw):
        raise ConnectionError("Redis down")

    def ping(self):
        raise ConnectionError("Redis down")


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 1 — Redis Down
# ═══════════════════════════════════════════════════════════════════════════════

class TestRedisDown:
    """Redis Down: system degrades to DB-only mode, trading continues."""

    def test_redis_down_non_fatal(self, db_factory):
        broker = _healthy_broker()
        rec = StartupRecovery(
            db_session_factory=db_factory,
            redis_client=BrokenRedis(),
            broker=broker,
        )

        # 신규 주문 차단: Redis failure alone must NOT block trading
        result = rec.run()
        assert result is True
        assert SAFE_MODE.can_trade is True

        # 상태 일관성: StartupRecovery made no order/position/risk DB mutations
        sess = db_factory()
        try:
            assert sess.query(DBOrder).count() == 0
            assert sess.query(DBPosition).count() == 0
            assert sess.query(DailyRiskState).count() == 0
        finally:
            sess.close()

        # Kill Switch: Redis outage is not a KillTriggerEngine condition
        ks, _, _, _ = _kill_switch(db_factory=db_factory, _now=_FIXED_NOW)
        assert ks.state is TradingState.RUNNING

        # Recovery: IdempotencyStore falls back to DB when Redis is broken
        key = IdempotencyKey.from_order_params(
            "005930", "buy", 10, 70000.0, _now=_FIXED_NOW
        )
        store = IdempotencyStore(redis_client=BrokenRedis(), db_factory=db_factory)
        store.set(key.fingerprint, "ORD001", "submitted")
        rec_rec = store.get(key.fingerprint)
        assert rec_rec is not None
        assert rec_rec.order_id == "ORD001"

        # Audit Log: IdempotencyStore DB-fallback write produces an audit row
        assert _count_audit(db_factory, "idempotency_record") == 1

    def test_broken_redis_acquire_lock_fail_open(self, db_factory):
        """IdempotencyStore.acquire_lock() fails-open when Redis is down."""
        store = IdempotencyStore(redis_client=BrokenRedis(), db_factory=db_factory)
        key = IdempotencyKey.from_order_params(
            "005930", "buy", 10, 70000.0, _now=_FIXED_NOW
        )
        # Fail-open: lock returns True (not False) when Redis raises
        acquired = store.acquire_lock(key.fingerprint)
        assert acquired is True


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 2 — Worker Restart
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkerRestart:
    """Worker Restart: positions and pending orders restored; SAFE_MODE re-enabled."""

    def _seed_db(self, factory):
        sess = factory()
        sess.add(StrategyRun(strategy_type="indicator", name="test", broker="kis", is_active=True))
        sess.add(DBPosition(symbol="005930", qty=10, avg_price=70000.0, market="KR", broker="kis"))
        row = DBOrder(
            broker_order_id="KIS001", symbol="005930", side="buy", qty=10, price=70000.0,
            status="submitted", market="KR", broker="kis",
        )
        sess.add(row)
        sess.commit()
        sess.close()

    def test_pending_lock_restored(self, patched_runner_factory):
        db_factory = patched_runner_factory
        self._seed_db(db_factory)

        w = _bare_worker(poller=None)
        tracker = _tracker()

        # 신규 주문 차단: before restore, order can be placed
        assert tracker.can_place_order("005930") is True

        w._restore_pending_to_tracker(
            tracker, broker="kis", on_filled_cb=None, on_timeout_cb=None,
        )

        # After restore, the in-flight order's symbol is locked
        assert tracker.can_place_order("005930") is False

        # Audit Log: each restored pending order writes a row
        assert _count_audit(db_factory, "recovery_restore_pending") == 1

    def test_positions_restored(self, patched_runner_factory):
        db_factory = patched_runner_factory
        self._seed_db(db_factory)

        w = _bare_worker()
        tracker = _tracker()
        w._restore_positions(tracker, broker="kis")

        # 상태 일관성: position qty matches what was seeded
        pos = tracker.get_position("005930")
        assert pos is not None
        assert pos.qty == 10
        assert pos.avg_price == 70000.0

    def test_startup_recovery_enables_trading(self, db_factory):
        # Kill Switch / Recovery: clean restart re-enables SAFE_MODE
        rec = StartupRecovery(db_session_factory=db_factory, redis_client=None, broker=None)
        result = rec.run()
        assert result is True
        assert SAFE_MODE.can_trade is True


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 3 — Process Kill
# ═══════════════════════════════════════════════════════════════════════════════

class TestProcessKill:
    """Process Kill: orphaned order detected; in-flight order's lock restored."""

    def _seed_db(self, factory):
        sess = factory()
        # Order A: orphaned — process died before broker ack
        sess.add(DBOrder(
            broker_order_id=None, symbol="069500", side="buy", qty=5,
            price=30000.0, status="submitted", market="KR", broker="kis",
        ))
        # Order B: properly broker-acked, in flight at kill time
        sess.add(DBOrder(
            broker_order_id="KIS001", symbol="005930", side="buy", qty=10,
            price=70000.0, status="submitted", market="KR", broker="kis",
        ))
        sess.commit()
        sess.close()

    def test_orphaned_order_detected(self, db_factory):
        self._seed_db(db_factory)
        rec = StartupRecovery(db_session_factory=db_factory, broker=None)

        # Recovery + Kill Switch: _step_validate_state is non-fatal; run() succeeds despite orphan
        result = rec.run()
        assert result is True
        assert SAFE_MODE.can_trade is True

        # 상태 일관성: orphaned order row is NOT mutated by recovery
        sess = db_factory()
        orphan_count = sess.query(DBOrder).filter(
            DBOrder.broker_order_id.is_(None)
        ).count()
        sess.close()
        assert orphan_count == 1

        # Audit Log: exactly one "recovery_inconsistency" for orphaned order
        assert _count_audit(db_factory, "recovery_inconsistency") == 1
        sess = db_factory()
        row = sess.query(AuditLog).filter(
            AuditLog.event_type == "recovery_inconsistency"
        ).first()
        sess.close()
        detail = json.loads(row.detail)
        assert detail["kind"] == "orphaned_pending_order"

    def test_inflight_order_blocks_new_orders(self, patched_runner_factory):
        db_factory = patched_runner_factory
        self._seed_db(db_factory)

        w = _bare_worker(poller=None)
        tracker = _tracker()

        # 신규 주문 차단: in-flight order (Order B) locks the symbol
        assert tracker.can_place_order("005930") is True
        w._restore_pending_to_tracker(
            tracker, broker="kis", on_filled_cb=None, on_timeout_cb=None,
        )
        assert tracker.can_place_order("005930") is False


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 4 — Network Timeout
# ═══════════════════════════════════════════════════════════════════════════════

class TestNetworkTimeout:
    """Network Timeout: _step_balance failure disables SAFE_MODE; recovers on retry."""

    def test_timeout_blocks_trading_then_recovers(self, db_factory):
        from unittest.mock import MagicMock

        broker = MagicMock()
        broker.get_balance.side_effect = TimeoutError("KIS API timeout")

        # 신규 주문 차단: balance timeout → run() fails → SAFE_MODE disabled
        rec = StartupRecovery(db_session_factory=db_factory, broker=broker)
        result = rec.run()
        assert result is False
        assert SAFE_MODE.can_trade is False

        # Kill Switch: bridge explicit kill-switch signal (KillSwitch not auto-wired)
        ks, _, _, _ = _kill_switch(
            trigger_engine=KillTriggerEngine(broker_failure_critical=5),
            db_factory=db_factory,
            _now=_FIXED_NOW,
        )
        ks.report_broker_failure(5, detail={"step": "balance"}, _now=_FIXED_NOW)
        assert ks.state is TradingState.HALTED
        assert ks.check_order(OrderIntent.NEW).allowed is False

        # 상태 일관성: no DB mutations from failed run()
        sess = db_factory()
        assert sess.query(DBOrder).count() == 0
        sess.close()

        # Recovery: resume kill switch, clear broker error → new StartupRecovery succeeds
        outcome = ks.resume(requested_by="operator", _now=_FIXED_NOW + timedelta(seconds=301))
        assert outcome.approved is True
        SAFE_MODE._can_trade = False
        broker.get_balance.side_effect = None
        broker.get_balance.return_value = Balance(cash_krw=0, cash_usd=0, total_eval_krw=1_000_000.0)
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = None
        rec2 = StartupRecovery(db_session_factory=db_factory, broker=broker)
        result2 = rec2.run()
        assert result2 is True
        assert SAFE_MODE.can_trade is True

        # Audit Log: KillSwitch bridge writes kill_switch_event row
        assert _count_audit(db_factory, "kill_switch_event") >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 5 — Broker API Failure
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrokerApiFailure:
    """Broker API Failure: circuit breaker opens after threshold; kill switch halts."""

    def test_circuit_opens_and_recovers(self, db_factory):
        breaker = ConsecutiveFailureBreaker(threshold=5, cooldown_minutes=10)

        # 신규 주문 차단: 5 failures open the circuit
        for _ in range(5):
            breaker.record_failure()
        assert breaker.is_open() is True

        # Kill Switch: bridge kill-switch signal
        ks, _, _, _ = _kill_switch(
            trigger_engine=KillTriggerEngine(broker_failure_critical=5),
            db_factory=db_factory,
            _now=_FIXED_NOW,
        )
        event = ks.report_broker_failure(5, detail={"broker": "kis"}, _now=_FIXED_NOW)
        assert event is not None
        assert ks.state is TradingState.HALTED
        assert ks.check_order(OrderIntent.NEW).allowed is False

        # 상태 일관성: DailyRiskState.kill_switch=True written to DB
        sess = db_factory()
        row = sess.query(DailyRiskState).first()
        sess.close()
        assert row is not None and row.kill_switch is True

        # Recovery: circuit heals after one success
        breaker.record_success()
        assert breaker.is_open() is False

        # Kill Switch recovery: resume after cooldown
        resume_outcome = ks.resume(
            requested_by="operator",
            _now=_FIXED_NOW + timedelta(seconds=301),
        )
        assert resume_outcome.approved is True
        assert ks.state is TradingState.RUNNING

        # 상태 일관성 after resume: DailyRiskState.kill_switch=False
        sess = db_factory()
        row = sess.query(DailyRiskState).first()
        sess.close()
        assert row.kill_switch is False

        # Audit Log
        assert _count_audit(db_factory, "kill_switch_event") >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 6 — Polling Failure
# ═══════════════════════════════════════════════════════════════════════════════

class TestPollingFailure:
    """Polling Failure: 10 consecutive errors mark health as unhealthy; recovers."""

    def test_poller_unhealthy_then_recovers(self, db_factory):
        from unittest.mock import MagicMock

        broker = MagicMock()
        broker.get_order_status.side_effect = RuntimeError("connection timeout")

        _insert_order(db_factory, broker_order_id="ORD001", symbol="005930",
                      broker="kis", status="submitted", filled_qty=0)

        poller = OrderFillPoller(broker=broker, db_factory=db_factory)
        order = _broker_order(OrderStatus.SUBMITTED, broker_order_id="ORD001", symbol="005930")
        on_filled_mock = MagicMock()
        poller.register(order, on_filled=on_filled_mock)

        entry = poller._entries["ORD001"]

        # Inject 10 consecutive broker failures
        for _ in range(10):
            poller._poll_one(entry)

        # 신규 주문 차단: poller unhealthy → bridge KillSwitch
        assert poller.health.is_healthy is False

        ks, _, _, _ = _kill_switch(db_factory=db_factory, _now=_FIXED_NOW)
        ks.report_watchdog_failure(
            Severity.CRITICAL,
            "poller unhealthy: 10 consecutive errors",
            detail={"consecutive_poll_errors": 10},
            _now=_FIXED_NOW,
        )
        assert ks.check_order(OrderIntent.NEW).allowed is False

        # Kill Switch: CRITICAL → HALTED
        assert ks.state is TradingState.HALTED

        # 상태 일관성: failed polls do NOT update DBOrder
        sess = db_factory()
        try:
            db_order = sess.query(DBOrder).filter(DBOrder.broker_order_id == "ORD001").one()
            assert db_order.status == "submitted"
            assert db_order.filled_qty == 0
            assert sess.query(DBFill).count() == 0
        finally:
            sess.close()
        # poll_index increased with each failure
        assert entry.poll_index == 10

        # Recovery: one successful poll resets health
        broker.get_order_status.side_effect = None
        broker.get_order_status.return_value = _broker_order(
            OrderStatus.SUBMITTED, broker_order_id="ORD001", symbol="005930"
        )
        poller._poll_one(entry)
        assert poller.health.is_healthy is True

        # on_filled never called during failures (and order stayed SUBMITTED so still no fill)
        assert on_filled_mock.call_count == 0

        # Audit Log: kill_switch_event from bridge
        assert _count_audit(db_factory, "kill_switch_event") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 7 — Reconciliation Failure
# ═══════════════════════════════════════════════════════════════════════════════

class TestReconciliationFailure:
    """Reconciliation Failure: qty mismatch detected, repaired, KillSwitch escalates."""

    def _seed_position(self, factory, qty=10):
        sess = factory()
        sess.add(DBPosition(
            symbol="005930", qty=qty, avg_price=70000.0, market="KR", broker="kis",
        ))
        sess.commit()
        sess.close()

    def _mock_broker_with_qty(self, qty=15):
        from unittest.mock import MagicMock
        broker = MagicMock()
        broker.get_positions.return_value = [
            BPosition(symbol="005930", qty=qty, avg_price=70000.0, market="KR"),
        ]
        broker.get_order_status.return_value = None
        return broker

    def test_single_mismatch_warns_but_allows(self, db_factory):
        self._seed_position(db_factory, qty=10)
        broker = self._mock_broker_with_qty(qty=15)

        engine = PositionReconciler(broker=broker, db_factory=db_factory, broker_name="kis")
        result = engine.reconcile("periodic")

        # 신규 주문 차단 / Kill Switch (WARNING only, NEW still allowed)
        assert len(result.gaps) >= 1
        assert result.gaps[0]["kind"] == "qty_mismatch"

        ks, _, _, _ = _kill_switch(db_factory=db_factory, _now=_FIXED_NOW)
        ks.report_reconciliation_mismatch(1, detail={"symbol": "005930"}, _now=_FIXED_NOW)
        assert ks.state is TradingState.WARNING
        assert ks.check_order(OrderIntent.NEW).allowed is True  # WARNING still allows

        # 상태 일관성: position qty repaired to broker's value
        sess = db_factory()
        pos = sess.query(DBPosition).filter(DBPosition.symbol == "005930").first()
        sess.close()
        assert pos.qty == 15

        # Recovery: re-reconcile after repair finds no gaps
        result2 = engine.reconcile("periodic")
        assert result2.gaps == []

        # Audit Log: reconcile_fix_qty written + kill_switch_event
        assert _count_audit(db_factory, "reconcile_fix_qty") == 1
        assert _count_audit(db_factory, "kill_switch_event") == 1

        # ReconciliationLog row persisted
        sess = db_factory()
        log_count = sess.query(ReconciliationLog).count()
        sess.close()
        assert log_count >= 1

    def test_repeated_mismatch_halts_and_blocks(self, db_factory):
        self._seed_position(db_factory, qty=10)
        broker = self._mock_broker_with_qty(qty=15)

        engine = PositionReconciler(broker=broker, db_factory=db_factory, broker_name="kis")
        engine.reconcile("periodic")

        # Kill Switch: 3 mismatches (critical threshold=3) → HALTED
        ks, _, _, _ = _kill_switch(db_factory=db_factory, _now=_FIXED_NOW)
        ks.report_reconciliation_mismatch(3, detail={"symbol": "005930"}, _now=_FIXED_NOW)
        assert ks.state is TradingState.HALTED

        # 신규 주문 차단: HALTED blocks NEW
        assert ks.check_order(OrderIntent.NEW).allowed is False

        # 상태 일관성: DailyRiskState.kill_switch=True
        sess = db_factory()
        risk_row = sess.query(DailyRiskState).first()
        sess.close()
        assert risk_row is not None and risk_row.kill_switch is True

        # Recovery: resume after cooldown
        outcome = ks.resume(
            requested_by="operator",
            _now=_FIXED_NOW + timedelta(seconds=301),
        )
        assert outcome.approved is True
        assert ks.state is TradingState.RUNNING

        # Audit Log
        assert _count_audit(db_factory, "kill_switch_event") >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 8 — Stale Data
# ═══════════════════════════════════════════════════════════════════════════════

class TestStaleData:
    """Stale Data: WARNING → STALE → FRESH lifecycle, KillSwitch bridge, StaleFeedError."""

    _KEY = "005930:1m"

    def test_stale_lifecycle(self, db_factory):
        svc = StaleDataDetectionService(db_factory=db_factory)
        gate = TradingGate(block_on_unknown=True)

        t0 = _FIXED_NOW
        # Update feed at t0
        svc.record_update(self._KEY, ts=t0, now=t0)

        # ── WARNING (>300s, <600s) ──
        warn_time = t0 + timedelta(seconds=350)
        warn_result = svc.check(self._KEY, now=warn_time)
        assert warn_result.state is StaleState.WARNING
        assert not gate.is_blocking(warn_result)  # WARNING does not block

        # Kill Switch (WARNING → TradingState.WARNING, NEW still allowed)
        ks, _, _, _ = _kill_switch(db_factory=db_factory, _now=warn_time)
        ks.report_watchdog_failure(
            Severity.WARNING,
            f"OHLCV stale: {self._KEY}",
            detail={"key": self._KEY},
            _now=warn_time,
        )
        assert ks.state is TradingState.WARNING
        assert ks.check_order(OrderIntent.NEW).allowed is True

        # ── STALE (>600s) ──
        stale_time = t0 + timedelta(seconds=700)
        stale_result = svc.check(self._KEY, now=stale_time)
        assert stale_result.state is StaleState.STALE

        # 신규 주문 차단: TradingGate blocks and raises StaleFeedError
        assert gate.is_blocking(stale_result) is True
        with pytest.raises(StaleFeedError):
            gate.assert_fresh(stale_result)

        # Also available via service's assert_fresh
        with pytest.raises(StaleFeedError):
            svc.assert_fresh(self._KEY, now=stale_time)

        # Kill Switch: CRITICAL → HALTED
        ks.report_watchdog_failure(
            Severity.CRITICAL,
            f"OHLCV stale: {self._KEY}",
            detail={"key": self._KEY},
            _now=stale_time,
        )
        assert ks.state is TradingState.HALTED
        assert ks.check_order(OrderIntent.NEW).allowed is False

        # 상태 일관성: repeated STALE checks do NOT produce duplicate audit rows
        svc.check(self._KEY, now=stale_time)
        svc.check(self._KEY, now=stale_time)
        assert _count_audit(db_factory, "stale_data_stale") == 1  # not 3

        # Recovery: fresh update transitions back to FRESH
        recovered_time = stale_time + timedelta(seconds=1)
        svc.record_update(self._KEY, ts=recovered_time, now=recovered_time)
        fresh_result = svc.check(self._KEY, now=recovered_time)
        assert fresh_result.state is StaleState.FRESH

        # Audit Log: full WARNING→STALE→FRESH lifecycle
        assert _count_audit(db_factory, "stale_data_warning") == 1
        assert _count_audit(db_factory, "stale_data_stale") == 1
        assert _count_audit(db_factory, "stale_data_recovered") == 1

        # KillSwitch resume after HALTED
        resume_outcome = ks.resume(
            requested_by="operator",
            _now=stale_time + timedelta(seconds=310),
        )
        assert resume_outcome.approved is True
        assert ks.state is TradingState.RUNNING


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 9 — Duplicate Event
# ═══════════════════════════════════════════════════════════════════════════════

class TestDuplicateEvent:
    """Duplicate Event: poller dedup + DB persist dedup prevent double-counting."""

    def test_poller_partial_fill_dedup(self, db_factory):
        """Layer 1: second identical PARTIAL_FILLED broker response is a no-op."""
        from unittest.mock import MagicMock

        broker = MagicMock()
        poller = OrderFillPoller(broker=broker, db_factory=db_factory)
        order = _broker_order(OrderStatus.SUBMITTED, broker_order_id="ORD001", symbol="005930")

        tracker = _tracker()
        tracker.mark_pending("005930", "ORD001")

        fill_list = []

        def on_filled_real(filled_order: BOrder):
            f = Fill(
                order_id=filled_order.id,
                symbol=filled_order.symbol,
                side=filled_order.side,
                qty=filled_order.filled_qty,
                price=filled_order.avg_fill_price,
                market="KR",
            )
            fill_list.append(f)
            tracker.on_fill(f)

        poller.register(order, on_filled=on_filled_real)
        entry = poller._entries["ORD001"]

        # Broker always returns same PARTIAL_FILLED (filled_qty=5, qty=10)
        partial = _broker_order(
            OrderStatus.PARTIAL_FILLED, filled_qty=5, avg_fill_price=70000.0,
            broker_order_id="ORD001", symbol="005930", qty=10,
        )
        broker.get_order_status.return_value = partial

        # First poll: incremental=5>0 → callback fires
        poller._poll_one(entry)
        assert len(fill_list) == 1
        assert entry.last_reported_qty == 5

        # Second poll (duplicate): incremental=5-5=0 → callback NOT fired
        poller._poll_one(entry)
        assert len(fill_list) == 1  # still 1, not 2
        assert entry.last_reported_qty == 5  # unchanged

        # 상태 일관성: position qty == 5 (not double-counted to 10)
        pos = tracker.get_position("005930")
        assert pos is not None
        assert pos.qty == 5

        # 신규 주문 차단: after fill, pending lock is released (on_fill pops it)
        assert tracker.can_place_order("005930") is True

        # Kill Switch: no spurious trigger from duplicate
        ks, _, _, _ = _kill_switch(db_factory=db_factory, _now=_FIXED_NOW)
        assert ks.state is TradingState.RUNNING

        # Audit Log: poller_partial_filled written exactly once
        assert _count_audit(db_factory, "poller_partial_filled") == 1

    def test_persist_fill_dedup(self, patched_runner_factory):
        """Layer 2: second identical _persist_fill call is a no-op at DB level."""
        db_factory = patched_runner_factory
        # Seed the DBOrder so _persist_fill can find it
        _insert_order(db_factory, broker_order_id="ORD001", symbol="005930",
                      broker="kis", status="submitted")

        w = _bare_worker()
        fill = Fill(order_id="ORD001", symbol="005930", side="buy",
                    qty=5, price=70000.0, market="KR")
        order = _broker_order(
            OrderStatus.PARTIAL_FILLED, filled_qty=5, avg_fill_price=70000.0,
            broker_order_id="ORD001", symbol="005930",
        )

        # Call twice — second call must be a no-op
        w._persist_fill(fill, order)
        w._persist_fill(fill, order)

        # 상태 일관성: only 1 DBFill row and filled_qty not double-counted
        sess = db_factory()
        try:
            fill_count = sess.query(DBFill).count()
            db_order = sess.query(DBOrder).filter(DBOrder.broker_order_id == "ORD001").one()
            assert db_order.filled_qty == 5
        finally:
            sess.close()
        assert fill_count == 1

        # Audit Log: "fill" written exactly once
        assert _count_audit(db_factory, "fill") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 10 — Server Restart
# ═══════════════════════════════════════════════════════════════════════════════

class TestServerRestart:
    """Server Restart: pre-existing kill-switch blocks resume until manually cleared."""

    def _seed_kill_switch(self, factory):
        from datetime import date
        sess = factory()
        sess.add(DailyRiskState(
            trade_date=date.today(),
            kill_switch=True,
            kill_reason="일일 손실 한도 초과",
            daily_pnl=0.0,
            weekly_pnl=0.0,
            peak_equity=0.0,
        ))
        sess.commit()
        sess.close()

    def test_restart_with_active_kill_switch(self, db_factory, monkeypatch):
        import backend.quant.risk.engine as _eng
        from datetime import date
        monkeypatch.setattr(_eng, "_seoul_today", date.today)
        self._seed_kill_switch(db_factory)
        broker = _healthy_broker()

        rec = StartupRecovery(
            db_session_factory=db_factory,
            redis_client=FakeRedis(),
            broker=broker,
        )

        # 신규 주문 차단: run() returns False, SAFE_MODE stays disabled
        result = rec.run()
        assert result is False
        assert SAFE_MODE.can_trade is False

        # Kill Switch: _step_risk restored kill_switch_active
        assert getattr(rec, "_kill_switch_active", False) is True
        assert getattr(rec, "_kill_reason", "") == "일일 손실 한도 초과"

        # 상태 일관성: DailyRiskState.kill_switch unchanged (not cleared by failed run)
        sess = db_factory()
        row = sess.query(DailyRiskState).first()
        sess.close()
        assert row.kill_switch is True

    def test_manual_resume_enables_subsequent_restart(self, db_factory, monkeypatch):
        import backend.quant.risk.engine as _eng
        from datetime import date
        monkeypatch.setattr(_eng, "_seoul_today", date.today)
        self._seed_kill_switch(db_factory)
        broker = _healthy_broker()

        # First run: fails due to active kill switch
        rec = StartupRecovery(
            db_session_factory=db_factory,
            redis_client=FakeRedis(),
            broker=broker,
        )
        assert rec.run() is False

        # Recovery: KillSwitch constructed from DB reads HALTED state
        ks, _, _, _ = _kill_switch(
            db_factory=db_factory,
            cooldown_seconds=0.0,
            _now=_FIXED_NOW,
        )
        assert ks.state is TradingState.HALTED

        # Manual resume clears DailyRiskState.kill_switch
        outcome = ks.resume(requested_by="operator", _now=_FIXED_NOW)
        assert outcome.approved is True
        assert ks.state is TradingState.RUNNING

        sess = db_factory()
        row = sess.query(DailyRiskState).first()
        sess.close()
        assert row.kill_switch is False

        # Second run: now succeeds (kill_switch cleared in DB)
        SAFE_MODE._can_trade = False
        rec2 = StartupRecovery(
            db_session_factory=db_factory,
            redis_client=FakeRedis(),
            broker=broker,
        )
        result2 = rec2.run()
        assert result2 is True
        assert SAFE_MODE.can_trade is True

        # Audit Log: kill_switch_event from resume
        assert _count_audit(db_factory, "kill_switch_event") >= 1
