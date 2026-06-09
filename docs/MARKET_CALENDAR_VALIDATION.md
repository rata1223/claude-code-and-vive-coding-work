# Market Calendar Validation — Design Specification (TASK 3-1B)

## 1. Purpose and Scope

This document specifies the design for `backend/data/calendar.py`, the single
authoritative calendar, session, and trading-permission service for the platform.
It replaces every hardcoded `day_of_week="mon-fri"` assumption documented in
`docs/MARKET_CALENDAR_AUDIT.md` with a data-driven, cached, fail-closed system.

**Deliverable:** `backend/data/calendar.py` — design only; no code in this task.

**Markets covered:** KRX (Korea Exchange), NYSE, NASDAQ.

**Problems solved (from the TASK 3-1A audit):**
- CS-01: daily risk reset mid-US-session
- CS-02/03: holiday scheduler fires
- CS-04: US half-day early close
- CS-05: `date.today()` vs `_seoul_today()` mismatch
- CS-06: market-closed KIS errors trip the circuit breaker
- CS-07: no pre-order session gate
- CS-08: reconciler runs on holidays
- CS-09: `on_market_close()` never fired
- CS-10/11/12/13: various timezone and close-window gaps

---

## 2. Architecture Overview

```
                    ┌──────────────────────────────────┐
                    │          CalendarService          │
                    │                                  │
  Redis ──────────► │  CalendarCache (L1: memory       │
                    │                L2: Redis)        │
  pykrx ──────────► │                                  │
  pandas_mkt_cal ──►│  CalendarDataSource              │
  StaticFallback ──►│  (KRX / NYSE / StaticFallback)   │
                    │                                  │
                    │  TimezoneHandler                 │
                    │  SessionDetector                 │
                    │  PermissionEngine                │
                    └──────────────────────────────────┘
                                    │
         ┌──────────────────────────┼────────────────────────────┐
         ▼                          ▼                            ▼
  scheduler.py               KISBroker /               quick_trade.py /
  (holiday gate)          _live_trade_allowed()       risk engine dates
```

A **single `CalendarService` instance** is created per process and shared via
a module-level singleton (`get_calendar_service()`). It has no mutable state
except the L1 in-process cache; all other state is in Redis or fetched from
external sources.

---

## 3. Data Model

### 3.1 Enums

```python
class Market(str, Enum):
    KRX    = "KRX"     # Korea Exchange (Korean domestic stocks and ETFs)
    NYSE   = "NYSE"    # New York Stock Exchange
    NASDAQ = "NASDAQ"  # NASDAQ (same holiday calendar as NYSE; separate for future rule divergence)

class SessionType(str, Enum):
    PRE_MARKET  = "pre_market"   # Before regular session; informational only
    REGULAR     = "regular"      # Normal trading window; orders allowed
    AFTER_HOURS = "after_hours"  # Post-close; cancels allowed, new entries blocked by default
    CLOSED      = "closed"       # No trading permitted

class BlockReason(str, Enum):
    HOLIDAY         = "holiday"          # Full-day exchange closure
    HALF_DAY_ENDED  = "half_day_ended"   # Early close (half-day); regular session finished early
    WRONG_SESSION   = "wrong_session"    # Outside REGULAR session (PRE_MARKET, AFTER_HOURS, or CLOSED)
    CACHE_FAILURE   = "cache_failure"    # All calendar sources failed; fail-closed assumed
```

### 3.2 Exceptions

```python
class MarketClosedError(Exception):
    """
    Raised when an order is attempted outside a permitted session.

    NOT a subclass of RuntimeError — ensures it is NOT counted by
    ConsecutiveFailureBreaker.record_failure().

    Attributes:
        market:   Market enum value
        session:  current SessionType
        reason:   BlockReason
        next_open: next REGULAR session open in UTC (None if cache failure)
    """
    def __init__(
        self,
        market: Market,
        session: SessionType,
        reason: BlockReason,
        next_open: datetime | None = None,
    ): ...

class CalendarDataError(Exception):
    """
    Raised internally when a data source (pykrx, pandas_market_calendars)
    fails. Never propagates to order paths — always handled by fallback logic.
    """
```

`MarketClosedError` is the ONLY exception the trading path (broker, scheduler,
API) will see. `CalendarDataError` is internal to `CalendarService` and its data
sources; it never crosses the service boundary.

### 3.3 Dataclasses

```python
@dataclass(frozen=True)
class MarketHoliday:
    date:             date
    market:           Market
    name:             str            # human-readable: "추석연휴", "Thanksgiving"
    is_early_close:   bool = False
    early_close_time: time | None = None  # local market time; None unless is_early_close

@dataclass(frozen=True)
class SessionWindow:
    """
    Describes the REGULAR session window for a single market on a single date.
    open_utc and close_utc are timezone-aware UTC datetimes.
    None when the market is closed that day (holiday or weekend).
    """
    market:    Market
    date:      date
    open_utc:  datetime
    close_utc: datetime  # for half-days: the early close time, not 16:00 ET

@dataclass
class TradingPermission:
    """
    Result of check_order_permission(). Consumed by _live_trade_allowed(),
    KISBroker.place_order(), and the quick_trade API.
    """
    allowed:      bool
    session_type: SessionType
    block_reason: BlockReason | None = None
    holiday_name: str | None = None      # populated when reason = HOLIDAY or HALF_DAY_ENDED
    next_open_utc: datetime | None = None  # UTC; None if cache failure
```

