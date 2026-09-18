"""P0-10 — the worker must survive being asked to stop.

`docker stop` sends SIGTERM and then SIGKILL 10 seconds later. Python does not
catch SIGTERM by itself, so before this the worker simply vanished: the
OrderFillPoller could be killed between reading a fill from the broker and
writing it, APScheduler jobs could fire into a half-dead process, and nothing
recorded that the stop was intentional rather than a crash.

Three things in here are easy to get backwards, so each has its own test:

1. **A shutdown must not deactivate running strategies.** ``WorkerSession._run``
   marks ``strategy_runs.is_active = False`` in its ``finally``, and
   ``_restore_active()`` only restores rows where that is ``True``. Stopping the
   sessions the ordinary way on the way out would turn every strategy off on
   every deploy.

2. **A shutdown must not delete the heartbeat key.** ``WorkerWatchdog`` runs in
   the API process and sets ``DailyRiskState.kill_switch`` the moment
   ``worker:heartbeat`` is gone. Tidying up the key would trip the MDD kill
   switch on every restart; letting the 90s TTL lapse lets a restart come back
   unnoticed.

3. **The signal handler must only set a flag.** It runs in the main thread at an
   arbitrary bytecode — possibly inside ``self._lock`` or a commit — so doing the
   teardown there deadlocks on the locks it needs.

No Redis, no broker, no network: SQLite in memory and fakes.
"""
import itertools
import signal
import threading
import time
from unittest.mock import MagicMock

import pytest
from backend.database.testing import make_test_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.worker.runner as runner
from backend.database.models import AuditLog, Base, StrategyRun


# ── Harness ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_factory():
    # StaticPool: the strategy threads open their own sessions, and a plain
    # in-memory SQLite engine hands each new connection an empty database — the
    # is_active assertions below would then pass for the wrong reason.
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def patched_factory(db_factory, monkeypatch):
    """Point runner._session() at the in-memory DB."""
    monkeypatch.setattr(runner, "_SessionFactory", db_factory)
    return db_factory


class FakePubSub:
    def __init__(self, messages=None):
        self.subscribed = None
        self.closed = False
        self.polls = 0
        self._messages = list(messages or [])

    def subscribe(self, *channels):
        self.subscribed = channels

    def get_message(self, timeout=None):
        self.polls += 1
        if self._messages:
            return self._messages.pop(0)
        # Mirror the real client: block for up to `timeout` when idle, so the
        # loop under test does not spin.
        time.sleep(min(timeout or 0.01, 0.05))
        return None

    def close(self):
        self.closed = True


class FakeRedis:
    def __init__(self, pubsub=None):
        self.pubsub_obj = pubsub or FakePubSub()
        self.deleted = []
        self.set_keys = []

    def pubsub(self):
        return self.pubsub_obj

    def ping(self):
        return True

    def setex(self, key, ttl, value):
        self.set_keys.append(key)

    def delete(self, *keys):
        self.deleted.extend(keys)


class FakeThread:
    """A stand-in for a poller/session worker thread that records its join."""

    def __init__(self, alive_after_join=False, name="fake"):
        self.name = name
        self.joined_with = []
        self._alive_after_join = alive_after_join
        self._joined = False

    def join(self, timeout=None):
        self.joined_with.append(timeout)
        self._joined = True

    def is_alive(self):
        if not self._joined:
            return True
        return self._alive_after_join


def _worker(calls=None, *, poller=None, heartbeat=None, scheduler=None,
            loss_tracker=None, redis_client=None):
    """A StrategyWorker with every collaborator faked and __init__ skipped.

    __init__ opens Redis, a broker and a DB; the teardown under test touches
    none of that, so it is built the way the other worker suites build it.
    """
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w._redis = redis_client or FakeRedis()
    w._sessions = {}
    w._lock = threading.Lock()
    w._last_market_open = {}
    w._shutdown = threading.Event()
    w._shutdown_done = False
    w._shutdown_at = None
    w._aux_threads = []
    w._poller = poller
    w._heartbeat = heartbeat if heartbeat is not None else MagicMock()
    w._scheduler = scheduler
    w._loss_tracker = loss_tracker
    w._reconciler = MagicMock()
    w._last_known_equity = None
    w._ca_runtime = MagicMock()
    return w


class FakeStrategy:
    def __init__(self):
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def on_market_open(self):
        pass


