"""The 4-week gate counts the time a run was actually running, not calendar days.

Before, a run passed after 28 calendar days without a stop, even if no worker
was running it for part of them. Each run's session records ``run_uptime``
(``UptimeRecorder``), and ``uptime_by_run`` counts only that time.
"""
import json
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.models import Base, Order, RunUptime, StrategyRun
from backend.worker import runner
from backend.worker.promotion_guard import (
    PAPER_RUN_MIN, LivePromotionGuard, covered_time, uptime_by_run,
)
from backend.worker.uptime import BEAT_INTERVAL_SEC, UPTIME_GRACE, UptimeRecorder

NOW = datetime(2026, 10, 4, 12, 0, 0)
DAY = timedelta(days=1)
H = timedelta(hours=1)
M = timedelta(minutes=1)


@pytest.fixture()
def factory(tmp_path):
    # A file, not one shared in-memory connection: the recorder writes from its
    # own threads while the test reads, and SQLite connections are not safe to
    # share across threads (a colliding write fails and is swallowed by design,
    # which made the threaded tests flaky). Each thread gets its own connection,
    # as each session does in the worker.
    eng = create_engine(f"sqlite:///{tmp_path / 'uptime.db'}",
                        connect_args={"check_same_thread": False, "timeout": 10})
    Base.metadata.create_all(eng)
    yield sessionmaker(bind=eng, expire_on_commit=False)
    eng.dispose()


def _up(factory, boot, last_beat, ended=None, run_id=1):
    with factory() as db:
        db.add(RunUptime(run_id=run_id, boot_at=boot, last_beat_at=last_beat, ended_at=ended))
        db.commit()


def _run(started, stopped=None, is_active=True, rid=1):
    return SimpleNamespace(id=rid, started_at=started, stopped_at=stopped, is_active=is_active)


def _uptime(factory, run, now=NOW):
    with factory() as db:
        return uptime_by_run(db, [run], now)[run.id]


# ── interval arithmetic ───────────────────────────────────────────────────

def test_covered_time_merges_overlaps_and_clips():
    a = NOW - 10 * H
    intervals = [(a, a + 3 * H), (a + 2 * H, a + 4 * H),   # overlap: 4h, not 5h
                 (a + 6 * H, a + 20 * H),                   # runs past the end
                 (a - 5 * H, a - H)]                        # before the start
    assert covered_time(intervals, a, a + 10 * H) == 8 * H


def test_covered_time_of_nothing_is_zero():
    assert covered_time([], NOW - H, NOW) == timedelta(0)


# ── uptime inside a run ───────────────────────────────────────────────────

def test_no_record_means_no_uptime(factory):
    """Runs from before this table existed never qualify."""
    assert _uptime(factory, _run(NOW - 40 * DAY)) == timedelta(0)


def test_running_the_whole_time_covers_the_whole_run(factory):
    _up(factory, NOW - 40 * DAY, NOW)
    assert _uptime(factory, _run(NOW - 40 * DAY)) == 40 * DAY


def test_a_crash_counts_only_to_the_last_beat_plus_grace(factory):
    start = NOW - 10 * DAY
    _up(factory, start, NOW - 5 * DAY)              # crashed five days ago, never ended
    _up(factory, NOW - 5 * DAY + 6 * H, NOW)        # restored six hours later
    got = _uptime(factory, _run(start))
    assert got == 10 * DAY - 6 * H + UPTIME_GRACE


def test_a_restart_costs_only_its_own_seconds(factory):
    start = NOW - 30 * DAY
    t = NOW - 10 * DAY
    _up(factory, start, t, ended=t)                 # clean stop (deploy)
    _up(factory, t + timedelta(seconds=20), NOW)    # restored 20 s later
    assert _uptime(factory, _run(start)) == 30 * DAY - timedelta(seconds=20)


def test_overlapping_rows_are_not_counted_twice(factory):
    start = NOW - 10 * DAY
    _up(factory, start, NOW)
    _up(factory, start + DAY, NOW - DAY)
    assert _uptime(factory, _run(start)) == 10 * DAY


def test_another_runs_rows_do_not_count(factory):
    """The worker being up is not this run running."""
    start = NOW - 10 * DAY
    _up(factory, start, NOW, run_id=2)
    assert _uptime(factory, _run(start, rid=1)) == timedelta(0)


def test_rows_outside_the_run_do_not_count(factory):
    start = NOW - 10 * DAY
    _up(factory, start - 30 * DAY, start - 20 * DAY, ended=start - 20 * DAY)
    assert _uptime(factory, _run(start)) == timedelta(0)


def test_a_stopped_run_is_clipped_at_its_stop(factory):
    start = NOW - 40 * DAY
    _up(factory, start, NOW)
    assert _uptime(factory, _run(start, stopped=start + 29 * DAY, is_active=False)) == 29 * DAY


def test_a_zero_day_run_has_no_uptime(factory):
    start = NOW - 40 * DAY
    _up(factory, start, NOW)
    assert _uptime(factory, _run(start, stopped=start, is_active=False)) == timedelta(0)