---

## 4. MarketCalendar — Session Rules

### 4.1 KRX (Korea Exchange)

**Trading timezone:** `Asia/Seoul` (KST = UTC+9, no DST).

| Session | Local KST time | Notes |
|---|---|---|
| `PRE_MARKET` | 08:00 – 09:00 | Simultaneous bid collection (동시호가); this is informational — no order routing in the current system |
| `REGULAR` | 09:00 – 15:30 | Main session; closing auction included (15:20–15:30) |
| `AFTER_HOURS` | 15:30 – 18:00 | 단일가 매매 (single-price); cancels allowed; no new entries by default |
| `CLOSED` | 18:00 – 08:00 (next day) | All order placement blocked |

**Half-day:** KRX does not have a standard half-day schedule. In rare cases
(e.g., day before Lunar New Year when declared a trading day by KRX notice),
the exchange may use modified hours. These are handled via the yearly holiday
list from `pykrx.market.get_market_holidays()` — KRX publishes the full year's
schedule including any modified-hours days. The `is_early_close` flag covers
these cases when pykrx reports them.

**Holiday source:** `pykrx.market.get_market_holidays(year)` — returns a
`pandas.DataFrame` of closed dates for the given year. Called once per year,
cached in Redis.

**KRX `trade_date` rule:** The KRX trading date is the **Seoul calendar date**.
A trade executed at 09:15 KST March 15 belongs to March 15 (KRX).

### 4.2 NYSE / NASDAQ

**Trading timezone:** `America/New_York` (ET = UTC-5 in EST, UTC-4 in EDT; DST
handled automatically by `pytz.timezone("America/New_York")`).

NYSE and NASDAQ share the same holiday calendar (both regulated by the same US
holiday schedule). NASDAQ is modelled as a separate `Market` enum value to allow
future rule divergence (e.g., if NASDAQ ever extends hours or adds a market-specific
closure), but today both map to the same `NYSEDataSource`.

| Session | Local ET time | Notes |
|---|---|---|
| `PRE_MARKET` | 04:00 – 09:30 | Extended hours; informational in current system |
| `REGULAR` | 09:30 – 16:00 | Normal session (or 09:30 – 13:00 on early-close days) |
| `AFTER_HOURS` | 16:00 – 20:00 | Extended hours; cancels allowed; new entries blocked by default |
| `CLOSED` | 20:00 – 04:00 (next day) | All order placement blocked |

**Half-day (early close):** Market closes at 13:00 ET. Regular session window
becomes 09:30–13:00 ET. AFTER_HOURS starts at 13:00 ET.

Typical early-close days:
- Day before Independence Day (when July 3 is a weekday)
- Day after Thanksgiving
- Christmas Eve (Dec 24, when a weekday)
- Day before New Year's Day (when Dec 31 is a weekday, occasionally)

**Holiday source:** `pandas_market_calendars.get_calendar("NYSE")`. Returns a
`Schedule` object with `market_open` / `market_close` per trading day. Queried
once per year, cached in Redis.

**NYSE/NASDAQ `trade_date` rule:** The trading date is the **Eastern calendar date**.
A trade at 23:45 KST March 14 (= 14:45 ET March 14) belongs to March 14 (NYSE).
A trade at 00:30 KST March 15 (= 15:30 ET March 14) belongs to March 14 (NYSE).

---

## 5. Session Detector

### 5.1 Algorithm

```
get_session(market, dt_utc):

1. Convert dt_utc to local market time using TimezoneHandler.to_market_local(market, dt_utc)
2. Extract local_date = local_dt.date()
3. Lookup TradingPermission cache entry for (market, local_date):
   a. If is_trading_day == False → return CLOSED
4. Get SessionWindow for (market, local_date):
   a. If local_dt < window.open_utc → return PRE_MARKET
   b. If window.open_utc ≤ local_dt < window.close_utc → return REGULAR
   c. If window.close_utc ≤ local_dt < after_hours_close_utc → return AFTER_HOURS
   d. Otherwise → return CLOSED
```

**AFTER_HOURS close time (not in SessionWindow; fixed per market):**
- KRX: 18:00 KST
- NYSE/NASDAQ: 20:00 ET

### 5.2 Edge Cases

**KRX US-session overlap:** KRX is closed during the US regular session
(22:30–05:00 KST). `get_session(KRX, dt_utc)` during those hours returns
`CLOSED`. `get_session(NYSE, dt_utc)` during those same hours returns `REGULAR`.
These are two separate `Market` values; no cross-market confusion is possible.

**Midnight KST during US session:** `trade_date(NYSE, midnight_kst)` returns the
Eastern date, which is still the same calendar day as the US session start.
Example: March 15 00:30 KST = March 14 15:30 ET → `trade_date` = March 14.
This is distinct from `trade_date(KRX, midnight_kst)` = March 15.

