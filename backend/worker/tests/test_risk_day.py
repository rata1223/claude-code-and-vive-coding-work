"""The daily-loss day is a 07:00 KST risk day; restarts keep the week and the peak.

Issue #166: the 3% daily-loss limit rolled over at Seoul midnight, which falls
inside the US session (22:30–05:00 KST, winter 23:30–06:00), so one overnight
session had two 3% budgets. The risk day now runs 07:00 → 07:00 KST — after the
US close and before the Korean open — so a session is always one day.

Two restart holes closed in the same change:

* the weekly limit's window started when the tracker was built, so every worker
  restart handed the week a fresh 6% budget — it is now the last 7 risk days,
  rebuilt from the stored rows;
* peak equity (the MDD baseline) was restored from today's row only, so a
  restart before the day's first write came up with 0 and the worker re-seeded
  it from the current balance — it is now the latest recorded peak.

The clock is frozen at real instants through ``models.datetime``, so these run
the actual ``trading_day()`` rather than a patched one.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.database.models as models
from backend.database.models import Base, DailyRiskState
from backend.database.testing import make_test_engine
from backend.quant.risk.engine import LossTracker, PersistentLossTracker, RiskConfig

_KST = timezone(timedelta(hours=9))
PEAK = 1_000_000.0


class _Clock:
    """A settable KST wall clock seen by ``trading_day()`` and ``seoul_date()``."""

    def __init__(self, monkeypatch):
        self.instant = datetime(2026, 9, 21, 12, 0, tzinfo=_KST)
        clock = self

        class _Frozen:
            @staticmethod
            def now(tz=None):
                return clock.instant.astimezone(tz) if tz else clock.instant

        monkeypatch.setattr(models, "datetime", _Frozen)

    def kst(self, y, m, d, hh, mm=0):
        self.instant = datetime(y, m, d, hh, mm, tzinfo=_KST)


@pytest.fixture()
def clock(monkeypatch):
    return _Clock(monkeypatch)


@pytest.fixture(autouse=True)
def _no_alert_thread(monkeypatch):
    """The kill-switch alert's I/O thread is not under test here."""
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


def _plain_tracker() -> LossTracker:
    t = LossTracker(config=RiskConfig())
    t.peak_equity = PEAK
    return t


def _persistent(factory) -> PersistentLossTracker:
    return PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                 db_factory=factory)


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


class TestTheOvernightSessionIsOneDay:
    """The filed bug: a session crossing Seoul midnight had two budgets."""

    def test_losses_either_side_of_seoul_midnight_share_one_budget(self, clock):
        """2% at 23:00 and 2% at 01:00 is 4% of one session — over the 3% limit.

        With the Seoul-midnight boundary the 01:00 fill opened a new day, each
        half stayed at 2%, and the session lost 4% without halting.
        """
        clock.kst(2026, 9, 21, 9, 30)
        t = _plain_tracker()

        clock.kst(2026, 9, 21, 23, 0)
        assert t.record_pnl(-20_000.0, PEAK - 20_000.0) == "unchanged"
        clock.kst(2026, 9, 22, 1, 0)
        result = t.record_pnl(-20_000.0, PEAK - 40_000.0)

        assert t.daily_pnl == -40_000.0, "the session was split across two days"
        assert result == "triggered"
        assert t.kill_switch is True
        assert "일일" in t.kill_reason

    def test_the_last_us_fill_before_07_00_is_still_the_same_day(self, clock):
        clock.kst(2026, 9, 21, 22, 40)
        t = _plain_tracker()
        t.record_pnl(-10_000.0, PEAK)

        clock.kst(2026, 9, 22, 5, 59)      # winter US close is 06:00
        t.record_pnl(-5_000.0, PEAK)

        assert t.trade_date == date(2026, 9, 21)
        assert t.daily_pnl == -15_000.0

    def test_the_korean_session_starts_a_fresh_day(self, clock):
        clock.kst(2026, 9, 22, 1, 0)
        t = _plain_tracker()
        t.record_pnl(-25_000.0, PEAK)

        clock.kst(2026, 9, 22, 9, 5)
        t.record_pnl(-1_000.0, PEAK)

        assert t.trade_date == date(2026, 9, 22)
        assert t.daily_pnl == -1_000.0
        assert t.weekly_pnl == -26_000.0, "the closed day stays in the week"

    def test_the_day_rolls_at_07_00_exactly(self, clock):
        clock.kst(2026, 9, 22, 6, 59)
        t = _plain_tracker()
        t.record_pnl(-1_000.0, PEAK)
        assert t.trade_date == date(2026, 9, 21)

        clock.kst(2026, 9, 22, 7, 0)
        t.record_pnl(-2_000.0, PEAK)
        assert t.trade_date == date(2026, 9, 22)
        assert t.daily_pnl == -2_000.0


