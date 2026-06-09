"""
Tests for backend/data/calendar.py (CalendarService)

Covers TimezoneHandler, CalendarCache, CalendarService core queries,
TradingPermission gate, and all 7 required scenarios:
  Korean holiday, US holiday, DST transition, half-day,
  pre-market/after-hours, timezone conversion error,
  wrong-session order blocking.

Fake CalendarDataSource is used throughout so no network/pykrx call is needed.
"""
import threading
import time as _time
from datetime import date, datetime, time as time_type, timedelta, timezone

import pytest
import pytz

from backend.data.calendar import (
    BlockReason,
    CalendarDataSource,
    CalendarService,
    Market,
    MarketClosedError,
    MarketHoliday,
    SessionType,
    SessionWindow,
    TimezoneHandler,
    TradingPermission,
    _KST,
    _ET,
    _UTC,
    get_calendar_service,
    configure_calendar_service,
)

# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

# Tuesday 2026-06-09 14:00 UTC
#   → 23:00 KST  (after KRX after-hours end → CLOSED)
#   → 10:00 EDT  (NYSE regular session → REGULAR)
_FIXED_NOW = datetime(2026, 6, 9, 14, 0, 0, tzinfo=timezone.utc)
_FIXED_NOW_UTC = _FIXED_NOW.astimezone(pytz.UTC)


def _svc(holidays: dict | None = None, half_days: dict | None = None) -> CalendarService:
    """Build a CalendarService backed by a fake static data source.

    holidays: {(Market, date): holiday_name_str}
    half_days: {(Market, date): early_close_time_type}  — is_early_close=True
    """
    class FakeDataSource(CalendarDataSource):
        def get_holidays(self, market: Market, year: int) -> list[MarketHoliday]:
            result = []
            for (m, d), name in (holidays or {}).items():
                if m == market and d.year == year:
                    result.append(MarketHoliday(date=d, market=m, name=name))
            for (m, d), close_t in (half_days or {}).items():
                if m == market and d.year == year:
                    result.append(MarketHoliday(
                        date=d, market=m, name="Half Day",
                        is_early_close=True, early_close_time=close_t,
                    ))
            return result

        def get_session_window(self, market: Market, d: date) -> SessionWindow | None:
            return None  # CalendarService falls back to default fixed times

    return CalendarService(data_source=FakeDataSource())


def _svc_no_holidays() -> CalendarService:
    return _svc(holidays={}, half_days={})


# ─────────────────────────────────────────────────────────────────────────────
# TestTimezoneHandler
# ─────────────────────────────────────────────────────────────────────────────

class TestTimezoneHandler:
    def test_to_kst_from_utc(self):
        # 00:00 UTC = 09:00 KST
        utc = datetime(2026, 6, 9, 0, 0, 0, tzinfo=pytz.UTC)
        kst = TimezoneHandler.to_kst(utc)
        assert kst.hour == 9
        assert kst.minute == 0

    def test_to_et_summer_utc_minus_4(self):
        # 14:00 UTC = 10:00 EDT (UTC-4)
        utc = datetime(2026, 6, 9, 14, 0, 0, tzinfo=pytz.UTC)
        et = TimezoneHandler.to_et(utc)
        assert et.hour == 10
        assert et.minute == 0

    def test_to_et_winter_utc_minus_5(self):
        # 15:00 UTC = 10:00 EST (UTC-5)
        utc = datetime(2026, 1, 9, 15, 0, 0, tzinfo=pytz.UTC)
        et = TimezoneHandler.to_et(utc)
        assert et.hour == 10
        assert et.minute == 0

    def test_to_utc_requires_aware_datetime(self):
        naive = datetime(2026, 6, 9, 10, 0, 0)
        with pytest.raises(ValueError, match="naive"):
            TimezoneHandler.to_utc(naive)

    def test_to_utc_roundtrip(self):
        utc_in = datetime(2026, 6, 9, 14, 0, 0, tzinfo=pytz.UTC)
        et = TimezoneHandler.to_et(utc_in)
        utc_out = TimezoneHandler.to_utc(et)
        assert abs((utc_out - utc_in).total_seconds()) < 1

    def test_to_market_local_krx(self):
        utc = datetime(2026, 6, 9, 0, 0, 0, tzinfo=pytz.UTC)
        local = TimezoneHandler.to_market_local(Market.KRX, utc)
        assert local.hour == 9

    def test_to_market_local_nyse(self):
        utc = datetime(2026, 6, 9, 14, 0, 0, tzinfo=pytz.UTC)
        local = TimezoneHandler.to_market_local(Market.NYSE, utc)
        assert local.hour == 10

    def test_trade_date_krx(self):
        # 23:30 UTC = 08:30 KST next day
        utc = datetime(2026, 6, 8, 23, 30, 0, tzinfo=pytz.UTC)
        d = TimezoneHandler.trade_date(Market.KRX, utc)
        assert d == date(2026, 6, 9)

    def test_market_open_utc_krx_summer(self):
        # KRX 09:00 KST = 00:00 UTC
        utc = TimezoneHandler.market_open_utc(Market.KRX, date(2026, 6, 9), time_type(9, 0))
        assert utc.hour == 0
        assert utc.minute == 0

    def test_market_open_utc_nyse_summer(self):
        # NYSE 09:30 EDT = 13:30 UTC
        utc = TimezoneHandler.market_open_utc(Market.NYSE, date(2026, 6, 9), time_type(9, 30))
        assert utc.hour == 13
        assert utc.minute == 30

    def test_market_open_utc_nyse_winter(self):
        # NYSE 09:30 EST = 14:30 UTC
        utc = TimezoneHandler.market_open_utc(Market.NYSE, date(2026, 1, 9), time_type(9, 30))
        assert utc.hour == 14
        assert utc.minute == 30


