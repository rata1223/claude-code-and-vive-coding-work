"""P0-09 — position writers on a real Postgres, with real overlapping sessions.

SQLite has no concurrent writers, so only Postgres shows the races the old
SELECT-then-INSERT lost. Each test holds one session's uncommitted write,
starts a real writer in a thread, checks the writer **waits**, then commits
and checks the final row. A duplicate-key error anywhere fails the test.
"""
import threading
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from backend.brokers.models import Position as BPosition
from backend.database.models import Base, Position as DBPosition, upsert_position
from backend.database.testing import make_test_engine
from backend.execution.reconciler import PositionReconciler
from backend.worker.recovery import StartupRecovery

_BLOCK_SEC = 0.5


@pytest.fixture()
def factory():
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _rows(factory, symbol="005930"):
    with factory() as s:
        return [(r.qty, r.avg_price) for r in
                s.query(DBPosition).filter(DBPosition.symbol == symbol).all()]


class _Writer(threading.Thread):
    def __init__(self, fn):
        super().__init__(daemon=True)
        self._fn = fn
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self._fn()
        except BaseException as e:  # noqa: BLE001 - reported by the test
            self.error = e


def _assert_blocked_then_run(writer, holder):
    writer.start()
    writer.join(_BLOCK_SEC)
    assert writer.is_alive(), "writer did not wait for the other session"
    holder.commit()
    writer.join(10)
    assert not writer.is_alive(), "writer still blocked after the other session committed"
    assert writer.error is None, writer.error


def _upsert(factory, qty, avg):
    def go():
        with factory() as s:
            upsert_position(s, symbol="005930", broker="kis", qty=qty, avg_price=avg, market="KR")
            s.commit()
    return go


def _recovery_fill(factory, side, qty, price):
    rec = StartupRecovery.__new__(StartupRecovery)

    def go():
        with factory() as s:
            rec._apply_fill_to_position_db(s, "005930", side, qty, price)
            s.commit()
    return go


def test_two_first_inserts_do_not_collide(factory):
    """The old race: both writers saw no row and both inserted; the loser's
    commit raised and its value was lost."""
    holder = factory()
    upsert_position(holder, symbol="005930", broker="kis", qty=10, avg_price=100.0, market="KR")
    _assert_blocked_then_run(_Writer(_upsert(factory, 20, 105.0)), holder)
    holder.close()
    assert _rows(factory) == [(20, 105.0)]


def test_a_recovery_delta_is_not_lost(factory):
    with factory() as s:
        upsert_position(s, symbol="005930", broker="kis", qty=10, avg_price=100.0, market="KR")
        s.commit()
    holder = factory()
    StartupRecovery.__new__(StartupRecovery)._apply_fill_to_position_db(
        holder, "005930", "buy", 5, 100.0)
    _assert_blocked_then_run(_Writer(_recovery_fill(factory, "buy", 5, 100.0)), holder)
    holder.close()
    assert _rows(factory) == [(20, 100.0)]


def test_two_first_recovery_buys_both_count(factory):
    holder = factory()
    StartupRecovery.__new__(StartupRecovery)._apply_fill_to_position_db(
        holder, "005930", "buy", 10, 100.0)
    _assert_blocked_then_run(_Writer(_recovery_fill(factory, "buy", 5, 130.0)), holder)
    holder.close()
    assert _rows(factory) == [(15, 110.0)]


def test_a_sell_waits_for_the_row_lock(factory):
    with factory() as s:
        upsert_position(s, symbol="005930", broker="kis", qty=10, avg_price=100.0, market="KR")
        s.commit()
    holder = factory()
    StartupRecovery.__new__(StartupRecovery)._apply_fill_to_position_db(
        holder, "005930", "sell", 4, 120.0)
    _assert_blocked_then_run(_Writer(_recovery_fill(factory, "sell", 6, 120.0)), holder)
    holder.close()
    assert _rows(factory) == []


def test_the_reconcilers_pass_survives_a_concurrent_first_insert(factory):
    """The fill pipeline inserts the symbol while a reconcile pass that read
    the table before it is about to insert the same symbol. The pass must
    commit — its other repairs with it — and keep the fill pipeline's row."""
    with factory() as s:
        s.add(DBPosition(symbol="AAPL", qty=5, avg_price=200.0, market="US", broker="kis"))
        s.commit()
    broker = MagicMock()
    broker.get_positions.return_value = [
        BPosition(symbol="005930", qty=10, avg_price=100.0, market="KR"),
        BPosition(symbol="AAPL", qty=8, avg_price=210.0, market="US"),
    ]
    reconciler = PositionReconciler(broker=broker, db_factory=factory, redis_client=None,
                                    broker_name="kis")

    holder = factory()
    upsert_position(holder, symbol="005930", broker="kis", qty=12, avg_price=101.0, market="KR")
    writer = _Writer(lambda: reconciler.reconcile("t"))
    _assert_blocked_then_run(writer, holder)
    holder.close()

    result = writer.result
    assert result.errors == []
    assert _rows(factory) == [(12, 101.0)]
    assert _rows(factory, "AAPL") == [(8, 210.0)]
    assert any(g["kind"] == "position_appeared_during_reconcile" for g in result.gaps)