def test_a_live_row_is_not_extended_past_now(factory):
    start = NOW - 2 * DAY
    _up(factory, start, NOW - timedelta(seconds=10))
    assert _uptime(factory, _run(start)) == 2 * DAY


def test_many_runs_in_one_call(factory):
    _up(factory, NOW - 40 * DAY, NOW - 20 * DAY, run_id=1)     # crashed 20 days ago
    _up(factory, NOW - 10 * DAY, NOW, run_id=2)
    runs = [_run(NOW - 40 * DAY, rid=1), _run(NOW - 10 * DAY, rid=2), _run(NOW - DAY, rid=3)]
    with factory() as db:
        got = uptime_by_run(db, runs, NOW)
    assert got == {1: 20 * DAY + UPTIME_GRACE, 2: 10 * DAY, 3: timedelta(0)}


# ── the gate, end to end ──────────────────────────────────────────────────

def _paper_run(factory, started, fills=1):
    with factory() as db:
        run = StrategyRun(name="r", strategy_type="indicator",
                          config=json.dumps({"kis_env": "paper"}),
                          is_active=True, started_at=started)
        db.add(run)
        db.flush()
        for i in range(fills):
            db.add(Order(broker_order_id=f"o{i}", symbol="SPY", side="buy", qty=1, price=1.0,
                         filled_qty=1, status="filled", market="US", strategy_run_id=run.id))
        db.commit()
        return run.id


def test_six_hours_down_move_the_gate_six_hours(factory, monkeypatch):
    now = datetime.utcnow()
    start = now - 28 * DAY - 3 * H                     # 28 days and 3 hours ago
    rid = _paper_run(factory, start)
    outage_at = start + 10 * DAY
    _up(factory, start, outage_at, run_id=rid)         # crashed
    _up(factory, outage_at + 6 * H, now, run_id=rid)   # down six hours
    assert LivePromotionGuard(factory)._check_paper_run() is False

    later = now + 3 * H + 2 * M                        # the six hours made up
    with factory() as db:
        db.query(RunUptime).filter(RunUptime.boot_at > outage_at).update(
            {RunUptime.last_beat_at: later})
        db.commit()

    class _Clock(datetime):
        @classmethod
        def utcnow(cls):
            return later
    monkeypatch.setattr("backend.worker.promotion_guard.datetime", _Clock)
    assert LivePromotionGuard(factory)._check_paper_run() is True


def test_a_run_that_ran_the_whole_time_passes(factory):
    now = datetime.utcnow()
    start = now - 28 * DAY - H
    rid = _paper_run(factory, start)
    _up(factory, start, now, run_id=rid)
    assert LivePromotionGuard(factory)._check_paper_run() is True


def test_a_run_without_any_uptime_record_does_not_pass(factory, caplog):
    _paper_run(factory, datetime.utcnow() - 40 * DAY)
    with caplog.at_level("WARNING"):
        assert LivePromotionGuard(factory)._check_paper_run() is False
    assert "'duration'" in caplog.text


def test_the_grace_is_above_the_beat_interval():
    """Otherwise the time between two beats would be downtime."""
    assert UPTIME_GRACE > timedelta(seconds=BEAT_INTERVAL_SEC)
    assert PAPER_RUN_MIN == 28 * DAY


# ── the recorder ──────────────────────────────────────────────────────────

class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _rows(factory):
    with factory() as db:
        return [(r.run_id, r.boot_at, r.last_beat_at, r.ended_at)
                for r in db.query(RunUptime).order_by(RunUptime.id)]


def test_beats_extend_one_row_and_stop_records_a_clean_end(factory):
    clock = _Clock(NOW)
    rec = UptimeRecorder(factory, run_id=7, clock=clock)
    rec.beat()
    clock.t = NOW + M
    rec.beat()
    clock.t = NOW + 2 * M
    assert rec.stop() is True
    assert _rows(factory) == [(7, NOW, NOW + 2 * M, NOW + 2 * M)]


def test_a_db_failure_never_raises_and_the_next_beat_recovers(factory, caplog):
    clock = _Clock(NOW)
    state = {"down": True}

    def flaky():
        if state["down"]:
            raise RuntimeError("db down")
        return factory()

    rec = UptimeRecorder(flaky, run_id=7, clock=clock)
    with caplog.at_level("WARNING"):
        rec.beat()                                 # the first insert fails
    assert "가동 기록 실패" in caplog.text
    assert _rows(factory) == []
    state["down"] = False
    clock.t = NOW + M
    rec.beat()
    assert _rows(factory) == [(7, NOW + M, NOW + M, None)], "counts from when it was written"


def test_a_gap_longer_than_the_grace_starts_a_new_row(factory):
    """Beats that could not be written (DB down, process frozen) are not uptime."""
    clock = _Clock(NOW)
    rec = UptimeRecorder(factory, run_id=7, clock=clock)
    rec.beat()
    clock.t = NOW + M
    rec.beat()
    clock.t = NOW + M + UPTIME_GRACE + timedelta(seconds=1)   # beats lost meanwhile
    rec.beat()
    rows = _rows(factory)
    assert len(rows) == 2
    assert rows[0] == (7, NOW, NOW + M, None)
    assert rows[1][1] == NOW + M + UPTIME_GRACE + timedelta(seconds=1)


