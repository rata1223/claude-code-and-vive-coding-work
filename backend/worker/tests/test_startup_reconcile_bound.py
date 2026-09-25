"""Issue #170 — the startup reconcile must be bounded *and* stop when abandoned.

#161 bounded recovery steps 4 and 5 with `call_with_deadline`. Step 6 — the
startup `PositionReconciler.reconcile()` — was left out, and `StrategyWorker.run()`
runs a second one right after. Both make one `get_order_status` per open order
(up to ~32s each against Docker's 10s SIGKILL), so a SIGTERM landing there
still lost the equity checkpoint and the `worker_shutdown` audit row.

Bounding alone is not enough for this step, because it writes: an abandoned
reconcile would carry on through every open order on its daemon thread —
cancelling lost orders and running `resync` — while the teardown runs. So the
reconciler is handed a `StopGatedBroker`, whose reads refuse once the call has
been given up on. The reconciler itself is unchanged.

The one thing the gate must never do is refuse `cancel_order`:
`_mark_order_lost` logs a failed cancel and commits the row as CANCELED anyway,
which would record "canceled" for an order still live at the broker.
"""
import threading
import time
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.brokers.models import Order as BOrder, OrderStatus
from backend.database.models import Base, Order as DBOrder
from backend.database.testing import make_test_engine
from backend.execution.reconciler import PositionReconciler
from backend.worker import recovery
from backend.worker.recovery import (
    RecoveryAborted, StartupRecovery, StopGatedBroker, run_reconcile_bounded,
)


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_factory():
    # StaticPool: the reconcile under test runs on its own thread.
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _isolate_safe_mode():
    """SAFE_MODE is a module-level singleton; `run()` writes to it."""
    from backend.worker.recovery import SAFE_MODE
    saved = (SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause)
    yield
    SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause = saved


def _insert_order(factory, broker_order_id, *, status="submitted") -> int:
    sess = factory()
    try:
        row = DBOrder(
            broker_order_id=broker_order_id, symbol="005930", side="buy",
            qty=10, price=70000.0, filled_qty=0, status=status,
            market="KR", broker="kis",
            # > 1h old, so a broker "no record" routes to _mark_order_lost
            created_at=datetime(2026, 1, 1),
        )
        sess.add(row)
        sess.commit()
        return row.id
    finally:
        sess.close()


def _status(factory, order_id) -> str:
    sess = factory()
    try:
        return sess.get(DBOrder, order_id).status
    finally:
        sess.close()


class FakeBroker:
    """Records every call. `hang_on` makes that order's status lookup block."""

    def __init__(self, *, hang_on=None, statuses=None, on_cancel=None):
        self.calls: list[tuple] = []
        self.hang_on = hang_on
        self.statuses = statuses or {}
        self.on_cancel = on_cancel
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_positions(self):
        self.calls.append(("get_positions",))
        return []

    def get_order_status(self, order_id, symbol=""):
        self.calls.append(("get_order_status", order_id))
        if order_id == self.hang_on:
            self.entered.set()
            self.release.wait()
            return BOrder(id=order_id, symbol=symbol, side="buy", qty=10,
                          price=70000.0, status=OrderStatus.FILLED,
                          filled_qty=10, avg_fill_price=70000.0)
        return self.statuses.get(order_id)

    def cancel_order(self, **kwargs):
        self.calls.append(("cancel_order", kwargs["order_id"]))
        if self.on_cancel is not None:
            self.on_cancel()
        return True

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture()
def cleanup():
    brokers: list[FakeBroker] = []
    yield brokers
    for b in brokers:
        b.release.set()


def _factory_for(broker, db_factory):
    return lambda gate: PositionReconciler(
        broker=gate(broker), db_factory=db_factory, broker_name="kis")


# ── the gate itself ─────────────────────────────────────────────────────────

class TestStopGatedBroker:
    def test_reads_pass_through_while_open(self):
        b = FakeBroker(statuses={"A": "x"})
        g = StopGatedBroker(b, lambda: False)
        assert g.get_positions() == []
        assert g.get_order_status("A", "005930") == "x"

    def test_reads_are_refused_once_stopped(self):
        b = FakeBroker()
        g = StopGatedBroker(b, lambda: True)
        with pytest.raises(RecoveryAborted):
            g.get_positions()
        with pytest.raises(RecoveryAborted):
            g.get_order_status("A", "005930")
        assert b.calls == [], "a refused read must not reach the broker"

    def test_an_answer_arriving_after_the_stop_is_discarded(self):
        """In flight when the stop landed: acting on it would run a resync or
        DB sync from a reconcile that has already been given up on."""
        stopped = threading.Event()
        b = FakeBroker(statuses={"A": "answer"})
        real = b.get_order_status

        def _slow(order_id, symbol=""):
            value = real(order_id, symbol)
            stopped.set()             # the stop lands while the request is out
            return value
        b.get_order_status = _slow

        g = StopGatedBroker(b, stopped.is_set)
        with pytest.raises(RecoveryAborted):
            g.get_order_status("A", "005930")
        assert b.names() == ["get_order_status"]

    def test_cancel_order_is_never_refused(self):
        """_mark_order_lost commits CANCELED even when the cancel raises."""
        b = FakeBroker()
        g = StopGatedBroker(b, lambda: True)
        assert g.cancel_order(order_id="A", symbol="005930", qty=1, price=0.0)
        assert b.names() == ["cancel_order"]


