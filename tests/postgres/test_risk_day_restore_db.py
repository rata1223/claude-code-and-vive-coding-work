"""The risk tracker's restore queries on Postgres (#166): the weekly window is
rebuilt from the six prior risk days' rows and the MDD peak from the latest row
that has one — the same answers as on SQLite."""
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from backend.database.models import DailyRiskState
from backend.quant.risk.engine import PersistentLossTracker, RiskConfig

TODAY = date(2026, 9, 10)


@pytest.fixture()
def factory(pg_trading_engine, monkeypatch):
    monkeypatch.setattr("backend.database.models.trading_day", lambda: TODAY)
    monkeypatch.setattr(PersistentLossTracker, "_do_kill_switch_io",
                        lambda self, reason: None)
    f = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    with f() as db:
        db.query(DailyRiskState).delete()
        db.commit()
    yield f
    with f() as db:
        db.query(DailyRiskState).delete()
        db.commit()


def _seed(factory, rows):
    with factory() as db:
        db.add_all(DailyRiskState(trade_date=d, **kw) for d, kw in rows)
        db.commit()


def _tracker(factory):
    return PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                 db_factory=factory)


def test_the_week_and_the_peak_come_back_after_a_restart(factory):
    _seed(factory, [
        (TODAY - timedelta(days=7), dict(daily_pnl=-500_000.0, peak_equity=9_000_000.0)),
        (TODAY - timedelta(days=6), dict(daily_pnl=-10_000.0, peak_equity=1_200_000.0)),
        (TODAY - timedelta(days=2), dict(daily_pnl=-20_000.0, peak_equity=1_300_000.0)),
        (TODAY - timedelta(days=1), dict(daily_pnl=-5_000.0, peak_equity=0.0)),
        (TODAY + timedelta(days=1), dict(daily_pnl=-999.0, peak_equity=0.0)),
    ])

    t = _tracker(factory)

    assert t.daily_pnl == 0.0
    assert t.weekly_pnl == -35_000.0, "six prior days only: not the 7th back, not tomorrow"
    assert t.peak_equity == 1_300_000.0, "the latest row with a peak"


def test_todays_row_wins_for_the_peak_and_adds_to_the_week(factory):
    _seed(factory, [
        (TODAY - timedelta(days=1), dict(daily_pnl=-5_000.0, peak_equity=2_000_000.0)),
        (TODAY, dict(daily_pnl=-1_000.0, weekly_pnl=-77_000.0, peak_equity=1_500_000.0)),
    ])

    t = _tracker(factory)

    assert t.peak_equity == 1_500_000.0
    assert t.weekly_pnl == -6_000.0, "built from daily figures, not the stored column"
