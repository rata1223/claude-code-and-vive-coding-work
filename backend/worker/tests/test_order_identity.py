"""Issue #168 — which DB row a broker order number belongs to.

A KIS order number (ODNO, stored as `Order.broker_order_id`) is unique only
within one trading day, and it **restarts every day**: yesterday's 0000117 and
today's 0000117 are routinely two different orders. The worker used to find
"the row" by the number alone (`.first()`, no ordering), which overwrote old
orders, filed fills under them, and left new orders with no row.

The trading day cannot tell them apart either — PR #165 tried twice and rolled
both back: the US session crosses Seoul midnight, so one order legitimately
spans two trading days, and an order can stay open past any window.

What does tell them apart is whether the order is still open. And the one step
that must reach a row *after* it closed — `_persist_fill`, which runs after the
FILLED transition was already written — uses the primary key the worker
recorded while the order was open.

What these tests assert is *identity* — which row, how many rows, where the
Fill rows land — not the `filled_qty` total: the pipeline counts that twice
today (step 1 writes the cumulative figure, step 4 adds the fill on top), which
predates this change and is issue #172.

Most tests here drive the real sequence: `OrderStateMachine.register` →
`process_fill` (each fires `_persist_order`) → `_persist_fill`, in the order
the fill pipeline runs them.
"""
import json
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

import backend.worker.runner as runner
from backend.brokers.models import Order as BOrder, OrderStatus
from backend.database.models import (
    AuditLog, Base, Fill as DBFill, Order as DBOrder,
)
from backend.database.testing import make_test_engine
from backend.execution.order_machine import FillEvent, OrderStateMachine
from backend.execution.position_tracker import Fill, PositionTracker

ODNO = "0000117"
TODAY = date(2026, 9, 25)


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def factory(monkeypatch):
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(runner, "_SessionFactory", f)
    return f


@pytest.fixture()
def day(monkeypatch):
    """The Seoul trading day `_persist_order` sees; tests move it."""
    state = {"day": TODAY}
    monkeypatch.setattr("backend.database.models.trading_day", lambda: state["day"])
    return state


def _worker(poller=None):
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w._poller = poller
    w._loss_tracker = None
    return w


def _seed(factory, *, status, trade_date, filled_qty=0, qty=10, symbol="005930",
          side="buy", odno=ODNO, created_at=None):
    sess = factory()
    try:
        row = DBOrder(
            broker_order_id=odno, symbol=symbol, side=side, qty=qty, price=70000.0,
            filled_qty=filled_qty, status=status, market="KR", broker="kis",
            trade_date=trade_date,
            idempotency_key=(f"{odno}:{symbol}:{side}:{trade_date.isoformat()}"
                             if trade_date else None),
            created_at=created_at or datetime.utcnow(),
        )
        sess.add(row)
        sess.commit()
        return row.id
    finally:
        sess.close()


def _rows(factory, odno=ODNO):
    sess = factory()
    try:
        return [(r.id, r.status, r.filled_qty, r.trade_date)
                for r in sess.query(DBOrder).filter(DBOrder.broker_order_id == odno)
                .order_by(DBOrder.id)]
    finally:
        sess.close()


def _fills(factory, row_id):
    sess = factory()
    try:
        return sess.query(DBFill).filter(DBFill.order_id == row_id).count()
    finally:
        sess.close()


def _submit(worker, *, qty=10, symbol="005930", odno=ODNO):
    """A live order reaching the worker: registered → persisted as submitted."""
    machine = OrderStateMachine(on_state_change=worker._persist_order)
    order = BOrder(id=odno, symbol=symbol, side="buy", qty=qty, price=70000.0,
                   status=OrderStatus.SUBMITTED)
    machine.register(order)
    return machine


def _fill(worker, machine, *, qty=10, odno=ODNO, symbol="005930"):
    """Steps 1 and 4 of the fill pipeline, in its order."""
    order = machine.process_fill(FillEvent(order_id=odno, filled_qty=qty,
                                           fill_price=70100.0))   # → _persist_order
    worker._persist_fill(Fill(order_id=odno, symbol=symbol, side="buy", qty=qty,
                              price=70100.0, market="KR"), order)
    return order


# ── the bug: a recycled number ──────────────────────────────────────────────