# ── bounded reconcile, against the real PositionReconciler ─────────────────

class TestRunReconcileBounded:
    def test_normal_run_is_unchanged(self, db_factory):
        """Open gate → same outcome as calling reconcile() directly: a lost
        order is cancelled at the broker *and* committed."""
        oid = _insert_order(db_factory, "LOST1")
        b = FakeBroker(statuses={"LOST1": None})

        result = run_reconcile_bounded(_factory_for(b, db_factory), "startup", 5.0)

        assert result.ok
        assert b.names() == ["get_positions", "get_order_status", "cancel_order"]
        assert _status(db_factory, oid) == OrderStatus.CANCELED.value

    def test_shutdown_mid_reconcile_returns_promptly(self, db_factory, cleanup):
        _insert_order(db_factory, "HANG")
        b = FakeBroker(hang_on="HANG")
        cleanup.append(b)
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()

        t0 = time.monotonic()
        with pytest.raises(RecoveryAborted):
            run_reconcile_bounded(_factory_for(b, db_factory), "startup", 30.0,
                                  should_abort=stop.is_set)
        assert time.monotonic() - t0 < 2.0, "must answer SIGTERM inside the grace period"

    def test_a_stop_that_ends_the_reconcile_early_is_still_an_abort(self, db_factory):
        """Review finding: once the gate trips, the reconcile drains into caught
        errors and returns before the deadline wait next polls `should_abort`.
        Returned as-is, step 6 read it as a *failed* reconcile and SafeMode
        recorded a broker failure for what was a SIGTERM."""
        _insert_order(db_factory, "A")
        stop = threading.Event()
        b = FakeBroker()
        real = b.get_positions

        def _signal_lands_mid_request():
            value = real()
            stop.set()
            return value
        b.get_positions = _signal_lands_mid_request

        with pytest.raises(RecoveryAborted):
            run_reconcile_bounded(_factory_for(b, db_factory), "startup", 5.0,
                                  should_abort=stop.is_set)

    def test_deadline_bounds_the_step(self, db_factory, cleanup):
        _insert_order(db_factory, "HANG")
        b = FakeBroker(hang_on="HANG")
        cleanup.append(b)

        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            run_reconcile_bounded(_factory_for(b, db_factory), "startup", 0.5)
        assert time.monotonic() - t0 < 2.0

    def test_abandoned_reconcile_stops_at_its_next_read(self, db_factory, cleanup):
        """The core of #170: after giving up, the orphan must not carry on
        through the remaining open orders — here, cancelling a lost one."""
        hang_id = _insert_order(db_factory, "HANG")
        lost_id = _insert_order(db_factory, "LOST2")
        b = FakeBroker(hang_on="HANG", statuses={"LOST2": None})
        cleanup.append(b)

        # Identify *this* test's orphan, not any thread by name — an earlier
        # test's abandoned reconcile shares it and may still be winding down.
        before = set(threading.enumerate())
        with pytest.raises(TimeoutError):
            run_reconcile_bounded(_factory_for(b, db_factory), "startup", 0.3)
        assert b.entered.is_set()
        orphans = [t for t in threading.enumerate()
                   if t not in before and t.name == "recovery-reconcile-startup"]
        assert len(orphans) == 1

        b.release.set()               # the in-flight lookup finally answers
        # Wait for the orphan to finish, not for a fixed sleep: asserting while
        # it is still running would pass even with the gate broken.
        orphans[0].join(3.0)
        assert not orphans[0].is_alive(), "the abandoned reconcile did not drain"

        assert "cancel_order" not in b.names(), \
            "an abandoned reconcile cancelled an order during shutdown"
        assert b.names().count("get_order_status") == 1, \
            "an abandoned reconcile issued a new broker request"
        # the late FILLED answer for HANG was not applied either
        assert _status(db_factory, hang_id) == "submitted"
        assert _status(db_factory, lost_id) == "submitted"

    def test_a_stop_landing_during_cancel_does_not_split_it_from_its_commit(
            self, db_factory):
        """Once a lost order's cancel is sent, its DB commit must follow —
        otherwise the next boot sees a live 'submitted' row for a cancelled
        order (or, worse, the reverse)."""
        oid = _insert_order(db_factory, "LOST3")
        stop = threading.Event()
        b = FakeBroker(statuses={"LOST3": None}, on_cancel=stop.set)

        # Still reported as a stop — but only after the commit has landed.
        with pytest.raises(RecoveryAborted):
            run_reconcile_bounded(_factory_for(b, db_factory), "startup", 5.0,
                                  should_abort=stop.is_set)

        assert b.names() == ["get_positions", "get_order_status", "cancel_order"]
        assert _status(db_factory, oid) == OrderStatus.CANCELED.value

    def test_a_refused_read_fails_closed(self, db_factory):
        """A stop before the reconcile starts must read as *unverified*, not
        as a clean reconcile — step 6 returns `result.ok`."""
        _insert_order(db_factory, "A")
        b = FakeBroker()
        result = run_reconcile_bounded(
            lambda gate: PositionReconciler(
                broker=StopGatedBroker(b, lambda: True), db_factory=db_factory,
                broker_name="kis"),
            "startup", 5.0)
        assert not result.ok
        assert b.calls == []


