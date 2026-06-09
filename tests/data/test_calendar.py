"""Tests for backend/data/calendar.py — market calendar service."""

from datetime import date, datetime, time as time_type
import pytz
import pytest

_UTC = pytz.UTC
_ET = pytz.timezone("America/New_York")
_KST = pytz.timezone("Asia/Seoul")


# ── Mock data source (duck-typed, no network calls) ──────────────────

class _MockSource:
    """Configurable CalendarDataSource mock — no network calls."""

    def __init__(self, holidays=None, windows=None):
        self._holidays = holidays or {}   # {(market, year): [MarketHoliday]}
        self._windows = windows or {}     # {(market, date): SessionWindow}

    def get_holidays(self, market, year):
        return self._holidays.get((market, year), [])

    def get_session_window(self, market, d):
        return self._windows.get((market, d))


def _svc(holidays=None, loaded_years=None, data_source=None):
    """Return an isolated CalendarService with pre-seeded state. No network calls."""
    from backend.data.calendar import configure_calendar_service, get_calendar_service
    configure_calendar_service(data_source=data_source)
    svc = get_calendar_service()
    if holidays:
        for h in holidays:
            svc._holiday_index[(h.market, h.date)] = h
            svc._loaded_years.add((h.market, h.date.year))
    if loaded_years:
        for k in loaded_years:
            svc._loaded_years.add(k)
    return svc


# ── MarketClosedError safety ─────────────────────────────────────────

def test_market_closed_error_not_runtime_error():
    """MarketClosedError must NOT subclass RuntimeError — circuit breaker safety."""
    from backend.data.calendar import MarketClosedError
    assert not issubclass(MarketClosedError, RuntimeError)
    assert issubclass(MarketClosedError, Exception)


# ── KRX holiday blocking ─────────────────────────────────────────────

def test_krx_holiday_blocks_trading():
    """KRX full holiday → is_trading_day=False, permission=HOLIDAY."""
    from backend.data.calendar import Market, MarketHoliday, BlockReason
    # May 5, 2026 = Tuesday (어린이날)
    holiday = MarketHoliday(date=date(2026, 5, 5), market=Market.KRX, name="어린이날")
    svc = _svc(holidays=[holiday])

    assert not svc.is_trading_day(Market.KRX, date(2026, 5, 5))

    # 10:00 KST = 01:00 UTC
    dt_utc = datetime(2026, 5, 5, 1, 0, tzinfo=_UTC)
    perm = svc.check_order_permission(Market.KRX, dt_utc)
    assert not perm.allowed
    assert perm.block_reason == BlockReason.HOLIDAY
    assert perm.holiday_name == "어린이날"


def test_krx_holiday_blocks_assert_tradeable():
    """assert_tradeable raises MarketClosedError on KRX holiday."""
    from backend.data.calendar import Market, MarketHoliday, MarketClosedError
    holiday = MarketHoliday(date=date(2026, 5, 5), market=Market.KRX, name="어린이날")
    svc = _svc(holidays=[holiday])

    dt_utc = datetime(2026, 5, 5, 1, 0, tzinfo=_UTC)
    with pytest.raises(MarketClosedError):
        svc.assert_tradeable(Market.KRX, dt_utc)


# ── NYSE holiday blocking ────────────────────────────────────────────

def test_nyse_holiday_blocks_trading():
    """NYSE full holiday (Thanksgiving) → is_trading_day=False, HOLIDAY."""
    from backend.data.calendar import Market, MarketHoliday, BlockReason
    holiday = MarketHoliday(date=date(2026, 11, 26), market=Market.NYSE, name="Thanksgiving")
    svc = _svc(holidays=[holiday])

    assert not svc.is_trading_day(Market.NYSE, date(2026, 11, 26))

    # 11:00 ET = 16:00 UTC (EST)
    dt_utc = datetime(2026, 11, 26, 16, 0, tzinfo=_UTC)
    perm = svc.check_order_permission(Market.NYSE, dt_utc)
    assert not perm.allowed
    assert perm.block_reason == BlockReason.HOLIDAY
    assert perm.holiday_name == "Thanksgiving"