**NYSE holiday on a KRX trading day:** The two calendars are fully independent.
A US Thanksgiving does not affect KRX. `is_trading_day(KRX, thanksgiving)` may
return `True`; `is_trading_day(NYSE, thanksgiving)` returns `False`.

---

## 6. Trading Permission Check

### 6.1 Permission Engine

```
check_order_permission(market, dt_utc) → TradingPermission:

1. holiday = get_holiday(market, trade_date(market, dt_utc))
   a. If holiday and not holiday.is_early_close:
      → return TradingPermission(
             allowed=False, session_type=CLOSED,
             block_reason=HOLIDAY, holiday_name=holiday.name,
             next_open_utc=next_session_open(market, dt_utc))

2. session = get_session(market, dt_utc)
   a. If session == REGULAR:
      If holiday.is_early_close and dt_utc >= holiday.early_close_utc:
         → return TradingPermission(
                allowed=False, session_type=AFTER_HOURS,
                block_reason=HALF_DAY_ENDED, holiday_name=holiday.name,
                next_open_utc=next_session_open(market, dt_utc))
      Else:
         → return TradingPermission(allowed=True, session_type=REGULAR)
   b. If session in {PRE_MARKET, AFTER_HOURS, CLOSED}:
      → return TradingPermission(
             allowed=False, session_type=session,
             block_reason=WRONG_SESSION,
             next_open_utc=next_session_open(market, dt_utc))

3. On any CalendarDataError (all sources failed):
   → return TradingPermission(
          allowed=False, session_type=CLOSED,
          block_reason=CACHE_FAILURE,
          next_open_utc=None)
```

### 6.2 Order Blocking Rules

| Condition | `allowed` | `block_reason` | Circuit breaker incremented? |
|---|---|---|---|
| Holiday (full day) | `False` | `HOLIDAY` | **No** — `MarketClosedError` not `RuntimeError` |
| Early close window exceeded | `False` | `HALF_DAY_ENDED` | **No** |
| PRE_MARKET | `False` | `WRONG_SESSION` | **No** |
| AFTER_HOURS | `False` | `WRONG_SESSION` | **No** |
| CLOSED (no session) | `False` | `WRONG_SESSION` | **No** |
| All calendar sources failed | `False` | `CACHE_FAILURE` | **No** |
| REGULAR session | `True` | `None` | N/A |

### 6.3 `assert_tradeable()` Contract

`CalendarService.assert_tradeable(market, dt_utc)`:
- Calls `check_order_permission(market, dt_utc)`
- If `allowed == True`: returns `None` silently
- If `allowed == False`: raises `MarketClosedError(market, session_type, block_reason, next_open_utc)`

This is the single call-site API for broker and API layers. They call
`assert_tradeable()` and catch `MarketClosedError`; no if/else on the returned
`TradingPermission` object is needed at the call site.

### 6.4 Session Permission Flags (configurable)

Two environment variables control whether non-REGULAR sessions are soft-blocked
or hard-blocked. Both default to `false` (blocked).

| Env var | Default | Effect |
|---|---|---|
| `ALLOW_PREMARKET_ORDERS` | `false` | If `true`: PRE_MARKET → `allowed=True`; still flagged in audit log |
| `ALLOW_AFTERHOURS_ORDERS` | `false` | If `true`: AFTER_HOURS → `allowed=True`; still flagged in audit log |

These flags do NOT affect holiday or early-close blocking — those are always
hard-blocked regardless of env vars.

---

## 7. Timezone Handler

### 7.1 Design

`TimezoneHandler` is a stateless utility class (all classmethods / static
methods). No instances needed.

```python
class TimezoneHandler:
    KST = pytz.timezone("Asia/Seoul")          # UTC+9, no DST
    ET  = pytz.timezone("America/New_York")    # UTC-5 / UTC-4, DST auto

    @classmethod
    def to_kst(cls, dt_utc: datetime) -> datetime
    # Convert UTC-aware datetime to KST-aware datetime.

    @classmethod
    def to_et(cls, dt_utc: datetime) -> datetime
    # Convert UTC-aware datetime to ET-aware datetime (DST handled by pytz).

    @classmethod
    def to_utc(cls, dt_local: datetime) -> datetime
    # Convert a tz-aware local datetime to UTC.
    # dt_local must already carry tzinfo; raises ValueError if naive.

    @classmethod
    def to_market_local(cls, market: Market, dt_utc: datetime) -> datetime
    # Dispatches to to_kst() for KRX, to_et() for NYSE/NASDAQ.

    @classmethod
    def trade_date(cls, market: Market, dt_utc: datetime) -> date
    # Returns the "trading date" for a given market and UTC datetime.
    # KRX  → Seoul calendar date of the converted local datetime.
    # NYSE/NASDAQ → Eastern calendar date.

    @classmethod
    def market_open_utc(cls, market: Market, local_date: date, local_open: time) -> datetime
    # Construct a UTC datetime from a local date + time for the given market.
    # Handles DST: pytz.localize() with is_dst=False (conservative on ambiguous times).

    @classmethod
    def market_close_utc(cls, market: Market, local_date: date, local_close: time) -> datetime
    # Same as market_open_utc for close times.
    # For half-day close (13:00 ET), still uses pytz.localize() correctly.
```

