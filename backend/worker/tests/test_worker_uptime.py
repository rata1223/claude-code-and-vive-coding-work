"""The 4-week gate counts worker uptime, not calendar days.

Before, a run passed after 28 calendar days without a stop, even if the worker
was down for part of them. ``worker_uptime`` records when the worker was alive
(``UptimeRecorder``), and ``uptime_by_run`` counts only that time.
"""
import json
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, Order, StrategyRun, WorkerUptime
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
def factory():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield sessionmaker(bind=eng, expire_on_commit=False)
    eng.dispose()


def _up(factory, boot, last_beat, ended=None):
    with factory() as db:
        db.add(WorkerUptime(worker_id="kis-worker", boot_at=boot, last_beat_at=last_beat,
                            ended_at=ended))
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


def test_a_worker_up_the_whole_time_covers_the_whole_run(factory):
    _up(factory, NOW - 41 * DAY, NOW)
    assert _uptime(factory, _run(NOW - 40 * DAY)) == 40 * DAY


def test_a_crash_counts_only_to_the_last_beat_plus_grace(factory):
    start = NOW - 10 * DAY
    _up(factory, start - H, NOW - 5 * DAY)          # crashed five days ago, never ended
    _up(factory, NOW - 5 * DAY + 6 * H, NOW)        # back six hours later
    got = _uptime(factory, _run(start))
    assert got == 10 * DAY - 6 * H + UPTIME_GRACE


def test_a_restart_costs_only_its_own_seconds(factory):
    start = NOW - 30 * DAY
    t = NOW - 10 * DAY
    _up(factory, start - H, t, ended=t)             # clean stop
    _up(factory, t + timedelta(seconds=20), NOW)    # back 20 s later
    assert _uptime(factory, _run(start)) == 30 * DAY - timedelta(seconds=20)


def test_overlapping_rows_are_not_counted_twice(factory):
    start = NOW - 10 * DAY
    _up(factory, start, NOW)
    _up(factory, start + DAY, NOW - DAY)            # e.g. two workers, or a stray row
    assert _uptime(factory, _run(start)) == 10 * DAY


def test_rows_outside_the_run_do_not_count(factory):
    start = NOW - 10 * DAY
    _up(factory, start - 30 * DAY, start - 20 * DAY, ended=start - 20 * DAY)
    assert _uptime(factory, _run(start)) == timedelta(0)


def test_a_stopped_run_is_clipped_at_its_stop(factory):
    start = NOW - 40 * DAY
    _up(factory, start - DAY, NOW)
    assert _uptime(factory, _run(start, stopped=start + 29 * DAY, is_active=False)) == 29 * DAY


def test_a_zero_day_run_has_no_uptime(factory):
    start = NOW - 40 * DAY
    _up(factory, start - DAY, NOW)
    assert _uptime(factory, _run(start, stopped=start, is_active=False)) == timedelta(0)


def test_a_live_row_is_not_extended_past_now(factory):
    start = NOW - 2 * DAY
    _up(factory, start, NOW - timedelta(seconds=10))
    assert _uptime(factory, _run(start)) == 2 * DAY


def test_many_runs_in_one_call(factory):
    _up(factory, NOW - 50 * DAY, NOW - 20 * DAY)    # crashed 20 days ago
    runs = [_run(NOW - 40 * DAY, rid=1), _run(NOW - 10 * DAY, rid=2)]
    with factory() as db:
        got = uptime_by_run(db, runs, NOW)
    assert got == {1: 20 * DAY + UPTIME_GRACE, 2: timedelta(0)}


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
    _paper_run(factory, start)
    outage_at = start + 10 * DAY
    _up(factory, start - M, outage_at)                 # crashed
    _up(factory, outage_at + 6 * H, now)               # down six hours
    assert LivePromotionGuard(factory)._check_paper_run() is False

    later = now + 3 * H + 2 * M                        # the six hours made up
    with factory() as db:
        db.query(WorkerUptime).filter(WorkerUptime.boot_at > outage_at).update(
            {WorkerUptime.last_beat_at: later})
        db.commit()

    class _Clock(datetime):
        @classmethod
        def utcnow(cls):
            return later
    monkeypatch.setattr("backend.worker.promotion_guard.datetime", _Clock)
    assert LivePromotionGuard(factory)._check_paper_run() is True