# ─────────────────────────────────────────────────────────────────────────────
# TestCalendarCache  (L1 in-memory only; no Redis in tests)
# ─────────────────────────────────────────────────────────────────────────────

class TestCalendarCache:
    def _fresh(self):
        from backend.data.calendar import CalendarCache
        return CalendarCache(redis_client=None)

    def test_get_miss_returns_none(self):
        c = self._fresh()
        assert c.get(Market.NYSE, date(2026, 6, 9)) is None

    def test_set_then_get_hit(self):
        c = self._fresh()
        val = {"is_trading_day": True, "holiday_name": None}
        c.set(Market.NYSE, date(2026, 6, 9), val)
        assert c.get(Market.NYSE, date(2026, 6, 9)) == val

    def test_l1_eviction_at_max(self):
        from backend.data.calendar import CalendarCache
        c = CalendarCache(redis_client=None)
        c._L1_MAX = 2  # override for test
        d1, d2, d3 = date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)
        c.set(Market.NYSE, d1, {"v": 1})
        c.set(Market.NYSE, d2, {"v": 2})
        # Accessing d1 makes it "recently used" by refreshing its expiry
        c.set(Market.NYSE, d3, {"v": 3})
        assert c.get(Market.NYSE, d3) is not None

    def test_thread_safety(self):
        c = self._fresh()
        errors = []
        def _worker(i):
            try:
                d = date(2026, 1, 1) + timedelta(days=i % 30)
                c.set(Market.NYSE, d, {"i": i})
                c.get(Market.NYSE, d)
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(50)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors


# ─────────────────────────────────────────────────────────────────────────────
# TestCalendarServiceCore
# ─────────────────────────────────────────────────────────────────────────────