#: Process-wide, so no two tests ever share a run_id.
#:
#: They otherwise all get id 1 (a fresh in-memory engine per test restarts the
#: sequence), and a strategy thread left over from an earlier test runs
#: ``_mark_stopped()`` against whatever ``runner._SessionFactory`` points at
#: *when it finally wakes* — which is the next test's database. That leak
#: silently flipped the next test's row and made an ordering test pass for the
#: wrong reason. Distinct ids make the stale write a no-op (``db.get`` misses).
_next_run_id = itertools.count(1000)


def _run_row(factory, is_active=True):
    sess = factory()
    row = StrategyRun(id=next(_next_run_id), strategy_type="indicator", name="t",
                      config="{}", broker="kis", is_active=is_active)
    sess.add(row)
    sess.commit()
    rid = row.id
    sess.close()
    return rid


def _is_active(factory, run_id):
    sess = factory()
    try:
        return sess.get(StrategyRun, run_id).is_active
    finally:
        sess.close()


def _audit_rows(factory, event_type):
    sess = factory()
    try:
        return sess.query(AuditLog).filter(AuditLog.event_type == event_type).all()
    finally:
        sess.close()


# ── The signal handler itself ────────────────────────────────────────────────

class TestSignalRegistration:
    def test_sigterm_and_sigint_are_both_registered(self, monkeypatch):
        """SIGTERM is `docker stop`; SIGINT is Ctrl-C in a foreground container.
        Handling only one leaves the other as an abrupt kill."""
        registered = {}
        monkeypatch.setattr(signal, "signal",
                            lambda s, h: registered.__setitem__(s, h))

        runner.install_signal_handlers(_worker())

        assert signal.SIGTERM in registered
        assert signal.SIGINT in registered

    def test_the_handler_only_raises_the_flag(self, monkeypatch):
        """It runs in the main thread at an arbitrary bytecode — possibly inside
        self._lock. Running the teardown there would deadlock on the very locks
        it needs, so the handler must do nothing but set the flag."""
        handlers = {}
        monkeypatch.setattr(signal, "signal",
                            lambda s, h: handlers.__setitem__(s, h))
        w = _worker()
        w.shutdown = MagicMock()

        runner.install_signal_handlers(w)
        handlers[signal.SIGTERM](signal.SIGTERM, None)

        assert w.shutdown_requested is True
        w.shutdown.assert_not_called()

    def test_a_second_signal_gives_up_and_exits(self, monkeypatch):
        """An operator who sends SIGTERM twice wants out now, not another wait."""
        handlers = {}
        exits = []
        monkeypatch.setattr(signal, "signal",
                            lambda s, h: handlers.__setitem__(s, h))
        monkeypatch.setattr(runner.os, "_exit", lambda code: exits.append(code))
        w = _worker()

        runner.install_signal_handlers(w)
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        assert exits == []
        handlers[signal.SIGTERM](signal.SIGTERM, None)

        assert exits == [1]


# ── The loops have to notice ─────────────────────────────────────────────────

class TestLoopsExit:
    def test_the_pubsub_loop_returns_when_shutdown_is_requested(self):
        """A flag nothing polls is not a shutdown. `listen()` blocks in a socket
        read that PEP 475 resumes after the handler returns, so the loop has to
        poll with a timeout instead."""
        w = _worker()
        done = threading.Event()

        def _go():
            w._run_with_pubsub()
            done.set()

        t = threading.Thread(target=_go, daemon=True)
        t.start()
        time.sleep(0.2)
        w.request_shutdown()

        assert done.wait(5), "pub/sub loop never returned after SIGTERM"

    def test_the_pubsub_loop_closes_its_subscription(self):
        w = _worker()
        t = threading.Thread(target=w._run_with_pubsub, daemon=True)
        t.start()
        time.sleep(0.2)
        w.request_shutdown()
        t.join(5)

        assert w._redis.pubsub_obj.closed is True

    def test_the_db_polling_fallback_returns_too(self, patched_factory):
        """The Redis-outage path has its own `while True`. If only the pub/sub
        loop learned to exit, a SIGTERM during an outage still hangs to SIGKILL."""
        w = _worker()
        w._redis.ping = MagicMock(side_effect=Exception("down"))
        done = threading.Event()

        def _go():
            w._enter_db_polling_mode()
            done.set()

        threading.Thread(target=_go, daemon=True).start()
        time.sleep(0.2)
        w.request_shutdown()

        assert done.wait(5), "DB polling fallback never returned after SIGTERM"


# ── The trap: a restart must not switch every strategy off ───────────────────