class TestRecycledNumber:
    @pytest.mark.parametrize("days_ago", [1, 5], ids=["yesterday", "last-week"])
    def test_a_new_order_gets_its_own_row(self, factory, day, days_ago):
        """Yesterday is the realistic case: the numbering restarts daily, and
        a rule that treated a *recent* closed row as the same order — to keep
        overnight fills together — would file today's order under it."""
        old = _seed(factory, status="filled", filled_qty=10,
                    trade_date=TODAY - timedelta(days=days_ago))
        w = _worker()

        _fill(w, _submit(w, qty=3), qty=3)

        rows = _rows(factory)
        assert len(rows) == 2, "the new order was merged into an earlier one"
        assert rows[0] == (old, "filled", 10, TODAY - timedelta(days=days_ago)), \
            "an earlier order was overwritten"
        new, status, _, trade_date = rows[1]
        assert (status, trade_date) == ("filled", TODAY)
        assert _fills(factory, new) == 1
        assert _fills(factory, old) == 0, "the fill was filed under an earlier order"

    def test_an_old_closed_row_without_a_trade_date_is_not_this_order(self, factory, day):
        """Rows written before PR #165 have no `trade_date`."""
        old = _seed(factory, status="filled", filled_qty=10, trade_date=None,
                    created_at=datetime(2026, 9, 1))
        w = _worker()

        _fill(w, _submit(w, qty=3), qty=3)

        rows = _rows(factory)
        assert len(rows) == 2
        assert rows[0][:3] == (old, "filled", 10)
        assert _fills(factory, old) == 0

    def test_a_stuck_earlier_order_does_not_capture_the_number(self, factory, day):
        """Review finding: a row that was never closed (nothing re-polls an
        `unknown` one) stays "open" forever, so matching on openness alone let
        it take the next order to draw its number."""
        stuck = _seed(factory, status="unknown", symbol="360750",
                      trade_date=TODAY - timedelta(days=6))
        w = _worker()

        _fill(w, _submit(w))

        rows = _rows(factory)
        assert len(rows) == 2
        assert rows[0][:3] == (stuck, "unknown", 0), "a stuck order was overwritten"
        assert _fills(factory, stuck) == 0
        assert _fills(factory, rows[1][0]) == 1

    def test_a_canceled_earlier_order_is_not_reopened(self, factory, day):
        old = _seed(factory, status="canceled", trade_date=TODAY - timedelta(days=1))
        w = _worker()

        _submit(w)

        rows = _rows(factory)
        assert len(rows) == 2
        assert rows[0][:2] == (old, "canceled")
        assert rows[1][1] == "submitted"


# ── what the two rolled-back attempts in PR #165 broke ─────────────────────

class TestOneOrderStaysOneRow:
    def test_submitted_before_midnight_filled_after(self, factory, day):
        day["day"] = TODAY - timedelta(days=1)          # 23:50
        w = _worker()
        machine = _submit(w)
        day["day"] = TODAY                              # 00:10

        _fill(w, machine)

        rows = _rows(factory)
        assert len(rows) == 1, "one overnight order split into two rows"
        pk, status, _, trade_date = rows[0]
        assert (status, trade_date) == ("filled", TODAY - timedelta(days=1))
        assert _fills(factory, pk) == 1

    def test_open_longer_than_any_window_across_a_restart(self, factory, day):
        """Friday's order, Monday's fill, the worker restarted in between."""
        pk = _seed(factory, status="submitted", trade_date=TODAY - timedelta(days=3))
        w = _worker()                                   # fresh process: nothing recorded

        _fill(w, _submit(w))

        rows = _rows(factory)
        assert len(rows) == 1
        assert rows[0][:2] == (pk, "filled")
        assert _fills(factory, pk) == 1

    def test_partial_fills_accumulate_on_the_same_row(self, factory, day):
        w = _worker()
        machine = _submit(w)
        _fill(w, machine, qty=4)
        _fill(w, machine, qty=6)

        rows = _rows(factory)
        assert len(rows) == 1
        assert rows[0][1] == "filled"
        assert _fills(factory, rows[0][0]) == 2


# ── `_persist_fill` on its own ──────────────────────────────────────────────