class TestTheWeeklyWindow:
    """A rolling 7 risk days: today plus the six before it."""

    def _day(self, clock, t, d, pnl):
        clock.kst(2026, 9, d, 10, 0)
        t.record_pnl(pnl, PEAK)

    def test_it_accumulates_across_days(self, clock):
        clock.kst(2026, 9, 1, 10, 0)
        t = _plain_tracker()
        for d in range(1, 5):
            self._day(clock, t, d, -5_000.0)
        assert t.weekly_pnl == -20_000.0
        assert t.daily_pnl == -5_000.0

    def test_a_day_seven_risk_days_old_drops_out(self, clock):
        clock.kst(2026, 9, 1, 10, 0)
        t = _plain_tracker()
        self._day(clock, t, 1, -30_000.0)
        self._day(clock, t, 2, -1_000.0)
        self._day(clock, t, 7, -1_000.0)
        assert t.weekly_pnl == -32_000.0, "the 1st is six days back: still in"

        self._day(clock, t, 8, -1_000.0)
        assert t.weekly_pnl == -3_000.0, "the 1st is seven days back: out"

    def test_a_gap_of_a_week_empties_the_window(self, clock):
        clock.kst(2026, 9, 1, 10, 0)
        t = _plain_tracker()
        self._day(clock, t, 1, -30_000.0)
        self._day(clock, t, 2, -20_000.0)
        self._day(clock, t, 12, -1_000.0)
        assert t.weekly_pnl == -1_000.0

    def test_losses_spread_over_days_trip_the_weekly_limit(self, clock):
        """Each day under 3%, the week over 6%."""
        clock.kst(2026, 9, 1, 10, 0)
        t = _plain_tracker()
        for d in range(1, 4):
            self._day(clock, t, d, -20_000.0)
            assert t.kill_switch is False
        self._day(clock, t, 4, -5_000.0)
        assert t.kill_switch is True
        assert "주간" in t.kill_reason

    def test_a_manual_weekly_reset_clears_the_window_too(self, clock):
        clock.kst(2026, 9, 1, 10, 0)
        t = _plain_tracker()
        self._day(clock, t, 1, -30_000.0)
        self._day(clock, t, 2, -1_000.0)
        t.reset_weekly()
        self._day(clock, t, 3, -1_000.0)
        assert t.weekly_pnl == -1_000.0, "the reset came back at the next rollover"


class TestARestartKeepsTheWeek:
    def test_the_week_is_rebuilt_from_the_prior_rows(self, clock, factory):
        clock.kst(2026, 9, 10, 10, 0)
        for back in range(1, 7):
            _seed(factory, date(2026, 9, 10) - timedelta(days=back),
                  daily_pnl=-9_000.0, peak_equity=PEAK)
        _seed(factory, date(2026, 9, 3), daily_pnl=-500_000.0, peak_equity=PEAK)
        _seed(factory, date(2026, 9, 10), daily_pnl=-2_000.0, peak_equity=PEAK)

        t = _persistent(factory)

        assert t.weekly_pnl == -56_000.0, (
            "six prior days plus today; the 3rd is seven days back")

    def test_a_restarted_worker_still_halts_on_the_week(self, clock, factory):
        """The fresh-budget hole: 5.5% lost this week, restart, lose 1% more."""
        clock.kst(2026, 9, 10, 10, 0)
        for back in range(1, 6):
            _seed(factory, date(2026, 9, 10) - timedelta(days=back),
                  daily_pnl=-11_000.0, peak_equity=PEAK)

        t = _persistent(factory)
        assert t.kill_switch is False
        t.record_pnl(-10_000.0, PEAK - 65_000.0)

        assert t.kill_switch is True, (
            "a restart handed the week a fresh 6% budget")
        assert "주간" in t.kill_reason

    def test_the_rebuilt_week_rolls_over_like_a_live_one(self, clock, factory):
        clock.kst(2026, 9, 10, 10, 0)
        _seed(factory, date(2026, 9, 4), daily_pnl=-30_000.0, peak_equity=PEAK)
        _seed(factory, date(2026, 9, 9), daily_pnl=-1_000.0, peak_equity=PEAK)
        t = _persistent(factory)
        assert t.weekly_pnl == -31_000.0

        clock.kst(2026, 9, 11, 10, 0)
        t.record_pnl(-1_000.0, PEAK)

        assert t.weekly_pnl == -2_000.0, "the 4th left the window on the 11th"


