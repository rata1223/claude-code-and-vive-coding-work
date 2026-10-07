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


def test_a_reset_racing_the_daily_carry_clears_the_carried_row_too(factory, monkeypatch):
    """The 07:01 carry locks the old halted row, inserts today's halted row and
    commits while the operator's reset waits on that lock. The reset locks one
    date per statement in date order, so today's row — committed while it
    waited — is seen and cleared too: the release does not leave a halt behind.
    """
    import threading
    from backend.database.models import lock_risk_row

    old = TODAY - timedelta(days=3)
    with factory() as db:
        db.add(DailyRiskState(trade_date=old, kill_switch=True, kill_reason="old"))
        db.commit()

    carry = factory()
    lock_risk_rows(carry, [old])                      # the carry holds the old row
    row, created = lock_risk_row(carry, TODAY)
    assert created
    row.kill_switch, row.kill_reason = True, "old"    # inserted, not committed

    done = {}

    def _reset():
        with factory() as db:
            rows = lock_risk_rows(db, risk_days_in_play(db, TODAY))
            for r in rows:
                r.kill_switch = False
            db.commit()
            done["cleared"] = sorted(r.trade_date for r in rows)

    t = threading.Thread(target=_reset, daemon=True)
    t.start()
    t.join(1.0)
    assert t.is_alive(), "the reset did not wait for the carry's lock"
    carry.commit()
    carry.close()
    t.join(10)
    assert not t.is_alive()

    with factory() as db:
        assert db.get(DailyRiskState, old).kill_switch is False
        assert db.get(DailyRiskState, TODAY).kill_switch is False, (
            "the carried halt survived the release")


def test_a_failed_lookup_on_a_shared_session_still_restores_the_halt(
        pg_trading_engine, factory, monkeypatch):
    """The legacy shared-session path (production uses ``db_factory``).

    On Postgres a failed statement aborts the transaction. The two-day fallback
    reads through the same session, so without a rollback it reads nothing and
    the halt is not restored — fail-open. Whether a row happens to be cached in
    the session must not decide that, so the lookup clears the session first.
    """
    from sqlalchemy import text
    import backend.database.models as models
    from backend.quant.risk.engine import PersistentLossTracker, RiskConfig

    monkeypatch.setattr(models, "trading_day", lambda: TODAY)
    monkeypatch.setattr(PersistentLossTracker, "_do_kill_switch_io",
                        lambda self, reason: None)
    # Yesterday's row: today's is read before the failing lookup, so only a
    # halt on another day needs the fallback to query through the session again.
    with factory() as db:
        db.add(DailyRiskState(trade_date=TODAY - timedelta(days=1),
                              kill_switch=True, kill_reason="어제"))
        db.commit()

    def _broken(sess, today=None):
        sess.expunge_all()
        sess.execute(text("SELECT no_such_column FROM daily_risk_states"))

    monkeypatch.setattr(models, "risk_days_in_play", _broken)
    shared = factory()
    try:
        t = PersistentLossTracker(config=RiskConfig(), redis_client=None, db_session=shared)
        assert t.kill_switch is True
        assert t.kill_reason == "어제"
        # The tracker keeps writing through that session: left in a failed
        # transaction, its first write would fail and the halt never reach
        # today's row.
        t.record_pnl(0.0, 1.0)
    finally:
        shared.close()
    with factory() as db:
        today_row = db.get(DailyRiskState, TODAY)
        assert today_row is not None and today_row.kill_switch is True