### 7.2 DST Rules

**KST:** No DST. `Asia/Seoul` is always UTC+9. `TimezoneHandler.to_kst()` is a
simple arithmetic conversion with no ambiguity.

**ET (Eastern Time):** Uses `pytz.timezone("America/New_York")` with
`dt_utc.astimezone(cls.ET)` for UTC-to-local conversions. This automatically
applies the correct offset (EST = UTC-5, EDT = UTC-4) based on the date.

For local-to-UTC (`market_open_utc`, `market_close_utc`), `pytz.localize()` is
used with `is_dst=False` for times that are ambiguous on fall-back Sunday
(02:00 ET clocks back to 01:00 ET). `is_dst=False` picks the POST-transition
(EST) interpretation, which is the conservative choice — the window is slightly
earlier in UTC, meaning the session is treated as starting/ending fractionally
earlier. The practical impact on a 09:30 ET open is zero since 09:30 is never
in the ambiguous window.

**Spring-forward gap (02:00 → 03:00 ET):** No market-relevant times fall in
this gap. NYSE opens at 09:30, which is well clear of the transition window.

### 7.3 Comparison Rule

All datetime comparisons inside `CalendarService` use UTC-aware datetimes.
Local (KST or ET) times are only produced for:
- Logging / human-readable messages
- Determining `trade_date()` (which requires local calendar date)
- Computing session window start/end from local market times

No naive datetimes are accepted or produced by any public method of
`CalendarService` or `TimezoneHandler`. Methods that receive a naive datetime
raise `ValueError` immediately.

---

## 8. Calendar Cache

### 8.1 Two-Layer Architecture

```
CalendarCache (L1 in-process) → CalendarCache (L2 Redis) → CalendarDataSource
```

| Layer | Store | TTL | Capacity | Notes |
|---|---|---|---|---|
| L1 | Python `dict` (LRU-capped) | 1 hour (checked on read) | 200 entries | Thread-safe via `threading.Lock`; survives Redis outage |
| L2 | Redis | 24 hours | Unbounded (per-key TTL) | Shared across processes; primary durable cache |

### 8.2 Cache Key Schema

```
calendar:{market}:{date_iso}
```

Examples:
```
calendar:KRX:2026-09-14     ← KRX Chuseok holiday
calendar:NYSE:2026-11-26    ← Thanksgiving (full close)
calendar:NYSE:2026-11-27    ← Day after Thanksgiving (early close 13:00 ET)
```

**Value (JSON):**
```json
{
  "is_trading_day":   true,
  "holiday_name":     null,
  "is_early_close":   false,
  "early_close_kst":  null,
  "early_close_et":   null,
  "session_open_utc": "2026-11-25T14:30:00+00:00",
  "session_close_utc": "2026-11-25T21:00:00+00:00"
}
```

Fields:
- `is_trading_day`: `false` on full holidays and weekends
- `holiday_name`: localized name if holiday; `null` otherwise
- `is_early_close`: `true` on half-days
- `early_close_et` / `early_close_kst`: ISO local time string on half-days; `null` otherwise
- `session_open_utc` / `session_close_utc`: UTC ISO strings for REGULAR session; `null` if not a trading day

### 8.3 Cache Warm-up

`CalendarCache.warm_up(markets, days_ahead=14)` is called on process startup
(after DB connectivity confirmed, before SAFE_MODE.enable()). It pre-fetches
calendar entries for today + 14 calendar days for all markets.

If Redis is unavailable during warm-up, warm-up is skipped (logged as WARNING)
and the L1 cache starts empty. The first live request will trigger a data-source
query and populate both L1 and L2 on success.

### 8.4 Cache Invalidation

Cache entries are never manually invalidated. TTL-based expiry is the only
mechanism:
- L2 Redis TTL: 24 hours (set on write, not on read)
- L1 in-process: checked on every read; entries older than 1 hour are evicted

Re-fetch on expiry always goes to the data source (pykrx / pandas_market_calendars
/ static fallback). The fresh result is written to both L1 and L2.

---

## 9. Data Sources

### 9.1 `CalendarDataSource` Abstract Base

```python
class CalendarDataSource(ABC):
    @abstractmethod
    def get_holidays(self, market: Market, year: int) -> list[MarketHoliday]:
        """
        Return all holidays (full-close and early-close) for the market in the given year.
        Raises CalendarDataError on failure.
        """

    @abstractmethod
    def get_session_window(self, market: Market, d: date) -> SessionWindow | None:
        """
        Return the REGULAR session window for a single date, or None if closed.
        Raises CalendarDataError on failure.
        """
```

### 9.2 `KRXDataSource` (primary for KRX)

**Library:** `pykrx` (already in `requirements.txt`).

