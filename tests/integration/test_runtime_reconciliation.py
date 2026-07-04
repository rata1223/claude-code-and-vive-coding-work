"""
TASK P3-02C-B — Runtime Reconciliation Synchronization.

End-to-end tests that the PositionReconciler repairs *runtime* state (in-memory
PositionTracker + OrderStateMachine + pending locks) by routing every
broker-confirmed fill through the SINGLE existing OrderFillPoller processing
pipeline — never a second fill processor — and does so idempotently.

The "pipeline" under test is the one fill-processing authority: the
`on_filled` callback the strategy/worker registers on the poller
(tracker.on_fill → persist Fill/Order). These tests supply a faithful,
minimal stand-in for that callback so the assertions are deterministic and
free of the live worker/broker.

Required scenarios (one test each):
  1. callback lost                 → TestCallbackLost
  2. restart before callback       → TestRestartBeforeCallback
  3. reconciliation repairs runtime→ TestReconciliationRepairsRuntime
  4. duplicate reconciliation      → TestDuplicateReconciliation
  5. duplicate fill                → TestDuplicateFill
  6. repeated reconciliation       → TestRepeatedReconciliation
  7. restart after reconciliation  → TestRestartAfterReconciliation
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.brokers.models import Order as BOrder, OrderStatus, Position as BPosition
from backend.database.models import (
    Base, Fill as DBFill, Order as DBOrder,
)
from backend.execution.order_machine import OrderStateMachine
from backend.execution.order_poller import OrderFillPoller
from backend.execution.position_tracker import Fill, PositionTracker
from backend.execution.reconciler import PositionReconciler


# ── Fixtures / helpers ──────────────────────────────────────────────────────

@pytest.fixture()
def db_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _tracker() -> PositionTracker:
    return PositionTracker(OrderStateMachine())


def _insert_order(factory, *, broker_order_id="ORD001", symbol="005930",
                  broker="kis", status="submitted", side="buy",
                  qty=10, price=70000.0, filled_qty=0) -> int:
    from datetime import datetime
    sess = factory()
    try:
        row = DBOrder(
            broker_order_id=broker_order_id, symbol=symbol, side=side,
            qty=qty, price=price, filled_qty=filled_qty, status=status,
            market="KR", broker=broker, created_at=datetime(2026, 1, 1),
        )
        sess.add(row)
        sess.commit()
        return row.id
    finally:
        sess.close()


def _broker_order(status=OrderStatus.FILLED, filled_qty=10, qty=10,
                  avg=70000.0, broker_order_id="ORD001", symbol="005930",
                  side="buy", price=70000.0) -> BOrder:
    return BOrder(
        id=broker_order_id, symbol=symbol, side=side, qty=qty, price=price,
        status=status, filled_qty=filled_qty, avg_fill_price=avg,
    )


def _mock_broker(order_status: BOrder, positions=None):
    """Broker whose get_order_status returns a fixed terminal order and whose
    get_positions returns [] (position phase is a no-op for these tests)."""
    b = MagicMock()
    b.get_positions.return_value = positions or []
    b.get_order_status.return_value = order_status
    return b


def _pipeline(tracker: PositionTracker, factory):
    """The SINGLE fill-processing authority the poller/reconciler route through.

    Faithful minimal copy of the worker's `_make_fill_callback` order:
      1. tracker.on_fill (runtime, increment qty)   ← single position mutation
      2. persist Fill (dedup) + advance DBOrder.filled_qty/status
    Returns (on_filled, calls) where `calls` records each increment applied.
    """
    calls: list[int] = []

    def on_filled(order: BOrder):
        calls.append(order.filled_qty)
        is_kr = len(order.symbol) == 6 and order.symbol.isdigit()
        tracker.on_fill(Fill(
            order_id=order.id, symbol=order.symbol, side=order.side,
            qty=order.filled_qty, price=order.avg_fill_price or order.price,
            market="KR" if is_kr else "US",
        ))
        sess = factory()
        try:
            dbo = sess.query(DBOrder).filter(
                DBOrder.broker_order_id == order.id).first()
            if dbo is None:
                return
            price = order.avg_fill_price or order.price
            dup = sess.query(DBFill).filter(
                DBFill.order_id == dbo.id, DBFill.qty == order.filled_qty,
                DBFill.price == price).first()
            if dup is not None:
                return
            sess.add(DBFill(order_id=dbo.id, qty=order.filled_qty, price=price))
            dbo.filled_qty = (dbo.filled_qty or 0) + order.filled_qty
            dbo.status = order.status.value
            sess.commit()
        finally:
            sess.close()

    return on_filled, calls


def _register(poller, order: BOrder, on_filled, initial_reported_qty=0):
    poller.register(order, on_filled=on_filled, on_timeout=lambda o: None,
                    initial_reported_qty=initial_reported_qty)


# ── 1. callback lost ────────────────────────────────────────────────────────

class TestCallbackLost:
    """A fill callback raised (lost) on first delivery. The poller must NOT
    drop the fill; the next poll re-drives it through the same pipeline exactly
    once — no worker restart required."""

    def test_lost_callback_self_heals_on_next_poll(self, db_factory):
        _insert_order(db_factory, filled_qty=0)
        tracker = _tracker()
        good, calls = _pipeline(tracker, db_factory)

        flaky = {"fail_next": True}

        def on_filled(order):
            if flaky["fail_next"]:
                flaky["fail_next"] = False
                raise RuntimeError("DB down — callback lost")
            good(order)

        broker = _mock_broker(_broker_order(OrderStatus.FILLED, filled_qty=10))
        poller = OrderFillPoller(broker=broker, db_factory=db_factory)
        _register(poller, _broker_order(OrderStatus.SUBMITTED, filled_qty=0), on_filled)
        entry = poller._entries["ORD001"]

        # First poll: callback raises → fill NOT lost, entry stays registered.
        poller._poll_one(entry)
        assert "ORD001" in poller._entries
        assert tracker.get_position("005930") is None

        # Second poll: callback succeeds → fill applied exactly once, entry popped.
        poller._poll_one(entry)
        assert "ORD001" not in poller._entries
        pos = tracker.get_position("005930")
        assert pos is not None and pos.qty == 10
        assert calls == [10]


# ── 2. restart before callback ──────────────────────────────────────────────

class TestRestartBeforeCallback:
    """Worker crashed after the broker filled the order but before the fill
    callback ran (DB filled_qty still 0). After restart the reconciler repairs
    the runtime via the poller pipeline."""

    def test_reconcile_repairs_fill_missed_by_crash(self, db_factory):
        oid = _insert_order(db_factory, status="submitted", filled_qty=0)
        tracker = _tracker()
        on_filled, calls = _pipeline(tracker, db_factory)

        broker = _mock_broker(_broker_order(OrderStatus.FILLED, filled_qty=10))
        poller = OrderFillPoller(broker=broker, db_factory=db_factory)
        # Recovery re-registers the still-open order (seed watermark from DB=0).
        _register(poller, _broker_order(OrderStatus.SUBMITTED, filled_qty=0), on_filled)

        reconciler = PositionReconciler(
            broker=broker, db_factory=db_factory, poller=poller, broker_name="kis")
        result = reconciler.reconcile("startup")

        pos = tracker.get_position("005930")
        assert pos is not None and pos.qty == 10   # runtime repaired
        assert calls == [10]
        sess = db_factory()
        try:
            dbo = sess.get(DBOrder, oid)
            assert dbo.status == "filled"
            assert dbo.filled_qty == 10
            assert sess.query(DBFill).count() == 1
        finally:
            sess.close()
        assert any(r["kind"] == "sync_order_runtime" for r in result.repairs)


# ── 3. reconciliation repairs runtime ───────────────────────────────────────

class TestReconciliationRepairsRuntime:
    """Baseline: a broker-confirmed fill for a DB-open order that the poller
    owns is applied to the in-memory tracker by reconcile()."""

    def test_runtime_tracker_updated(self, db_factory):
        _insert_order(db_factory)
        tracker = _tracker()
        on_filled, calls = _pipeline(tracker, db_factory)
        broker = _mock_broker(_broker_order(OrderStatus.FILLED, filled_qty=10))
        poller = OrderFillPoller(broker=broker, db_factory=db_factory)
        _register(poller, _broker_order(OrderStatus.SUBMITTED, filled_qty=0), on_filled)

        reconciler = PositionReconciler(
            broker=broker, db_factory=db_factory, poller=poller, broker_name="kis")
        reconciler.reconcile("periodic")

        pos = tracker.get_position("005930")
        assert pos is not None and pos.qty == 10
        # pending lock released by the pipeline's tracker.on_fill
        assert tracker.can_place_order("005930") is True
        assert calls == [10]


# ── 4. duplicate reconciliation ─────────────────────────────────────────────

class TestDuplicateReconciliation:
    """Two reconcilers observing the same broker fill must apply it once."""

    def test_two_reconciles_apply_once(self, db_factory):
        _insert_order(db_factory)
        tracker = _tracker()
        on_filled, calls = _pipeline(tracker, db_factory)
        broker = _mock_broker(_broker_order(OrderStatus.FILLED, filled_qty=10))
        poller = OrderFillPoller(broker=broker, db_factory=db_factory)
        _register(poller, _broker_order(OrderStatus.SUBMITTED, filled_qty=0), on_filled)

        rec = PositionReconciler(
            broker=broker, db_factory=db_factory, poller=poller, broker_name="kis")
        rec.reconcile("periodic")
        rec.reconcile("periodic")   # duplicate

        pos = tracker.get_position("005930")
        assert pos is not None and pos.qty == 10   # not 20
        assert calls == [10]
        sess = db_factory()
        try:
            assert sess.query(DBFill).count() == 1
        finally:
            sess.close()


# ── 5. duplicate fill ───────────────────────────────────────────────────────

class TestDuplicateFill:
    """The same broker fill delivered twice (poll + reconcile racing) is
    processed once. The persistent watermark gates the second delivery."""

    def test_poll_then_resync_apply_once(self, db_factory):
        _insert_order(db_factory)
        tracker = _tracker()
        on_filled, calls = _pipeline(tracker, db_factory)
        broker = _mock_broker(_broker_order(OrderStatus.FILLED, filled_qty=10))
        poller = OrderFillPoller(broker=broker, db_factory=db_factory)
        _register(poller, _broker_order(OrderStatus.SUBMITTED, filled_qty=0), on_filled)
        entry = poller._entries["ORD001"]

        # First delivery via the live poll loop.
        poller._poll_one(entry)
        assert tracker.get_position("005930").qty == 10

        # Second delivery via reconcile — order already terminal → not re-owned.
        rec = PositionReconciler(
            broker=broker, db_factory=db_factory, poller=poller, broker_name="kis")
        rec.reconcile("periodic")

        assert tracker.get_position("005930").qty == 10   # not 20
        assert calls == [10]
        sess = db_factory()
        try:
            assert sess.query(DBFill).count() == 1
        finally:
            sess.close()


# ── 6. repeated reconciliation ──────────────────────────────────────────────

class TestRepeatedReconciliation:
    """N reconciles converge and stay converged (idempotent)."""

    def test_five_reconciles_idempotent(self, db_factory):
        _insert_order(db_factory)
        tracker = _tracker()
        on_filled, calls = _pipeline(tracker, db_factory)
        broker = _mock_broker(_broker_order(OrderStatus.FILLED, filled_qty=10))
        poller = OrderFillPoller(broker=broker, db_factory=db_factory)
        _register(poller, _broker_order(OrderStatus.SUBMITTED, filled_qty=0), on_filled)

        rec = PositionReconciler(
            broker=broker, db_factory=db_factory, poller=poller, broker_name="kis")
        for _ in range(5):
            rec.reconcile("periodic")

        assert tracker.get_position("005930").qty == 10
        assert calls == [10]
        sess = db_factory()
        try:
            assert sess.query(DBFill).count() == 1
        finally:
            sess.close()


# ── 7. restart after reconciliation ─────────────────────────────────────────

class TestRestartAfterReconciliation:
    """After a reconcile has already applied the fill (DB filled_qty=full), a
    restart re-registers the order with a watermark seeded from the DB, so a
    fresh poll does NOT re-apply the fill."""

    def test_restart_does_not_reapply(self, db_factory):
        _insert_order(db_factory)
        tracker = _tracker()
        on_filled, calls = _pipeline(tracker, db_factory)
        broker = _mock_broker(_broker_order(OrderStatus.FILLED, filled_qty=10))

        # First run: reconcile applies the fill.
        poller1 = OrderFillPoller(broker=broker, db_factory=db_factory)
        _register(poller1, _broker_order(OrderStatus.SUBMITTED, filled_qty=0), on_filled)
        rec1 = PositionReconciler(
            broker=broker, db_factory=db_factory, poller=poller1, broker_name="kis")
        rec1.reconcile("periodic")
        assert tracker.get_position("005930").qty == 10
        assert calls == [10]

        # Restart: new tracker restored from DB position; new poller re-registers
        # the order (now DB filled_qty=10) — watermark must seed from DB.
        tracker2 = _tracker()
        tracker2.restore_positions([BPosition(symbol="005930", qty=10, avg_price=70000.0, market="KR")])
        on_filled2, calls2 = _pipeline(tracker2, db_factory)
        poller2 = OrderFillPoller(broker=broker, db_factory=db_factory)
        # DB order is 'filled' now, so it would not be re-registered in practice;
        # simulate the race where it is still seen open and re-registered with the
        # persisted filled_qty as the seed.
        _register(poller2, _broker_order(OrderStatus.SUBMITTED, filled_qty=10), on_filled2,
                  initial_reported_qty=10)   # recovery seeds watermark from DB filled_qty
        entry = poller2._entries["ORD001"]
        poller2._poll_one(entry)   # broker still reports FILLED total=10

        assert tracker2.get_position("005930").qty == 10   # unchanged, not 20
        assert calls2 == []                                # nothing re-applied
        sess = db_factory()
        try:
            assert sess.query(DBFill).count() == 1
        finally:
            sess.close()


# ── 8. terminal non-fill states (CANCELED / REJECTED / EXPIRED) ──────────────

class TestTerminalStateSync:
    """A broker-confirmed CANCELED/REJECTED/EXPIRED must release the pending lock
    and transition the state machine exactly once — no fill, idempotent under
    duplicate delivery. Exercises the shared apply_terminal_event() authority the
    worker wires into the poller's terminal callbacks."""

    def _worker_terminal_cb(self, tracker, machine):
        from backend.execution.order_events import apply_terminal_event
        return lambda o: apply_terminal_event(machine, tracker, o, actor="poller")

    @pytest.mark.parametrize("status", [
        OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED,
    ])
    def test_terminal_releases_lock_and_transitions_once(self, db_factory, status):
        machine = OrderStateMachine()
        tracker = PositionTracker(machine)
        # Order is live in the machine + holds a pending lock.
        machine.register(BOrder(id="ORD001", symbol="005930", side="buy",
                                qty=10, price=70000.0, status=OrderStatus.SUBMITTED))
        tracker.mark_pending("005930", "ORD001")
        assert tracker.can_place_order("005930") is False

        on_terminal = self._worker_terminal_cb(tracker, machine)
        broker = _mock_broker(_broker_order(status, filled_qty=0))
        poller = OrderFillPoller(broker=broker, db_factory=db_factory)
        poller.register(
            _broker_order(OrderStatus.SUBMITTED, filled_qty=0),
            on_filled=lambda o: None, on_timeout=lambda o: None,
            on_canceled=on_terminal, on_rejected=on_terminal, on_expired=on_terminal,
        )
        entry = poller._entries["ORD001"]

        poller._poll_one(entry)   # broker reports the terminal state
        assert tracker.can_place_order("005930") is True          # lock released
        assert machine.get("ORD001").status == status             # transitioned
        assert "ORD001" not in poller._entries                    # unregistered

        # Duplicate delivery (reconcile resync path) must be a no-op — already terminal.
        on_terminal(_broker_order(status, filled_qty=0))
        assert machine.get("ORD001").status == status
        assert tracker.can_place_order("005930") is True
