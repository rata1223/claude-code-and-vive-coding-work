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


# ── code-review findings ─────────────────────────────────────────────────────

class TestAWriteBeforeTheDaysFirstFill:
    """Between 07:00 and the first fill the tracker still holds the closed day.

    A write in that gap — a reset, the shutdown checkpoint — put the closed
    day's P&L on the new day's row; a restart then read it as today's loss and
    counted it in the week twice (once from its own row, once from today's).
    """

    def _closed_day(self, clock, factory):
        clock.kst(2026, 9, 10, 10, 0)
        t = _persistent(factory)
        t.record_pnl(-25_000.0, PEAK)
        clock.kst(2026, 9, 11, 8, 0)
        return t

    def _restarted(self, factory):
        t = _persistent(factory)
        return t.daily_pnl, t.weekly_pnl

    def test_a_reset_rolls_the_tracker_before_writing(self, clock, factory):
        t = self._closed_day(clock, factory)
        t.manual_reset()

        sess = factory()
        try:
            assert sess.get(DailyRiskState, date(2026, 9, 11)).daily_pnl == 0.0
            assert sess.get(DailyRiskState, date(2026, 9, 10)).daily_pnl == -25_000.0
        finally:
            sess.close()
        assert self._restarted(factory) == (0.0, -25_000.0)

    def test_the_shutdown_checkpoint_rolls_the_tracker_before_writing(
            self, clock, factory, monkeypatch):
        from contextlib import contextmanager
        from backend.worker import runner as runner_mod

        t = self._closed_day(clock, factory)

        @contextmanager
        def _sess():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        monkeypatch.setattr(runner_mod, "_session", _sess)
        w = object.__new__(runner_mod.StrategyWorker)
        w._loss_tracker = t
        w._checkpoint_equity()

        assert self._restarted(factory) == (0.0, -25_000.0), (
            "the checkpoint wrote yesterday's loss onto today's row")

    def test_redis_gets_the_number_under_its_own_day(self, clock, factory):
        written = {}

        class _Redis:
            def get(self, key):
                return written.get(key)

            def setex(self, key, ttl, value):
                written[key] = value

        clock.kst(2026, 9, 10, 10, 0)
        t = PersistentLossTracker(config=RiskConfig(), redis_client=_Redis(),
                                  db_factory=factory)
        t.record_pnl(-25_000.0, PEAK)
        clock.kst(2026, 9, 11, 8, 0)
        t.manual_reset()

        assert written["risk:daily_pnl:2026-09-10"] == "-25000.0"
        assert written.get("risk:daily_pnl:2026-09-11") in (None, "0.0"), (
            "the closed day's loss went under the new day's key")


class TestTheBootStraddlingSevenOClock:
    def test_a_boot_across_07_00_does_not_drop_the_closing_day(
            self, factory, monkeypatch):
        """The dataclass default reads the clock before `_restore_state` does.

        Booting across 07:00 left `trade_date` on the closed day while the
        restore had already put that day among the prior days; the first fill
        then re-pushed it with a zero share and the week lost its loss.
        """
        instants = iter([datetime(2026, 9, 11, 6, 59, 59, tzinfo=_KST)])
        later = datetime(2026, 9, 11, 7, 0, 1, tzinfo=_KST)

        class _Ticking:
            @staticmethod
            def now(tz=None):
                instant = next(instants, later)
                return instant.astimezone(tz) if tz else instant

        monkeypatch.setattr(models, "datetime", _Ticking)
        _seed(factory, date(2026, 9, 10), daily_pnl=-20_000.0, peak_equity=PEAK)

        t = _persistent(factory)
        t.record_pnl(-1_000.0, PEAK)

        assert t.weekly_pnl == -21_000.0


