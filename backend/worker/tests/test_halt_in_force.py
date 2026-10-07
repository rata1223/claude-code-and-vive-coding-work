"""A halt stays in force however long nothing wrote a newer row.

Every reader used to ask "is trading halted?" of two rows — today's risk day
and yesterday's. A halt reaches a newer row only when something writes one
(the tracker at a fill or shutdown, the 07:01 job), so a worker down for two
whole risk days left the halt on a row nobody read: the next boot came up
unhalted, the reporters said "not halted", and the reset could not reach it.

Now every still-halted row counts (``models.risk_days_in_play``). These pin
the worker side; the app's status and reset endpoints are covered in
``api/tests/test_risk_killswitch_reset.py``.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, DailyRiskState
from backend.database.testing import make_test_engine
from backend.quant.risk.engine import PersistentLossTracker, RiskConfig

TODAY = date(2026, 10, 12)          # a Monday risk day
LONG_AGO = TODAY - timedelta(days=3)  # Friday: halted, then the worker was down


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("backend.database.models.trading_day", lambda: TODAY)


@pytest.fixture(autouse=True)
def _no_alert_thread(monkeypatch):
    monkeypatch.setattr(PersistentLossTracker, "_do_kill_switch_io",
                        lambda self, reason: None)


@pytest.fixture(autouse=True)
def _isolate_safe_mode():
    from backend.worker.recovery import SAFE_MODE
    saved = (SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause)
    yield
    SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause = saved


@pytest.fixture()
def factory():
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed(factory, day, **kw):
    sess = factory()
    try:
        row = sess.get(DailyRiskState, day) or DailyRiskState(trade_date=day)
        for k, v in kw.items():
            setattr(row, k, v)
        sess.add(row)
        sess.commit()
    finally:
        sess.close()


def _row(factory, day):
    sess = factory()
    try:
        return sess.get(DailyRiskState, day)
    finally:
        sess.close()


def _tracker(factory):
    return PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                 db_factory=factory)


class TestTheHelper:
    def test_it_adds_every_halted_day_to_today_and_yesterday(self, factory):
        from backend.database.models import risk_days_in_play
        _seed(factory, TODAY - timedelta(days=9), kill_switch=True)
        _seed(factory, LONG_AGO, kill_switch=True)
        _seed(factory, TODAY - timedelta(days=5), kill_switch=False)

        sess = factory()
        try:
            days = risk_days_in_play(sess)
        finally:
            sess.close()

        assert days == [TODAY, TODAY - timedelta(days=1), LONG_AGO,
                        TODAY - timedelta(days=9)]


class TestTheTrackerAfterALongOutage:
    def test_a_restart_comes_up_halted(self, factory):
        _seed(factory, LONG_AGO, kill_switch=True, kill_reason="일일 손실 한도 초과",
              peak_equity=1_000_000.0)

        t = _tracker(factory)

        assert t.kill_switch is True, "a worker down three risk days came up unhalted"
        assert t.kill_reason == "일일 손실 한도 초과"

    def test_its_first_write_carries_the_halt_onto_today(self, factory):
        """And does not adopt today's default False as an operator's clear."""
        _seed(factory, LONG_AGO, kill_switch=True, kill_reason="일일 손실 한도 초과",
              peak_equity=1_000_000.0)
        t = _tracker(factory)

        t.record_pnl(0.0, 1_000_000.0)

        assert _row(factory, TODAY).kill_switch is True
        assert t.kill_switch is True

    def test_the_newest_halts_reason_is_restored(self, factory):
        _seed(factory, TODAY - timedelta(days=8), kill_switch=True, kill_reason="오래됨")
        _seed(factory, LONG_AGO, kill_switch=True, kill_reason="최근")
        assert _tracker(factory).kill_reason == "최근"

    def test_an_old_cleared_row_does_not_halt(self, factory):
        _seed(factory, LONG_AGO, kill_switch=False)
        assert _tracker(factory).kill_switch is False

    def test_a_failed_lookup_still_reads_today_and_yesterday(
            self, factory, monkeypatch, caplog):
        """Never less safe than the two-day read it replaced."""
        import backend.database.models as models

        def _broken(sess, today=None):
            raise RuntimeError("db hiccup")

        monkeypatch.setattr(models, "risk_days_in_play", _broken)
        _seed(factory, TODAY - timedelta(days=1), kill_switch=True, kill_reason="어제")

        with caplog.at_level("ERROR", logger="backend.quant.risk.engine"):
            t = _tracker(factory)

        assert t.kill_switch is True
        assert "정지 날짜" in " ".join(r.getMessage() for r in caplog.records)


class TestTheDailyJob:
    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch, factory):
        monkeypatch.setattr("backend.worker.scheduler._get_db", lambda: factory())

        class _Redis:
            def delete(self, *a):
                pass

        monkeypatch.setattr("redis.from_url", lambda *a, **k: _Redis())

    def test_an_old_halt_blocks_the_re_arm_and_is_carried_to_today(self, factory):
        from backend.worker.recovery import SAFE_MODE
        from backend.worker.scheduler import _reset_daily_risk
        _seed(factory, LONG_AGO, kill_switch=True, kill_reason="일일 손실 한도 초과",
              peak_equity=1_000_000.0)
        SAFE_MODE.disable("일일 손실 한도 초과")

        _reset_daily_risk()

        assert SAFE_MODE.can_trade is False, "re-armed over a three-day-old halt"
        assert _row(factory, TODAY).kill_switch is True
        assert _row(factory, TODAY).kill_reason == "일일 손실 한도 초과"

    def test_with_no_halt_anywhere_it_still_re_arms(self, factory):
        from backend.worker.recovery import SAFE_MODE
        from backend.worker.scheduler import _reset_daily_risk
        _seed(factory, LONG_AGO, kill_switch=False)
        SAFE_MODE.disable("테스트")

        _reset_daily_risk()

        assert SAFE_MODE.can_trade is True


class TestTheFlaskReporters:
    @pytest.fixture()
    def client(self, factory, monkeypatch):
        from backend.api import server as srv
        monkeypatch.setattr(srv, "_get_factory", lambda: factory)
        monkeypatch.setattr(srv, "_API_KEY", "")
        srv.app.config.update(TESTING=True)
        return srv.app.test_client()

    def test_status_and_metrics_report_an_old_halt(self, factory, client):
        _seed(factory, LONG_AGO, kill_switch=True, kill_reason="MDD 한도 초과")

        status = client.get("/api/status").get_json()
        assert status["kill_switch"] is True
        assert status["kill_reason"] == "MDD 한도 초과"
        assert client.get("/api/metrics").get_json()["kill_switch"] is True


class TestTheHarnessKillSwitch:
    def test_it_starts_halted_and_its_resume_clears_the_old_row(self, factory):
        from datetime import datetime, timezone
        from backend.risk.kill_switch import KillSwitch, TradingState
        _seed(factory, LONG_AGO, kill_switch=True, kill_reason="MDD 한도 초과")

        ks = KillSwitch(db_factory=factory)
        assert ks.state is TradingState.HALTED

        outcome = ks.resume("operator:test",
                            _now=datetime.now(timezone.utc) + timedelta(days=1))
        assert outcome.approved, outcome.reason
        assert _row(factory, LONG_AGO).kill_switch is False
