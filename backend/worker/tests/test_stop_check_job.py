"""
The worker runs the stop-loss sweep (known issue 17).

``StrategyWorker`` schedules ``stop_check`` every ``STOP_CHECK_SEC``; each run
calls ``WorkerSession.trigger_stop_check`` on every active session, which runs
the strategy's ``check_stops`` as a counted callback — skipped once the session
is stopping, and waited for before the strategy slot is released, exactly like
``on_market_open``. The sweep itself is tested in
``backend/strategy/tests/test_stop_sweep.py``.
"""
import threading
from datetime import timedelta

from backend.worker.runner import STOP_CHECK_SEC, StrategyWorker, WorkerSession


class _Strategy:
    def __init__(self, block=False):
        self.calls = 0
        self.should_stop = None
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()

    def check_stops(self, should_stop=None):
        self.calls += 1
        self.should_stop = should_stop
        self.entered.set()
        self.release.wait(5)


class _NoStops:
    pass


def _worker(sessions):
    w = StrategyWorker.__new__(StrategyWorker)
    w._shutdown = threading.Event()
    w._shutdown_at = None
    w._lock = threading.RLock()
    w._sessions = sessions
    w._aux_threads = []
    return w


def _sweep(w):
    """Run one tick and wait for the sweep thread it started."""
    w._check_stops()
    t = getattr(w, "_stop_sweep", None)
    if t is not None:
        t.join(5)
        assert not t.is_alive()
    return t


def test_the_job_runs_every_five_minutes():
    from apscheduler.schedulers.background import BackgroundScheduler
    w = _worker({})
    scheduler = BackgroundScheduler()
    w.attach_scheduler(scheduler)
    job = scheduler.get_job("stop_check")
    assert job.func == w._check_stops
    assert job.trigger.interval == timedelta(seconds=STOP_CHECK_SEC) == timedelta(minutes=5)
    assert job.coalesce is True and job.max_instances == 1


def test_each_active_session_is_swept():
    a, b = _Strategy(), _Strategy()
    w = _worker({1: WorkerSession(1, a), 2: WorkerSession(2, b), 3: None})
    t = _sweep(w)
    assert (a.calls, b.calls) == (1, 1)
    assert t in w._aux_threads, "a shutdown must wait for the sweep"


def test_nothing_runs_once_shutdown_is_requested():
    a = _Strategy()
    w = _worker({1: WorkerSession(1, a)})
    w.request_shutdown()
    assert _sweep(w) is None
    assert a.calls == 0


def test_a_stopping_session_is_not_swept():
    a = _Strategy()
    session = WorkerSession(1, a)
    session._stop_event.set()
    session.trigger_stop_check()
    assert a.calls == 0


def test_a_strategy_without_a_stop_is_left_alone():
    WorkerSession(1, _NoStops()).trigger_stop_check()            # no AttributeError


def test_an_error_in_one_sweep_does_not_stop_the_next_session():
    class _Boom:
        def check_stops(self):
            raise RuntimeError("boom")
    b = _Strategy()
    w = _worker({1: WorkerSession(1, _Boom()), 2: WorkerSession(2, b)})
    _sweep(w)
    assert b.calls == 1


def test_a_sweep_in_flight_holds_the_drain():
    a = _Strategy(block=True)
    session = WorkerSession(1, a)
    sweeper = threading.Thread(target=session.trigger_stop_check)
    sweeper.start()
    assert a.entered.wait(5)

    drained = threading.Event()
    threading.Thread(target=lambda: (session._drain_callbacks(), drained.set()),
                     daemon=True).start()
    assert not drained.wait(0.3), "the drain must wait for the sweep"
    a.release.set()
    sweeper.join(5)
    assert drained.wait(5)


def test_the_strategy_is_told_when_the_session_stops():
    a = _Strategy()
    session = WorkerSession(1, a)
    session.trigger_stop_check()
    assert a.should_stop() is False
    session._stop_event.set()
    assert a.should_stop() is True


def test_a_sweep_still_running_is_not_doubled():
    a = _Strategy(block=True)
    w = _worker({1: WorkerSession(1, a)})
    w._check_stops()
    assert a.entered.wait(5)
    first = w._stop_sweep
    w._check_stops()                                           # next tick, still busy
    assert w._stop_sweep is first and a.calls == 1
    a.release.set()
    first.join(5)
    a.entered.clear()
    _sweep(w)
    assert a.calls == 2


def test_a_shutdown_mid_sweep_skips_the_remaining_sessions():
    a, b = _Strategy(block=True), _Strategy()
    w = _worker({1: WorkerSession(1, a), 2: WorkerSession(2, b)})
    w._check_stops()
    assert a.entered.wait(5)
    w.request_shutdown()
    a.release.set()
    w._stop_sweep.join(5)
    assert b.calls == 0
