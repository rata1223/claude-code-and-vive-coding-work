"""The 4-week paper gate (LivePromotionGuard._check_paper_run).

It used to pass on any strategy_runs row started 28+ days ago — including one
stopped a minute after it began. It now needs a run that was not stopped for
28 days: still active with no recorded stop, or stopped after 28 days.
"""
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