```python
class KRXDataSource(CalendarDataSource):
    def get_holidays(self, market: Market, year: int) -> list[MarketHoliday]:
        from pykrx import market as krx_market
        holidays_df = krx_market.get_market_holidays(year)
        # holidays_df: index = date, columns = ['장휴구분', '시장구분']
        # Filter to closed days only; map to MarketHoliday(is_early_close=False)
        # Raises CalendarDataError if pykrx raises any exception
```

`pykrx.market.get_market_holidays()` returns the official KRX published list.
It does NOT separate "official holiday" from "exchange-announced closure" — both
appear as closed days. The `name` field is populated from the '장휴구분' column
if present, defaulting to "KRX 휴장" when absent.

**Session window for KRX:** REGULAR session is always 09:00–15:30 KST on trading
days (no variable close like NYSE). `get_session_window()` returns `None` (closed)
or a fixed 09:00–15:30 KST window converted to UTC.

### 9.3 `NYSEDataSource` (primary for NYSE and NASDAQ)

**Library:** `pandas_market_calendars` (add to `requirements.txt`).

```python
class NYSEDataSource(CalendarDataSource):
    def get_holidays(self, market: Market, year: int) -> list[MarketHoliday]:
        import pandas_market_calendars as mcal
        cal = mcal.get_calendar("NYSE")
        schedule = cal.schedule(
            start_date=f"{year}-01-01",
            end_date=f"{year}-12-31",
        )
        # Closed days (not in schedule) → MarketHoliday(is_early_close=False)
        # Days where market_close < 21:00 UTC → MarketHoliday(is_early_close=True, early_close_time=...)

    def get_session_window(self, market: Market, d: date) -> SessionWindow | None:
        import pandas_market_calendars as mcal
        cal = mcal.get_calendar("NYSE")
        schedule = cal.schedule(
            start_date=d.isoformat(),
            end_date=d.isoformat(),
        )
        if schedule.empty:
            return None
        row = schedule.iloc[0]
        return SessionWindow(
            market=market,
            date=d,
            open_utc=row["market_open"].to_pydatetime(),
            close_utc=row["market_close"].to_pydatetime(),
        )
```

`pandas_market_calendars` returns market_open=14:30 UTC (winter, EST) or 13:30 UTC
(summer, EDT) and market_close=21:00 UTC (normal) or 18:00 UTC (early close). The
library handles DST and early-close days natively.

### 9.4 `StaticFallbackDataSource` (last resort)

A hardcoded `dict[Market, dict[int, list[MarketHoliday]]]` compiled for the
current and next calendar year. Maintained manually each year. Used only when both
pykrx and pandas_market_calendars raise `CalendarDataError`.

The static list does NOT need to be exhaustive — it covers the most impactful
holidays (US: all 9 full-close days; KR: 설날, 추석, 국경일). Half-days are NOT
included in the static fallback (too complex to maintain); on early-close days when
the fallback is active, the system defaults to treating them as full-close days
(fail-closed).

---

## 10. Failure and Fallback Policies

### 10.1 Source Failure Policy

```
Query CalendarDataSource:

Attempt 1: Primary source (KRXDataSource or NYSEDataSource)
   → Success: write to L2 Redis, L1 memory, return result
   → CalendarDataError: log WARNING, try next

Attempt 2: StaticFallbackDataSource
   → Success: write to L1 memory only (NOT L2 Redis — don't cache approximate data persistently)
   → Note: log INFO that static fallback was used
   → CalendarDataError: log ERROR, proceed to fail-closed

All sources failed:
   → return TradingPermission(allowed=False, block_reason=CACHE_FAILURE, next_open_utc=None)
   → Do NOT raise CalendarDataError to caller; eat the exception and return CLOSED
```

**Rationale for fail-closed:** A system that defaults to "market open" when it
cannot verify the calendar could place orders on a closed market, burning
rate-limit budget and producing circuit-breaker trips. A system that defaults to
"market closed" when uncertain costs at most a few missed signals — recoverable in
minutes once calendar sources recover.

### 10.2 Cache Failure Policy

| L1 state | L2 (Redis) state | Action |
|---|---|---|
| Hit (fresh) | Any | Return L1 result immediately |
| Miss | Hit | Return L2 result, populate L1 |
| Miss | Miss | Query data source; on success populate both layers |
| Miss | Redis down | Query data source directly; populate L1 only |
| Miss | Redis down + source fails | Fail-closed (CACHE_FAILURE) |

Redis unavailability (`ConnectionError`, `TimeoutError`) is logged as WARNING on
first occurrence and then suppressed (not logged again for 5 minutes) to avoid
alert fatigue. The L1 in-process cache absorbs the load during a Redis outage.

### 10.3 Holiday Fallback Policy

When the data source cannot determine whether a day is a holiday:
- **Do not assume it is a trading day.** Default to CLOSED.
- Log `CRITICAL` if this state persists beyond 24 hours (suggests data source
  outage, not transient error).
- Do not alert for single-request failures (transient network issue at startup).

### 10.4 Stale Cache Policy

L2 Redis entries older than 24 hours are naturally expired by TTL. There is no
explicit staleness check — TTL expiry is the staleness mechanism.