class TestPersistFill:
    @staticmethod
    def _filled():
        return BOrder(id=ODNO, symbol="005930", side="buy", qty=10, price=70000.0,
                      status=OrderStatus.FILLED, filled_qty=10, avg_fill_price=70100.0)

    @staticmethod
    def _event():
        return Fill(order_id=ODNO, symbol="005930", side="buy", qty=10,
                    price=70100.0, market="KR")

    def test_without_a_recorded_row_it_takes_the_open_one(self, factory, day):
        """The state machine skipped this order, so nothing was recorded."""
        old = _seed(factory, status="filled", filled_qty=10,
                    trade_date=TODAY - timedelta(days=1))
        live = _seed(factory, status="submitted", trade_date=TODAY)

        _worker()._persist_fill(self._event(), self._filled())

        assert _fills(factory, live) == 1
        assert _fills(factory, old) == 0

    def test_with_two_open_rows_it_takes_the_newest(self, factory, day):
        """Only an earlier order that was never closed leaves a second open row
        (reported by `_step_validate_state`). A live event is about the newer."""
        stale = _seed(factory, status="submitted", trade_date=TODAY - timedelta(days=4))
        live = _seed(factory, status="submitted", trade_date=TODAY)

        _worker()._persist_fill(self._event(), self._filled())

        assert _fills(factory, live) == 1
        assert _fills(factory, stale) == 0

    def test_it_never_guesses_at_a_closed_row(self, factory, day):
        old = _seed(factory, status="filled", filled_qty=10,
                    trade_date=TODAY - timedelta(days=1))

        _worker()._persist_fill(self._event(), self._filled())

        assert _fills(factory, old) == 0
        assert _rows(factory)[0][2] == 10

    def test_a_leftover_record_for_another_order_is_not_trusted(self, factory, day):
        """Review finding: the record is dropped only after a successful write,
        so a run of failures can leave it pointing at an earlier order."""
        earlier = _seed(factory, status="filled", filled_qty=5, symbol="360750",
                        trade_date=TODAY - timedelta(days=1))
        live = _seed(factory, status="submitted", trade_date=TODAY)
        w = _worker()
        w._remember_order_row(ODNO, earlier)

        w._persist_fill(self._event(), self._filled())

        assert _fills(factory, earlier) == 0
        assert _fills(factory, live) == 1

    def test_the_record_is_dropped_once_the_order_closes(self, factory, day):
        w = _worker()
        _fill(w, _submit(w))
        assert w._remembered_order_row(ODNO) is None

    def test_a_cancel_drops_the_record(self, factory, day):
        w = _worker()
        machine = _submit(w)
        assert w._remembered_order_row(ODNO) is not None
        order = machine.get(ODNO)
        order.status = OrderStatus.CANCELED
        w._persist_order(order)
        assert w._remembered_order_row(ODNO) is None

    def test_a_stale_record_is_dropped_before_a_new_row_is_tried(self, factory, day):
        """If the new order's insert does not happen, its fill must not fall
        back to the earlier order the stale record still points at."""
        old = _seed(factory, status="filled", filled_qty=10,
                    trade_date=TODAY - timedelta(days=1))
        # Today's row for this order is already closed, so `_persist_order`
        # finds no open row and returns on the idempotency key without inserting.
        _seed(factory, status="filled", filled_qty=10, trade_date=TODAY)
        w = _worker()
        w._remember_order_row(ODNO, old)                # left over from yesterday

        w._persist_order(BOrder(id=ODNO, symbol="005930", side="buy", qty=10,
                                price=70000.0, status=OrderStatus.SUBMITTED))
        w._persist_fill(self._event(), self._filled())

        assert w._remembered_order_row(ODNO) is None
        assert _fills(factory, old) == 0


# ── recovery ────────────────────────────────────────────────────────────────

def _restore(worker, on_filled_cb):
    captured = []
    worker._poller.register.side_effect = (
        lambda order, on_filled=None, **_: captured.append((order, on_filled)))
    worker._restore_pending_to_tracker(
        PositionTracker(OrderStateMachine()), broker="kis",
        on_filled_cb=on_filled_cb, on_timeout_cb=lambda o: None)
    return captured