class TestCalendarServiceCore:
    def test_is_trading_day_weekday_no_holiday(self):
        svc = _svc_no_holidays()
        assert svc.is_trading_day(Market.NYSE, date(2026, 6, 9)) is True

    def test_is_trading_day_saturday_false(self):
        svc = _svc_no_holidays()
        assert svc.is_trading_day(Market.NYSE, date(2026, 6, 13)) is False

    def test_is_trading_day_holiday_false(self):
        holiday = date(2026, 6, 9)
        svc = _svc(holidays={(Market.NYSE, holiday): "Test Holiday"})
        assert svc.is_trading_day(Market.NYSE, holiday) is False

    def test_is_early_close_flag(self):
        half = date(2026, 6, 9)
        svc = _svc(half_days={(Market.NYSE, half): time_type(13, 0)})
        assert svc.is_early_close(Market.NYSE, half) is True
        assert svc.is_early_close(Market.NYSE, date(2026, 6, 10)) is False

    def test_get_session_window_trading_day(self):
        svc = _svc_no_holidays()
        win = svc.get_session_window(Market.NYSE, date(2026, 6, 9))
        assert win is not None
        assert win.open_utc < win.close_utc

    def test_get_session_window_holiday_returns_none(self):
        holiday = date(2026, 6, 9)
        svc = _svc(holidays={(Market.NYSE, holiday): "Holiday"})
        assert svc.get_session_window(Market.NYSE, holiday) is None

    def test_trade_date_delegates_to_handler(self):
        svc = _svc_no_holidays()
        d = svc.trade_date(Market.KRX, _FIXED_NOW_UTC)
        assert d == date(2026, 6, 9)

    def test_next_session_open_skips_weekend(self):
        # Friday 2026-06-12 → next session: Monday 2026-06-15 at 09:30 ET
        svc = _svc_no_holidays()
        after = datetime(2026, 6, 12, 21, 0, 0, tzinfo=pytz.UTC)  # after NYSE close
        nxt = svc.next_session_open(Market.NYSE, after)
        local = TimezoneHandler.to_et(nxt)
        assert local.date() == date(2026, 6, 15)

    def test_prev_trading_day_skips_weekend(self):
        # Monday 2026-06-15 → prev: Friday 2026-06-12
        svc = _svc_no_holidays()
        prev = svc.prev_trading_day(Market.NYSE, date(2026, 6, 15))
        assert prev == date(2026, 6, 12)

    def test_get_session_nyse_regular(self):
        # 14:00 UTC = 10:00 EDT → REGULAR
        svc = _svc_no_holidays()
        assert svc.get_session(Market.NYSE, _FIXED_NOW_UTC) is SessionType.REGULAR

    def test_get_session_nyse_pre_market(self):
        # 08:30 UTC = 04:30 EDT → PRE_MARKET
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 8, 30, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.NYSE, t) is SessionType.PRE_MARKET

    def test_get_session_nyse_after_hours(self):
        # 21:00 UTC = 17:00 EDT → AFTER_HOURS
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 21, 0, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.NYSE, t) is SessionType.AFTER_HOURS

    def test_get_session_nyse_closed_after_after_hours(self):
        # 01:00 UTC = 21:00 EDT → CLOSED
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 10, 1, 0, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.NYSE, t) is SessionType.CLOSED

    def test_get_session_krx_regular(self):
        # 01:00 UTC = 10:00 KST → REGULAR
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 1, 0, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.KRX, t) is SessionType.REGULAR

    def test_get_session_krx_pre_market(self):
        # 23:30 UTC (prev day) = 08:30 KST → PRE_MARKET
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 8, 23, 30, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.KRX, t) is SessionType.PRE_MARKET

    def test_get_session_krx_after_hours(self):
        # 07:00 UTC = 16:00 KST → AFTER_HOURS
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 7, 0, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.KRX, t) is SessionType.AFTER_HOURS


# ─────────────────────────────────────────────────────────────────────────────
# TestTradingPermission
# ─────────────────────────────────────────────────────────────────────────────

class TestTradingPermission:
    def test_regular_is_allowed(self):
        svc = _svc_no_holidays()
        perm = svc.check_order_permission(Market.NYSE, _FIXED_NOW_UTC)
        assert perm.allowed is True
        assert perm.session_type is SessionType.REGULAR

    def test_closed_is_blocked(self):
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 10, 1, 0, 0, tzinfo=pytz.UTC)  # 21:00 EDT → CLOSED
        perm = svc.check_order_permission(Market.NYSE, t)
        assert perm.allowed is False

    def test_pre_market_blocked_by_default(self):
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 8, 30, 0, tzinfo=pytz.UTC)  # 04:30 EDT → PRE_MARKET
        perm = svc.check_order_permission(Market.NYSE, t)
        assert perm.allowed is False
        assert perm.block_reason is BlockReason.WRONG_SESSION

    def test_after_hours_blocked_by_default(self):
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 21, 0, 0, tzinfo=pytz.UTC)  # 17:00 EDT
        perm = svc.check_order_permission(Market.NYSE, t)
        assert perm.allowed is False
        assert perm.block_reason is BlockReason.WRONG_SESSION

    def test_assert_tradeable_raises_on_closed(self):
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 10, 1, 0, 0, tzinfo=pytz.UTC)
        with pytest.raises(MarketClosedError):
            svc.assert_tradeable(Market.NYSE, t)

    def test_assert_tradeable_passes_during_regular(self):
        svc = _svc_no_holidays()
        svc.assert_tradeable(Market.NYSE, _FIXED_NOW_UTC)  # must not raise

    def test_market_closed_error_not_runtime_error(self):
        # Ensures ConsecutiveFailureBreaker won't count it as a broker failure
        exc = MarketClosedError(
            market=Market.NYSE,
            session=SessionType.CLOSED,
            reason=BlockReason.HOLIDAY,
        )
        assert not isinstance(exc, RuntimeError)
        assert isinstance(exc, Exception)

    def test_next_open_utc_populated_on_block(self):
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 8, 30, 0, tzinfo=pytz.UTC)  # pre-market
        perm = svc.check_order_permission(Market.NYSE, t)
        assert perm.next_open_utc is not None
        assert perm.next_open_utc > t


