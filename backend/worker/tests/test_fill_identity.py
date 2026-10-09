"""P2-03 — a fill is identified by the total it brings its order to, not by (qty, price).

`_persist_fill` used to treat any stored fill with the same `(order_id, qty,
price)` as a duplicate. Two real fills of 5 shares at the limit price share
that key, so the second was never written — and where the state machine did
not know the order, `orders.filled_qty` missed it too.

The poller already hands each fill over as an increment against its
watermark; it now also says what total the increment brings the order to
(`Order.cumulative_filled_qty`). A redelivery carries the same total, a second
fill of the same size a larger one. The fills on file add up to how far the
order has been recorded, so a fill is a duplicate exactly when they already
reach its total.
"""
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

import backend.worker.runner as runner
from backend.brokers.models import Order as BOrder, OrderStatus
from backend.database.models import AuditLog, Base, Fill as DBFill, Order as DBOrder
from backend.database.testing import make_test_engine
from backend.execution.order_machine import OrderStateMachine
from backend.execution.order_poller import OrderFillPoller
from backend.execution.position_tracker import Fill, PositionTracker

ODNO = "0000221"


@pytest.fixture()
def factory(monkeypatch):
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(runner, "_SessionFactory", f)
    monkeypatch.setattr("backend.database.models.seoul_date", lambda: date(2026, 10, 9))
    return f


def _worker():
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w._poller = None
    w._loss_tracker = None
    w._publish_order_update = lambda o: None
    return w


def _seed_row(factory, filled_qty=0, status="submitted"):
    with factory() as s:
        s.add(DBOrder(broker_order_id=ODNO, symbol="005930", side="buy", qty=10,
                      price=70000.0, filled_qty=filled_qty, status=status,
                      market="KR", broker="kis", created_at=datetime.utcnow()))
        s.commit()


def _state(factory):
    with factory() as s:
        r = s.query(DBOrder).filter(DBOrder.broker_order_id == ODNO).one()
        fills = [(f.qty, f.price) for f in
                 s.query(DBFill).filter(DBFill.order_id == r.id).order_by(DBFill.id)]
        return r.filled_qty, fills


def _audits(factory, kind):
    with factory() as s:
        return s.query(AuditLog).filter(AuditLog.event_type == kind).count()


def _broker_order(filled_qty, status=OrderStatus.PARTIAL_FILLED, **kw):
    return BOrder(id=ODNO, symbol="005930", side="buy", qty=10, price=70000.0,
                  status=status, filled_qty=filled_qty, avg_fill_price=70000.0, **kw)


def _fill(qty=5, price=70000.0):
    return Fill(order_id=ODNO, symbol="005930", side="buy", qty=qty, price=price, market="KR")


class TestEqualFillsBothLand:
    def test_without_the_state_machine(self, factory):
        """The defect: where the machine skipped the order, the second fill
        vanished from `fills` *and* from `orders.filled_qty`."""
        _seed_row(factory)
        w = _worker()

        w._persist_fill(_fill(), _broker_order(5), cumulative=5)
        w._persist_fill(_fill(), _broker_order(5, OrderStatus.FILLED), cumulative=10)

        assert _state(factory) == (10, [(5, 70000.0), (5, 70000.0)])

    def test_through_the_fill_callback_with_the_state_machine(self, factory):
        w = _worker()
        machine = OrderStateMachine(on_state_change=w._persist_order)
        machine.register(BOrder(id=ODNO, symbol="005930", side="buy", qty=10,
                                price=70000.0, status=OrderStatus.SUBMITTED))
        cb = w._make_fill_callback(PositionTracker(machine), machine, run_id=1)

        cb(_broker_order(5, cumulative_filled_qty=5))
        cb(_broker_order(5, OrderStatus.FILLED, cumulative_filled_qty=10))

        assert _state(factory) == (10, [(5, 70000.0), (5, 70000.0)])


class TestARedeliveryIsSkipped:
    def test_same_total_is_the_same_fill(self, factory):
        _seed_row(factory)
        w = _worker()

        w._persist_fill(_fill(), _broker_order(5), cumulative=5)
        w._persist_fill(_fill(), _broker_order(5), cumulative=5)

        assert _state(factory) == (5, [(5, 70000.0)])
        assert _audits(factory, "fill") == 1

    def test_through_the_fill_callback(self, factory):
        """The callback hands the poller's total on to `_persist_fill`. Without
        it, a second 5 of 10 fits the order and would be written."""
        _seed_row(factory)
        w = _worker()
        machine = OrderStateMachine()                     # does not know the order
        cb = w._make_fill_callback(PositionTracker(machine), machine, run_id=1)

        cb(_broker_order(5, cumulative_filled_qty=5))
        cb(_broker_order(5, cumulative_filled_qty=5))

        assert _state(factory) == (5, [(5, 70000.0)])

    def test_an_earlier_total_after_a_later_one_is_skipped(self, factory):
        """Out of order: the 5-share total arrives after the order is recorded
        to 10 — already covered."""
        _seed_row(factory)
        w = _worker()
        w._persist_fill(_fill(), _broker_order(5), cumulative=5)
        w._persist_fill(_fill(), _broker_order(5, OrderStatus.FILLED), cumulative=10)

        w._persist_fill(_fill(), _broker_order(5), cumulative=5)

        assert len(_state(factory)[1]) == 2