L1 in-process entries carry a `fetched_at` timestamp. Reads check
`now() - fetched_at > 3600s` (1 hour) and treat the entry as expired (triggers
L2/source fetch). This prevents a long-lived worker process from serving day-old
calendar data.

---

## 11. State Diagram — Session Lifecycle

```
                               ┌─────────────────────────────────┐
                               │  CalendarService startup        │
                               │  warm_up(all_markets, days=14)  │
                               └──────────────┬──────────────────┘
                                              │
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                           KRX            NYSE           NASDAQ
                              │
          ┌───────────────────┴────────────────────┐
          ▼                                         ▼
   is_trading_day == False                  is_trading_day == True
   (holiday or weekend)                            │
          │                        ┌───────────────┼────────────────┐
       CLOSED                      ▼               ▼                ▼
     (all day)                PRE_MARKET        REGULAR       AFTER_HOURS
                             (08:00-09:00)   (09:00-15:30)  (15:30-18:00)
                                                   │                │
                                         ┌─────────┴──────┐         │
                                         ▼                ▼         ▼
                                    Normal day       Half-day     CLOSED
                                  (close 15:30)    (close early)  (18:00+)
```

---

## 12. `CalendarService` Public API

```python
class CalendarService:
    """
    Single authoritative source of market calendar truth.
    All methods accept UTC-aware datetimes.
    All methods are thread-safe.
    """

    def __init__(
        self,
        redis_client=None,       # optional; L2 cache disabled if None
        data_source: CalendarDataSource | None = None,  # defaults to appropriate primary
    ): ...

    # ── Core queries ─────────────────────────────────────────────────────────

    def is_trading_day(self, market: Market, d: date) -> bool:
        """True if market holds a REGULAR session on date d."""

    def get_session(self, market: Market, dt_utc: datetime) -> SessionType:
        """Return the current session type for market at the given UTC datetime."""

    def get_session_window(self, market: Market, d: date) -> SessionWindow | None:
        """
        Return the REGULAR session window (UTC open + close) for market on date d.
        Returns None if the market is closed that day.
        Half-day: close_utc reflects the early close time, not the standard 16:00 ET.
        """

    def get_holiday(self, market: Market, d: date) -> MarketHoliday | None:
        """Return holiday info for date d, or None if it is a normal trading day."""

    def is_early_close(self, market: Market, d: date) -> bool:
        """True if market has an early close on date d."""

    # ── Permission check ─────────────────────────────────────────────────────

    def check_order_permission(
        self,
        market: Market,
        dt_utc: datetime,
    ) -> TradingPermission:
        """
        Full permission decision: holiday + session + half-day checks combined.
        Never raises; always returns a TradingPermission (even on source failure).
        """

    def assert_tradeable(self, market: Market, dt_utc: datetime) -> None:
        """
        Raises MarketClosedError if an order is not permitted at dt_utc.
        Primary call-site API for broker and API layers.
        """

    # ── Navigation ───────────────────────────────────────────────────────────

    def next_session_open(
        self,
        market: Market,
        after_utc: datetime,
    ) -> datetime:
        """
        Return the UTC datetime of the next REGULAR session open after after_utc.
        Scans forward up to 30 calendar days.
        Returns None only if 30+ consecutive non-trading days are found (data error).
        """

    def prev_trading_day(self, market: Market, before: date) -> date:
        """Return the most recent trading day strictly before `before`."""

    # ── Timezone utilities ───────────────────────────────────────────────────

    def trade_date(self, market: Market, dt_utc: datetime) -> date:
        """
        Return the 'trading date' for an event at dt_utc in the given market.
        KRX  → Seoul calendar date
        NYSE / NASDAQ → Eastern calendar date
        Use this everywhere date.today() or datetime.utcnow().date() is currently used.
        """

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def warm_up(self, markets: list[Market], days_ahead: int = 14) -> None:
        """Pre-populate cache for today + days_ahead. Called at process startup."""

    def get_singleton() -> "CalendarService":
        """Module-level singleton. Use this instead of constructing directly."""
```

---

## 13. Integration Points

### 13.1 Scheduler (`backend/worker/scheduler.py`)

**G1 — Pre-publish gate (highest-value insertion):**

```python
# In _trigger_kr_session():
def _trigger_kr_session():
    svc = CalendarService.get_singleton()
    if not svc.is_trading_day(Market.KRX, svc.trade_date(Market.KRX, datetime.now(UTC))):
        logger.info("오늘 KRX 휴장 — 세션 신호 생략")
        return
    _publish_session_signal("session:kr_open")

# In _trigger_us_session():
def _trigger_us_session():
    svc = CalendarService.get_singleton()
    if not svc.is_trading_day(Market.NYSE, svc.trade_date(Market.NYSE, datetime.now(UTC))):
        logger.info("오늘 NYSE 휴장 — 세션 신호 생략")
        return
    _publish_session_signal("session:us_open")
```

**G7 — Reconciler gate:**