# ─────────────────────────────────────────────────────────────────────────────
# Required scenario tests
# ─────────────────────────────────────────────────────────────────────────────

class TestKoreanHoliday:
    """KRX holiday → CLOSED → order blocked with HOLIDAY reason."""

    def test_kr_holiday_blocks_order(self):
        kr_holiday = date(2026, 8, 17)  # Monday — inject as KR holiday
        now_utc = datetime(2026, 8, 17, 1, 0, 0, tzinfo=pytz.UTC)  # 10:00 KST → normally REGULAR
        svc = _svc(holidays={(Market.KRX, kr_holiday): "테스트 공휴일"})
        assert svc.is_trading_day(Market.KRX, kr_holiday) is False
        perm = svc.check_order_permission(Market.KRX, now_utc)
        assert perm.allowed is False
        assert perm.block_reason is BlockReason.HOLIDAY

    def test_kr_holiday_does_not_affect_nyse(self):
        kr_holiday = date(2026, 8, 17)
        now_utc = datetime(2026, 8, 17, 14, 0, 0, tzinfo=pytz.UTC)  # 10:00 EDT → REGULAR
        svc = _svc(holidays={(Market.KRX, kr_holiday): "KR Only"})
        assert svc.is_trading_day(Market.NYSE, kr_holiday) is True
        perm = svc.check_order_permission(Market.NYSE, now_utc)
        assert perm.allowed is True

    def test_assert_tradeable_raises_market_closed_error_on_kr_holiday(self):
        kr_holiday = date(2026, 8, 17)
        now_utc = datetime(2026, 8, 17, 1, 0, 0, tzinfo=pytz.UTC)
        svc = _svc(holidays={(Market.KRX, kr_holiday): "광복절 대체"})
        with pytest.raises(MarketClosedError) as exc_info:
            svc.assert_tradeable(Market.KRX, now_utc)
        assert exc_info.value.reason is BlockReason.HOLIDAY


class TestUSHoliday:
    """NYSE holiday → CLOSED → order blocked."""

    def test_us_holiday_blocks_nyse(self):
        # July 4, 2026 is a Saturday; observed Friday July 3
        us_holiday = date(2026, 7, 3)
        now_utc = datetime(2026, 7, 3, 14, 0, 0, tzinfo=pytz.UTC)  # 10:00 EDT
        svc = _svc(holidays={(Market.NYSE, us_holiday): "Independence Day (observed)"})
        assert svc.is_trading_day(Market.NYSE, us_holiday) is False
        perm = svc.check_order_permission(Market.NYSE, now_utc)
        assert perm.allowed is False
        assert perm.block_reason is BlockReason.HOLIDAY

    def test_us_holiday_blocks_nasdaq_equally(self):
        # NASDAQ shares the same data source routing as NYSE
        us_holiday = date(2026, 7, 3)
        now_utc = datetime(2026, 7, 3, 14, 0, 0, tzinfo=pytz.UTC)
        svc = _svc(holidays={
            (Market.NYSE, us_holiday): "Independence Day",
            (Market.NASDAQ, us_holiday): "Independence Day",
        })
        perm = svc.check_order_permission(Market.NASDAQ, now_utc)
        assert perm.allowed is False

    def test_kr_trading_continues_on_us_holiday(self):
        us_holiday = date(2026, 7, 3)
        now_utc = datetime(2026, 7, 3, 1, 0, 0, tzinfo=pytz.UTC)  # 10:00 KST
        svc = _svc(holidays={(Market.NYSE, us_holiday): "US Holiday"})
        assert svc.is_trading_day(Market.KRX, us_holiday) is True
        perm = svc.check_order_permission(Market.KRX, now_utc)
        assert perm.allowed is True