class TestThePeakFallbackIsBounded:
    def test_a_peak_older_than_the_week_is_not_trusted(self, clock, factory):
        """A worker idle for weeks, or a peak from before a capital change,
        re-seeds from the balance as it always did."""
        clock.kst(2026, 9, 30, 10, 0)
        _seed(factory, date(2026, 9, 1), peak_equity=100_000_000.0)
        assert _persistent(factory).peak_equity == 0.0

    def test_the_oldest_day_in_the_week_still_counts(self, clock, factory):
        clock.kst(2026, 9, 10, 10, 0)
        _seed(factory, date(2026, 9, 4), peak_equity=1_200_000.0)
        assert _persistent(factory).peak_equity == 1_200_000.0


class TestAFailedBootReadIsLogged:
    def test_a_failed_restore_query_is_an_error_not_silence(
            self, clock, caplog):
        class _Broken:
            def get(self, *a, **k):
                raise RuntimeError("db down")

            query = get

            def close(self):
                pass

        clock.kst(2026, 9, 10, 10, 0)
        with caplog.at_level("ERROR", logger="backend.quant.risk.engine"):
            t = PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                      db_factory=_Broken)

        assert t.weekly_pnl == 0.0 and t.peak_equity == 0.0
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "주간 창" in messages and "고점" in messages


class TestTheDailyJobCarriesAHaltForward:
    """A halt has to outlive two quiet risk days (no fills, no shutdown)."""

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch, factory):
        monkeypatch.setattr("backend.worker.scheduler._get_db", lambda: factory())

        class _Redis:
            def delete(self, *a):
                pass

        monkeypatch.setattr("redis.from_url", lambda *a, **k: _Redis())

    def _run_job(self, clock, d):
        clock.kst(2026, 9, d, 7, 1)
        from backend.worker.scheduler import _reset_daily_risk
        _reset_daily_risk()

    def test_a_weekend_halt_still_blocks_on_monday(self, clock, factory):
        """Halt on Friday's risk day (fired 03:00 Saturday KST)."""
        from backend.worker.recovery import SAFE_MODE
        _seed(factory, date(2026, 9, 25), kill_switch=True,
              kill_reason="일일 손실 한도 초과", peak_equity=PEAK)
        SAFE_MODE.disable("일일 손실 한도 초과")

        for d in (26, 27, 28):                     # Sat, Sun, Mon 07:01
            self._run_job(clock, d)
            assert SAFE_MODE.can_trade is False, f"re-armed on the {d}th"

        sess = factory()
        try:
            row = sess.get(DailyRiskState, date(2026, 9, 28))
            assert row.kill_switch is True
            assert row.kill_reason == "일일 손실 한도 초과"
            assert row.peak_equity == PEAK
        finally:
            sess.close()
        assert _persistent(factory).kill_switch is True, (
            "a Monday restart came up unhalted")

    def test_an_existing_row_is_not_overwritten(self, clock, factory):
        """Somebody's decision is on it — an operator's clear included."""
        _seed(factory, date(2026, 9, 25), kill_switch=True, peak_equity=PEAK)
        _seed(factory, date(2026, 9, 26), kill_switch=False, daily_pnl=-5.0)

        self._run_job(clock, 26)

        sess = factory()
        try:
            row = sess.get(DailyRiskState, date(2026, 9, 26))
            assert row.kill_switch is False
            assert row.daily_pnl == -5.0
        finally:
            sess.close()

    def test_no_halt_no_row(self, clock, factory):
        from backend.worker.recovery import SAFE_MODE
        SAFE_MODE.disable("테스트")
        self._run_job(clock, 26)
        assert SAFE_MODE.can_trade is True
        sess = factory()
        try:
            assert sess.get(DailyRiskState, date(2026, 9, 26)) is None
        finally:
            sess.close()

    def test_the_job_runs_just_after_the_risk_day_turns(self):
        from backend.worker.scheduler import build_scheduler
        job = build_scheduler().get_job("risk_reset")
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert (fields["hour"], fields["minute"]) == ("7", "1")