# ── Weekend checks ───────────────────────────────────────────────────

def test_is_trading_day_weekend_false():
    """Saturday and Sunday are never trading days."""
    from backend.data.calendar import Market
    svc = _svc(loaded_years=[(Market.KRX, 2026)])
    assert not svc.is_trading_day(Market.KRX, date(2026, 6, 13))  # Saturday
    assert not svc.is_trading_day(Market.KRX, date(2026, 6, 14))  # Sunday


def test_session_closed_weekend():
    """Any time on Saturday → CLOSED."""
    from backend.data.calendar import Market, SessionType
    svc = _svc(loaded_years=[(Market.KRX, 2026)])
    dt_utc = datetime(2026, 6, 13, 1, 0, tzinfo=_UTC)  # Saturday 10:00 KST
    assert svc.get_session(Market.KRX, dt_utc) == SessionType.CLOSED


# ── KRX session detection ────────────────────────────────────────────

def test_session_pre_market_krx():
    """08:30 KST (23:30 UTC prev day) → PRE_MARKET on a trading day."""
    from backend.data.calendar import Market, SessionType
    svc = _svc(loaded_years=[(Market.KRX, 2026)])
    # 23:30 UTC June 9 = 08:30 KST June 10 (Wednesday)
    dt_utc = datetime(2026, 6, 9, 23, 30, tzinfo=_UTC)
    assert svc.get_session(Market.KRX, dt_utc) == SessionType.PRE_MARKET


def test_session_regular_krx():
    """10:00 KST (01:00 UTC) → REGULAR."""
    from backend.data.calendar import Market, SessionType
    svc = _svc(loaded_years=[(Market.KRX, 2026)])
    dt_utc = datetime(2026, 6, 10, 1, 0, tzinfo=_UTC)  # 10:00 KST June 10
    assert svc.get_session(Market.KRX, dt_utc) == SessionType.REGULAR


def test_session_after_hours_krx():
    """16:00 KST (07:00 UTC) → AFTER_HOURS."""
    from backend.data.calendar import Market, SessionType
    svc = _svc(loaded_years=[(Market.KRX, 2026)])
    dt_utc = datetime(2026, 6, 10, 7, 0, tzinfo=_UTC)  # 16:00 KST June 10
    assert svc.get_session(Market.KRX, dt_utc) == SessionType.AFTER_HOURS


# ── NYSE session detection ───────────────────────────────────────────

def test_session_nyse_after_hours():
    """17:00 ET on a normal trading day → AFTER_HOURS."""
    from backend.data.calendar import Market, SessionType
    svc = _svc(loaded_years=[(Market.NYSE, 2026)])
    # 17:00 EST Dec 9, 2026 = 22:00 UTC
    dt_utc = datetime(2026, 12, 9, 22, 0, tzinfo=_UTC)
    assert svc.get_session(Market.NYSE, dt_utc) == SessionType.AFTER_HOURS


# ── DST boundary tests ───────────────────────────────────────────────

def test_dst_summer_session_opens_1330_utc():
    """NYSE opens at 09:30 EDT = 13:30 UTC in summer (DST active)."""
    from backend.data.calendar import Market, SessionType
    svc = _svc(loaded_years=[(Market.NYSE, 2026)])
    # July 9, 2026 (Thursday) — EDT = UTC-4
    dt_before = datetime(2026, 7, 9, 13, 29, tzinfo=_UTC)  # 09:29 EDT — pre-market
    dt_at = datetime(2026, 7, 9, 13, 30, tzinfo=_UTC)      # 09:30 EDT — regular opens
    assert svc.get_session(Market.NYSE, dt_before) == SessionType.PRE_MARKET
    assert svc.get_session(Market.NYSE, dt_at) == SessionType.REGULAR