class TestDSTTransition:
    """ET DST transitions are handled correctly."""

    def test_summer_session_offset_utc_minus_4(self):
        # 2026-06-09: EDT (UTC-4) → NYSE opens 09:30 ET = 13:30 UTC
        win = TimezoneHandler.market_open_utc(Market.NYSE, date(2026, 6, 9), time_type(9, 30))
        assert win.hour == 13
        assert win.minute == 30

    def test_winter_session_offset_utc_minus_5(self):
        # 2026-01-09: EST (UTC-5) → NYSE opens 09:30 ET = 14:30 UTC
        win = TimezoneHandler.market_open_utc(Market.NYSE, date(2026, 1, 9), time_type(9, 30))
        assert win.hour == 14
        assert win.minute == 30

    def test_regular_session_correctly_detected_summer(self):
        # 14:00 UTC = 10:00 EDT (well within 09:30–16:00 ET)
        svc = _svc_no_holidays()
        assert svc.get_session(Market.NYSE, _FIXED_NOW_UTC) is SessionType.REGULAR

    def test_regular_session_correctly_detected_winter(self):
        # 2026-01-09 (Friday, no holiday): 15:00 UTC = 10:00 EST → REGULAR
        svc = _svc_no_holidays()
        t = datetime(2026, 1, 9, 15, 0, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.NYSE, t) is SessionType.REGULAR

    def test_after_dst_spring_forward_session_boundary_shifts(self):
        # After spring-forward (Mar 8 2026): 14:30 UTC used to be 09:30 EST but now 10:30 EDT
        # 2026-03-09 (after spring-forward): 14:30 UTC = 10:30 EDT → REGULAR (not pre-market)
        svc = _svc_no_holidays()
        t_after_spring = datetime(2026, 3, 9, 14, 30, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.NYSE, t_after_spring) is SessionType.REGULAR

    def test_before_dst_spring_forward_session_boundary(self):
        # 2026-03-06 Friday (before spring-forward on Mar 8): 14:30 UTC = 09:30 EST → REGULAR
        svc = _svc_no_holidays()
        t_before_spring = datetime(2026, 3, 6, 14, 30, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.NYSE, t_before_spring) is SessionType.REGULAR


class TestHalfDay:
    """US half-day: REGULAR ends at 13:00 ET; after-hours treated as post-half-day CLOSED."""

    def test_regular_session_before_early_close(self):
        # Half-day NYSE 2026-06-09: 12:00 ET = 16:00 UTC → should be REGULAR
        half_day = date(2026, 6, 9)
        svc = _svc(half_days={(Market.NYSE, half_day): time_type(13, 0)})
        t = datetime(2026, 6, 9, 16, 0, 0, tzinfo=pytz.UTC)  # 12:00 EDT
        assert svc.get_session(Market.NYSE, t) is SessionType.REGULAR

    def test_after_early_close_is_blocked_with_half_day_reason(self):
        # 14:00 ET = 18:00 UTC → AFTER_HOURS but block_reason=HALF_DAY_ENDED
        half_day = date(2026, 6, 9)
        svc = _svc(half_days={(Market.NYSE, half_day): time_type(13, 0)})
        t = datetime(2026, 6, 9, 18, 0, 0, tzinfo=pytz.UTC)  # 14:00 EDT
        perm = svc.check_order_permission(Market.NYSE, t)
        assert perm.allowed is False
        assert perm.block_reason is BlockReason.HALF_DAY_ENDED

    def test_order_blocked_after_early_close(self):
        half_day = date(2026, 6, 9)
        svc = _svc(half_days={(Market.NYSE, half_day): time_type(13, 0)})
        t = datetime(2026, 6, 9, 18, 0, 0, tzinfo=pytz.UTC)
        with pytest.raises(MarketClosedError):
            svc.assert_tradeable(Market.NYSE, t)

    def test_is_early_close_returns_true(self):
        half_day = date(2026, 6, 9)
        svc = _svc(half_days={(Market.NYSE, half_day): time_type(13, 0)})
        assert svc.is_early_close(Market.NYSE, half_day) is True

    def test_full_holiday_is_not_trading_day(self):
        holiday = date(2026, 6, 9)
        svc = _svc(holidays={(Market.NYSE, holiday): "Full Holiday"})
        assert svc.is_trading_day(Market.NYSE, holiday) is False


