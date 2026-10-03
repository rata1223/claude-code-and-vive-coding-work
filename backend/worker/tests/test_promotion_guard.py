"""The 4-week paper gate (LivePromotionGuard._check_paper_run).

It used to pass on any strategy_runs row started 28+ days ago — including one
stopped a minute after it began. It now needs a run that was not stopped for
28 days: still active with no recorded stop, or stopped after 28 days.
"""
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, StrategyRun
from backend.worker.promotion_guard import (
    PAPER_RUN_MIN, LivePromotionGuard, paper_run_qualifies,
)

NOW = datetime(2026, 10, 3, 12, 0, 0)
DAY = timedelta(days=1)


def _run(started_ago, stopped_after=None, is_active=True):
    started = NOW - started_ago
    stopped = started + stopped_after if stopped_after is not None else None
    return SimpleNamespace(started_at=started, stopped_at=stopped, is_active=is_active)


# ── the rule ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("run, expected", [
    # still running
    (_run(28 * DAY), True),
    (_run(27 * DAY + timedelta(hours=23)), False),
    # stopped by the worker after 28 days
    (_run(40 * DAY, stopped_after=28 * DAY, is_active=False), True),
    # the loophole: started long ago, stopped almost at once
    (_run(40 * DAY, stopped_after=timedelta(minutes=1), is_active=False), False),
    (_run(40 * DAY, stopped_after=27 * DAY, is_active=False), False),
    # stop requested (kis-api clears is_active) but the worker never recorded
    # the stop: when it stopped is unknown
    (_run(40 * DAY, is_active=False), False),
])
def test_paper_run_qualifies(run, expected):
    assert paper_run_qualifies(run, NOW) is expected


def test_a_row_without_a_start_never_qualifies():
    assert paper_run_qualifies(SimpleNamespace(started_at=None, stopped_at=None,
                                               is_active=True), NOW) is False


def test_the_bar_is_four_weeks():
    assert PAPER_RUN_MIN == timedelta(days=28)


# ── against the database ─────────────────────────────────────────────────

@pytest.fixture()
def factory():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield sessionmaker(bind=eng)
    eng.dispose()


def _add(factory, started_ago, stopped_after=None, is_active=True):
    now = datetime.utcnow()
    with factory() as db:
        started = now - started_ago
        db.add(StrategyRun(name="r", strategy_type="indicator", config="{}",
                           is_active=is_active, started_at=started,
                           stopped_at=started + stopped_after if stopped_after else None))
        db.commit()


def test_no_runs_fails(factory):
    assert LivePromotionGuard(factory)._check_paper_run() is False


def test_an_old_row_stopped_at_once_no_longer_passes(factory, caplog):
    _add(factory, 60 * DAY, stopped_after=timedelta(minutes=1), is_active=False)
    with caplog.at_level("WARNING"):
        assert LivePromotionGuard(factory)._check_paper_run() is False
    assert "중지되지 않은 실행 없음" in caplog.text


def test_a_run_active_for_four_weeks_passes(factory):
    _add(factory, 28 * DAY + timedelta(hours=1))
    assert LivePromotionGuard(factory)._check_paper_run() is True


def test_a_run_that_lasted_four_weeks_then_stopped_passes(factory):
    _add(factory, 45 * DAY, stopped_after=29 * DAY, is_active=False)
    assert LivePromotionGuard(factory)._check_paper_run() is True


def test_a_young_active_run_does_not_pass(factory):
    _add(factory, 10 * DAY)
    assert LivePromotionGuard(factory)._check_paper_run() is False


def test_one_qualifying_run_among_short_ones_passes(factory):
    _add(factory, 60 * DAY, stopped_after=DAY, is_active=False)
    _add(factory, 50 * DAY, is_active=False)                # stop never recorded
    _add(factory, 30 * DAY)
    assert LivePromotionGuard(factory)._check_paper_run() is True


def test_a_database_error_fails_closed():
    def broken():
        raise RuntimeError("db down")
    assert LivePromotionGuard(broken)._check_paper_run() is False