class TestStrategiesSurviveARestart:
    def test_shutdown_leaves_running_strategies_active(self, patched_factory):
        """`_restore_active()` restores rows with is_active == True. If the
        teardown marks them stopped, a deploy silently disables every strategy
        and nobody finds out until the next bar that never trades."""
        run_id = _run_row(patched_factory)
        w = _worker()
        session = runner.WorkerSession(run_id, FakeStrategy())
        w._sessions[run_id] = session
        session.start()

        w.shutdown()

        assert _is_active(patched_factory, run_id) is True, (
            "a restart deactivated a running strategy — it will not come back")

    def test_shutdown_still_ends_the_strategy_thread(self, patched_factory):
        run_id = _run_row(patched_factory)
        w = _worker()
        strategy = FakeStrategy()
        session = runner.WorkerSession(run_id, strategy)
        w._sessions[run_id] = session
        session.start()

        w.shutdown()

        assert strategy.stopped is True
        assert session._thread.is_alive() is False

    def test_an_operator_stop_still_deactivates_it(self, patched_factory):
        """The counterpart. Making shutdown leave rows active must not make a
        real `strategy:stop` leave them active too — that would resurrect a
        strategy the operator switched off."""
        run_id = _run_row(patched_factory)
        w = _worker()
        session = runner.WorkerSession(run_id, FakeStrategy())
        w._sessions[run_id] = session
        session.start()

        w._handle_stop({"run_id": run_id})
        session.join(5)

        assert _is_active(patched_factory, run_id) is False


# ── Ordered teardown ─────────────────────────────────────────────────────────

class TestTeardownOrder:
    def test_the_scheduler_stops_before_the_strategies(self, patched_factory):
        """A job firing mid-teardown starts work on components already gone."""
        order = []
        scheduler = MagicMock()
        scheduler.shutdown.side_effect = lambda **kw: order.append("scheduler")
        run_id = _run_row(patched_factory)
        w = _worker(scheduler=scheduler)

        strategy = FakeStrategy()
        strategy.stop = lambda: order.append("strategy")
        session = runner.WorkerSession(run_id, strategy)
        w._sessions[run_id] = session
        session.start()

        w.shutdown()

        assert order[:2] == ["scheduler", "strategy"], order

    def test_the_heartbeat_stops_last(self, patched_factory):
        """Stopping it early makes the API's watchdog see a dead worker while the
        teardown is still running, and the watchdog's response is to set the
        kill switch."""
        order = []
        heartbeat = MagicMock()
        heartbeat.stop.side_effect = lambda: order.append("heartbeat")
        poller = MagicMock()
        poller.stop.side_effect = lambda: order.append("poller")
        poller._thread = FakeThread(name="order-poller")
        w = _worker(poller=poller, heartbeat=heartbeat)

        w.shutdown()

        assert order == ["poller", "heartbeat"], order

    def test_the_poller_is_joined_not_just_signalled(self, patched_factory):
        """OrderFillPoller.stop() only sets an event and its thread is a daemon,
        so without a join it can be killed between reading a fill from the broker
        and writing it. That is the 'drain active polls' half of P0-10."""
        poller = MagicMock()
        poller._thread = FakeThread(name="order-poller")
        w = _worker(poller=poller)

        w.shutdown()

        poller.stop.assert_called_once()
        assert poller._thread.joined_with, "poller thread was never joined"

    # The equity checkpoint is covered by TestCheckpointDoesNotTouchTheHalt
    # below. It deliberately does NOT go through PersistentLossTracker._persist()
    # — asserting that it does would pin a fail-open path (see that class).

    def test_auxiliary_threads_are_joined(self, patched_factory):
        """market-open broadcasts and periodic reconciles do broker I/O and DB
        writes on untracked one-shot threads — exactly the mid-flight kill this
        item is about."""
        w = _worker()
        aux = FakeThread(name="reconcile-KR")
        w._aux_threads.append(aux)

        w.shutdown()

        assert aux.joined_with, "an auxiliary thread was left to be SIGKILLed"


# ── The heartbeat key is not ours to tidy ────────────────────────────────────

