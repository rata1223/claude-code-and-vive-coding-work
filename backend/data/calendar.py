"""
Market calendar service — KRX, NYSE, NASDAQ.

Single source of truth for "is the market open right now / today?".
Fail-closed: any calendar lookup failure defaults to CLOSED (orders blocked).

Usage:
    from backend.data.calendar import get_calendar_service, Market
    svc = get_calendar_service()
    svc.assert_tradeable(Market.KRX, datetime.now(UTC))   # raises MarketClosedError if closed
    svc.is_trading_day(Market.NYSE, date.today())         # bool
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time as time_type, timedelta
from enum import Enum
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

_KST = pytz.timezone("Asia/Seoul")
_ET = pytz.timezone("America/New_York")
_UTC = pytz.UTC

# ─────────────────────────────────────────────────────────────────
# 1. Public enums
# ─────────────────────────────────────────────────────────────────

class Market(str, Enum):
    KRX = "KRX"
    NYSE = "NYSE"
    NASDAQ = "NASDAQ"


class SessionType(str, Enum):
    PRE_MARKET = "pre_market"
    REGULAR = "regular"
    AFTER_HOURS = "after_hours"
    CLOSED = "closed"


class BlockReason(str, Enum):
    HOLIDAY = "holiday"
    HALF_DAY_ENDED = "half_day_ended"
    WRONG_SESSION = "wrong_session"
    CACHE_FAILURE = "cache_failure"


# ─────────────────────────────────────────────────────────────────
# 2. Exceptions
# ─────────────────────────────────────────────────────────────────

class MarketClosedError(Exception):
    """Raised when an order is blocked due to market being closed.

    NOT a subclass of RuntimeError — intentional. This prevents
    ConsecutiveFailureBreaker from counting market-closed rejections
    as broker failures (see backend/execution/circuit_breaker.py).
    """

    def __init__(
        self,
        market: Market,
        session: SessionType,
        reason: BlockReason,
        detail: str = "",
        next_open_utc: Optional[datetime] = None,
    ) -> None:
        self.market = market
        self.session = session
        self.reason = reason
        self.next_open_utc = next_open_utc
        super().__init__(detail or f"{market} market closed ({reason})")


class CalendarDataError(Exception):
    """Internal data source failure — never crosses CalendarService boundary."""


# ─────────────────────────────────────────────────────────────────
# 3. Dataclasses
# ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MarketHoliday:
    date: date
    market: Market
    name: str
    is_early_close: bool = False
    early_close_time: Optional[time_type] = None  # local market time (ET for NYSE)


@dataclass(frozen=True)
class SessionWindow:
    market: Market
    date: date
    open_utc: datetime
    close_utc: datetime  # early-close time on half-days


@dataclass
class TradingPermission:
    allowed: bool
    session_type: SessionType
    block_reason: Optional[BlockReason] = None
    holiday_name: Optional[str] = None
    next_open_utc: Optional[datetime] = None


# ─────────────────────────────────────────────────────────────────
# 4. Session time constants
# ─────────────────────────────────────────────────────────────────

# KRX (Korea Exchange) — Asia/Seoul (UTC+9, no DST)
_KRX_PRE_OPEN = time_type(8, 0)
_KRX_OPEN = time_type(9, 0)
_KRX_CLOSE = time_type(15, 30)
_KRX_AFTER_END = time_type(18, 0)

# NYSE / NASDAQ — America/New_York (EST/EDT, auto-DST)
_NYSE_PRE_OPEN = time_type(4, 0)
_NYSE_OPEN = time_type(9, 30)
_NYSE_CLOSE = time_type(16, 0)
_NYSE_EARLY_CLOSE = time_type(13, 0)
_NYSE_AFTER_END = time_type(20, 0)


# ─────────────────────────────────────────────────────────────────
# 5. TimezoneHandler
# ─────────────────────────────────────────────────────────────────

class TimezoneHandler:
    """Stateless timezone utility. All methods are classmethods."""

    @classmethod
    def to_kst(cls, dt_utc: datetime) -> datetime:
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=_UTC)
        return dt_utc.astimezone(_KST)

    @classmethod
    def to_et(cls, dt_utc: datetime) -> datetime:
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=_UTC)
        return dt_utc.astimezone(_ET)

    @classmethod
    def to_utc(cls, dt_local: datetime) -> datetime:
        if dt_local.tzinfo is None:
            raise ValueError("dt_local must be timezone-aware; got naive datetime")
        return dt_local.astimezone(_UTC)

    @classmethod
    def to_market_local(cls, market: Market, dt_utc: datetime) -> datetime:
        if market == Market.KRX:
            return cls.to_kst(dt_utc)
        return cls.to_et(dt_utc)

    @classmethod
    def trade_date(cls, market: Market, dt_utc: datetime) -> date:
        """Return the trading calendar date for the given market."""
        return cls.to_market_local(market, dt_utc).date()

    @classmethod
    def market_open_utc(cls, market: Market, local_date: date, local_time: time_type) -> datetime:
        """Convert a local market time to UTC. is_dst=False is conservative for ambiguous times."""
        tz = _KST if market == Market.KRX else _ET
        naive = datetime.combine(local_date, local_time)
        aware = tz.localize(naive, is_dst=False)
        return aware.astimezone(_UTC)

    @classmethod
    def market_close_utc(cls, market: Market, local_date: date, local_time: time_type) -> datetime:
        """Convert a local market close time to UTC."""
        return cls.market_open_utc(market, local_date, local_time)


# ─────────────────────────────────────────────────────────────────
# 6. CalendarDataSource ABC
# ─────────────────────────────────────────────────────────────────

class CalendarDataSource(ABC):

    @abstractmethod
    def get_holidays(self, market: Market, year: int) -> list[MarketHoliday]:
        """Return all holidays (full-close + early-close) for the year."""
        ...

    @abstractmethod
    def get_session_window(self, market: Market, d: date) -> Optional[SessionWindow]:
        """Return the REGULAR session window for trading day d, or None if closed."""
        ...


# ─────────────────────────────────────────────────────────────────
# 7. KRXDataSource  (pykrx)
# ─────────────────────────────────────────────────────────────────

class KRXDataSource(CalendarDataSource):

    def get_holidays(self, market: Market, year: int) -> list[MarketHoliday]:
        if market != Market.KRX:
            return []
        try:
            from pykrx import market as krx_market  # type: ignore
            df = krx_market.get_market_holidays(year)
            holidays: list[MarketHoliday] = []
            for dt_idx, row in df.iterrows():
                d = dt_idx.date() if hasattr(dt_idx, "date") else dt_idx
                # pykrx column names vary by version; try several
                if hasattr(row, "get"):
                    name = (
                        row.get("holiday_name")
                        or row.get("공휴일")
                        or row.get("휴장사유")
                        or "휴장일"
                    )
                else:
                    name = "휴장일"
                holidays.append(MarketHoliday(date=d, market=Market.KRX, name=str(name)))
            return holidays
        except Exception as exc:
            raise CalendarDataError(f"pykrx holiday fetch failed for {year}: {exc}") from exc

    def get_session_window(self, market: Market, d: date) -> Optional[SessionWindow]:
        if market != Market.KRX:
            return None
        if d.weekday() >= 5:
            return None
        # Check holidays without triggering recursive source calls
        try:
            holidays = self.get_holidays(market, d.year)
            if any(h.date == d and not h.is_early_close for h in holidays):
                return None
            h_ec = next((h for h in holidays if h.date == d and h.is_early_close), None)
            close_time = h_ec.early_close_time if (h_ec and h_ec.early_close_time) else _KRX_CLOSE
        except CalendarDataError:
            close_time = _KRX_CLOSE

        return SessionWindow(
            market=market,
            date=d,
            open_utc=TimezoneHandler.market_open_utc(market, d, _KRX_OPEN),
            close_utc=TimezoneHandler.market_close_utc(market, d, close_time),
        )


# ─────────────────────────────────────────────────────────────────
# 8. NYSEDataSource  (pandas_market_calendars)
# ─────────────────────────────────────────────────────────────────

class NYSEDataSource(CalendarDataSource):

    def get_holidays(self, market: Market, year: int) -> list[MarketHoliday]:
        if market not in (Market.NYSE, Market.NASDAQ):
            return []
        try:
            import pandas as pd  # type: ignore
            import pandas_market_calendars as mcal  # type: ignore

            cal = mcal.get_calendar("NYSE")
            schedule = cal.schedule(
                start_date=f"{year}-01-01",
                end_date=f"{year}-12-31",
                tz="UTC",
            )
            # Build set of weekday trading days from schedule
            trading_days: set[date] = set()
            for ts in schedule.index:
                trading_days.add(pd.Timestamp(ts).date())

            holidays: list[MarketHoliday] = []
            d = date(year, 1, 1)
            end = date(year, 12, 31)
            while d <= end:
                if d.weekday() < 5:  # weekday only
                    if d not in trading_days:
                        holidays.append(MarketHoliday(date=d, market=market, name="NYSE Holiday"))
                    else:
                        # Check for early close: close < 20:00 UTC (< 16:00 ET normal)
                        ts = pd.Timestamp(d)
                        if ts in schedule.index:
                            close_raw = schedule.loc[ts, "market_close"]
                            if hasattr(close_raw, "to_pydatetime"):
                                close_utc = close_raw.to_pydatetime()
                                if close_utc.tzinfo is None:
                                    close_utc = close_utc.replace(tzinfo=_UTC)
                            else:
                                close_utc = close_raw
                            # Normal close is 21:00 UTC (16:00 ET winter) or 20:00 UTC (summer)
                            # Early close is 18:00 UTC (13:00 ET winter) or 17:00 UTC (summer)
                            # Conservative: if close before 20:30 UTC, it's early close
                            close_threshold = datetime(
                                d.year, d.month, d.day, 20, 30, tzinfo=_UTC
                            )
                            if close_utc < close_threshold:
                                et_close = TimezoneHandler.to_et(close_utc)
                                ec_time = et_close.time().replace(second=0, microsecond=0)
                                holidays.append(MarketHoliday(
                                    date=d, market=market, name="Early Close",
                                    is_early_close=True, early_close_time=ec_time,
                                ))
                d += timedelta(days=1)
            return holidays
        except Exception as exc:
            raise CalendarDataError(
                f"pandas_market_calendars holiday fetch failed for {year}: {exc}"
            ) from exc

    def get_session_window(self, market: Market, d: date) -> Optional[SessionWindow]:
        if market not in (Market.NYSE, Market.NASDAQ):
            return None
        try:
            import pandas as pd  # type: ignore
            import pandas_market_calendars as mcal  # type: ignore

            cal = mcal.get_calendar("NYSE")
            schedule = cal.schedule(
                start_date=d.isoformat(), end_date=d.isoformat(), tz="UTC"
            )
            if schedule.empty:
                return None
            ts = pd.Timestamp(d)
            if ts not in schedule.index:
                return None
            row = schedule.loc[ts]
            open_raw = row["market_open"]
            close_raw = row["market_close"]

            def _to_utc_dt(raw) -> datetime:
                if hasattr(raw, "to_pydatetime"):
                    dt = raw.to_pydatetime()
                else:
                    dt = raw
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=_UTC)
                return dt.astimezone(_UTC)

            return SessionWindow(
                market=market,
                date=d,
                open_utc=_to_utc_dt(open_raw),
                close_utc=_to_utc_dt(close_raw),
            )
        except Exception as exc:
            raise CalendarDataError(
                f"pandas_market_calendars session fetch failed for {d}: {exc}"
            ) from exc


# ─────────────────────────────────────────────────────────────────
# 9. StaticFallbackDataSource
# ─────────────────────────────────────────────────────────────────

# Hardcoded holidays for 2026 (primary operational year).
# Half-days are NOT included — early-close treated as full-close in fallback (safe/fail-closed).
_STATIC_HOLIDAYS: dict[tuple[Market, date], str] = {
    # ── KRX 2026 ──────────────────────────────────────────────────
    (Market.KRX, date(2026, 1, 1)): "신정",
    (Market.KRX, date(2026, 1, 28)): "설날 연휴",
    (Market.KRX, date(2026, 1, 29)): "설날",
    (Market.KRX, date(2026, 1, 30)): "설날 연휴",
    (Market.KRX, date(2026, 3, 1)): "삼일절",
    (Market.KRX, date(2026, 5, 5)): "어린이날",
    (Market.KRX, date(2026, 6, 6)): "현충일",
    (Market.KRX, date(2026, 8, 15)): "광복절",
    (Market.KRX, date(2026, 9, 24)): "추석 연휴",
    (Market.KRX, date(2026, 9, 25)): "추석",
    (Market.KRX, date(2026, 9, 26)): "추석 연휴",
    (Market.KRX, date(2026, 10, 3)): "개천절",
    (Market.KRX, date(2026, 10, 9)): "한글날",
    (Market.KRX, date(2026, 12, 25)): "성탄절",
    (Market.KRX, date(2026, 12, 31)): "연말 휴장",
    # ── NYSE 2026 ─────────────────────────────────────────────────
    (Market.NYSE, date(2026, 1, 1)): "New Year's Day",
    (Market.NYSE, date(2026, 1, 19)): "MLK Day",
    (Market.NYSE, date(2026, 2, 16)): "Presidents Day",
    (Market.NYSE, date(2026, 4, 3)): "Good Friday",
    (Market.NYSE, date(2026, 5, 25)): "Memorial Day",
    (Market.NYSE, date(2026, 6, 19)): "Juneteenth",
    (Market.NYSE, date(2026, 7, 3)): "Independence Day (observed)",
    (Market.NYSE, date(2026, 9, 7)): "Labor Day",
    (Market.NYSE, date(2026, 11, 26)): "Thanksgiving",
    (Market.NYSE, date(2026, 12, 25)): "Christmas Day",
    # ── NASDAQ 2026 (mirrors NYSE) ────────────────────────────────
    (Market.NASDAQ, date(2026, 1, 1)): "New Year's Day",
    (Market.NASDAQ, date(2026, 1, 19)): "MLK Day",
    (Market.NASDAQ, date(2026, 2, 16)): "Presidents Day",
    (Market.NASDAQ, date(2026, 4, 3)): "Good Friday",
    (Market.NASDAQ, date(2026, 5, 25)): "Memorial Day",
    (Market.NASDAQ, date(2026, 6, 19)): "Juneteenth",
    (Market.NASDAQ, date(2026, 7, 3)): "Independence Day (observed)",
    (Market.NASDAQ, date(2026, 9, 7)): "Labor Day",
    (Market.NASDAQ, date(2026, 11, 26)): "Thanksgiving",
    (Market.NASDAQ, date(2026, 12, 25)): "Christmas Day",
}


class StaticFallbackDataSource(CalendarDataSource):

    def get_holidays(self, market: Market, year: int) -> list[MarketHoliday]:
        return [
            MarketHoliday(date=d, market=m, name=name)
            for (m, d), name in _STATIC_HOLIDAYS.items()
            if m == market and d.year == year
        ]

    def get_session_window(self, market: Market, d: date) -> Optional[SessionWindow]:
        if d.weekday() >= 5:
            return None
        if _STATIC_HOLIDAYS.get((market, d)):
            return None  # holiday
        if market == Market.KRX:
            return SessionWindow(
                market=market, date=d,
                open_utc=TimezoneHandler.market_open_utc(market, d, _KRX_OPEN),
                close_utc=TimezoneHandler.market_close_utc(market, d, _KRX_CLOSE),
            )
        else:  # NYSE / NASDAQ
            return SessionWindow(
                market=market, date=d,
                open_utc=TimezoneHandler.market_open_utc(market, d, _NYSE_OPEN),
                close_utc=TimezoneHandler.market_close_utc(market, d, _NYSE_CLOSE),
            )


# ─────────────────────────────────────────────────────────────────
# 10. CalendarCache (two-layer: L1 in-process LRU + L2 Redis)
# ─────────────────────────────────────────────────────────────────

_CACHE_PREFIX = "calendar:"


class CalendarCache:
    _REDIS_TTL = 86_400   # 24 h
    _L1_TTL = 3_600       # 1 h
    _L1_MAX = 200

    def __init__(self, redis_client=None) -> None:
        self._redis = redis_client
        self._l1: dict[str, tuple[dict, float]] = {}   # key → (value, expiry_monotonic)
        self._lock = threading.Lock()

    @staticmethod
    def _key(market: Market, d: date) -> str:
        return f"{_CACHE_PREFIX}{market.value}:{d.isoformat()}"

    def get(self, market: Market, d: date) -> Optional[dict]:
        key = self._key(market, d)
        # L1 check
        with self._lock:
            entry = self._l1.get(key)
            if entry is not None:
                val, exp = entry
                if time.monotonic() < exp:
                    return val
                del self._l1[key]
        # L2 check
        if self._redis is not None:
            try:
                raw = self._redis.get(key)
                if raw:
                    val = json.loads(raw)
                    self._set_l1(key, val)
                    return val
            except Exception as exc:
                logger.warning("Redis calendar cache read error: %s", exc)
        return None

    def set(self, market: Market, d: date, value: dict, to_l2: bool = True) -> None:
        key = self._key(market, d)
        self._set_l1(key, value)
        if to_l2 and self._redis is not None:
            try:
                self._redis.setex(key, self._REDIS_TTL, json.dumps(value, default=str))
            except Exception as exc:
                logger.warning("Redis calendar cache write error: %s", exc)

    def _set_l1(self, key: str, value: dict) -> None:
        with self._lock:
            if len(self._l1) >= self._L1_MAX:
                # Evict the entry with the earliest expiry (LRU-approximate)
                oldest = min(self._l1, key=lambda k: self._l1[k][1])
                del self._l1[oldest]
            self._l1[key] = (value, time.monotonic() + self._L1_TTL)


# ─────────────────────────────────────────────────────────────────
# 11. CalendarService
# ─────────────────────────────────────────────────────────────────

class CalendarService:
    """Single authoritative calendar service.  Fail-closed: any internal error
    returns TradingPermission(allowed=False, block_reason=CACHE_FAILURE).
    """

    def __init__(
        self,
        redis_client=None,
        data_source: Optional[CalendarDataSource] = None,
    ) -> None:
        self._cache = CalendarCache(redis_client)
        self._injected_source = data_source
        self._fallback = StaticFallbackDataSource()
        self._allow_premarket = (
            os.environ.get("ALLOW_PREMARKET_ORDERS", "false").lower() == "true"
        )
        self._allow_afterhours = (
            os.environ.get("ALLOW_AFTERHOURS_ORDERS", "false").lower() == "true"
        )
        # In-memory holiday index: {(market, date) → MarketHoliday}
        self._holiday_index: dict[tuple[Market, date], MarketHoliday] = {}
        self._loaded_years: set[tuple[Market, int]] = set()
        self._idx_lock = threading.Lock()

    # ── Source selection ──────────────────────────────────────────

    def _primary_source(self, market: Market) -> CalendarDataSource:
        if self._injected_source is not None:
            return self._injected_source
        return KRXDataSource() if market == Market.KRX else NYSEDataSource()

    # ── Holiday loading ───────────────────────────────────────────

    def _ensure_holidays_loaded(self, market: Market, year: int) -> None:
        with self._idx_lock:
            if (market, year) in self._loaded_years:
                return

        holidays: list[MarketHoliday] = []
        to_l2 = True
        try:
            holidays = self._primary_source(market).get_holidays(market, year)
            logger.debug(
                "Holidays loaded from primary for %s %d: %d days",
                market, year, len(holidays),
            )
        except Exception as primary_exc:
            logger.warning(
                "Primary holiday source failed (%s %d): %s — trying static fallback",
                market, year, primary_exc,
            )
            try:
                holidays = self._fallback.get_holidays(market, year)
                to_l2 = False  # fallback data: L1 only, not L2
                logger.info("Static fallback holidays loaded for %s %d: %d days", market, year, len(holidays))
            except Exception as fallback_exc:
                logger.error(
                    "Static fallback also failed (%s %d): %s — no holiday data",
                    market, year, fallback_exc,
                )
                return  # do not mark as loaded; will retry next call

        with self._idx_lock:
            for h in holidays:
                self._holiday_index[(market, h.date)] = h
            self._loaded_years.add((market, year))

        # Populate cache for each holiday date
        for h in holidays:
            existing = self._cache.get(market, h.date)
            if existing is None:
                self._cache.set(market, h.date, {
                    "is_trading_day": False if not h.is_early_close else True,
                    "holiday_name": h.name,
                    "is_early_close": h.is_early_close,
                    "early_close_et": (
                        h.early_close_time.strftime("%H:%M")
                        if h.early_close_time else None
                    ),
                }, to_l2=to_l2)

    # ── Core queries ──────────────────────────────────────────────

    def get_holiday(self, market: Market, d: date) -> Optional[MarketHoliday]:
        self._ensure_holidays_loaded(market, d.year)
        return self._holiday_index.get((market, d))

    def is_trading_day(self, market: Market, d: date) -> bool:
        """Return True if the market has a regular or early-close session on d."""
        if d.weekday() >= 5:
            return False
        h = self.get_holiday(market, d)
        if h is None:
            return True
        return h.is_early_close  # early-close days are partial trading days

    def is_early_close(self, market: Market, d: date) -> bool:
        h = self.get_holiday(market, d)
        return h is not None and h.is_early_close

    def get_session_window(self, market: Market, d: date) -> Optional[SessionWindow]:
        """Return REGULAR session window for d (adjusted for early-close), or None."""
        if not self.is_trading_day(market, d):
            return None
        h = self.get_holiday(market, d)

        if market == Market.KRX:
            close_time = (
                h.early_close_time
                if (h and h.is_early_close and h.early_close_time)
                else _KRX_CLOSE
            )
            return SessionWindow(
                market=market, date=d,
                open_utc=TimezoneHandler.market_open_utc(market, d, _KRX_OPEN),
                close_utc=TimezoneHandler.market_close_utc(market, d, close_time),
            )
        else:  # NYSE / NASDAQ
            # Prefer primary source (has exact times from exchange schedule)
            try:
                win = self._primary_source(market).get_session_window(market, d)
                if win is not None:
                    return win
            except Exception:
                pass
            # Fallback: use fixed times
            close_time = _NYSE_EARLY_CLOSE if (h and h.is_early_close) else _NYSE_CLOSE
            return SessionWindow(
                market=market, date=d,
                open_utc=TimezoneHandler.market_open_utc(market, d, _NYSE_OPEN),
                close_utc=TimezoneHandler.market_close_utc(market, d, close_time),
            )

    def get_session(self, market: Market, dt_utc: datetime) -> SessionType:
        """Classify the session type for dt_utc in the given market."""
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=_UTC)

        local_dt = TimezoneHandler.to_market_local(market, dt_utc)
        d = local_dt.date()

        win = self.get_session_window(market, d)

        # Compute pre-market start and after-hours end in UTC for this local date
        if market == Market.KRX:
            pre_start = TimezoneHandler.market_open_utc(market, d, _KRX_PRE_OPEN)
            after_end = TimezoneHandler.market_close_utc(market, d, _KRX_AFTER_END)
        else:
            pre_start = TimezoneHandler.market_open_utc(market, d, _NYSE_PRE_OPEN)
            after_end = TimezoneHandler.market_close_utc(market, d, _NYSE_AFTER_END)

        if win is None:
            # Holiday or weekend — pre/after-hours still exist on non-trading days
            # but for order-blocking purposes, treat as CLOSED
            return SessionType.CLOSED

        if dt_utc < pre_start:
            return SessionType.CLOSED
        if dt_utc < win.open_utc:
            return SessionType.PRE_MARKET
        if dt_utc <= win.close_utc:
            return SessionType.REGULAR
        if dt_utc < after_end:
            return SessionType.AFTER_HOURS
        return SessionType.CLOSED

    # ── Permission check ──────────────────────────────────────────

    def check_order_permission(self, market: Market, dt_utc: datetime) -> TradingPermission:
        """Check if an order may be placed.  Never raises — fail-closed on any error."""
        try:
            if dt_utc.tzinfo is None:
                dt_utc = dt_utc.replace(tzinfo=_UTC)

            local_d = TimezoneHandler.trade_date(market, dt_utc)

            # Holiday check (full-close only)
            h = self.get_holiday(market, local_d)
            if h is not None and not h.is_early_close:
                return TradingPermission(
                    allowed=False,
                    session_type=SessionType.CLOSED,
                    block_reason=BlockReason.HOLIDAY,
                    holiday_name=h.name,
                    next_open_utc=self._safe_next_open(market, dt_utc),
                )

            session = self.get_session(market, dt_utc)

            if session == SessionType.REGULAR:
                # On an early-close day, 'REGULAR' may run past the early-close window
                # because get_session_window already uses the truncated close time.
                # This branch is only reached for times within the actual window → allowed.
                return TradingPermission(allowed=True, session_type=session)

            if session == SessionType.AFTER_HOURS:
                # Distinguish half-day (early close) from regular after-hours
                if h is not None and h.is_early_close:
                    return TradingPermission(
                        allowed=False,
                        session_type=session,
                        block_reason=BlockReason.HALF_DAY_ENDED,
                        holiday_name=h.name,
                        next_open_utc=self._safe_next_open(market, dt_utc),
                    )
                if self._allow_afterhours:
                    return TradingPermission(allowed=True, session_type=session)
                return TradingPermission(
                    allowed=False, session_type=session,
                    block_reason=BlockReason.WRONG_SESSION,
                    next_open_utc=self._safe_next_open(market, dt_utc),
                )

            if session == SessionType.PRE_MARKET:
                if self._allow_premarket:
                    return TradingPermission(allowed=True, session_type=session)
                return TradingPermission(
                    allowed=False, session_type=session,
                    block_reason=BlockReason.WRONG_SESSION,
                    next_open_utc=self._safe_next_open(market, dt_utc),
                )

            # CLOSED
            return TradingPermission(
                allowed=False, session_type=SessionType.CLOSED,
                block_reason=BlockReason.WRONG_SESSION,
                next_open_utc=self._safe_next_open(market, dt_utc),
            )

        except Exception as exc:
            logger.error(
                "Calendar permission check failed (fail-closed) for %s at %s: %s",
                market, dt_utc, exc,
            )
            return TradingPermission(
                allowed=False,
                session_type=SessionType.CLOSED,
                block_reason=BlockReason.CACHE_FAILURE,
            )

    def assert_tradeable(self, market: Market, dt_utc: datetime) -> None:
        """Raise MarketClosedError if market is not open for regular trading."""
        perm = self.check_order_permission(market, dt_utc)
        if not perm.allowed:
            raise MarketClosedError(
                market=market,
                session=perm.session_type,
                reason=perm.block_reason or BlockReason.WRONG_SESSION,
                detail=f"{market} market not open ({perm.block_reason})",
                next_open_utc=perm.next_open_utc,
            )

    # ── Navigation ────────────────────────────────────────────────

    def next_session_open(self, market: Market, after_utc: datetime) -> datetime:
        """Return the UTC datetime of the next regular session open after after_utc."""
        if after_utc.tzinfo is None:
            after_utc = after_utc.replace(tzinfo=_UTC)
        d = TimezoneHandler.trade_date(market, after_utc)
        for days_ahead in range(31):
            check = d + timedelta(days=days_ahead)
            if check.weekday() >= 5:
                continue
            win = self.get_session_window(market, check)
            if win is not None and win.open_utc > after_utc:
                return win.open_utc
        raise ValueError(f"No session found within 31 days for {market}")

    def prev_trading_day(self, market: Market, before: date) -> date:
        """Return the most recent trading day strictly before `before`."""
        for i in range(1, 32):
            d = before - timedelta(days=i)
            if d.weekday() < 5 and self.is_trading_day(market, d):
                return d
        raise ValueError(f"No trading day found within 31 days before {before}")

    def trade_date(self, market: Market, dt_utc: datetime) -> date:
        return TimezoneHandler.trade_date(market, dt_utc)

    # ── Lifecycle ─────────────────────────────────────────────────

    def warm_up(self, markets: list[Market], days_ahead: int = 14) -> None:
        """Pre-populate cache for upcoming days.  Non-fatal on any error."""
        now_utc = datetime.now(_UTC)
        for market in markets:
            for year in (now_utc.year, now_utc.year + 1):
                try:
                    self._ensure_holidays_loaded(market, year)
                except Exception as exc:
                    logger.warning("warm_up: holiday load failed %s %d: %s", market, year, exc)
            for i in range(days_ahead):
                d = (now_utc + timedelta(days=i)).date()
                try:
                    self.get_session_window(market, d)
                except Exception as exc:
                    logger.warning("warm_up: session_window failed %s %s: %s", market, d, exc)

    # ── Internal helpers ──────────────────────────────────────────

    def _safe_next_open(self, market: Market, dt_utc: datetime) -> Optional[datetime]:
        try:
            return self.next_session_open(market, dt_utc)
        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────
# 12. Module-level singleton (replicates get_kis_broker() pattern)
# ─────────────────────────────────────────────────────────────────

_CALENDAR_SERVICE: Optional[CalendarService] = None
_CALENDAR_SERVICE_LOCK = threading.Lock()


def get_calendar_service() -> CalendarService:
    """Return the process-level CalendarService singleton.  Thread-safe."""
    global _CALENDAR_SERVICE
    if _CALENDAR_SERVICE is None:
        with _CALENDAR_SERVICE_LOCK:
            if _CALENDAR_SERVICE is None:
                redis_client = None
                try:
                    import redis as _redis  # type: ignore
                    redis_client = _redis.from_url(
                        os.environ.get("REDIS_URL", "redis://redis:6379")
                    )
                except Exception as exc:
                    logger.warning("CalendarService: Redis unavailable (%s) — L1 cache only", exc)
                _CALENDAR_SERVICE = CalendarService(redis_client=redis_client)
    return _CALENDAR_SERVICE


def configure_calendar_service(
    redis_client=None,
    data_source: Optional[CalendarDataSource] = None,
) -> CalendarService:
    """Inject dependencies at startup (e.g., from worker startup recovery)."""
    global _CALENDAR_SERVICE
    with _CALENDAR_SERVICE_LOCK:
        _CALENDAR_SERVICE = CalendarService(
            redis_client=redis_client, data_source=data_source
        )
    return _CALENDAR_SERVICE