```python
def _periodic_reconcile():
    svc = CalendarService.get_singleton()
    now_utc = datetime.now(UTC)
    kr_open = svc.is_trading_day(Market.KRX, svc.trade_date(Market.KRX, now_utc))
    us_open = svc.is_trading_day(Market.NYSE, svc.trade_date(Market.NYSE, now_utc))
    if not kr_open and not us_open:
        logger.debug("양 시장 휴장 — 조정 건너뜀")
        return
    ...
```

**G8 — Risk reset timing:** Move `risk_reset` cron from `00:01 KST` to
`06:01 KST`. No `CalendarService` call needed for the reschedule itself, but the
new time ensures it fires after the US session closes (~05:00 KST at latest).

### 13.2 Broker (`backend/brokers/kis.py`)

**G3 — Order placement gate:**

```python
def place_order(self, symbol, side, qty, price, order_type="limit") -> Order:
    market = Market.KRX if self._is_kr(symbol) else Market.NYSE
    CalendarService.get_singleton().assert_tradeable(market, datetime.now(UTC))
    # ... existing order placement code ...
```

`assert_tradeable()` raises `MarketClosedError`; `place_order()`'s caller
(`StrategyBase.buy()`/`sell()`) does NOT catch it — it propagates to
`_execute_buy()`/`_execute_sell()` in `indicator/strategy.py`, which catches
`MarketClosedError` and logs it. The `ConsecutiveFailureBreaker` is NOT
incremented because `MarketClosedError` is not a `RuntimeError`.

### 13.3 `_live_trade_allowed()` (`backend/strategy/base.py`)

**G3 (secondary) — Strategy-level gate:**

```python
def _live_trade_allowed(broker, name, symbol, side):
    ...
    # After existing SAFE_MODE and ENABLE_LIVE_TRADING gates:
    # Gate 3: session gate
    if getattr(broker, "is_live", True):
        market = Market.KRX if _is_kr(symbol) else Market.NYSE
        perm = CalendarService.get_singleton().check_order_permission(market, datetime.now(UTC))
        if not perm.allowed:
            logger.warning("[%s] 시장 미개장 — %s %s 차단 (%s, 다음개장: %s)",
                           name, side, symbol, perm.block_reason, perm.next_open_utc)
            return False, Order(id="", ..., status=OrderStatus.REJECTED)
    return True, None
```

### 13.4 Quick-trade API (`api/routers/quick_trade.py`)

**G4 — API layer gate:**

```python
@router.post("/place-order")
def place_order(body: PlaceOrderRequest, ...):
    market = Market.KRX if body.market.lower() == "kr" else Market.NYSE
    perm = CalendarService.get_singleton().check_order_permission(market, datetime.utcnow().replace(tzinfo=UTC))
    if not perm.allowed:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "market_closed",
                "reason": perm.block_reason,
                "holiday": perm.holiday_name,
                "next_open": perm.next_open_utc.isoformat() if perm.next_open_utc else None,
            }
        )
    ...
```

### 13.5 KIS Client (`kis_adapter/client.py`)

**G5 — Error code parsing:**

```python
def post(self, path, tr_id, body, hashkey=None):
    ...
    data = response.json()
    if data.get("rt_cd") != "0":
        msg = data.get("msg1", "")
        code = data.get("msg_cd", "")
        if code in ("-90", "-91", "-100") or "거래가능시간" in msg or "매매시간" in msg:
            from backend.data.calendar import MarketClosedError, Market, SessionType, BlockReason
            raise MarketClosedError(
                market=Market.KRX,   # or NYSE; determined by caller context
                session=SessionType.CLOSED,
                reason=BlockReason.WRONG_SESSION,
            )
        raise RuntimeError(f"KIS API error: {msg}")
```

### 13.6 Risk Engine (`backend/quant/risk/engine.py`)

**G9 — Date normalization:**

```python
# In PersistentLossTracker._write_db():
def _write_db(self):
    from backend.data.calendar import CalendarService, Market
    # Replace: today = date.today()
    today = CalendarService.get_singleton().trade_date(Market.KRX, datetime.now(UTC))
    ...
```

**In `runner.py:_persist_order()`:**

```python
# Replace: trade_date=datetime.utcnow().date()
from backend.data.calendar import CalendarService, Market
market = Market.KRX if _is_kr(order.symbol) else Market.NYSE
trade_date = CalendarService.get_singleton().trade_date(market, datetime.now(UTC))
```

---

## 14. `backend/data/calendar.py` — Module Structure

```
backend/
└── data/
    ├── __init__.py
    └── calendar.py          ← this file

calendar.py sections (in order):

1. Imports
   - stdlib: abc, datetime, date, time, timedelta, enum, dataclasses, threading, functools, logging
   - pytz (already in requirements)
   - TYPE_CHECKING imports

2. Public enums
   - Market
   - SessionType
   - BlockReason

3. Exceptions
   - MarketClosedError(Exception)
   - CalendarDataError(Exception)    [internal only]

4. Dataclasses
   - MarketHoliday
   - SessionWindow
   - TradingPermission

5. TimezoneHandler (static methods)
   - KST, ET timezone objects
   - to_kst, to_et, to_utc, to_market_local
   - trade_date
   - market_open_utc, market_close_utc

6. CalendarDataSource (ABC)
   - get_holidays()
   - get_session_window()

7. KRXDataSource(CalendarDataSource)
   - pykrx.market integration

8. NYSEDataSource(CalendarDataSource)
   - pandas_market_calendars integration

9. StaticFallbackDataSource(CalendarDataSource)
   - hardcoded dict for current + next year

10. CalendarCache
    - L1 (in-process dict + lock)
    - L2 (Redis)
    - get(), set(), warm_up()

11. PermissionEngine (internal helper)
    - check_order_permission() logic

12. CalendarService
    - constructor
    - all public methods listed in §12
    - _singleton: CalendarService | None = None  (module-level)

13. Module-level accessor
    - get_calendar_service() -> CalendarService
    - configure_calendar_service(redis_client, ...) — called at process startup
```

