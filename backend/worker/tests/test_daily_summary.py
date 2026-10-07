"""The daily summary reports the whole risk day that just closed.

It ran at 23:50 KST — the Korean session and the first ~80 minutes of the US
one — while a risk day runs 07:00 → 07:00 (#166). At 06:50, after the US close
and before the boundary, ``trading_day()`` is still the closing day and its row
holds the whole of it. The halt is now read on its own (it hid behind the peak
check) and from every risk day a halt can be on (#216).

No real broker: ``get_kis_broker`` is a fake; the notifier is captured.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, DailyRiskState, EquitySnapshot
from backend.database.testing import make_test_engine

DAY = date(2026, 10, 9)     # the risk day closing at 07:00 on the 10th


class _FakeBroker:
    def get_balance(self):
        return SimpleNamespace(total_eval_krw=2_000_000.0, cash_krw=500_000.0,
                               cash_usd=0.0)

    def get_positions(self):
        return [object(), object()]


@pytest.fixture()
def factory():
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def sent(monkeypatch, factory):
    monkeypatch.setattr("backend.database.models.trading_day", lambda: DAY)
    monkeypatch.setattr("backend.worker.scheduler._get_db", lambda: factory())
    monkeypatch.setattr("backend.brokers.kis.get_kis_broker", lambda: _FakeBroker())
    captured = []
    monkeypatch.setattr("bot.notifier.alert_daily_summary", captured.append)
    return captured


def _seed(factory, day, **kw):
    sess = factory()
    try:
        sess.add(DailyRiskState(trade_date=day, **kw))
        sess.commit()
    finally:
        sess.close()


def _run():
    from backend.worker.scheduler import _save_equity_snapshot
    _save_equity_snapshot()


def test_it_runs_at_06_50_kst_before_the_risk_day_turns():
    from backend.worker.scheduler import build_scheduler
    job = build_scheduler().get_job("equity_snapshot")
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("6", "50")
    assert str(job.trigger.timezone) == "Asia/Seoul"


def test_it_reports_the_closing_risk_day(factory, sent):
    _seed(factory, DAY, daily_pnl=-20_000.0, peak_equity=2_000_000.0)

    _run()

    (summary,) = sent
    assert summary["risk_day"] == "2026-10-09"
    assert summary["daily_pnl_pct"] == pytest.approx(-1.0)
    assert summary["total_equity"] == 2_000_000.0
    assert summary["position_count"] == 2
    assert summary["kill_switch"] is False
    sess = factory()
    try:
        assert sess.query(EquitySnapshot).count() == 1
    finally:
        sess.close()


def test_a_halt_on_a_row_with_no_peak_is_still_reported(factory, sent):
    """A row the 07:01 job carried forward has no peak; the halt hid behind it."""
    _seed(factory, DAY, kill_switch=True, kill_reason="일일 손실 한도 초과",
          peak_equity=0.0)

    _run()

    assert sent[0]["kill_switch"] is True
    assert sent[0]["kill_reason"] == "일일 손실 한도 초과"


def test_a_halt_on_an_older_row_is_reported(factory, sent):
    _seed(factory, DAY, daily_pnl=0.0, peak_equity=2_000_000.0)
    _seed(factory, DAY - timedelta(days=3), kill_switch=True,
          kill_reason="MDD 한도 초과", peak_equity=2_000_000.0)

    _run()

    assert sent[0]["kill_switch"] is True
    assert sent[0]["kill_reason"] == "MDD 한도 초과"


def test_the_days_own_halt_reason_is_shown_first(factory, sent):
    _seed(factory, DAY, kill_switch=True, kill_reason="오늘", peak_equity=2_000_000.0)
    _seed(factory, DAY + timedelta(days=1), kill_switch=True, kill_reason="옛 키")

    _run()

    assert sent[0]["kill_reason"] == "오늘"


def test_a_broker_failure_still_sends_the_halt(factory, monkeypatch, sent):
    """The halt line needs only the database; the morning notice must not
    depend on the balance call."""
    class _Down:
        def get_balance(self):
            raise RuntimeError("KIS 응답 없음")

        def get_positions(self):
            raise RuntimeError("KIS 응답 없음")

    monkeypatch.setattr("backend.brokers.kis.get_kis_broker", lambda: _Down())
    _seed(factory, DAY, kill_switch=True, kill_reason="MDD 한도 초과",
          daily_pnl=-10_000.0, peak_equity=2_000_000.0)

    _run()

    (summary,) = sent
    assert summary["kill_switch"] is True
    assert summary["total_equity"] is None
    assert summary["position_count"] is None
    assert summary["daily_pnl_pct"] == pytest.approx(-0.5)


def test_the_day_is_fixed_when_the_job_starts(factory, monkeypatch, sent):
    """A slow broker call that ends after 07:00 must not move the summary onto
    the new, empty risk day."""
    days = iter([DAY])
    monkeypatch.setattr("backend.database.models.trading_day",
                        lambda: next(days, DAY + timedelta(days=1)))
    _seed(factory, DAY, daily_pnl=-20_000.0, peak_equity=2_000_000.0)

    _run()

    assert sent[0]["risk_day"] == "2026-10-09"
    assert sent[0]["daily_pnl_pct"] == pytest.approx(-1.0)


def test_a_database_failure_reports_the_halt_as_unknown(factory, monkeypatch, sent):
    class _Broken:
        def get(self, *a, **k):
            raise RuntimeError("db down")

        def add(self, *a):
            raise RuntimeError("db down")

        query = get

        def close(self):
            pass

    monkeypatch.setattr("backend.worker.scheduler._get_db", lambda: _Broken())

    _run()

    assert sent[0]["kill_switch"] is None
    assert sent[0]["total_equity"] == 2_000_000.0


def test_a_late_start_is_allowed_only_until_06_59():
    from backend.worker.scheduler import build_scheduler
    job = build_scheduler().get_job("equity_snapshot")
    assert job.misfire_grace_time == 9 * 60
    assert job.coalesce is True


class TestTheMessage:
    def _msg(self, monkeypatch, summary):
        import bot.notifier as notifier
        out = []
        monkeypatch.setattr(notifier, "send_alert", out.append)
        notifier.alert_daily_summary(summary)
        return out[0]

    def test_it_names_the_risk_day(self, monkeypatch):
        msg = self._msg(monkeypatch, {"risk_day": "2026-10-09", "total_equity": 1.0})
        assert "리스크 데이: 2026-10-09 (07:00~07:00 KST)" in msg

    def test_unread_values_are_dashes_not_zero(self, monkeypatch):
        msg = self._msg(monkeypatch, {"total_equity": None, "daily_pnl_pct": None,
                                      "position_count": None, "kill_switch": None})
        assert "총 자산: —" in msg and "일 수익률: —" in msg and "포지션 수: —" in msg
        assert "0원" not in msg
        assert "킬스위치 상태 조회 실패" in msg

    def test_callers_without_a_risk_day_are_unchanged(self, monkeypatch):
        msg = self._msg(monkeypatch, {"total_equity": 1.0})
        assert "리스크 데이" not in msg
        assert msg.startswith("📊 <b>일일 결산</b>\n총 자산:")
