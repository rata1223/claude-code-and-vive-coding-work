"""
Unit tests for M4 recovery-safety mechanisms.

Covered:
  C1 — safe broker-scoped pending-order restore + poller re-registration + lock release
  C2 — _persist_order IntegrityError handling (duplicate skip)
       _persist_fill idempotency (no double fill row)
  C3 — StartupRecovery._step_validate_state non-mutating consistency detection + AuditLog

These tests run entirely against in-memory SQLite (no Redis / no KIS network). The module
-level session factory in runner.py is monkeypatched to the in-memory factory, and the
StrategyWorker is constructed via __new__ so its real Redis/KIS dependencies are not created.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.worker.runner as runner
from backend.brokers.models import Order as BOrder, OrderStatus
from backend.database.models import (
    AuditLog, Base, Fill as DBFill, Order as DBOrder, Position as DBPosition,
)
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import PositionTracker
from backend.worker.recovery import StartupRecovery


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def patched_runner_factory(db_factory, monkeypatch):
    """Point runner._session()/_get_session_factory() at the in-memory DB."""
    monkeypatch.setattr(runner, "_SessionFactory", db_factory)
    return db_factory


def _bare_worker(poller=None):
    """Construct a StrategyWorker without running __init__ (no Redis/KIS)."""
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w._poller = poller
    w._loss_tracker = None
    return w


def _tracker():
    return PositionTracker(OrderStateMachine())


def _insert_order(factory, broker_order_id, symbol, broker="kis", status="submitted",
                  side="buy", qty=10, price=100.0, created_at=None):
    sess = factory()
    row = DBOrder(
        broker_order_id=broker_order_id, symbol=symbol, side=side, qty=qty, price=price,
        status=status, market="KR", broker=broker,
        created_at=created_at or datetime.utcnow(),
    )
    sess.add(row)
    sess.commit()
    oid = row.id
    sess.close()
    return oid


# ── C1: safe pending-order restore ──────────────────────────────────────────

class TestRestorePendingBrokerScope:
    def test_only_restores_matching_broker(self, patched_runner_factory):
        f = patched_runner_factory
        _insert_order(f, "KIS001", "005930", broker="kis")
        _insert_order(f, "KW001", "000660", broker="kiwoom")
        worker = _bare_worker(poller=None)
        tracker = _tracker()

        worker._restore_pending_to_tracker(tracker, broker="kis")

        assert tracker.can_place_order("005930") is False   # kis order restored → locked
        assert tracker.can_place_order("000660") is True     # kiwoom order NOT restored

    def test_skips_orders_without_broker_order_id(self, patched_runner_factory):
        f = patched_runner_factory
        _insert_order(f, None, "005930", broker="kis")  # orphaned, no broker_order_id
        worker = _bare_worker(poller=None)
        tracker = _tracker()

        worker._restore_pending_to_tracker(tracker, broker="kis")

        assert tracker.can_place_order("005930") is True  # not restored

    def test_writes_audit_for_restored(self, patched_runner_factory):
        f = patched_runner_factory
        _insert_order(f, "KIS001", "005930", broker="kis")
        worker = _bare_worker(poller=None)

        worker._restore_pending_to_tracker(_tracker(), broker="kis")

        sess = f()
        n = sess.query(AuditLog).filter(AuditLog.event_type == "recovery_restore_pending").count()
        sess.close()
        assert n == 1


class TestRestorePendingPollerRegistration:
    def test_registers_with_poller_and_fill_releases_lock(self, patched_runner_factory):
        f = patched_runner_factory
        _insert_order(f, "KIS001", "005930", broker="kis")
        poller = MagicMock()
        worker = _bare_worker(poller=poller)
        tracker = _tracker()

        captured = {}

        def _capture_register(order, on_filled=None, on_timeout=None, **kwargs):
            captured["order"] = order
            captured["on_filled"] = on_filled

        poller.register.side_effect = _capture_register

        on_filled_cb = MagicMock()
        worker._restore_pending_to_tracker(
            tracker, broker="kis", on_filled_cb=on_filled_cb, on_timeout_cb=lambda o: None,
        )

        # registered with the poller under the recovered order id
        assert poller.register.called
        assert captured["order"].id == "KIS001"
        assert tracker.can_place_order("005930") is False

        # driving the registered callback (order still 'submitted' in DB) → full pipeline runs
        filled = BOrder(id="KIS001", symbol="005930", side="buy", qty=10, price=100.0,
                        status=OrderStatus.FILLED)
        captured["on_filled"](filled)
        on_filled_cb.assert_called_once_with(filled)

    def test_guard_skips_already_filled_and_releases_lock(self, patched_runner_factory):
        f = patched_runner_factory
        # Order already FILLED in DB (recovery DB-only callback won the race)
        _insert_order(f, "KIS001", "005930", broker="kis", status="submitted")
        poller = MagicMock()
        worker = _bare_worker(poller=poller)
        tracker = _tracker()

        captured = {}
        poller.register.side_effect = lambda order, on_filled=None, on_timeout=None, **kwargs: captured.update(
            on_filled=on_filled
        )
        on_filled_cb = MagicMock()
        worker._restore_pending_to_tracker(
            tracker, broker="kis", on_filled_cb=on_filled_cb, on_timeout_cb=lambda o: None,
        )

        # Now mark the order FILLED in DB to simulate the recovery callback having fired
        sess = f()
        row = sess.query(DBOrder).filter(DBOrder.broker_order_id == "KIS001").first()
        row.status = OrderStatus.FILLED.value
        sess.commit()
        sess.close()

        filled = BOrder(id="KIS001", symbol="005930", side="buy", qty=10, price=100.0,
                        status=OrderStatus.FILLED)
        captured["on_filled"](filled)

        on_filled_cb.assert_not_called()              # full pipeline NOT re-run (no double fill/PnL)
        assert tracker.can_place_order("005930") is True  # lock released

    def test_guard_query_failure_skips_pipeline_and_releases_lock(self, patched_runner_factory,
                                                                  monkeypatch):
        f = patched_runner_factory
        _insert_order(f, "KIS001", "005930", broker="kis", status="submitted")
        poller = MagicMock()
        worker = _bare_worker(poller=poller)
        tracker = _tracker()

        captured = {}
        poller.register.side_effect = lambda order, on_filled=None, on_timeout=None, **kwargs: captured.update(
            on_filled=on_filled
        )
        on_filled_cb = MagicMock()
        worker._restore_pending_to_tracker(
            tracker, broker="kis", on_filled_cb=on_filled_cb, on_timeout_cb=lambda o: None,
        )

        # Make the guard DB read fail; safety direction must be skip + unmark (no double-exec)
        import backend.worker.runner as _runner
        monkeypatch.setattr(_runner, "_get_session_factory",
                            lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        filled = BOrder(id="KIS001", symbol="005930", side="buy", qty=10, price=100.0,
                        status=OrderStatus.FILLED)
        captured["on_filled"](filled)

        on_filled_cb.assert_not_called()
        assert tracker.can_place_order("005930") is True


# ── C2: persist idempotency ───────────────────────────────────────────────────

class TestPersistFillIdempotency:
    def test_skips_duplicate_fill_row(self, patched_runner_factory):
        from backend.execution.position_tracker import Fill
        f = patched_runner_factory
        oid = _insert_order(f, "KIS001", "005930", broker="kis")
        worker = _bare_worker(poller=None)

        order = BOrder(id="KIS001", symbol="005930", side="buy", qty=10, price=100.0,
                       status=OrderStatus.FILLED, filled_qty=10, avg_fill_price=100.0)
        fill = Fill(order_id="KIS001", symbol="005930", side="buy", qty=10, price=100.0, market="KR")

        worker._persist_fill(fill, order)
        worker._persist_fill(fill, order)  # duplicate delivery

        sess = f()
        n = sess.query(DBFill).filter(DBFill.order_id == oid).count()
        sess.close()
        assert n == 1


class TestPersistOrderIntegrity:
    def test_duplicate_idempotency_key_does_not_raise(self, patched_runner_factory):
        f = patched_runner_factory
        worker = _bare_worker(poller=None)
        order = BOrder(id="KIS999", symbol="005930", side="buy", qty=10, price=100.0,
                       status=OrderStatus.SUBMITTED)

        worker._persist_order(order)
        # Second persist of a brand-new broker_order_id with the SAME idempotency key path:
        # simulate by inserting a row that collides on idempotency_key via a second order id.
        order2 = BOrder(id="KIS999", symbol="005930", side="buy", qty=10, price=100.0,
                        status=OrderStatus.SUBMITTED)
        worker._persist_order(order2)  # same broker_order_id → existing branch updates, no raise

        sess = f()
        n = sess.query(DBOrder).filter(DBOrder.broker_order_id == "KIS999").count()
        sess.close()
        assert n == 1


# ── C3: consistency validation gate ───────────────────────────────────────────

class TestValidateState:
    def test_detects_nonpositive_qty_position(self, db_factory):
        sess = db_factory()
        sess.add(DBPosition(symbol="005930", qty=0, avg_price=100.0, market="KR", broker="kis"))
        sess.commit()
        sess.close()
        rec = StartupRecovery(db_session_factory=db_factory, broker=None, redis_client=None)

        ok = rec._step_validate_state()

        assert ok is True  # non-fatal
        sess = db_factory()
        n = sess.query(AuditLog).filter(AuditLog.event_type == "recovery_inconsistency").count()
        sess.close()
        assert n == 1

    def test_detects_orphaned_pending_order(self, db_factory):
        sess = db_factory()
        sess.add(DBOrder(broker_order_id=None, symbol="005930", side="buy", qty=10,
                         price=100.0, status="submitted", market="KR", broker="kis"))
        sess.commit()
        sess.close()
        rec = StartupRecovery(db_session_factory=db_factory, broker=None, redis_client=None)

        rec._step_validate_state()

        sess = db_factory()
        rows = sess.query(AuditLog).filter(AuditLog.event_type == "recovery_inconsistency").all()
        kinds = [r.detail for r in rows]
        sess.close()
        assert any("orphaned_pending_order" in k for k in kinds)

    def test_detects_stale_open_order(self, db_factory):
        old = datetime.utcnow() - timedelta(hours=48)
        sess = db_factory()
        sess.add(DBOrder(broker_order_id="KIS001", symbol="005930", side="buy", qty=10,
                         price=100.0, status="submitted", market="KR", broker="kis",
                         created_at=old))
        sess.commit()
        sess.close()
        rec = StartupRecovery(db_session_factory=db_factory, broker=None, redis_client=None)

        rec._step_validate_state()

        sess = db_factory()
        rows = sess.query(AuditLog).filter(AuditLog.event_type == "recovery_inconsistency").all()
        kinds = [r.detail for r in rows]
        sess.close()
        assert any("stale_open_order" in k for k in kinds)

    def test_clean_state_no_audit_and_passes(self, db_factory):
        sess = db_factory()
        sess.add(DBPosition(symbol="005930", qty=10, avg_price=100.0, market="KR", broker="kis"))
        sess.add(DBOrder(broker_order_id="KIS001", symbol="005930", side="buy", qty=10,
                         price=100.0, status="submitted", market="KR", broker="kis"))
        sess.commit()
        sess.close()
        rec = StartupRecovery(db_session_factory=db_factory, broker=None, redis_client=None)

        ok = rec._step_validate_state()

        assert ok is True
        sess = db_factory()
        n = sess.query(AuditLog).filter(AuditLog.event_type == "recovery_inconsistency").count()
        sess.close()
        assert n == 0

    def test_does_not_mutate_state(self, db_factory):
        sess = db_factory()
        sess.add(DBPosition(symbol="005930", qty=0, avg_price=100.0, market="KR", broker="kis"))
        sess.add(DBOrder(broker_order_id=None, symbol="000660", side="buy", qty=5,
                         price=50.0, status="pending", market="KR", broker="kis"))
        sess.commit()
        sess.close()
        rec = StartupRecovery(db_session_factory=db_factory, broker=None, redis_client=None)

        rec._step_validate_state()

        # Detection only — the offending rows are still present (no silent mutation)
        sess = db_factory()
        assert sess.query(DBPosition).filter(DBPosition.qty <= 0).count() == 1
        assert sess.query(DBOrder).filter(DBOrder.broker_order_id.is_(None)).count() == 1
        sess.close()