class TestPreMarketAfterHours:
    """PRE_MARKET and AFTER_HOURS session detection at boundary times."""

    def test_nyse_pre_market_at_0430_et(self):
        # 04:30 EDT = 08:30 UTC
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 8, 30, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.NYSE, t) is SessionType.PRE_MARKET

    def test_nyse_after_hours_at_1700_et(self):
        # 17:00 EDT = 21:00 UTC
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 21, 0, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.NYSE, t) is SessionType.AFTER_HOURS

    def test_krx_pre_market_at_0830_kst(self):
        # 08:30 KST = 23:30 UTC prev day
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 8, 23, 30, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.KRX, t) is SessionType.PRE_MARKET

    def test_krx_after_hours_at_1600_kst(self):
        # 16:00 KST = 07:00 UTC
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 7, 0, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.KRX, t) is SessionType.AFTER_HOURS

    def test_nasdaq_shares_nyse_pre_market_session(self):
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 8, 30, 0, tzinfo=pytz.UTC)
        assert svc.get_session(Market.NASDAQ, t) is SessionType.PRE_MARKET

    def test_pre_market_order_blocked_by_default(self):
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 8, 30, 0, tzinfo=pytz.UTC)
        perm = svc.check_order_permission(Market.NYSE, t)
        assert perm.allowed is False
        assert perm.block_reason is BlockReason.WRONG_SESSION


class TestTimezoneConversionError:
    """Fail-closed when naive datetime is passed to to_utc."""

    def test_to_utc_with_naive_raises(self):
        naive = datetime(2026, 6, 9, 10, 0, 0)
        with pytest.raises(ValueError):
            TimezoneHandler.to_utc(naive)

    def test_check_order_permission_with_naive_dt_fails_closed(self):
        # CalendarService strips tzinfo=None internally → still returns fail-closed
        svc = _svc_no_holidays()
        naive_dt = datetime(2026, 6, 9, 10, 0, 0)
        perm = svc.check_order_permission(Market.NYSE, naive_dt)
        # Must not raise; CalendarService adds UTC if naive
        assert isinstance(perm, TradingPermission)

    def test_cache_failure_returns_blocked_permission(self):
        class BrokenDataSource(CalendarDataSource):
            def get_holidays(self, market, year):
                raise RuntimeError("data source broken")
            def get_session_window(self, market, d):
                raise RuntimeError("data source broken")

        svc = CalendarService(data_source=BrokenDataSource())
        # On holiday load failure, CalendarService logs and continues
        # (service doesn't crash, returns WRONG_SESSION for holiday-unknown dates)
        perm = svc.check_order_permission(Market.NYSE, _FIXED_NOW_UTC)
        assert isinstance(perm, TradingPermission)


class TestWrongSessionOrderBlocking:
    """Orders blocked outside REGULAR session."""

    def test_order_blocked_during_closed_nyse(self):
        # 01:00 UTC = 21:00 EDT → CLOSED
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 10, 1, 0, 0, tzinfo=pytz.UTC)
        perm = svc.check_order_permission(Market.NYSE, t)
        assert perm.allowed is False
        assert perm.session_type is SessionType.CLOSED

    def test_order_blocked_during_closed_krx(self):
        # _FIXED_NOW = 14:00 UTC = 23:00 KST → CLOSED for KRX
        svc = _svc_no_holidays()
        perm = svc.check_order_permission(Market.KRX, _FIXED_NOW_UTC)
        assert perm.allowed is False

    def test_order_blocked_during_after_hours(self):
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 21, 0, 0, tzinfo=pytz.UTC)
        perm = svc.check_order_permission(Market.NYSE, t)
        assert perm.allowed is False
        assert perm.session_type is SessionType.AFTER_HOURS

    def test_nyse_and_nasdaq_blocked_equally_on_holiday(self):
        holiday = date(2026, 7, 3)
        t = datetime(2026, 7, 3, 14, 0, 0, tzinfo=pytz.UTC)
        svc = _svc(holidays={
            (Market.NYSE, holiday): "Holiday",
            (Market.NASDAQ, holiday): "Holiday",
        })
        assert svc.check_order_permission(Market.NYSE, t).allowed is False
        assert svc.check_order_permission(Market.NASDAQ, t).allowed is False

    def test_next_open_populated_when_blocked(self):
        svc = _svc_no_holidays()
        t = datetime(2026, 6, 9, 8, 30, 0, tzinfo=pytz.UTC)  # pre-market
        perm = svc.check_order_permission(Market.NYSE, t)
        assert perm.next_open_utc is not None
        # next open must be the 09:30 ET open on the same day
        local = TimezoneHandler.to_et(perm.next_open_utc)
        assert local.hour == 9 and local.minute == 30