# ── wired into StartupRecovery step 6 ───────────────────────────────────────

@pytest.fixture()
def no_redis(monkeypatch):
    import redis as _redis

    def _refuse(*a, **k):
        raise ConnectionError("no redis in tests")
    monkeypatch.setattr(_redis, "from_url", _refuse)


class TestStepReconcile:
    def test_shutdown_during_step_6_is_a_shutdown_not_a_failure(
            self, db_factory, cleanup, no_redis):
        _insert_order(db_factory, "HANG")
        b = FakeBroker(hang_on="HANG")
        cleanup.append(b)
        stop = threading.Event()
        rec = StartupRecovery(db_factory, redis_client=None, broker=b,
                              poller=None, should_abort=stop.is_set)
        threading.Timer(0.2, stop.set).start()

        t0 = time.monotonic()
        with pytest.raises(RecoveryAborted):
            rec._step_reconcile()
        assert time.monotonic() - t0 < 2.0

    def test_step_6_timeout_keeps_safe_mode_off(self, db_factory, cleanup,
                                                no_redis, monkeypatch):
        monkeypatch.setattr(recovery, "_RECONCILE_STARTUP_TIMEOUT", 0.3)
        _insert_order(db_factory, "HANG")
        b = FakeBroker(hang_on="HANG")
        cleanup.append(b)
        rec = StartupRecovery(db_factory, redis_client=None, broker=b, poller=None)

        assert rec._step_reconcile() is False

    def test_run_records_the_abort_reason(self, db_factory, cleanup, monkeypatch):
        """Through `run()`: an abort in step 6 is recorded as a stop signal,
        the same reason the per-step boundary check writes."""
        from backend.worker.recovery import SAFE_MODE
        stop = threading.Event()
        rec = StartupRecovery(db_factory, redis_client=None, broker=FakeBroker(),
                              poller=None, should_abort=stop.is_set)
        for name in ("_step_db", "_step_redis", "_step_risk",
                     "_step_balance", "_step_positions"):
            monkeypatch.setattr(rec, name, lambda: True)

        def _aborting():
            raise RecoveryAborted("reconcile-startup abandoned")
        monkeypatch.setattr(rec, "_step_reconcile", _aborting)

        assert rec.run() is False
        assert SAFE_MODE._reason == "기동 중 종료 요청 — 복구 중단"


# ── wired into StrategyWorker.run() ─────────────────────────────────────────

class TestWorkerStartupReconcile:
    def _worker(self, monkeypatch, db_factory, broker):
        from unittest.mock import MagicMock
        from backend.worker import runner
        monkeypatch.setattr(runner, "_SessionFactory", db_factory)
        monkeypatch.setattr(runner, "get_kis_broker", lambda: broker)
        w = runner.StrategyWorker.__new__(runner.StrategyWorker)
        w._redis = None
        w._shutdown = threading.Event()
        w._poller = None
        w._ca_runtime = None
        w._reconciler = MagicMock()
        return w

    def test_sigterm_during_the_workers_startup_reconcile(
            self, monkeypatch, db_factory, cleanup):
        _insert_order(db_factory, "HANG")
        b = FakeBroker(hang_on="HANG")
        cleanup.append(b)
        w = self._worker(monkeypatch, db_factory, b)
        threading.Timer(0.2, w._shutdown.set).start()

        t0 = time.monotonic()
        assert w._startup_reconcile() is False
        assert time.monotonic() - t0 < 2.0
        w._reconciler.reconcile.assert_not_called()

    def test_other_failures_still_let_boot_continue(self, monkeypatch, db_factory):
        """Unchanged behaviour: a failed startup reconcile is logged, not fatal."""
        w = self._worker(monkeypatch, db_factory, FakeBroker())

        def _boom(*a, **k):
            raise RuntimeError("broker down")
        monkeypatch.setattr(recovery, "run_reconcile_bounded", _boom)

        assert w._startup_reconcile() is True

    def test_the_worker_passes_its_shutdown_flag(self, monkeypatch, db_factory):
        w = self._worker(monkeypatch, db_factory, FakeBroker())
        seen = {}

        def _capture(factory, trigger, timeout, *, should_abort=None):
            seen.update(trigger=trigger, should_abort=should_abort)
        monkeypatch.setattr(recovery, "run_reconcile_bounded", _capture)

        w._startup_reconcile()
        assert seen["trigger"] == "startup"
        assert seen["should_abort"] == w._shutdown.is_set