def test_dst_winter_session_opens_1430_utc():
    """NYSE opens at 09:30 EST = 14:30 UTC in winter (DST inactive)."""
    from backend.data.calendar import Market, SessionType
    svc = _svc(loaded_years=[(Market.NYSE, 2026)])
    # Dec 9, 2026 (Wednesday) — EST = UTC-5
    dt_before = datetime(2026, 12, 9, 14, 29, tzinfo=_UTC)  # 09:29 EST — pre-market
    dt_at = datetime(2026, 12, 9, 14, 30, tzinfo=_UTC)       # 09:30 EST — regular opens
    assert svc.get_session(Market.NYSE, dt_before) == SessionType.PRE_MARKET
    assert svc.get_session(Market.NYSE, dt_at) == SessionType.REGULAR


# ── NYSE half-day (early close) ──────────────────────────────────────

def test_nyse_early_close_is_trading_day():
    """Early-close day is still a trading day."""
    from backend.data.calendar import Market, MarketHoliday
    ec = MarketHoliday(
        date=date(2026, 11, 27), market=Market.NYSE,
        name="Black Friday", is_early_close=True, early_close_time=time_type(13, 0),
    )
    svc = _svc(holidays=[ec])
    assert svc.is_trading_day(Market.NYSE, date(2026, 11, 27))


def test_nyse_early_close_blocks_after_1300_et():
    """Before 13:00 ET on half-day → allowed; after → HALF_DAY_ENDED."""
    from backend.data.calendar import (
        Market, BlockReason, SessionType, MarketHoliday, SessionWindow,
    )
    d = date(2026, 11, 27)  # Friday after Thanksgiving, EST = UTC-5
    ec = MarketHoliday(
        date=d, market=Market.NYSE, name="Black Friday",
        is_early_close=True, early_close_time=time_type(13, 0),
    )
    mock_win = SessionWindow(
        market=Market.NYSE, date=d,
        open_utc=datetime(2026, 11, 27, 14, 30, tzinfo=_UTC),  # 09:30 EST
        close_utc=datetime(2026, 11, 27, 18, 0, tzinfo=_UTC),   # 13:00 EST
    )
    mock_src = _MockSource(
        holidays={(Market.NYSE, 2026): [ec]},
        windows={(Market.NYSE, d): mock_win},
    )
    svc = _svc(data_source=mock_src)

    # 12:00 EST = 17:00 UTC — before early close → REGULAR → allowed
    perm_ok = svc.check_order_permission(Market.NYSE, datetime(2026, 11, 27, 17, 0, tzinfo=_UTC))
    assert perm_ok.allowed
    assert perm_ok.session_type == SessionType.REGULAR

    # 14:00 EST = 19:00 UTC — after early close → HALF_DAY_ENDED
    perm_block = svc.check_order_permission(Market.NYSE, datetime(2026, 11, 27, 19, 0, tzinfo=_UTC))
    assert not perm_block.allowed
    assert perm_block.block_reason == BlockReason.HALF_DAY_ENDED


# ── Timezone utilities ───────────────────────────────────────────────

def test_timezone_naive_raises():
    """to_utc with a naive datetime raises ValueError."""
    from backend.data.calendar import TimezoneHandler
    with pytest.raises(ValueError):
        TimezoneHandler.to_utc(datetime(2026, 6, 10, 1, 0))  # no tzinfo


def test_timezone_kst_correct():
    """00:30 UTC → 09:30 KST."""
    from backend.data.calendar import TimezoneHandler
    dt_utc = datetime(2026, 6, 10, 0, 30, tzinfo=_UTC)
    dt_kst = TimezoneHandler.to_kst(dt_utc)
    assert dt_kst.hour == 9
    assert dt_kst.minute == 30