def test_a_gap_within_the_grace_keeps_the_row(factory):
    clock = _Clock(NOW)
    rec = UptimeRecorder(factory, run_id=7, clock=clock)
    rec.beat()
    clock.t = NOW + UPTIME_GRACE
    rec.beat()
    assert _rows(factory) == [(7, NOW, NOW + UPTIME_GRACE, None)]


def test_start_beats_at_once_and_on_its_interval(factory):
    rec = UptimeRecorder(factory, run_id=7, interval_sec=0.05)
    rec.start()
    try:
        assert len(_rows(factory)) == 1
        first = _rows(factory)[0][2]
        deadline = time.monotonic() + 5
        while _rows(factory)[0][2] == first and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _rows(factory)[0][2] > first
    finally:
        rec.stop()
    rec._thread.join(2)
    assert not rec._thread.is_alive()
    assert _rows(factory)[0][3] is not None


def test_a_late_final_write_records_the_stop_time_not_the_write_time(factory):
    """A beat stuck in the DB holds the lock past stop()'s wait; when the final
    write gets in later it must not stretch the run past when it stopped (CodeRabbit)."""
    clock = _Clock(NOW)
    rec = UptimeRecorder(factory, run_id=7, clock=clock)
    rec.beat()
    rec._lock.acquire()                            # a beat stuck in the DB
    try:
        clock.t = NOW + M                          # stop() is called here
        assert rec.stop(timeout=0.1) is False
        clock.t = NOW + M + timedelta(seconds=50)  # the write gets in later
    finally:
        rec._lock.release()
    deadline = time.monotonic() + 5
    while _rows(factory)[0][3] is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _rows(factory) == [(7, NOW, NOW + M, NOW + M)]


def test_a_stalled_db_does_not_hold_up_stop(factory):
    """The worker's shutdown budget is 8 s; the final write gets a bounded wait."""
    release = threading.Event()

    def stalled():
        release.wait(10)
        return factory()

    rec = UptimeRecorder(stalled, run_id=7)
    t0 = time.monotonic()
    assert rec.stop(timeout=0.2) is False
    assert time.monotonic() - t0 < 2
    release.set()


# ── the session records its run ───────────────────────────────────────────

class _Strategy:
    def __init__(self, fail=False):
        self.fail = fail

    def start(self):
        if self.fail:
            raise RuntimeError("start failed")

    def stop(self):
        pass


@pytest.fixture()
def recording(factory, monkeypatch):
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_run_uptime_factory", None)
    runner.enable_run_uptime(factory)
    return factory


def _active_run(factory):
    with factory() as db:
        run = StrategyRun(name="r", strategy_type="indicator", config="{}", is_active=True,
                          started_at=datetime.utcnow() - DAY)
        db.add(run)
        db.commit()
        return run.id


def test_a_running_session_records_its_run_until_it_ends(recording):
    rid = _active_run(recording)
    session = runner.WorkerSession(rid, _Strategy())
    session.start()
    deadline = time.monotonic() + 5
    while not _rows(recording) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [r[0] for r in _rows(recording)] == [rid]
    assert _rows(recording)[0][3] is None, "still running"

    session.stop(deactivate=False)                 # e.g. the worker shutting down
    assert session.join(5)
    assert _rows(recording)[0][3] is not None, "a clean end"


def test_a_session_whose_start_fails_records_nothing(recording):
    rid = _active_run(recording)
    session = runner.WorkerSession(rid, _Strategy(fail=True))
    session.start()
    assert session.join(5)
    assert _rows(recording) == []


def test_nothing_is_recorded_until_recording_is_enabled(factory, monkeypatch):
    """main() enables it only once startup recovery has succeeded."""
    monkeypatch.setattr(runner, "_SessionFactory", factory)
    monkeypatch.setattr(runner, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_run_uptime_factory", None)
    rid = _active_run(factory)
    session = runner.WorkerSession(rid, _Strategy())
    session.start()
    time.sleep(0.1)
    session.stop(deactivate=False)
    assert session.join(5)
    assert _rows(factory) == []


def test_a_recorder_that_cannot_start_does_not_stop_the_run(recording, monkeypatch):
    import backend.worker.uptime as uptime_mod

    def boom(*a, **k):
        raise RuntimeError("no recorder")
    monkeypatch.setattr(uptime_mod, "UptimeRecorder", boom)
    assert runner._start_run_uptime(5) is None


def test_main_enables_recording_only_after_a_successful_recovery():
    import inspect
    src = inspect.getsource(runner.main)
    i = src.index("enable_run_uptime(factory)")
    assert src.rindex("if not recovered and not recovery.halted_by_risk:", 0, i) \
        < src.rindex("elif recovered:", 0, i) < i
    assert src.index("if worker.shutdown_requested:") < i