class TestHeartbeatKey:
    def test_shutdown_does_not_delete_the_heartbeat_key(self, patched_factory):
        """WorkerWatchdog (in the API process) sets DailyRiskState.kill_switch
        the moment `worker:heartbeat` is missing. Deleting it on the way out
        would halt trading on every deploy; the 90s TTL is what lets a restart
        pass unnoticed."""
        from backend.worker.heartbeat import WorkerHeartbeat

        redis_client = FakeRedis()
        # The real object, not a MagicMock: a delete added inside
        # WorkerHeartbeat.stop() has to fail this test too, not just one added to
        # the runner's teardown.
        w = _worker(redis_client=redis_client,
                    heartbeat=WorkerHeartbeat(redis_client))

        w.shutdown()

        assert redis_client.deleted == [], (
            "shutdown deleted the heartbeat key — the API watchdog will trip "
            "the kill switch on every restart")


# ── The record the next boot reads ───────────────────────────────────────────

class TestShutdownRecord:
    def test_shutdown_writes_an_audit_row(self, patched_factory):
        """'no recovery record' is the harm P0-10 names. A clean stop has to be
        distinguishable from a crash after the fact."""
        w = _worker()

        w.shutdown()

        rows = _audit_rows(patched_factory, "worker_shutdown")
        assert len(rows) == 1, "a graceful shutdown left no record behind"
        assert rows[0].actor == "worker"

    def test_the_record_is_written_even_when_a_step_fails(self, patched_factory):
        poller = MagicMock()
        poller.stop.side_effect = RuntimeError("poller wedged")
        w = _worker(poller=poller)

        w.shutdown()

        rows = _audit_rows(patched_factory, "worker_shutdown")
        assert len(rows) == 1
        assert "poller" in (rows[0].detail or "")

    def test_a_failing_step_does_not_abort_the_rest(self, patched_factory):
        """One wedged component must not cost the others their cleanup."""
        scheduler = MagicMock()
        scheduler.shutdown.side_effect = RuntimeError("scheduler wedged")
        heartbeat = MagicMock()
        w = _worker(scheduler=scheduler, heartbeat=heartbeat)

        w.shutdown()

        heartbeat.stop.assert_called_once()


# ── Idempotence and the grace period ─────────────────────────────────────────

class TestShutdownDiscipline:
    def test_shutdown_runs_once(self, patched_factory):
        """It is reached from both the signal path and run()'s finally."""
        heartbeat = MagicMock()
        w = _worker(heartbeat=heartbeat)

        w.shutdown()
        w.shutdown()

        heartbeat.stop.assert_called_once()
        assert len(_audit_rows(patched_factory, "worker_shutdown")) == 1

    def test_a_wedged_strategy_cannot_outlast_the_grace_period(self, patched_factory):
        """Docker's default grace is 10s, then SIGKILL. A strategy that ignores
        its stop event must not consume the budget the other steps need."""
        run_id = _run_row(patched_factory)
        w = _worker()
        session = runner.WorkerSession(run_id, FakeStrategy())
        w._sessions[run_id] = session
        # A thread that never ends, standing in for a wedged strategy.
        session._thread = threading.Thread(
            target=lambda: threading.Event().wait(60), daemon=True)
        session._thread.start()

        t0 = time.monotonic()
        w.shutdown()
        elapsed = time.monotonic() - t0

        assert elapsed < runner._SHUTDOWN_BUDGET_SEC + 1.0, (
            f"teardown took {elapsed:.1f}s — SIGKILL arrives at 10s")

    def test_run_tears_down_even_when_the_loop_raises(self, patched_factory):
        """run()'s finally is the only guarantee that a crash out of the loop
        still checkpoints and records."""
        heartbeat = MagicMock()
        w = _worker(heartbeat=heartbeat)
        w._restore_active = MagicMock()
        w._run_with_pubsub = MagicMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            w.run()

        heartbeat.stop.assert_called_once()


# ── Review findings: the fail-open the checkpoint could have caused ──────────