class TestTheBrokerTotalBoundsTheRecord:
    def test_fills_on_file_behind_the_watermark(self, factory):
        """3 shares are on file, the broker says 10 and the increment is 10:
        the two records disagree. Only the 7 missing shares are filed and
        `orders.filled_qty` is the broker's 10 — not 13 of a 10-share order."""
        _seed_row(factory, filled_qty=3, status="partial_filled")
        with factory() as s:
            s.add(DBFill(order_id=s.query(DBOrder.id).scalar(), qty=3, price=70000.0))
            s.commit()
        w = _worker()

        w._persist_fill(_fill(qty=10), _broker_order(10, OrderStatus.FILLED), cumulative=10)

        assert _state(factory) == (10, [(3, 70000.0), (7, 70000.0)])
        with factory() as s:
            detail = s.query(AuditLog.detail).filter(AuditLog.event_type == "fill").scalar()
        assert '"qty": 7' in detail                      # what was filed, not the increment

    def test_a_fill_lost_to_an_earlier_failed_write(self, factory):
        """The first 5-share write failed (a warning only), the poller moved on,
        and the next 5 bring the broker total to 10. Nothing is on file: both
        are filed, so the fills add up to the `filled_qty` of 10."""
        _seed_row(factory)
        w = _worker()

        w._persist_fill(_fill(), _broker_order(5, OrderStatus.FILLED), cumulative=10)

        assert _state(factory) == (10, [(10, 70000.0)])

    def test_a_redelivery_after_the_first_write_failed_is_filed_once(self, factory):
        """Nothing on file reaches 5, so the redelivered fill is not a
        duplicate of anything recorded — it is the missing record."""
        _seed_row(factory)
        w = _worker()

        w._persist_fill(_fill(), _broker_order(5), cumulative=5)
        w._persist_fill(_fill(), _broker_order(5), cumulative=5)

        assert _state(factory) == (5, [(5, 70000.0)])


class TestWithoutATotal:
    """Nothing in between told us the total (no poller), so a redelivery and a
    new fill look alike. Only the invariant can be checked."""

    def test_a_fill_that_fits_is_written(self, factory):
        _seed_row(factory)
        w = _worker()
        w._persist_fill(_fill(), _broker_order(5))
        w._persist_fill(_fill(), _broker_order(5, OrderStatus.FILLED))
        assert _state(factory) == (10, [(5, 70000.0), (5, 70000.0)])
        assert _audits(factory, "fill_overfill_rejected") == 0

    def test_a_fill_past_the_order_quantity_is_refused_and_audited(self, factory):
        _seed_row(factory)
        w = _worker()
        w._persist_fill(_fill(qty=10), _broker_order(10, OrderStatus.FILLED))
        with factory() as s:
            row_id = s.query(DBOrder.id).scalar()

        # The row is closed now; the pipeline names it (see `on_filled`).
        w._persist_fill(_fill(qty=10), _broker_order(10, OrderStatus.FILLED), row_id=row_id)

        assert _state(factory) == (10, [(10, 70000.0)])
        assert _audits(factory, "fill_overfill_rejected") == 1


class TestThroughThePoller:
    """End to end from a broker status update: the poller's copy carries the
    total, and two equal increments reach the database as two fills."""

    def _poller(self, factory):
        poller = OrderFillPoller(broker=MagicMock())
        seen = []
        w = _worker()

        def on_filled(o):
            seen.append((o.filled_qty, o.cumulative_filled_qty))
            w._persist_fill(_fill(qty=o.filled_qty, price=o.avg_fill_price), o,
                            cumulative=o.cumulative_filled_qty)

        poller.register(_broker_order(0, OrderStatus.SUBMITTED), on_filled=on_filled)
        return poller, poller._entries[ODNO], seen

    def test_two_equal_increments(self, factory):
        _seed_row(factory)
        poller, entry, seen = self._poller(factory)

        poller._apply_update(entry, _broker_order(5))
        poller._apply_update(entry, _broker_order(10, OrderStatus.FILLED))

        assert seen == [(5, 5), (5, 10)]
        assert _state(factory) == (10, [(5, 70000.0), (5, 70000.0)])

    def test_a_callback_retried_after_its_write_landed(self, factory):
        """The callback raised after `_persist_fill` committed, so the poller
        kept its watermark and hands the same increment over again: one fill.
        (The worker's callback catches each step's errors, so this is the
        database's second line, not a path the pipeline is expected to take.)"""
        _seed_row(factory)
        poller, entry, seen = self._poller(factory)
        inner = entry.on_filled
        calls = {"n": 0}

        def flaky(o):
            inner(o)
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("lost after the write")
        entry.on_filled = flaky

        assert poller._apply_update(entry, _broker_order(5)) is False
        assert poller._apply_update(entry, _broker_order(5)) is True

        assert seen == [(5, 5), (5, 5)]
        assert _state(factory) == (5, [(5, 70000.0)])
