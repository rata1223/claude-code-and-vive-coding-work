"""Every still-halted row counts on Postgres too (``risk_days_in_play``), and
the multi-day lock the reset takes covers an old halted row in date order."""
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from backend.database.models import DailyRiskState, lock_risk_rows, risk_days_in_play

TODAY = date(2026, 10, 12)


@pytest.fixture()
def factory(pg_trading_engine):
    f = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    with f() as db:
        db.query(DailyRiskState).delete()
        db.commit()
    yield f
    with f() as db:
        db.query(DailyRiskState).delete()
        db.commit()


def test_old_halted_days_are_in_play_newest_first(factory):
    old, older = TODAY - timedelta(days=3), TODAY - timedelta(days=9)
    with factory() as db:
        db.add_all([
            DailyRiskState(trade_date=older, kill_switch=True),
            DailyRiskState(trade_date=old, kill_switch=True),
            DailyRiskState(trade_date=TODAY - timedelta(days=5), kill_switch=False),
        ])
        db.commit()

    with factory() as db:
        assert risk_days_in_play(db, TODAY) == [
            TODAY, TODAY - timedelta(days=1), old, older]


def test_the_reset_lock_reaches_an_old_halted_row(factory):
    old = TODAY - timedelta(days=4)
    with factory() as db:
        db.add_all([DailyRiskState(trade_date=old, kill_switch=True),
                    DailyRiskState(trade_date=TODAY, kill_switch=True)])
        db.commit()

    with factory() as db:
        rows = lock_risk_rows(db, risk_days_in_play(db, TODAY))
        assert [r.trade_date for r in rows] == [old, TODAY], "date order"
        for r in rows:
            r.kill_switch = False
        db.commit()

    with factory() as db:
        assert risk_days_in_play(db, TODAY) == [TODAY, TODAY - timedelta(days=1)]