def test_trade_date_kr_uses_seoul():
    """23:30 UTC June 9 → June 10 in Seoul (next calendar day)."""
    from backend.data.calendar import TimezoneHandler, Market
    dt_utc = datetime(2026, 6, 9, 23, 30, tzinfo=_UTC)
    assert TimezoneHandler.trade_date(Market.KRX, dt_utc) == date(2026, 6, 10)


def test_trade_date_us_uses_et():
    """02:00 UTC Monday (summer) → Sunday in Eastern Time."""
    from backend.data.calendar import TimezoneHandler, Market
    # June 15 = Monday; 02:00 UTC = 22:00 EDT June 14 (Sunday)
    dt_utc = datetime(2026, 6, 15, 2, 0, tzinfo=_UTC)
    assert TimezoneHandler.trade_date(Market.NYSE, dt_utc) == date(2026, 6, 14)


# ── Fail-closed policy ───────────────────────────────────────────────

def test_permission_fail_closed_on_exception():
    """Any internal exception in check_order_permission → CACHE_FAILURE, never raises."""
    from backend.data.calendar import Market, BlockReason
    from backend.data.calendar import configure_calendar_service, get_calendar_service
    configure_calendar_service()
    svc = get_calendar_service()

    def _broken(*_args, **_kwargs):
        raise RuntimeError("simulated cache failure")

    svc.get_holiday = _broken  # force exception inside check_order_permission

    perm = svc.check_order_permission(Market.KRX, datetime(2026, 6, 10, 1, 0, tzinfo=_UTC))
    assert not perm.allowed
    assert perm.block_reason == BlockReason.CACHE_FAILURE


# ── Static fallback data ─────────────────────────────────────────────

def test_static_fallback_krx_2026():
    """Static KRX 2026 fallback includes 신정 and 삼일절."""
    from backend.data.calendar import StaticFallbackDataSource, Market
    dates = {h.date for h in StaticFallbackDataSource().get_holidays(Market.KRX, 2026)}
    assert date(2026, 1, 1) in dates   # 신정
    assert date(2026, 3, 1) in dates   # 삼일절


def test_static_fallback_nyse_2026():
    """Static NYSE 2026 fallback includes New Year's Day and Thanksgiving."""
    from backend.data.calendar import StaticFallbackDataSource, Market
    dates = {h.date for h in StaticFallbackDataSource().get_holidays(Market.NYSE, 2026)}
    assert date(2026, 1, 1) in dates    # New Year's Day
    assert date(2026, 11, 26) in dates  # Thanksgiving


# ── Navigation ───────────────────────────────────────────────────────

def test_next_session_open_skips_weekend():
    """next_session_open after Friday KRX close returns Monday open."""
    from backend.data.calendar import Market
    svc = _svc(loaded_years=[(Market.KRX, 2026)])
    # June 12, 2026 = Friday; KRX closes 06:30 UTC
    dt = datetime(2026, 6, 12, 6, 31, tzinfo=_UTC)
    next_open = svc.next_session_open(Market.KRX, dt)
    assert next_open.date() == date(2026, 6, 15)  # Monday


def test_prev_trading_day_skips_holiday():
    """prev_trading_day(before=T+1) skips a holiday on T, returns T-1."""
    from backend.data.calendar import Market, MarketHoliday
    holiday = MarketHoliday(date=date(2026, 6, 10), market=Market.KRX, name="테스트 휴장")
    svc = _svc(holidays=[holiday], loaded_years=[(Market.KRX, 2026)])
    prev = svc.prev_trading_day(Market.KRX, date(2026, 6, 11))
    assert prev == date(2026, 6, 9)


# ── Warm-up ──────────────────────────────────────────────────────────

def test_warm_up_does_not_raise():
    """warm_up with an empty source completes without raising."""
    from backend.data.calendar import Market
    svc = _svc(data_source=_MockSource())
    svc.warm_up([Market.KRX, Market.NYSE], days_ahead=3)