class TestCheckpointDoesNotTouchTheHalt:
    """`PersistentLossTracker._persist()` would have written kill_switch from the
    tracker's in-memory value, and at shutdown that direction is fail-**open**:

        Redis down → heartbeat key cannot be refreshed → it expires → the API's
        WorkerWatchdog sets kill_switch=True in the DB while this worker is alive
        holding an in-memory False → SIGTERM writes False back over it → the
        restarted worker trades with the halt erased.
    """

    def _tracker(self, **kw):
        t = MagicMock()
        t._lock = threading.Lock()
        t.daily_pnl = kw.get("daily_pnl", -1000.0)
        t.weekly_pnl = kw.get("weekly_pnl", -2000.0)
        t.peak_equity = kw.get("peak_equity", 2_000_000.0)
        return t

    def test_it_writes_the_equity_columns(self, patched_factory):
        from datetime import date
        from backend.database.models import DailyRiskState

        w = _worker(loss_tracker=self._tracker())
        w.shutdown()

        sess = patched_factory()
        row = sess.get(DailyRiskState, date.today())
        try:
            assert row is not None, "nothing was checkpointed"
            assert row.daily_pnl == -1000.0
            assert row.weekly_pnl == -2000.0
            assert row.peak_equity == 2_000_000.0
        finally:
            sess.close()

    def test_it_does_not_clear_a_kill_switch_set_by_the_watchdog(self, patched_factory):
        """The real tracker, not a mock — this is the end-to-end fail-open."""
        from datetime import date
        from backend.database.models import DailyRiskState
        from backend.quant.risk.engine import PersistentLossTracker, RiskConfig

        # Built while nothing is halted, so its in-memory kill_switch is False.
        tracker = PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                        db_factory=patched_factory)
        assert tracker.kill_switch is False
        tracker.peak_equity = 2_000_000.0

        # Now the API-side WorkerWatchdog sets the halt in the DB — the worker
        # process never learns of it (that is the whole point: Redis is down).
        sess = patched_factory()
        row = sess.get(DailyRiskState, date.today())
        if row is None:
            row = DailyRiskState(trade_date=date.today())
            sess.add(row)
        row.kill_switch = True
        row.kill_reason = "Worker 하트비트 없음 — 프로세스 재시작 필요"
        sess.commit()
        sess.close()

        _worker(loss_tracker=tracker).shutdown()

        sess = patched_factory()
        row = sess.get(DailyRiskState, date.today())
        try:
            assert row.kill_switch is True, (
                "shutdown erased a halt the watchdog set — the worker would come "
                "back up and trade")
            assert row.kill_reason is not None
            # and the equity checkpoint still landed
            assert row.peak_equity == 2_000_000.0
        finally:
            sess.close()

    def test_it_does_not_go_through_the_trackers_persist(self, patched_factory):
        """_persist() is the path that writes kill_switch (issue #158)."""
        tracker = self._tracker()
        w = _worker(loss_tracker=tracker)

        w.shutdown()

        tracker._persist.assert_not_called()


# ── Review findings: a signal during startup must not be deferred ────────────

class TestStartupIsInterruptible:
    def test_restore_is_skipped_when_shutdown_was_already_requested(self, patched_factory):
        """Boot does a DB restore and a broker-backed reconcile. Running those
        first spends the SIGKILL clock before the teardown even starts."""
        w = _worker()
        w._restore_active = MagicMock()
        w._run_with_pubsub = MagicMock()
        w.request_shutdown()

        w.run()

        w._restore_active.assert_not_called()
        w._reconciler.reconcile.assert_not_called()
        w._run_with_pubsub.assert_not_called()

    def test_reconcile_is_skipped_when_the_signal_lands_during_restore(self, patched_factory):
        w = _worker()
        w._restore_active = MagicMock(side_effect=lambda: w.request_shutdown())
        w._run_with_pubsub = MagicMock()

        w.run()

        w._restore_active.assert_called_once()
        w._reconciler.reconcile.assert_not_called()

    def test_the_budget_runs_from_the_signal_not_from_shutdown(self, patched_factory):
        """A teardown that starts 7s after the signal has 1s left, not 8."""
        run_id = _run_row(patched_factory)
        w = _worker()
        session = runner.WorkerSession(run_id, FakeStrategy())
        w._sessions[run_id] = session
        session._thread = threading.Thread(
            target=lambda: threading.Event().wait(60), daemon=True)
        session._thread.start()

        w.request_shutdown()
        # Pretend the signal arrived a full budget ago.
        w._shutdown_at = time.monotonic() - runner._SHUTDOWN_BUDGET_SEC

        t0 = time.monotonic()
        w.shutdown()
        elapsed = time.monotonic() - t0

        assert elapsed < 1.0, (
            f"teardown blocked {elapsed:.1f}s on a budget that was already spent")
        assert len(_audit_rows(patched_factory, "worker_shutdown")) == 1, (
            "the record must still be written when the budget is gone")


# ── Review findings: the aux join cannot eat the poller's budget ─────────────