def test_a_full_uptime_run_passes(factory):
    now = datetime.utcnow()
    start = now - 28 * DAY - H
    _paper_run(factory, start)
    _up(factory, start - M, now)
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
        return [(r.boot_at, r.last_beat_at, r.ended_at)
                for r in db.query(WorkerUptime).order_by(WorkerUptime.id)]


def test_beats_extend_one_row_and_stop_records_a_clean_end(factory):
    clock = _Clock(NOW)
    rec = UptimeRecorder(factory, clock=clock)
    rec.beat()
    clock.t = NOW + M
    rec.beat()
    clock.t = NOW + 2 * M
    rec.stop()
    assert _rows(factory) == [(NOW, NOW + 2 * M, NOW + 2 * M)]


def test_a_db_failure_never_raises_and_the_next_beat_recovers(factory, caplog):
    clock = _Clock(NOW)
    state = {"down": True}

    def flaky():
        if state["down"]:
            raise RuntimeError("db down")
        return factory()

    rec = UptimeRecorder(flaky, clock=clock)
    with caplog.at_level("WARNING"):
        rec.beat()                                 # the boot insert fails
    assert "가동 기록 실패" in caplog.text
    assert _rows(factory) == []
    state["down"] = False
    clock.t = NOW + M
    rec.beat()
    assert _rows(factory) == [(NOW + M, NOW + M, None)], "uptime starts when it was written"


def test_a_gap_longer_than_the_grace_starts_a_new_row(factory):
    """Beats that could not be written (DB down, process frozen) are not uptime."""
    clock = _Clock(NOW)
    rec = UptimeRecorder(factory, clock=clock)
    rec.beat()
    clock.t = NOW + M
    rec.beat()
    clock.t = NOW + M + UPTIME_GRACE + timedelta(seconds=1)   # beats lost meanwhile
    rec.beat()
    rows = _rows(factory)
    assert len(rows) == 2
    assert rows[0] == (NOW, NOW + M, None)
    assert rows[1][0] == NOW + M + UPTIME_GRACE + timedelta(seconds=1)


def test_a_gap_within_the_grace_keeps_the_row(factory):
    clock = _Clock(NOW)
    rec = UptimeRecorder(factory, clock=clock)
    rec.beat()
    clock.t = NOW + UPTIME_GRACE
    rec.beat()
    assert _rows(factory) == [(NOW, NOW + UPTIME_GRACE, None)]


def test_start_beats_at_once_and_on_its_interval(factory):
    rec = UptimeRecorder(factory, interval_sec=0.05)
    rec.start()
    try:
        assert len(_rows(factory)) == 1
        first = _rows(factory)[0][1]
        deadline = datetime.utcnow() + timedelta(seconds=5)
        while _rows(factory)[0][1] == first and datetime.utcnow() < deadline:
            threading.Event().wait(0.02)
        assert _rows(factory)[0][1] > first
    finally:
        rec.stop()
    assert rec._thread is not None
    rec._thread.join(2)
    assert not rec._thread.is_alive()
    assert _rows(factory)[0][2] is not None


# ── the worker ────────────────────────────────────────────────────────────

def test_shutdown_closes_the_uptime_row():
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    stopped = []
    w._uptime = SimpleNamespace(stop=lambda: stopped.append(1))
    w._shutdown_uptime()
    assert stopped == [1]


def test_a_worker_without_a_recorder_shuts_down_fine():
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w._shutdown_uptime()                           # no AttributeError


def test_shutdown_runs_the_uptime_step():
    import inspect
    src = inspect.getsource(runner.StrategyWorker.shutdown)
    assert '_step("uptime", self._shutdown_uptime)' in src
    # before the heartbeat, which must stay last
    assert src.index('"uptime"') < src.index('"heartbeat"')


def test_main_starts_the_recorder_only_after_a_successful_recovery():
    import inspect
    src = inspect.getsource(runner.main)
    i = src.index("worker.start_uptime(factory)")
    assert src.rindex("if not recovered:", 0, i) < src.rindex("else:", 0, i) < i
    assert src.index("if worker.shutdown_requested:") < i


def test_start_uptime_uses_the_given_factory(factory):
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w.start_uptime(factory)
    try:
        assert len(_rows(factory)) == 1
    finally:
        w._shutdown_uptime()
    assert _rows(factory)[0][2] is not None
