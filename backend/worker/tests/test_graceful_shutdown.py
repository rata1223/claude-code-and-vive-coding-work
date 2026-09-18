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


def _run_row(factory, is_active=True):
    sess = factory()
    row = StrategyRun(strategy_type="indicator", name="t", config="{}",
                      broker="kis", is_active=is_active)
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