class TestAuxJoinIsCapped:
    def test_a_running_scan_does_not_consume_the_whole_budget(self, patched_factory):
        """IndicatorStrategy._scan_and_trade never checks is_running(), so a scan
        already in flight keeps going after its strategy is stopped. Waiting it
        out would leave nothing for the poller drain."""
        poller = MagicMock()
        poller._thread = FakeThread(name="order-poller")
        w = _worker(poller=poller)
        forever = threading.Thread(target=lambda: threading.Event().wait(60),
                                   daemon=True, name="market-open-1")
        forever.start()
        w._aux_threads.append(forever)

        t0 = time.monotonic()
        w.shutdown()
        elapsed = time.monotonic() - t0

        assert elapsed < runner._AUX_JOIN_CAP_SEC + 1.0, (
            f"aux join took {elapsed:.1f}s — capped at "
            f"{runner._AUX_JOIN_CAP_SEC}s so the poller still gets drained")
        poller.stop.assert_called_once()
        assert poller._thread.joined_with, "poller lost its drain to the aux join"


# ── Review findings: the first Ctrl-C during a teardown is not a kill ────────

class TestSecondSignalCounting:
    def test_a_signal_during_an_in_progress_teardown_is_the_first_one(self, monkeypatch):
        """shutdown() raises the shutdown flag itself. Keying the 'give up now'
        branch off that flag would make the operator's first Ctrl-C during a
        finally-path teardown abort the checkpoint and the audit row."""
        handlers = {}
        exits = []
        monkeypatch.setattr(signal, "signal",
                            lambda s, h: handlers.__setitem__(s, h))
        monkeypatch.setattr(runner.os, "_exit", lambda code: exits.append(code))
        w = _worker()

        runner.install_signal_handlers(w)
        w._shutdown.set()          # as shutdown() does on entry
        handlers[signal.SIGINT](signal.SIGINT, None)

        assert exits == [], "the first signal must never hard-exit"


# ── Review findings (#159): user cleanup code must run inside the budget ─────

@pytest.fixture()
def tiny_budget(monkeypatch):
    """Shrink the teardown budget so these tests are fast but still meaningful.

    `shutdown()` reads both constants from the module at call time, so patching
    them here changes the behaviour under test rather than just the assertion.
    """
    monkeypatch.setattr(runner, "_SHUTDOWN_BUDGET_SEC", 1.0)
    monkeypatch.setattr(runner, "_AUX_JOIN_CAP_SEC", 0.5)