def test_the_checklist_reports_the_gate_by_name(factory, monkeypatch):
    for k, v in {"KIS_ENV": "real", "ENABLE_LIVE_TRADING": "true",
                 "TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}.items():
        monkeypatch.setenv(k, v)
    guard = LivePromotionGuard(factory)
    monkeypatch.setattr(guard, "_check_redis", lambda: True)
    _add(factory, 60 * DAY, stopped_after=timedelta(minutes=1), is_active=False)
    ok, failed = guard.check()
    assert ok is False
    assert failed == ["4주 모의투자 완료"]


# ── a start that never ran must not count ─────────────────────────────────
#
# The worker left a run active (no stopped_at) when it could not build the
# strategy — an unknown type, or no broker. Nothing ran, yet 28 days later the
# row read as a run that had been going all along.

import threading  # noqa: E402

from backend.worker import runner  # noqa: E402


def _worker():
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w._lock = threading.Lock()
    w._sessions = {}
    return w


def _active_row(factory, started_ago):
    with factory() as db:
        run = StrategyRun(name="r", strategy_type="nope", config="{}", is_active=True,
                          started_at=datetime.utcnow() - started_ago)
        db.add(run)
        db.commit()
        return run.id


def _row(factory, run_id):
    with factory() as db:
        return db.get(StrategyRun, run_id)


def test_a_new_start_that_cannot_build_is_recorded_as_stopped(factory, monkeypatch):
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    w = _worker()
    monkeypatch.setattr(w, "_build_strategy", lambda data: None)
    run_id = _active_row(factory, 40 * DAY)

    w._handle_start({"run_id": run_id, "strategy_type": "nope"})

    row = _row(factory, run_id)
    assert row.is_active is False
    # zero duration: handled 40 days late, it still never ran
    assert row.stopped_at == row.started_at
    assert run_id not in w._sessions
    assert LivePromotionGuard(factory)._check_paper_run() is False


def test_a_restore_that_cannot_build_keeps_the_run_for_the_next_boot(factory, monkeypatch):
    """A transient failure at boot (e.g. broker unreachable) must not end a
    running strategy; the next boot retries it."""
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    w = _worker()
    monkeypatch.setattr(w, "_build_strategy", lambda data: None)
    run_id = _active_row(factory, 3 * DAY)

    w._restore_active()

    row = _row(factory, run_id)
    assert row.is_active is True and row.stopped_at is None


def test_a_start_failure_does_not_overwrite_an_existing_stop(factory, monkeypatch):
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    w = _worker()
    stopped = datetime.utcnow() - 5 * DAY
    with factory() as db:
        run = StrategyRun(name="r", strategy_type="nope", config="{}", is_active=False,
                          started_at=stopped - 30 * DAY, stopped_at=stopped)
        db.add(run)
        db.commit()
        run_id = run.id

    w._mark_start_failed(run_id, "nope")

    assert _row(factory, run_id).stopped_at == stopped


# ── kis-api refuses a type the worker cannot build ────────────────────────

def test_kis_api_rejects_an_unknown_strategy_type_before_writing_a_run(monkeypatch):
    """An unknown type used to become an active strategy_runs row the worker
    dropped ("알 수 없는 전략 유형") — a run that never ran but aged."""
    pytest.importorskip("flask")
    from backend.api import server as srv

    monkeypatch.setattr(srv, "_API_KEY", "k")
    monkeypatch.setattr(srv, "_strategy_start_calls", [])
    monkeypatch.setattr(srv, "get_db", lambda: pytest.fail("no row may be written"))
    srv.app.config.update(TESTING=True)
    res = srv.app.test_client().post(
        "/api/strategies/start", headers={"X-API-Key": "k"},
        json={"name": "x", "strategy_type": "ai", "config": {}})
    assert res.status_code == 400
    assert "전략 유형" in res.get_json()["error"]


def test_kis_api_startable_types_match_what_the_worker_builds():
    import inspect
    from backend.api import server as srv
    src = inspect.getsource(runner.StrategyWorker._build_strategy)
    built = set(re.findall(r'stype == "(\w+)"', src))
    assert built == set(srv._STARTABLE_STRATEGY_TYPES)


# ── strategy.start() itself fails (CodeRabbit) ────────────────────────────

class _FailingStrategy:
    def __init__(self):
        self.stopped = False

    def start(self):
        raise RuntimeError("start failed")

    def stop(self):
        self.stopped = True


def test_a_new_session_whose_start_raises_is_recorded_as_never_ran(factory, monkeypatch):
    """Handled 40 days late and failing at once must not become a 40-day run."""
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    run_id = _active_row(factory, 40 * DAY)

    strat = _FailingStrategy()
    runner.WorkerSession(run_id, strat, new_start=True)._run()

    row = _row(factory, run_id)
    assert row.is_active is False and row.stopped_at == row.started_at
    assert strat.stopped is True, "cleanup still runs"
    assert LivePromotionGuard(factory)._check_paper_run() is False


def test_a_restored_session_whose_start_raises_keeps_the_old_stop_record(factory, monkeypatch):
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    run_id = _active_row(factory, 3 * DAY)
    before = datetime.utcnow()

    runner.WorkerSession(run_id, _FailingStrategy(), new_start=False)._run()

    row = _row(factory, run_id)
    assert row.is_active is False and row.stopped_at >= before


def test_handle_start_marks_a_command_session_as_a_new_start(monkeypatch):
    w = _worker()
    built = object()
    monkeypatch.setattr(w, "_build_strategy", lambda data: built)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    made = []

    class _Session:
        def __init__(self, run_id, strategy, new_start=False):
            made.append(new_start)

        def start(self):
            pass

    monkeypatch.setattr(runner, "WorkerSession", _Session)
    monkeypatch.setattr(w, "_run_still_wanted", lambda run_id: True)
    w._handle_start({"run_id": 1})
    w._handle_start({"run_id": 2}, restoring=True)
    assert made == [True, False]


# ── the record is retried, then escalated ─────────────────────────────────

def _flaky_session(factory, failures):
    from contextlib import contextmanager
    state = {"left": failures}

    @contextmanager
    def session():
        if state["left"] > 0:
            state["left"] -= 1
            raise RuntimeError("db down")
        db = factory()
        try:
            yield db
        finally:
            db.close()
    return session


def test_a_transient_db_error_is_retried(factory, monkeypatch):
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    run_id = _active_row(factory, 40 * DAY)
    monkeypatch.setattr(runner, "_session", _flaky_session(factory, failures=2))

    assert runner._record_never_ran(run_id, "nope", "test") is True
    row = _row(factory, run_id)
    assert row.stopped_at == row.started_at


def test_a_lasting_db_error_raises_an_emergency_alert(factory, monkeypatch, caplog):
    import sys
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    monkeypatch.setattr(runner, "_session", _flaky_session(factory, failures=99))
    alerts = []
    notifier = SimpleNamespace(alert_emergency=lambda msg: alerts.append(msg))
    monkeypatch.setitem(sys.modules, "bot.notifier", notifier)

    with caplog.at_level("CRITICAL"):
        assert runner._record_never_ran(77, "nope", "test") is False
    assert alerts and "run_id=77" in alerts[0] and "stopped_at = started_at" in alerts[0]
    assert "77" in caplog.text


# ── a replayed start command must not revive a stopped run (CodeRabbit) ───

def _built_worker(monkeypatch, factory):
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    w = _worker()
    monkeypatch.setattr(w, "_build_strategy", lambda data: pytest.fail("must not build"))
    return w


def test_a_replayed_start_of_a_never_ran_run_is_ignored(factory, monkeypatch):
    w = _built_worker(monkeypatch, factory)
    run_id = _active_row(factory, 2 * DAY)
    runner._record_never_ran(run_id, "nope", "test")

    w._handle_start({"run_id": run_id, "strategy_type": "indicator"})

    assert run_id not in w._sessions


def test_a_replayed_start_of_an_operator_stopped_run_is_ignored(factory, monkeypatch):
    w = _built_worker(monkeypatch, factory)
    with factory() as db:
        run = StrategyRun(name="r", strategy_type="indicator", config="{}", is_active=False,
                          started_at=datetime.utcnow() - DAY)
        db.add(run)
        db.commit()
        run_id = run.id

    w._handle_start({"run_id": run_id, "strategy_type": "indicator"})

    assert run_id not in w._sessions


def test_a_start_for_a_missing_row_is_ignored(factory, monkeypatch):
    w = _built_worker(monkeypatch, factory)
    w._handle_start({"run_id": 4242, "strategy_type": "indicator"})
    assert 4242 not in w._sessions


def test_an_active_run_still_starts(factory, monkeypatch):
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    w = _worker()
    built = []
    monkeypatch.setattr(w, "_build_strategy", lambda data: built.append(1) or _FailingStrategy())
    monkeypatch.setattr(runner.WorkerSession, "start", lambda self: None)
    run_id = _active_row(factory, DAY)

    w._handle_start({"run_id": run_id, "strategy_type": "indicator"})

    assert built == [1] and w._sessions.get(run_id) is not None


# ── a stop with no session must still free the slot (code-review) ─────────
#
# kis-api counts a row without stopped_at as holding the single strategy slot,
# and only a running session used to write stopped_at. A stop that found no
# session (stopped before it was built, or replayed after a boot that skipped
# the inactive row) left the slot taken for good.

def test_a_stop_with_no_session_records_a_zero_length_end(factory, monkeypatch):
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    w = _worker()
    run_id = _active_row(factory, 40 * DAY)
    with factory() as db:                      # kis-api's stop: is_active only
        db.get(StrategyRun, run_id).is_active = False
        db.commit()

    w._handle_stop({"run_id": run_id})

    row = _row(factory, run_id)
    assert row.stopped_at == row.started_at, "frees the slot, credits no days"
    assert LivePromotionGuard(factory)._check_paper_run() is False


def test_a_stop_with_no_session_frees_the_kis_api_slot(factory, monkeypatch):
    from backend.api import server as srv
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    run_id = _active_row(factory, DAY)
    with factory() as db:
        db.get(StrategyRun, run_id).is_active = False
        db.commit()
    with factory() as db:
        assert srv._occupying_run(db) == run_id

    _worker()._handle_stop({"run_id": run_id})

    with factory() as db:
        assert srv._occupying_run(db) is None


def test_a_stop_during_the_build_cancels_the_start(factory, monkeypatch):
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(runner.WorkerSession, "start",
                        lambda self: pytest.fail("a stopped run must not start"))
    w = _worker()
    run_id = _active_row(factory, DAY)

    def build_then_get_stopped(data):
        w._handle_stop({"run_id": run_id})    # arrives while building
        return _FailingStrategy()

    monkeypatch.setattr(w, "_build_strategy", build_then_get_stopped)
    w._handle_start({"run_id": run_id, "strategy_type": "indicator"})

    assert run_id not in w._sessions
    assert _row(factory, run_id).stopped_at is not None


# ── a stop that is still ending must not free the slot ────────────────────
#
# The first stop removes the session and returns at once; the session's thread
# and any on_market_open already running (on its own thread) can still act on
# the account. A second stop used to find no session, record a zero-day end and
# free the slot — letting a new run start beside the old one. The slot
# (stopped_at) is now released only once the old run has quiesced.

class _BlockingOpenStrategy:
    """on_market_open blocks until released, like a slow broker call."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.opens = 0

    def start(self):
        pass

    def stop(self):
        pass

    def on_market_open(self):
        self.opens += 1
        self.entered.set()
        assert self.release.wait(10)


def _running_session(w, factory, monkeypatch, strategy):
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    run_id = _active_row(factory, DAY)
    session = runner.WorkerSession(run_id, strategy)
    w._sessions[run_id] = session
    session.start()
    return run_id, session


def _kis_api_stop(factory, run_id):
    with factory() as db:                      # kis-api's stop: is_active only
        db.get(StrategyRun, run_id).is_active = False
        db.commit()


def test_a_stop_waits_for_an_in_flight_market_open_before_freeing_the_slot(factory, monkeypatch):
    from backend.api import server as srv
    w = _worker()
    strategy = _BlockingOpenStrategy()
    run_id, session = _running_session(w, factory, monkeypatch, strategy)
    opener = threading.Thread(target=session.trigger_market_open, args=("US",))
    opener.start()
    assert strategy.entered.wait(5)

    _kis_api_stop(factory, run_id)
    w._handle_stop({"run_id": run_id})
    assert session.join(1.5) is False, "the session waits for the callback"
    row = _row(factory, run_id)
    assert row.is_active is False and row.stopped_at is None
    with factory() as db:
        assert srv._occupying_run(db) == run_id

    strategy.release.set()
    opener.join(5)
    assert session.join(5) is True
    row = _row(factory, run_id)
    assert row.stopped_at is not None and row.stopped_at > row.started_at
    with factory() as db:
        assert srv._occupying_run(db) is None


def test_a_repeated_stop_while_still_stopping_does_not_free_the_slot(factory, monkeypatch):
    w = _worker()
    strategy = _BlockingOpenStrategy()
    run_id, session = _running_session(w, factory, monkeypatch, strategy)
    opener = threading.Thread(target=session.trigger_market_open, args=("US",))
    opener.start()
    assert strategy.entered.wait(5)
    _kis_api_stop(factory, run_id)
    w._handle_stop({"run_id": run_id})

    w._handle_stop({"run_id": run_id})         # the screen's "stop again"

    assert _row(factory, run_id).stopped_at is None
    strategy.release.set()
    opener.join(5)
    assert session.join(5) is True
    row = _row(factory, run_id)
    assert row.stopped_at > row.started_at, "the real end, not a zero-day record"


def test_no_market_open_starts_once_the_session_is_stopping(factory, monkeypatch):
    w = _worker()
    strategy = _BlockingOpenStrategy()
    strategy.release.set()
    run_id, session = _running_session(w, factory, monkeypatch, strategy)
    w._handle_stop({"run_id": run_id})

    session.trigger_market_open("US")

    assert strategy.opens == 0


def test_a_stop_after_the_session_ended_keeps_the_recorded_end(factory, monkeypatch):
    w = _worker()
    strategy = _BlockingOpenStrategy()
    run_id, session = _running_session(w, factory, monkeypatch, strategy)
    _kis_api_stop(factory, run_id)
    w._handle_stop({"run_id": run_id})
    assert session.join(5) is True
    ended = _row(factory, run_id).stopped_at

    w._handle_stop({"run_id": run_id})

    assert _row(factory, run_id).stopped_at == ended
    assert run_id not in w._stopping


class _BlockingStopStrategy(_BlockingOpenStrategy):
    """stop() (on_stop) blocks until released."""

    def __init__(self):
        super().__init__()
        self.stopping = threading.Event()
        self.stop_release = threading.Event()

    def stop(self):
        self.stopping.set()
        assert self.stop_release.wait(10)


def test_the_slot_is_held_until_the_stop_hook_returns(factory, monkeypatch):
    from backend.api import server as srv
    w = _worker()
    strategy = _BlockingStopStrategy()
    run_id, session = _running_session(w, factory, monkeypatch, strategy)
    _kis_api_stop(factory, run_id)
    w._handle_stop({"run_id": run_id})
    assert strategy.stopping.wait(5)

    row = _row(factory, run_id)
    assert row.is_active is False, "never restored, even while on_stop runs"
    assert row.stopped_at is None
    with factory() as db:
        assert srv._occupying_run(db) == run_id

    strategy.stop_release.set()
    assert session.join(5) is True
    with factory() as db:
        assert srv._occupying_run(db) is None
