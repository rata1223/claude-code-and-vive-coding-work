"""Issue #172 — the fill pipeline counted every fill twice in `orders.filled_qty`.

The poller hands the fill callback the *increment* (`filled_qty` replaced with
what is new since its watermark). The callback's step 1 feeds it to the state
machine, which keeps the running total, and `_persist_order` writes that total
to the row. Step 4 (`_persist_fill`) then added the increment on top again —
4 + 6 of a 10-share order came out as 16.

That total is not just a display figure: after a restart, recovery seeds the
poller's watermark from it (`initial_reported_qty`), so an inflated value makes
the poller treat real, not-yet-reported fills as already seen and skip them.

These drive the worker's real fill callback (`_make_fill_callback`).
"""
from datetime import date, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

import backend.worker.runner as runner
from backend.brokers.models import Order as BOrder, OrderStatus
from backend.database.models import Base, Fill as DBFill, Order as DBOrder
from backend.database.testing import make_test_engine
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import Fill, PositionTracker

ODNO = "0000117"


@pytest.fixture()
def factory(monkeypatch):
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(runner, "_SessionFactory", f)
    monkeypatch.setattr("backend.database.models.seoul_date", lambda: date(2026, 9, 28))
    return f


def _worker(poller=None):
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w._poller = poller
    w._loss_tracker = None
    w._publish_order_update = lambda o: None
    return w


def _row(factory):
    sess = factory()
    try:
        r = sess.query(DBOrder).filter(DBOrder.broker_order_id == ODNO).one()
        fills = [f.qty for f in sess.query(DBFill).filter(DBFill.order_id == r.id)
                 .order_by(DBFill.id)]
        return r.status, r.filled_qty, fills
    finally:
        sess.close()


def _deliver(worker, machine, increment, *, qty=10, price=70100.0, prior=None):
    """What the poller hands the callback: the broker order, filled_qty = increment.

    ``prior`` is what the broker had already filled; it defaults to the
    machine's figure and must be given when the machine does not know the order.
    """
    if prior is None:
        live = machine.get(ODNO)
        prior = live.filled_qty if live else 0
    done = prior + increment >= qty
    worker._make_fill_callback(PositionTracker(machine), machine, run_id=1)(BOrder(
        id=ODNO, symbol="005930", side="buy", qty=qty, price=70000.0,
        status=OrderStatus.FILLED if done else OrderStatus.PARTIAL_FILLED,
        filled_qty=increment, avg_fill_price=price))


def _submitted(worker, qty=10):
    machine = OrderStateMachine(on_state_change=worker._persist_order)
    machine.register(BOrder(id=ODNO, symbol="005930", side="buy", qty=qty,
                            price=70000.0, status=OrderStatus.SUBMITTED))
    return machine


class TestTheTotalIsCountedOnce:
    def test_two_partial_fills(self, factory):
        """The case from the issue: 4 + 6 of 10 used to come out as 16."""
        w = _worker()
        machine = _submitted(w)

        _deliver(w, machine, 4)
        assert _row(factory) == ("partial_filled", 4, [4])

        _deliver(w, machine, 6)
        assert _row(factory) == ("filled", 10, [4, 6])

    def test_one_full_fill(self, factory):
        w = _worker()
        _deliver(w, _submitted(w), 10)
        assert _row(factory) == ("filled", 10, [10])

    def test_three_legs(self, factory):
        w = _worker()
        machine = _submitted(w)
        for leg in (3, 3, 4):
            _deliver(w, machine, leg, price=70100.0 + leg)
        assert _row(factory)[:2] == ("filled", 10)

    def test_without_the_state_machine_the_increment_is_added(self, factory):
        """The machine does not know this order (it skipped it), so step 1
        wrote nothing and step 4 is the only write: it has to add."""
        sess = factory()
        sess.add(DBOrder(broker_order_id=ODNO, symbol="005930", side="buy", qty=10,
                         price=70000.0, filled_qty=4, status="partial_filled",
                         market="KR", broker="kis", created_at=datetime.utcnow()))
        sess.commit()
        sess.close()
        w = _worker()

        _deliver(w, OrderStateMachine(), 6, prior=4)      # an empty machine

        assert _row(factory)[:2] == ("filled", 10)


class TestTheWatermarkAfterARestart:
    def test_recovery_seeds_the_poller_with_the_true_total(self, factory):
        """The consequence that made this dangerous: the watermark is seeded
        from `filled_qty`. At 8 for a 4-share fill, the poller would have
        swallowed the next 4 shares the broker reported."""
        w = _worker()
        _deliver(w, _submitted(w), 4)

        poller = MagicMock()
        restarted = _worker(poller=poller)
        restarted._restore_pending_to_tracker(
            PositionTracker(OrderStateMachine()), broker="kis",
            on_filled_cb=MagicMock(), on_timeout_cb=lambda o: None)

        (order,), kwargs = poller.register.call_args
        assert kwargs["initial_reported_qty"] == 4
        assert order.filled_qty == 4


class TestPersistFillOnItsOwn:
    @staticmethod
    def _seed(factory, filled_qty):
        sess = factory()
        row = DBOrder(broker_order_id=ODNO, symbol="005930", side="buy", qty=10,
                      price=70000.0, filled_qty=filled_qty, status="partial_filled",
                      market="KR", broker="kis", created_at=datetime.utcnow())
        sess.add(row)
        sess.commit()
        pk = row.id
        sess.close()
        return pk

    @staticmethod
    def _call(w, pk, *, increment, total):
        w._persist_fill(
            Fill(order_id=ODNO, symbol="005930", side="buy", qty=increment,
                 price=70100.0, market="KR"),
            BOrder(id=ODNO, symbol="005930", side="buy", qty=10, price=70000.0,
                   status=OrderStatus.PARTIAL_FILLED, filled_qty=increment),
            row_id=pk, filled_total=total)

    def test_a_given_total_is_set_not_added(self, factory):
        pk = self._seed(factory, 7)          # step 1 already wrote the total
        self._call(_worker(), pk, increment=3, total=7)
        assert _row(factory)[1] == 7

    def test_a_given_total_also_heals_a_failed_step_1_write(self, factory):
        """If `_persist_order` failed (it logs and swallows), the row still has
        the previous total; step 4 setting the machine's total repairs it."""
        pk = self._seed(factory, 4)
        self._call(_worker(), pk, increment=3, total=7)
        assert _row(factory)[1] == 7