class TestStrategyCleanupIsBounded:
    """`StrategyBase.stop()` is `self._running = False; self.on_stop()`, and
    `on_stop()` is overridable — `ScriptStrategy.on_stop()`
    (`backend/strategy/script/strategy.py:79`) runs a **sandboxed user script**.

    Calling it synchronously from `WorkerSession.stop()` put it ahead of, and
    outside, the join deadline: one user script that never returns cost the
    poller drain, the equity checkpoint, the heartbeat stop and the audit row.
    The budget bounded `join()`, never `stop()`.
    """

    @pytest.fixture()
    def wedged(self):
        """A strategy whose stop() hangs until the test releases it.

        The release also fires on a timer, so a regression fails the assertion
        instead of hanging the suite.
        """
        release = threading.Event()
        entered = threading.Event()
        strategy = FakeStrategy()

        def _never_returns():
            entered.set()
            release.wait(8)          # safety net: far above the 1s test budget

        strategy.stop = _never_returns
        timer = threading.Timer(6.0, release.set)
        timer.daemon = True
        timer.start()
        try:
            yield strategy, entered
        finally:
            # Release first, then wait the strategy threads out, so none of them
            # survives into the next test still holding a reference to
            # ``runner._SessionFactory``.
            release.set()
            timer.cancel()
            for t in threading.enumerate():
                if t.name.startswith("strategy-") and t is not threading.current_thread():
                    t.join(3)

    def test_a_wedged_on_stop_cannot_outlast_the_budget(
            self, patched_factory, tiny_budget, wedged):
        strategy, _ = wedged
        run_id = _run_row(patched_factory)
        poller = MagicMock()
        poller._thread = FakeThread(name="order-poller")
        w = _worker(poller=poller)
        session = runner.WorkerSession(run_id, strategy)
        w._sessions[run_id] = session
        session.start()

        t0 = time.monotonic()
        w.shutdown()
        elapsed = time.monotonic() - t0

        assert elapsed < runner._SHUTDOWN_BUDGET_SEC + 1.0, (
            f"a user on_stop() held the teardown for {elapsed:.1f}s against a "
            f"{runner._SHUTDOWN_BUDGET_SEC}s budget")

    def test_the_steps_after_the_strategies_still_run(
            self, patched_factory, tiny_budget, wedged):
        """The whole point of bounding it."""
        strategy, _ = wedged
        run_id = _run_row(patched_factory)
        poller = MagicMock()
        poller._thread = FakeThread(name="order-poller")
        heartbeat = MagicMock()
        w = _worker(poller=poller, heartbeat=heartbeat)
        session = runner.WorkerSession(run_id, strategy)
        w._sessions[run_id] = session
        session.start()

        w.shutdown()

        poller.stop.assert_called_once()
        heartbeat.stop.assert_called_once()
        assert len(_audit_rows(patched_factory, "worker_shutdown")) == 1

    def test_stop_returns_without_waiting_for_user_cleanup(
            self, patched_factory, wedged):
        """`WorkerSession.stop()` itself must not block. `_handle_stop()` runs on
        the pub/sub loop thread, so a wedged user script froze the worker's whole
        command loop — it could no longer start or stop anything."""
        strategy, entered = wedged
        run_id = _run_row(patched_factory)
        session = runner.WorkerSession(run_id, strategy)
        session.start()

        t0 = time.monotonic()
        session.stop()
        elapsed = time.monotonic() - t0

        assert elapsed < 1.0, (
            f"stop() blocked {elapsed:.1f}s on user cleanup — the caller is the "
            f"pub/sub command loop")

    def test_an_operator_stop_does_not_block_the_command_loop(
            self, patched_factory, wedged):
        strategy, _ = wedged
        run_id = _run_row(patched_factory)
        w = _worker()
        session = runner.WorkerSession(run_id, strategy)
        w._sessions[run_id] = session
        session.start()

        returned = threading.Event()
        threading.Thread(
            target=lambda: (w._handle_stop({"run_id": run_id}), returned.set()),
            daemon=True).start()

        assert returned.wait(2), (
            "_handle_stop blocked on a wedged on_stop() — the pub/sub command "
            "loop is frozen")

    def test_the_stop_intent_is_recorded_before_user_cleanup(
            self, patched_factory, wedged):
        """`_mark_stopped()` is the durable record that the operator switched the
        strategy off. Running user cleanup ahead of it would let a script that
        never returns leave the row `is_active = True`, and the next boot would
        restore a strategy the operator had switched off."""
        strategy, _ = wedged
        run_id = _run_row(patched_factory)
        w = _worker()
        session = runner.WorkerSession(run_id, strategy)
        w._sessions[run_id] = session
        session.start()

        w._handle_stop({"run_id": run_id})

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _is_active(patched_factory, run_id):
            time.sleep(0.05)

        assert _is_active(patched_factory, run_id) is False, (
            "a wedged on_stop() kept _mark_stopped() from ever running")

    def test_a_raising_on_stop_does_not_lose_the_record(self, patched_factory):
        strategy = FakeStrategy()
        strategy.stop = MagicMock(side_effect=RuntimeError("user script blew up"))
        run_id = _run_row(patched_factory)
        w = _worker()
        session = runner.WorkerSession(run_id, strategy)
        w._sessions[run_id] = session
        session.start()

        w._handle_stop({"run_id": run_id})
        session.join(3)

        assert _is_active(patched_factory, run_id) is False


# ── Review findings (#159): startup must observe the shutdown request ────────