class TestRecovery:
    def test_an_earlier_filled_order_does_not_suppress_a_live_one(self, factory, day):
        """The guard asks "was this already filled?". By number, an earlier
        FILLED order answered yes and skipped this order's whole pipeline."""
        _seed(factory, status="filled", filled_qty=10, trade_date=TODAY - timedelta(days=1))
        _seed(factory, status="submitted", trade_date=TODAY)
        on_filled_cb = MagicMock()

        captured = _restore(_worker(poller=MagicMock()), on_filled_cb)
        (order, guarded), = captured
        guarded(BOrder(id=ODNO, symbol="005930", side="buy", qty=10, price=70000.0,
                       status=OrderStatus.FILLED))

        on_filled_cb.assert_called_once()

    def test_the_guard_still_skips_this_orders_own_earlier_fill(self, factory, day):
        pk = _seed(factory, status="submitted", trade_date=TODAY)
        on_filled_cb = MagicMock()
        captured = _restore(_worker(poller=MagicMock()), on_filled_cb)
        sess = factory()
        sess.get(DBOrder, pk).status = "filled"          # recovery's DB-only callback won
        sess.commit()
        sess.close()

        captured[0][1](BOrder(id=ODNO, symbol="005930", side="buy", qty=10,
                              price=70000.0, status=OrderStatus.FILLED))

        on_filled_cb.assert_not_called()

    def test_two_open_rows_restore_once_from_the_newest(self, factory, day):
        _seed(factory, status="submitted", trade_date=TODAY - timedelta(days=4))
        newest = _seed(factory, status="submitted", trade_date=TODAY)
        w = _worker(poller=MagicMock())

        captured = _restore(w, MagicMock())

        assert len(captured) == 1
        assert w._remembered_order_row(ODNO) == newest

    def test_startup_recovery_registers_one_per_number(self, factory, day, monkeypatch):
        import redis as _redis
        from backend.worker.recovery import StartupRecovery

        def _refuse(*a, **k):
            raise ConnectionError("no redis in tests")
        monkeypatch.setattr(_redis, "from_url", _refuse)
        _seed(factory, status="submitted", trade_date=TODAY - timedelta(days=4))
        newest = _seed(factory, status="submitted", trade_date=TODAY)
        poller = MagicMock()
        rec = StartupRecovery(factory, redis_client=None, broker=MagicMock(), poller=poller)

        assert rec._step_pending_orders() is True

        assert poller.register.call_count == 1
        cb = poller.register.call_args.kwargs["on_filled"]
        assert cb is not None
        # bound to the newest row: its fill lands there
        cb(BOrder(id=ODNO, symbol="005930", side="buy", qty=10, price=70000.0,
                  status=OrderStatus.FILLED, filled_qty=10, avg_fill_price=70100.0))
        assert _fills(factory, newest) == 1

    def test_validate_state_reports_a_shared_open_number(self, factory, day):
        from backend.worker.recovery import StartupRecovery
        _seed(factory, status="submitted", trade_date=TODAY - timedelta(days=4))
        _seed(factory, status="submitted", trade_date=TODAY)
        _seed(factory, status="submitted", trade_date=TODAY, odno="0000999")
        before = _rows(factory)

        assert StartupRecovery(factory)._step_validate_state() is True

        sess = factory()
        try:
            kinds = [json.loads(a.detail) for a in sess.query(AuditLog).filter(
                AuditLog.event_type == "recovery_inconsistency")]
        finally:
            sess.close()
        shared = [k for k in kinds if k["kind"] == "duplicate_open_broker_order_id"]
        assert shared == [{"kind": "duplicate_open_broker_order_id",
                           "order_id": ODNO, "open_rows": 2}]
        assert _rows(factory) == before, "validation must not change orders"

    def test_validate_state_reports_a_stuck_unknown_order(self, factory, day):
        """Nothing re-polls an `unknown` row, so this report is how anyone
        learns it is stuck."""
        from backend.worker.recovery import StartupRecovery
        _seed(factory, status="unknown", trade_date=TODAY - timedelta(days=6),
              created_at=datetime.utcnow() - timedelta(days=6))

        StartupRecovery(factory)._step_validate_state()

        sess = factory()
        try:
            kinds = [json.loads(a.detail)["kind"] for a in sess.query(AuditLog)]
        finally:
            sess.close()
        assert "stale_open_order" in kinds