class TestARestartKeepsThePeak:
    def test_the_latest_peak_carries_into_a_day_with_no_row(
            self, clock, factory):
        clock.kst(2026, 9, 10, 8, 0)
        _seed(factory, date(2026, 9, 7), peak_equity=1_200_000.0)
        _seed(factory, date(2026, 9, 8), peak_equity=1_300_000.0)

        t = _persistent(factory)

        assert t.peak_equity == 1_300_000.0

    def test_a_drawdown_across_the_restart_is_still_measured(
            self, clock, factory, monkeypatch):
        """Peak 1.3M yesterday, equity 1.0M today: a 23% drawdown, not 0%."""
        monkeypatch.setattr(PersistentLossTracker, "_request_mdd_flatten",
                            lambda self, reason: None)
        clock.kst(2026, 9, 10, 8, 0)
        _seed(factory, date(2026, 9, 9), peak_equity=1_300_000.0)

        t = _persistent(factory)
        t.record_pnl(0.0, 1_000_000.0)

        assert t.kill_switch is True
        assert t.kill_reason.startswith("MDD")

    def test_todays_peak_wins(self, clock, factory):
        clock.kst(2026, 9, 10, 10, 0)
        _seed(factory, date(2026, 9, 9), peak_equity=9_000_000.0)
        _seed(factory, date(2026, 9, 10), peak_equity=1_100_000.0)
        assert _persistent(factory).peak_equity == 1_100_000.0

    def test_rows_without_a_peak_are_skipped(self, clock, factory):
        clock.kst(2026, 9, 10, 10, 0)
        _seed(factory, date(2026, 9, 5), peak_equity=1_250_000.0)
        _seed(factory, date(2026, 9, 9), peak_equity=0.0, daily_pnl=-1.0)
        _seed(factory, date(2026, 9, 10), peak_equity=0.0)
        assert _persistent(factory).peak_equity == 1_250_000.0

    def test_no_rows_at_all_still_starts_at_zero(self, clock, factory):
        """Then the worker seeds the peak from the balance, as before."""
        clock.kst(2026, 9, 10, 10, 0)
        assert _persistent(factory).peak_equity == 0.0


class TestOrderKeysStayOnTheSeoulDate:
    """KIS order numbers restart at Seoul midnight; the risk day does not."""

    def test_an_order_after_seoul_midnight_is_keyed_by_the_calendar_date(
            self, clock, factory, monkeypatch):
        from contextlib import contextmanager
        from backend.database.models import Order as DBOrder
        from backend.worker import runner as runner_mod

        @contextmanager
        def _sess():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        class _Order:
            id = "0000117"
            symbol = "AAPL"
            side = "buy"
            qty = 1
            price = 100.0
            filled_qty = 0
            avg_fill_price = None

            class status:
                value = "submitted"

        monkeypatch.setattr(runner_mod, "_session", _sess)
        clock.kst(2026, 9, 22, 1, 30)      # risk day 21st, Seoul date 22nd
        runner_mod.StrategyWorker._persist_order(
            object.__new__(runner_mod.StrategyWorker), _Order())

        sess = factory()
        try:
            row = sess.query(DBOrder).one()
            assert row.idempotency_key.endswith(":2026-09-22")
            assert row.trade_date == date(2026, 9, 22)
        finally:
            sess.close()

    def test_entry_dates_are_the_calendar_date(self, clock):
        from backend.quant.risk.engine import TrailingStopManager
        clock.kst(2026, 9, 22, 1, 30)
        m = TrailingStopManager(RiskConfig())
        m.open("AAPL", 1, 100.0)
        assert m._positions["AAPL"].entry_date == "2026-09-22"