No external code should instantiate `CalendarService` directly.
`get_calendar_service()` is the single entry point.

---

## 15. New Code Requirements Summary

| File | Action |
|---|---|
| `backend/data/__init__.py` | **NEW** — empty init |
| `backend/data/calendar.py` | **NEW** — full `CalendarService` implementation |
| `requirements.txt` | Add `pandas_market_calendars` |
| `backend/worker/scheduler.py` | Add holiday gate (G1) to `_trigger_kr/us_session()`; move `risk_reset` to `06:01 KST`; add G7 to `_periodic_reconcile()` |
| `backend/worker/runner.py` | Wire `session:kr/us_close` channels; invoke `on_market_close()`; use `trade_date()` |
| `backend/brokers/kis.py` | Add `assert_tradeable()` gate (G3) in `place_order()` |
| `backend/strategy/base.py` | Add session gate (G3 secondary) in `_live_trade_allowed()` |
| `kis_adapter/client.py` | Parse rt_cd for market-closed codes (G5); raise `MarketClosedError` |
| `backend/execution/circuit_breaker.py` | `record_failure(exc=None)` — skip for `MarketClosedError` |
| `backend/quant/risk/engine.py` | Replace `date.today()` with `CalendarService.trade_date()` in `_write_db()` |
| `backend/worker/runner.py` | Replace `datetime.utcnow().date()` with `CalendarService.trade_date()` in `_persist_order()` |
| `api/routers/quick_trade.py` | Add session gate (G4); return HTTP 422 on `MarketClosedError` |
| `backend/api/server.py` | Add `GET /api/market-status` endpoint |
| `bot/scheduler.py` | Fix US session cron to `CronTrigger(hour=9, minute=30, timezone="America/New_York")` |
| `backend/worker/recovery.py` | Call `CalendarService.warm_up()` during startup Phase 1 |

---

## 16. Verification

```bash
# CalendarService: KRX holidays correctly detected
pytest tests/data/test_calendar.py -v -k krx_is_trading_day

# NYSE full-close holidays
pytest tests/data/test_calendar.py -v -k nyse_holiday_closed

# NYSE early-close half-day window correct
pytest tests/data/test_calendar.py -v -k nyse_half_day_close_time

# Scheduler does not publish session signal on a KRX holiday
pytest tests/worker/test_scheduler.py -v -k no_kr_signal_on_holiday

# Scheduler does not publish session signal on a NYSE holiday
pytest tests/worker/test_scheduler.py -v -k no_us_signal_on_holiday

# assert_tradeable() raises MarketClosedError on holiday
pytest tests/data/test_calendar.py -v -k assert_tradeable_holiday

# assert_tradeable() raises MarketClosedError in CLOSED session
pytest tests/data/test_calendar.py -v -k assert_tradeable_closed_session

# MarketClosedError does NOT increment ConsecutiveFailureBreaker
pytest tests/execution/test_circuit_breaker.py -v -k market_closed_not_counted

# KIS rt_cd=-90 raises MarketClosedError (not RuntimeError)
pytest tests/brokers/test_kis_client.py -v -k rt_cd_90_is_market_closed

# trade_date(KRX) returns Seoul date; trade_date(NYSE) returns Eastern date
pytest tests/data/test_calendar.py -v -k trade_date_timezone

# trade_date consistent across midnight KST (00:00-09:00 KST window)
pytest tests/data/test_calendar.py -v -k trade_date_midnight_kst

# check_order_permission returns allowed=False on cache failure (fail-closed)
pytest tests/data/test_calendar.py -v -k permission_fails_closed_on_cache_error

# warm_up pre-populates cache without raising on Redis unavailability
pytest tests/data/test_calendar.py -v -k warm_up_tolerates_redis_down

# DST spring-forward: is_session_active returns REGULAR at 09:30 ET
pytest tests/data/test_calendar.py -v -k dst_spring_forward_regular_session

# DST fall-back: is_session_active returns REGULAR at 09:30 ET
pytest tests/data/test_calendar.py -v -k dst_fall_back_regular_session

# risk_reset job fires at 06:01 KST (after US session ends)
pytest tests/worker/test_scheduler.py -v -k risk_reset_after_us_close

# quick_trade API returns 422 with next_open field when market is closed
pytest tests/api/test_quick_trade.py -v -k market_closed_422_response
```