class TestStartupRecoveryIsCancellable:
    """`StartupRecovery.run()` walks nine steps and checked for cancellation at
    none of them. `_step_balance` and `_step_positions` each wait up to
    `_BROKER_STARTUP_TIMEOUT` (30s by default, `recovery.py:28`), so a SIGTERM
    during startup was only noticed after recovery finished and the scheduler had
    already started — long past the 10s SIGKILL deadline.
    """

    def _recovery(self, calls, should_abort=None, abort_after=None):
        """A StartupRecovery whose nine steps are replaced with recorders.

        The real `run()` loop drives them, so the cancellation check is tested
        where it actually lives rather than through a test-only parameter.
        """
        from backend.worker.recovery import StartupRecovery

        rec = StartupRecovery(db_session_factory=MagicMock(),
                              should_abort=should_abort)
        state = {"abort": False}

        def _make(name):
            def _fn():
                calls.append(name)
                if name == abort_after:
                    state["abort"] = True
                return True
            return _fn

        for name in ("_step_db", "_step_redis", "_step_risk", "_step_balance",
                     "_step_positions", "_step_reconcile", "_step_pending_orders",
                     "_step_validate_state", "_step_enable_trading"):
            setattr(rec, name, _make(name))
        return rec, state

    def test_recovery_stops_at_the_next_step_boundary(self):
        calls = []
        state_holder = {}

        def _abort():
            return state_holder.get("state", {}).get("abort", False)

        rec, state = self._recovery(calls, should_abort=_abort,
                                    abort_after="_step_risk")
        state_holder["state"] = state

        ok = rec.run()

        assert calls == ["_step_db", "_step_redis", "_step_risk"], (
            f"recovery kept going after the abort was requested: {calls}")
        assert ok is False

    def test_recovery_runs_every_step_when_not_asked_to_stop(self):
        calls = []
        rec, _ = self._recovery(calls, should_abort=lambda: False)

        assert rec.run() is True
        assert len(calls) == 9

    def test_the_callback_is_optional(self):
        """Existing constructions pass no callback and must keep working."""
        calls = []
        rec, _ = self._recovery(calls)

        assert rec.run() is True
        assert len(calls) == 9

    def test_an_aborted_recovery_is_not_reported_as_a_recovery_failure(self):
        """A shutdown is not a broken recovery. The log/SAFE_MODE reason has to
        say which one it was, or the next operator reads a clean stop as a
        failed boot."""
        from backend.worker.recovery import SAFE_MODE

        calls = []
        state_holder = {}
        rec, state = self._recovery(
            calls, should_abort=lambda: state_holder.get("s", {}).get("abort", False),
            abort_after="_step_db")
        state_holder["s"] = state

        rec.run()

        # SafeModeState exposes no public reason accessor (only can_trade /
        # halt_cause), so the recorded string is read directly.
        reason = SAFE_MODE._reason or ""
        assert "종료" in reason, (
            f"an aborted startup was recorded as {reason!r} — a shutdown is not "
            f"a failed recovery, and the next operator reads this string")


class TestMainSkipsStartupWhenStopping:
    """The finding's concrete claim: `main()` reached `BackgroundScheduler.start()`
    before anything looked at the shutdown flag. Starting the scheduler there
    means standing a component up only to tear it down, and firing jobs into a
    process already on its way out.
    """

    @pytest.fixture()
    def stub_main(self, monkeypatch):
        """Replace everything `main()` reaches for except the flow under test."""
        import backend.worker.recovery as recovery_mod
        import backend.worker.scheduler as scheduler_mod

        worker = _worker()
        worker.shutdown = MagicMock(wraps=worker.shutdown)
        worker.run = MagicMock()

        built = {"scheduler": 0}
        monkeypatch.setattr(runner, "StrategyWorker", lambda: worker)
        monkeypatch.setattr(runner, "_get_session_factory", lambda: MagicMock())
        monkeypatch.setattr(runner, "get_kis_broker", lambda: MagicMock())
        monkeypatch.setattr(runner.redis, "from_url", lambda *a, **k: MagicMock())

        recovery = MagicMock()
        recovery.run.return_value = True
        monkeypatch.setattr(recovery_mod, "StartupRecovery",
                            lambda **kw: recovery)

        def _build():
            built["scheduler"] += 1
            return MagicMock()

        monkeypatch.setattr(scheduler_mod, "build_scheduler", _build)
        return worker, recovery, built

    def test_the_scheduler_is_not_started(self, stub_main):
        worker, recovery, built = stub_main
        worker.request_shutdown()          # as the signal handler does

        runner.main()

        assert built["scheduler"] == 0, (
            "the scheduler was started for a process that is shutting down")
        worker.run.assert_not_called()
        worker.shutdown.assert_called_once()

    def test_recovery_is_handed_the_flag(self, stub_main, monkeypatch):
        """Passing it is what lets recovery stop at a step boundary instead of
        running all nine steps out past the SIGKILL deadline."""
        import backend.worker.recovery as recovery_mod

        captured = {}
        recovery = MagicMock()
        recovery.run.return_value = True
        monkeypatch.setattr(recovery_mod, "StartupRecovery",
                            lambda **kw: (captured.update(kw), recovery)[1])
        worker, _, _ = stub_main

        runner.main()

        assert callable(captured.get("should_abort")), (
            "StartupRecovery was built without a cancellation callback")
        assert captured["should_abort"]() is False
        worker.request_shutdown()
        assert captured["should_abort"]() is True

    def test_a_normal_boot_still_starts_everything(self, stub_main):
        worker, _, built = stub_main

        runner.main()

        assert built["scheduler"] == 1
        worker.run.assert_called_once()
