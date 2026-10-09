"""P0-09 — position rows are written atomically.

Three writers touch ``positions``: the fill pipeline (absolute value from the
tracker), startup recovery (a delta) and the reconciler (inserts what the
broker has and the DB lacks). Each used to SELECT and then UPDATE or INSERT:
two first inserts raced to a duplicate-key error that threw a write away, and
for the reconciler, its whole pass.

These run on SQLite (or Postgres when ``TEST_DATABASE_URL`` is set); the
cross-session races are in ``tests/postgres/test_position_upsert_db.py``.
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sqlalchemy import delete as sa_delete

import backend.database.models as models
from backend.brokers.models import Position as BPosition
from backend.database.models import (
    Base, Position as DBPosition, insert_position_if_missing, lock_position, upsert_position,
)
from backend.database.testing import make_test_engine
from backend.execution.reconciler import PositionReconciler
from backend.worker import runner
from backend.worker.recovery import StartupRecovery
from backend.worker.runner import StrategyWorker


@pytest.fixture()
def factory():
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def shared_factory():
    """One connection for every thread — in-memory SQLite otherwise gives each
    thread its own empty database. The write lock keeps the threads apart."""
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _rows(factory, symbol="005930"):
    with factory() as s:
        return [(r.qty, r.avg_price, r.market) for r in
                s.query(DBPosition).filter(DBPosition.symbol == symbol).all()]


def _seed(factory, symbol="005930", qty=10, avg=100.0, market="KR"):
    with factory() as s:
        s.add(DBPosition(symbol=symbol, qty=qty, avg_price=avg, market=market, broker="kis"))
        s.commit()


# ── helpers ─────────────────────────────────────────────────────────────────
class TestHelpers:
    def test_upsert_creates_then_replaces_in_place(self, factory):
        with factory() as s:
            upsert_position(s, symbol="005930", broker="kis", qty=10, avg_price=100.0, market="KR")
            s.commit()
        with factory() as s:
            upsert_position(s, symbol="005930", broker="kis", qty=25, avg_price=110.0, market="KR")
            s.commit()
        assert _rows(factory) == [(25, 110.0, "KR")]

    def test_insert_if_missing_leaves_an_existing_row_alone(self, factory):
        _seed(factory, qty=10, avg=100.0)
        with factory() as s:
            assert insert_position_if_missing(s, symbol="005930", broker="kis", qty=99,
                                              avg_price=1.0, market="KR") is False
            s.commit()
        assert _rows(factory) == [(10, 100.0, "KR")]
        with factory() as s:
            assert insert_position_if_missing(s, symbol="AAPL", broker="kis", qty=3,
                                              avg_price=200.0, market="US") is True
            s.commit()
        assert _rows(factory, "AAPL") == [(3, 200.0, "US")]

    def test_the_same_symbol_on_another_broker_is_another_row(self, factory):
        with factory() as s:
            upsert_position(s, symbol="005930", broker="kis", qty=1, avg_price=1.0, market="KR")
            upsert_position(s, symbol="005930", broker="kiwoom", qty=2, avg_price=2.0, market="KR")
            s.commit()
        assert sorted(_rows(factory)) == [(1, 1.0, "KR"), (2, 2.0, "KR")]

    def test_lock_position_reads_the_committed_row(self, factory):
        _seed(factory, qty=10)
        with factory() as s:
            stale = s.query(DBPosition).one()
            with factory() as other:
                upsert_position(other, symbol="005930", broker="kis", qty=40,
                                avg_price=100.0, market="KR")
                other.commit()
            assert lock_position(s, "005930", "kis").qty == 40   # populate_existing
            assert stale.qty == 40
            assert lock_position(s, "NOPE", "kis") is None


# ── fill pipeline ───────────────────────────────────────────────────────────
class _Tracker:
    def __init__(self, pos):
        self.pos = pos

    def get_position(self, symbol):
        return self.pos


def _worker(monkeypatch, factory):
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    return StrategyWorker.__new__(StrategyWorker)


class TestFillPipeline:
    def test_writes_the_trackers_value(self, monkeypatch, factory):
        w = _worker(monkeypatch, factory)
        w._upsert_position_db("005930", "KR", _Tracker(SimpleNamespace(qty=10, avg_price=100.0)))
        w._upsert_position_db("005930", "KR", _Tracker(SimpleNamespace(qty=15, avg_price=105.0)))
        assert _rows(factory) == [(15, 105.0, "KR")]

    def test_a_closed_position_deletes_the_row(self, monkeypatch, factory):
        _seed(factory)
        w = _worker(monkeypatch, factory)
        w._upsert_position_db("005930", "KR", _Tracker(None))
        assert _rows(factory) == []
        w._upsert_position_db("005930", "KR", _Tracker(SimpleNamespace(qty=0, avg_price=0.0)))
        assert _rows(factory) == []

    def test_a_first_insert_over_an_existing_row_does_not_fail(self, monkeypatch, factory):
        """The race the old SELECT-then-INSERT lost: the row appeared after the
        check. Now there is no check — one statement either way."""
        _seed(factory, qty=7)
        w = _worker(monkeypatch, factory)
        w._upsert_position_db("005930", "KR", _Tracker(SimpleNamespace(qty=12, avg_price=101.0)))
        assert _rows(factory) == [(12, 101.0, "KR")]

    def test_the_tracker_is_read_inside_the_lock(self, monkeypatch, shared_factory):
        """Two fills on one symbol reach step 5 together. The tracker must be
        read under the write lock, so the last commit is the latest value."""
        factory = shared_factory
        w = _worker(monkeypatch, factory)
        entered, release = threading.Event(), threading.Event()
        state = {"pos": SimpleNamespace(qty=10, avg_price=100.0)}

        class _Blocking:
            def get_position(self, symbol):
                entered.set()
                release.wait(5)
                return state["pos"]

        first = threading.Thread(target=w._upsert_position_db,
                                 args=("005930", "KR", _Blocking()))
        first.start()
        assert entered.wait(5)
        state["pos"] = SimpleNamespace(qty=20, avg_price=105.0)   # a newer fill landed

        reads = []

        class _Recording:
            def get_position(self, symbol):
                reads.append(state["pos"].qty)
                return state["pos"]

        second = threading.Thread(target=w._upsert_position_db,
                                  args=("005930", "KR", _Recording()))
        second.start()
        second.join(0.3)
        assert second.is_alive() and reads == [], "second writer read the tracker outside the lock"
        release.set()
        first.join(5)
        second.join(5)
        assert reads == [20]
        assert _rows(factory) == [(20, 105.0, "KR")]


    def test_a_wait_on_one_symbol_does_not_stall_another(self, monkeypatch, shared_factory):
        w = _worker(monkeypatch, shared_factory)
        entered, release = threading.Event(), threading.Event()

        class _Blocking:
            def get_position(self, symbol):
                entered.set()
                release.wait(5)
                return SimpleNamespace(qty=1, avg_price=1.0)

        held = threading.Thread(target=w._upsert_position_db, args=("005930", "KR", _Blocking()))
        held.start()
        assert entered.wait(5)
        other = threading.Thread(target=w._upsert_position_db,
                                 args=("AAPL", "US", _Tracker(SimpleNamespace(qty=3, avg_price=200.0))))
        other.start()
        other.join(5)
        try:
            assert not other.is_alive(), "a write on AAPL waited for 005930's lock"
        finally:
            release.set()
            held.join(5)
        assert _rows(shared_factory, "AAPL") == [(3, 200.0, "US")]


# ── startup recovery ────────────────────────────────────────────────────────
def _apply(factory, side, qty, price, symbol="005930"):
    rec = StartupRecovery.__new__(StartupRecovery)
    with factory() as s:
        rec._apply_fill_to_position_db(s, symbol, side, qty, price)
        s.commit()


class TestRecovery:
    def test_a_first_buy_creates_the_row(self, factory):
        _apply(factory, "buy", 10, 100.0)
        assert _rows(factory) == [(10, 100.0, "KR")]

    def test_a_buy_into_a_row_is_a_weighted_delta(self, factory):
        _seed(factory, qty=10, avg=100.0)
        _apply(factory, "buy", 10, 120.0)
        assert _rows(factory) == [(20, 110.0, "KR")]

    def test_a_sell_reduces_then_deletes(self, factory):
        _seed(factory, qty=10, avg=100.0)
        _apply(factory, "sell", 4, 130.0)
        assert _rows(factory) == [(6, 100.0, "KR")]
        _apply(factory, "sell", 6, 130.0)
        assert _rows(factory) == []

    def test_a_sell_without_a_row_is_a_no_op(self, factory):
        _apply(factory, "sell", 4, 130.0)
        assert _rows(factory) == []

    def test_a_row_deleted_between_the_conflict_and_the_lock_is_inserted_again(
            self, monkeypatch, factory):
        """DO NOTHING does not lock the row it ran into; a delete can commit
        before the lock, which then finds nothing. The fill must still land."""
        _seed(factory, qty=10, avg=100.0)
        real, calls = models.lock_position, []

        def _closed_in_between(sess, symbol, broker):
            if not calls:
                sess.execute(sa_delete(DBPosition).where(DBPosition.symbol == symbol))
            calls.append(symbol)
            return real(sess, symbol, broker)

        monkeypatch.setattr(models, "lock_position", _closed_in_between)
        _apply(factory, "buy", 5, 130.0)
        assert _rows(factory) == [(5, 130.0, "KR")]

    def test_us_symbols_get_the_us_market(self, factory):
        _apply(factory, "buy", 2, 200.0, symbol="AAPL")
        assert _rows(factory, "AAPL") == [(2, 200.0, "US")]


# ── reconciler ──────────────────────────────────────────────────────────────
def _reconciler(factory, positions):
    broker = MagicMock()
    broker.get_positions.return_value = positions
    broker.get_order_status = MagicMock(return_value=None)
    return PositionReconciler(broker=broker, db_factory=factory, redis_client=None,
                              broker_name="kis")


class TestReconciler:
    def test_inserts_what_the_broker_has(self, factory):
        result = _reconciler(factory, [BPosition(symbol="005930", qty=10, avg_price=100.0,
                                                 market="KR")]).reconcile("t")
        assert _rows(factory) == [(10, 100.0, "KR")]
        assert any(r["kind"] == "insert_position" for r in result.repairs)

    def test_a_row_that_appears_during_the_pass_is_kept_and_the_pass_commits(
            self, monkeypatch, factory):
        """Another writer inserts the symbol after the pass read the table. The
        old plain add failed the pass's single commit and rolled back every other
        repair in it; now the newer row is kept and the rest commits."""
        _seed(factory, symbol="AAPL", qty=5, avg=200.0, market="US")   # needs a qty fix
        real = models.insert_position_if_missing

        def _row_appears_first(sess, **kw):
            if kw["symbol"] == "005930":
                upsert_position(sess, symbol="005930", broker="kis", qty=12,
                                avg_price=101.0, market="KR")      # the fill pipeline's row
            return real(sess, **kw)

        monkeypatch.setattr(models, "insert_position_if_missing", _row_appears_first)
        result = _reconciler(factory, [
            BPosition(symbol="005930", qty=10, avg_price=100.0, market="KR"),
            BPosition(symbol="AAPL", qty=8, avg_price=210.0, market="US"),
        ]).reconcile("t")

        assert result.errors == []
        assert _rows(factory) == [(12, 101.0, "KR")]            # left for the next pass
        assert _rows(factory, "AAPL") == [(8, 210.0, "US")]     # the other repair committed
        kinds = [g["kind"] for g in result.gaps if g["symbol"] == "005930"]
        assert kinds == ["position_appeared_during_reconcile"]       # one gap, not two
        assert not any(r["kind"] == "insert_position" for r in result.repairs)

    @pytest.mark.parametrize("broker_qty, broker_avg, expect_repair", [
        (8, 210.0, "fix_qty"),           # quantity mismatch
        (5, 230.0, "fix_avg_price"),     # avg-price drift only
    ])
    def test_a_row_changed_during_the_pass_is_not_overwritten(
            self, monkeypatch, factory, broker_qty, broker_avg, expect_repair):
        """The pass read the row at its start; the fill pipeline then wrote a
        newer value. The broker value the pass holds is older — leave the row."""
        _seed(factory, symbol="AAPL", qty=5, avg=200.0, market="US")
        _seed(factory, symbol="MSFT", qty=1, avg=300.0, market="US")   # a repair that must commit
        real = models.lock_position

        def _fill_lands_first(sess, symbol, broker):
            if symbol == "AAPL":
                upsert_position(sess, symbol="AAPL", broker="kis", qty=9, avg_price=205.0,
                                market="US")
            return real(sess, symbol, broker)

        monkeypatch.setattr(models, "lock_position", _fill_lands_first)
        result = _reconciler(factory, [
            BPosition(symbol="AAPL", qty=broker_qty, avg_price=broker_avg, market="US"),
            BPosition(symbol="MSFT", qty=4, avg_price=300.0, market="US"),
        ]).reconcile("t")

        assert result.errors == []
        assert _rows(factory, "AAPL") == [(9, 205.0, "US")]
        assert _rows(factory, "MSFT") == [(4, 300.0, "US")]
        assert [g["kind"] for g in result.gaps if g["symbol"] == "AAPL"] == [
            "position_changed_during_reconcile"]
        assert not any(r["kind"] == expect_repair and r["symbol"] == "AAPL"
                       for r in result.repairs)

    def test_a_row_deleted_during_the_pass_does_not_fail_the_commit(self, monkeypatch, factory):
        """Updating a row the fill pipeline deleted used to fail the single
        commit (no row matched) and roll back the whole pass."""
        _seed(factory, symbol="AAPL", qty=5, avg=200.0, market="US")
        _seed(factory, symbol="MSFT", qty=1, avg=300.0, market="US")
        real = models.lock_position

        def _closed_first(sess, symbol, broker):
            if symbol == "AAPL":
                sess.execute(sa_delete(DBPosition).where(DBPosition.symbol == "AAPL"))
            return real(sess, symbol, broker)

        monkeypatch.setattr(models, "lock_position", _closed_first)
        result = _reconciler(factory, [
            BPosition(symbol="AAPL", qty=8, avg_price=210.0, market="US"),
            BPosition(symbol="MSFT", qty=4, avg_price=300.0, market="US"),
        ]).reconcile("t")

        assert result.errors == []
        assert _rows(factory, "AAPL") == []
        assert _rows(factory, "MSFT") == [(4, 300.0, "US")]
        assert any(g["kind"] == "position_changed_during_reconcile" for g in result.gaps)

    def test_a_stale_delete_spares_a_row_that_changed(self, monkeypatch, factory):
        from datetime import datetime, timedelta
        with factory() as s:
            s.add(DBPosition(symbol="AAPL", qty=5, avg_price=200.0, market="US", broker="kis",
                             updated_at=datetime.utcnow() - timedelta(hours=5)))
            s.commit()
        real = models.lock_position

        def _bought_again(sess, symbol, broker):
            upsert_position(sess, symbol=symbol, broker="kis", qty=7, avg_price=201.0,
                            market="US")
            return real(sess, symbol, broker)

        monkeypatch.setattr(models, "lock_position", _bought_again)
        result = _reconciler(factory, []).reconcile("t")
        assert result.errors == []
        assert _rows(factory, "AAPL") == [(7, 201.0, "US")]
        assert not any(r["kind"] == "delete_position" for r in result.repairs)
