"""A release's baseline round-trips through Postgres (P0-12): the floors on an
``AuditLog`` row and the rebased peak on the risk row, read back by a restart."""
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from backend.database.models import AuditLog, DailyRiskState
from backend.quant.risk.engine import (RELEASE_BASELINE_EVENT,
                                       PersistentLossTracker, RiskConfig)

DAY = date(2026, 10, 8)
PEAK = 1_000_000.0


def _clean(f):
    with f() as db:
        db.query(DailyRiskState).delete()
        db.query(AuditLog).filter(
            AuditLog.event_type.in_([RELEASE_BASELINE_EVENT, "kill_switch"])).delete(
                synchronize_session=False)
        db.commit()


@pytest.fixture()
def factory(pg_trading_engine, monkeypatch):
    monkeypatch.setattr("backend.database.models.trading_day", lambda: DAY)
    monkeypatch.setattr(PersistentLossTracker, "_do_kill_switch_io",
                        lambda self, reason: None)
    f = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    _clean(f)
    yield f
    _clean(f)


def _tracker(f):
    return PersistentLossTracker(config=RiskConfig(), redis_client=None, db_factory=f)


def test_the_release_baseline_survives_a_restart(factory):
    t = _tracker(factory)
    t.peak_equity = PEAK
    t.record_pnl(-35_000.0, PEAK * 0.80)          # MDD and daily both breached
    with factory() as db:
        row = db.get(DailyRiskState, DAY)
        row.kill_switch, row.kill_reason = False, None
        db.commit()

    assert t.refresh_from_db() is False

    with factory() as db:
        assert db.get(DailyRiskState, DAY).peak_equity == PEAK * 0.80
        assert db.query(AuditLog).filter(
            AuditLog.event_type == RELEASE_BASELINE_EVENT).count() == 1

    t2 = _tracker(factory)
    assert t2.peak_equity == PEAK * 0.80
    assert t2._daily_floor == (DAY, -35_000.0 - 0.01 * PEAK)
    t2.record_pnl(-5_000.0, PEAK * 0.80 - 5_000.0)
    assert t2.kill_switch is False
