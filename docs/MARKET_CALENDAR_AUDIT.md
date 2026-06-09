# Market Calendar & Session Handling — Audit Report (TASK 3-1A)

## 1. Purpose and Scope

This document audits every calendar- and timezone-related assumption currently baked
into the trading platform, classifies the resulting risks, and identifies the exact
insertion points where a future `CalendarService` would eliminate them.

**In scope:** scheduler, session trigger path, broker/order placement, API layer,
mobile UI, risk engine date handling, reconciler, order poller, market data staleness.

**Not in scope:** the `CalendarService` implementation itself (TASK 3-1B).

**No code changes are made in this task.**

---

## 2. Current Session / Time Handling Structure

### 2.1 Scheduler Layer (`backend/worker/scheduler.py`)

| Job ID | Cron expression | Timezone | Holiday gate |
|---|---|---|---|
| `kr_session` | `mon-fri 09:05` | `Asia/Seoul` | **None** |
| `us_session` | `mon-fri 09:30` | `America/New_York` | **None** |
| `risk_reset` | `daily 00:01` | `Asia/Seoul` | N/A |
| `equity_snapshot` | `daily 23:50` | `Asia/Seoul` | N/A |
| `periodic_reconcile` | `mon-fri 09-15,22-23 */30` | `Asia/Seoul` | **None** |

APScheduler correctly resolves `America/New_York` DST — `us_session` fires at
09:30 ET year-round. This is the **one correct timezone implementation** in the
codebase. Everything else ignores market reality.

`bot/scheduler.py:19` (legacy bot layer, still in-tree) hardcodes `22:35 KST` for
the US session (a fixed KST literal, not an ET time). In winter (EST, UTC-5) NYSE
opens at 23:30 KST — the bot fires **55 minutes before market open**. This file is
superseded by the backend scheduler in practice but can still be run independently.

### 2.2 Session Signal Path (no state validation anywhere)

```
APScheduler fires _trigger_kr/us_session()   scheduler.py:162,167
    │  NO is_trading_day() check
    │  NO is_session_active() check
    ▼
_publish_session_signal("session:kr/us_open") scheduler.py:135–159
    │  Writes DB Command + publishes Redis Pub/Sub
    ▼
StrategyWorker._handle_market_open()          runner.py:316
    │  5-min monotonic dedup gate (same-process restart guard)
    │  NO calendar check
    ▼
WorkerSession.trigger_market_open()           runner.py:106
    ▼
IndicatorStrategy.on_market_open()
    ▼
_scan_and_trade() → _execute_buy() → KISBroker.place_order()
    │  NO session-active check
    ▼
KISClient.post()                              kis_adapter/client.py:56
    │  All rt_cd != "0" errors raised as generic RuntimeError
    │  Market-closed codes (-90, -91, -100) indistinguishable from network errors
    ▼
ConsecutiveFailureBreaker.record_failure()
    │  Holiday rejection counts as broker failure → trips after 3 rejections
    │  30-min cooldown → retries → market still closed → loops all day
```

**No gate at any point checks "is today actually a trading day".**

There are also no `session:kr_close` / `session:us_close` channels.
`StrategyBase.on_market_close()` (`base.py:70`) is defined but never called;
strategies have no mechanism to react to market close.

### 2.3 Order Placement Gates (current)

`StrategyBase._live_trade_allowed()` (`base.py:14–41`):
- Gate 1: `SAFE_MODE.can_trade` (startup recovery / kill-switch)
- Gate 2: `ENABLE_LIVE_TRADING` env var
- **Absent:** market-hours check, holiday check, session-active check

`quick_trade.py:place_order()` (`api/routers/quick_trade.py:123–164`):
- Zero session validation before calling broker
- Orders accepted 24 / 7 / 365 through the API

Mobile `quick-trade/index.vue:100–105`:
- Zero market-hours gate; submits orders regardless of session state

`BrokerAdapter` ABC (`brokers/base.py`): no `is_market_open()` abstract method.

### 2.4 KIS Error Code Parsing

`kis_adapter/client.py:47–48, 68–69`:
```python
if data.get("rt_cd") != "0":
    raise RuntimeError(f"KIS API error: {data.get('msg1')}")
```

Known KIS market-state error codes that are NOT handled:

| Code | Korean message | Meaning |
|---|---|---|
| `-90` | 시간외거래 불가 | After-hours trading not allowed |
| `-91` | 매매시간이 아님 | Not within trading hours |
| `-100` | 거래가능시간이 아닙니다 | Not a tradeable time |
| `E0001` | 시스템점검 | System maintenance |

All four are treated identically to network errors and increment the circuit
breaker's failure counter.

### 2.5 Date Handling — Three Conflicting Clocks

| Location | Expression | Timezone | Used for |
|---|---|---|---|
| `engine.py:26` `_seoul_today()` | `datetime.now(UTC+9).date()` | UTC+9 hardcoded | In-memory `LossTracker.trade_date`, `week_start` |
| `engine.py:470` `_write_db()` | `date.today()` | Container TZ (UTC) | `DailyRiskState` DB primary key |
| `runner.py:551` | `datetime.utcnow().date()` | UTC | `DBOrder.trade_date` |
| `runner.py:520` idempotency key | `_date.today()` | Container TZ (UTC) | Idempotency key date component |
| `scheduler.py:142` payload | `datetime.utcnow().isoformat()` | UTC | Session signal timestamp in DB |
| `recovery.py:385` stale cutoff | `datetime.utcnow() - timedelta(hours=N)` | UTC | Stale order detection |

**The critical desync:** At 00:01 KST when `_reset_daily_risk()` fires, the UTC
clock reads `(Seoul date - 1 day)` — 00:01 KST = 15:01 UTC of the previous
calendar day. So `date.today()` returns yesterday's UTC date, but the in-memory
`LossTracker` already advanced its `_seoul_today()` to the new Seoul day. The reset
reads and resets the wrong `DailyRiskState` row.

More critically: **the US session spans midnight KST** (22:30–05:00 KST).
`_reset_daily_risk()` fires at 00:01 KST — 1.5 hours into the US session.
Daily PnL is zeroed mid-session. Losses between 22:30 KST and 00:01 KST are
erased before the 3% daily kill-switch can trigger on them.

### 2.6 Calendar Libraries Available

`requirements.txt`:
- `pytz==2024.1` — timezone conversion only; no exchange calendar
- `pykrx` — installed; `pykrx.market.get_market_holidays(year)` exists but is
  **never called** (only `pykrx.stock` is imported for OHLCV data)
- **Not present:** `pandas_market_calendars`, `exchange_calendars`, `trading_calendars`

### 2.7 DB Schema — No Calendar Tables

`backend/database/models.py`:
- No `MarketCalendar` table (US / KR trading days)
- No `HolidayCalendar` table (official holidays + observances)
- No `TradingSession` table (open/close times per market per day, half-days)
- `DailyRiskState` has `trade_date: Date` but no `is_trading_day: bool` — every
  day, including weekends and holidays, creates a row

---

## 3. Domestic vs US Market Time Assumptions

| Assumption | Location | Correct? | Gap |
|---|---|---|---|
| KR market = Mon–Fri 09:05 KST | `scheduler.py:197` | ✗ | Ignores ~12 KRX public holidays + observances |
| US market = Mon–Fri 09:30 ET | `scheduler.py:205` | ✗ | Ignores 9 NYSE full-close + ~4 early-close days/year |
| US = 22:35 KST (fixed literal) | `bot/scheduler.py:19` | ✗ | EST winter: fires 55 min before open; EDT summer: fires 5 min after open |
| KR session ends ~15:30; reconcile until 15:xx | `scheduler.py:226` | ✗ | Reconciler runs on holidays |
| US session 22:35–06:00 (reconcile window) | `scheduler.py:226` | ✗ | Hour range `"22-23"` misses US close (04:00–05:00 KST); reconciler stops at midnight KST |
| Daily risk resets at 00:01 KST | `scheduler.py:210` | ✗ | Fires 1.5 hours into US session; resets wrong UTC date's DB row |
| `trade_date = date.today()` (UTC) | `engine.py:470`, `runner.py:551` | ✗ | During 00:00–09:00 KST, UTC date ≠ Seoul date |

---

## 4. Holiday / Half-Day / DST Gaps

### 4.1 KRX Holidays (not excluded)

- 설날 연휴 (Lunar New Year, typically 3 days)
- 추석 연휴 (Chuseok, typically 3 days)
- 삼일절 March 1, 어린이날 May 5, 현충일 June 6, 광복절 Aug 15,
  개천절 Oct 3, 한글날 Oct 9, 성탄절 Dec 25, 신정 Jan 1
- Substitute holidays, year-end settlement closures, special elections
- `pykrx.market.get_market_holidays()` provides this data but is never called

### 4.2 NYSE / NASDAQ Holidays and Early Closes (not excluded)

Full-close (9): New Year's Day, MLK Day, Presidents' Day, Good Friday, Memorial Day,
Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas.

Early close at 13:00 ET (~4 days/year): day before Independence Day (when applicable),
day after Thanksgiving, Christmas Eve, day before New Year's Day (when applicable).

### 4.3 DST

`America/New_York` handled correctly by APScheduler for the `us_session` job.
No issues in steady-state. **No unit tests exist for spring-forward / fall-back
scheduler behavior.** A scheduler restart during a DST boundary is untested.

---

## 5. Order-Block Insertion Points

| Gate | Location | What to insert | Risk scenarios addressed |
|---|---|---|---|
| **G1 — Scheduler pre-publish** | `scheduler.py:_trigger_kr_session()` line 162; `_trigger_us_session()` line 167 | `if not CalendarService.is_trading_day(market, today): log and return` | CS-02, CS-03, CS-04 |
| **G2 — Session broadcaster** | `runner.py:_handle_market_open()` line 316, after lock acquired | Secondary `CalendarService.is_trading_day()` check | CS-02 (belt-and-suspenders) |
| **G3 — Broker place_order** | `KISBroker.place_order()` line 106 (or `IdempotencyBrokerAdapter` when built) | `if not CalendarService.is_session_active(market, now_local): raise MarketClosedError(...)` | CS-07 |
| **G4 — quick_trade API** | `quick_trade.py:place_order()` line 123 | Same session-active check; return HTTP 422 `{ "error": "market_closed", "next_open": "..." }` | CS-07 |
| **G5 — KIS error parsing** | `kis_adapter/client.py:post()` line 68 | Parse `rt_cd` / `msg1`; raise `MarketClosedError` for codes -90, -91, -100, E0001 instead of `RuntimeError` | CS-06 |
| **G6 — Circuit breaker exemption** | `execution/circuit_breaker.py:record_failure()` line 24 | `if isinstance(exc, MarketClosedError): return` (do not increment `_failures`) | CS-06 |
| **G7 — Reconciler gate** | `scheduler.py:_periodic_reconcile()` line 172 | Skip if `not CalendarService.is_trading_day(market_kr or market_us, today)` | CS-08 |
| **G8 — Risk reset timing** | `scheduler.py` `risk_reset` job line 210–213 | Reschedule from `00:01 KST` to `06:01 KST` (after US session ends ~05:00 KST) | CS-01 |
| **G9 — DB date normalization** | `engine.py:470` `_write_db()`; `runner.py:551` `_persist_order()` | Replace `date.today()` with `_seoul_today()` for KR / `_eastern_today()` for US | CS-05 |

---

## 6. Risk Scenarios

### CS-01 — CRITICAL: Daily risk reset fires mid-US session

**Trigger:** `_reset_daily_risk()` at `00:01 KST` every day.
**Gap:** US session runs 22:30–05:00 KST. The reset fires 1.5 hours into it.
**Effect:**
1. Daily PnL counter zeroed mid-session.
2. Losses incurred 22:30–00:01 KST (1.5 hours of active US trading) are erased before
   the 3% daily kill-switch can fire on them.
3. `date.today()` in `_write_db()` returns yesterday's UTC date, resetting the
   wrong `DailyRiskState` row while in-memory state already uses today's Seoul date.
**Likelihood:** 100% — every US trading day.

### CS-02 — HIGH: KR market holiday fires scheduler

**Trigger:** KR session cron fires at 09:05 KST on `설날`, `추석`, and all other
KRX holidays (≥12 days/year).
**Effect:** `on_market_open()` fires → strategy scans → orders submitted to KIS →
KIS returns market-closed error → `ConsecutiveFailureBreaker` increments → after
3 failures: strategy halted 30 min → cooldown expires → retries → market still
closed → loops all day. Operator receives circuit-breaker alerts with no indication
it is a holiday.
**Likelihood:** ~12+ days/year.

### CS-03 — HIGH: US market holiday fires scheduler

**Trigger:** US session cron fires at 09:30 ET on NYSE holidays (9 full-close days/year).
**Effect:** Same loop as CS-02 for US strategies.
**Likelihood:** 9 days/year.

### CS-04 — HIGH: US half-day early close (13:00 ET) not detected

**Trigger:** Market closes at 13:00 ET on day-after-Thanksgiving, Christmas Eve,
and similar early-close days (~4/year). Strategy was started at 09:30 ET and
continues to scan through 16:00 ET.
**Effect:** Orders placed 13:00–16:00 ET rejected → circuit breaker trips.
Positions acquired in the morning cannot be managed because sell orders are also
rejected.
**Likelihood:** ~4 days/year.

### CS-05 — HIGH: Date mismatch `_seoul_today()` vs `date.today()` (UTC)

**Trigger:** Any operation between 00:00–09:00 KST (= 15:00 UTC yesterday – 00:00 UTC today).
**Effect:** `LossTracker.trade_date` (in-memory) = Seoul March 15;
`DailyRiskState.trade_date` (DB write key) = UTC March 14. The reset at 00:01 KST
modifies yesterday's DB row. `DBOrder.trade_date` (runner.py:551) = UTC date, not
Seoul date. For US trades placed after midnight KST the order history, daily PnL
tracking, and kill-switch evaluation use inconsistent dates.
**Likelihood:** Affects every US session that spans midnight KST (i.e., every day).

### CS-06 — HIGH: KIS error codes -90/-91 (market closed) treated as broker failure

**Trigger:** Any order placed outside trading hours or on a holiday.
**Effect:** `RuntimeError` raised → circuit breaker increments → strategy halts
after 3 failures. No operator message distinguishes "holiday" from "real broker
outage". The circuit breaker's 30-min cooldown is useless on a full-day holiday.
**Likelihood:** Cooccurs with CS-02/CS-03/CS-04.

### CS-07 — MEDIUM: quick_trade API and mobile accept orders at any time

**Trigger:** Operator or user submits a manual order outside market hours.
**Effect:** Order forwarded to KIS; broker rejects with opaque error; caller
receives unstructured RuntimeError message. Mobile shows no "market closed" state.
**Likelihood:** Any time a human interacts with the UI outside session hours.

### CS-08 — MEDIUM: Reconciler runs on holidays

**Trigger:** `_periodic_reconcile()` has no holiday exclusion; runs at 30-min
intervals during the hardcoded KR/US windows on any weekday.
**Effect:** On a holiday the broker returns zero or stale positions. Reconciler
compares against DB positions from last trading day → flags false gaps → may
trigger auto-repair or alerts. At minimum, creates audit-log noise.
**Likelihood:** Per CS-02/CS-03.

### CS-09 — HIGH: `on_market_close()` never called

**Trigger:** N/A — there is no close-time trigger.
**Effect:** Strategies cannot clean up state, cancel working orders, or produce
end-of-day reports in response to market close. `StrategyBase.on_market_close()`
(`base.py:70`) is fully defined but zero call sites exist in the scheduler or
runner. No `session:kr_close` / `session:us_close` channels exist.
**Likelihood:** Permanent gap for any strategy that needs close-aware logic.

### CS-10 — HIGH: `bot/scheduler.py` US session hardcoded at 22:35 KST

**Trigger:** Bot scheduler run independently during EST months (November–March).
**Effect:** `22:35 KST` = `13:35 UTC` = `08:35 EST`. NYSE opens at `09:30 EST`
= `23:30 KST`. The bot fires **55 minutes before market open**, generates signals
on pre-open prices, and submits orders that the broker may reject or queue.
**Likelihood:** Every US trading day Nov–Mar if bot scheduler is used.

### CS-11 — MEDIUM: Reconciler misses US close window

**Trigger:** `periodic_reconcile` uses `hour="9-15,22-23"` KST.
**Effect:** US market closes at ~04:00–05:00 KST. The reconciler last ran at 23:59
KST and does not run again until 09:00 KST next day. Fills from the final 4–5 hours
of the US session go unreconciled for ~4–5 hours.
**Likelihood:** Every US trading day.

### CS-12 — LOW: DST transition boundary untested

**Trigger:** Spring-forward / fall-back clock change.
**Effect:** APScheduler handles DST correctly in steady state. A scheduler restart
during the exact transition hour is untested — could fire twice or skip a job.
**Likelihood:** 2 days/year; very narrow window.

### CS-13 — LOW: Mobile has no market-status display

**Trigger:** User opens the quick-trade UI outside session hours.
**Effect:** No "Market closed — opens at 09:30 ET" feedback. No
`/api/market-status` endpoint exists. User must know exchange hours independently.
**Likelihood:** Any time outside trading hours.

---

## 7. Affected Modules

| Module | File | Nature of gap |
|---|---|---|
| Scheduler | `backend/worker/scheduler.py` | No holiday gate; risk reset timing; reconciler window incomplete; no close triggers |
| Legacy bot scheduler | `bot/scheduler.py` | US session hardcoded to wrong KST literal; fires 55 min early in winter |
| Strategy worker | `backend/worker/runner.py` | No calendar check in `_handle_market_open()`; `on_market_close()` never invoked |
| Risk engine | `backend/quant/risk/engine.py` | `date.today()` vs `_seoul_today()` mismatch in `_write_db()` |
| KIS broker | `backend/brokers/kis.py` | No `is_market_open()` before order placement |
| KIS client | `kis_adapter/client.py` | Market-closed error codes parsed as generic `RuntimeError` |
| KIS orders | `kis_adapter/orders.py` | No session pre-check before `post()` |
| Circuit breaker | `backend/execution/circuit_breaker.py` | `MarketClosedError` treated same as real failure |
| Quick-trade API | `api/routers/quick_trade.py` | Zero session validation |
| Mobile UI | `mobile/src/views/quick-trade/index.vue` | Zero market-hours gate; no "market closed" state |
| DB models | `backend/database/models.py` | No `MarketCalendar`, `TradingSession`, or `HolidayCalendar` tables |
| Reconciler | `backend/execution/reconciler.py` | No holiday guard applied by scheduler |
| Data loader | `backend/quant/data/loader.py` | 26-hour staleness threshold not holiday-aware |
| Strategy base | `backend/strategy/base.py` | 3-day bar-staleness check ignores holiday gaps; `on_market_close()` stub never wired |
| Order poller | `backend/execution/order_poller.py` | 30-min timeout is market-agnostic (no pause during non-session hours) |

---

## 8. Unsafe Assumptions

1. **`day_of_week="mon-fri"` is sufficient as a market-open gate.**
   False — US has 9 full holidays plus ~4 early closes; KRX has 12+ holidays.

2. **Daily risk reset at 00:01 KST is a session boundary.**
   False — the US session spans midnight KST (22:30–05:00 KST). 00:01 KST is in
   the middle of an active trading session.

3. **`date.today()` equals the Seoul trading date.**
   False inside the 00:00–09:00 KST window. In a UTC container: Seoul March 15
   00:01 KST = UTC March 14 15:01. `date.today()` returns March 14.

4. **KIS API errors during market hours indicate a broker failure.**
   False — codes -90, -91, -100 indicate market-closed state, not broker failure.
   Treating them as failures feeds the circuit breaker incorrectly.

5. **Circuit breaker cooldown resolves holiday failures.**
   False — after a 30-minute cooldown, the market is still closed; the breaker
   trips again immediately and loops for the entire holiday.

6. **`StaleDataWatchdog` guards against stale bar data.**
   False — it is dead code (never instantiated). `_is_bar_stale()` in `base.py:80`
   only guards `on_bar()`; `_scan_and_trade()` has no staleness gate.

7. **`pykrx` is available for holiday data and is being used.**
   Partially true — `pykrx` is installed and `pykrx.market.get_market_holidays()`
   exists, but only `pykrx.stock` (OHLCV data) is imported. The holiday API is
   never called.

8. **Both schedulers (`backend/worker/scheduler.py` and `bot/scheduler.py`) agree
   on session times.**
   False — backend uses `CronTrigger(hour=9, minute=30, timezone="America/New_York")`;
   bot uses the fixed literal `hour=22, minute=35, timezone="Asia/Seoul"`. They
   diverge by 55 minutes in winter.

9. **`on_market_close()` will be called when the market closes.**
   False — there are no close-time triggers, no `session:*_close` channels, and no
   invocations of `on_market_close()` anywhere in the worker.

---

## 9. Recommended Safe Architecture (for TASK 3-1B)

### 9.1 CalendarService (`backend/execution/calendar.py`)

Single source of truth for "is the market open right now / today?"

```python
class CalendarService:
    def is_trading_day(self, market: str, d: date) -> bool
    def is_session_active(self, market: str, dt: datetime) -> bool
    def get_session_window(self, market: str, d: date) -> tuple[datetime, datetime] | None
    def is_early_close(self, market: str, d: date) -> bool
    def next_session_open(self, market: str, after: datetime) -> datetime
```

**Data sources (in priority order):**
1. `pykrx.market.get_market_holidays(year)` for KRX (already installed)
2. `pandas_market_calendars.get_calendar("NYSE")` for US (add to requirements)
3. Static fallback holiday list for current year (hardcoded; used when libraries
   are unavailable)

**Caching:** Redis key `calendar:{market}:{date.isoformat()}` with TTL=24h so
holiday checks are not repeated per-order.

### 9.2 MarketClosedError (new exception in `calendar.py`)

```python
class MarketClosedError(RuntimeError):
    def __init__(self, market: str, code: str, message: str): ...
```

`kis_adapter/client.py:post()` raises this instead of `RuntimeError` when
`rt_cd` is `-90`, `-91`, `-100`, or `E0001`.
`ConsecutiveFailureBreaker.record_failure(exc=None)` skips incrementing `_failures`
when `isinstance(exc, MarketClosedError)`.

### 9.3 Risk Reset Timing Fix

Move `risk_reset` job from `00:01 KST` to `06:01 KST` (after US session ends at
~05:00 KST in summer; the window is 05:01–06:00 KST between US close and KR
pre-open preparation). Alternatively, use `CalendarService.next_session_open("KR")`
to compute the reset time dynamically each night.

### 9.4 Date Normalization

`PersistentLossTracker._write_db()` (`engine.py:470`): replace `date.today()` with
`_seoul_today()`.

`runner.py:551` `trade_date=datetime.utcnow().date()`: replace with a
`market_date(market)` helper that returns Seoul date for KR and Eastern date for US.

### 9.5 Market Close Triggers

Add `_trigger_kr_close()` and `_trigger_us_close()` scheduler jobs that publish
`session:kr_close` / `session:us_close` Redis channels (using `CalendarService` to
skip holidays and use the half-day close time when applicable). Wire
`runner.py:_handle_market_open()` pattern to `_handle_market_close()` to invoke
`strategy.on_market_close()`.

---

## 10. New Code Requirements (for TASK 3-1B)

| File | Change |
|---|---|
| `backend/execution/calendar.py` | **NEW** — `CalendarService` + `MarketClosedError` |
| `requirements.txt` | Add `pandas_market_calendars` |
| `backend/worker/scheduler.py` | Holiday guard in `_trigger_kr/us_session()`; move `risk_reset` to `06:01 KST`; add `_trigger_kr/us_close()` jobs; holiday guard in `_periodic_reconcile()` |
| `backend/worker/runner.py` | Subscribe to `session:kr_close` / `session:us_close`; call `strategy.on_market_close()` |
| `kis_adapter/client.py` | Parse `rt_cd` / `msg1` for market-closed codes; raise `MarketClosedError` |
| `backend/execution/circuit_breaker.py` | `record_failure(exc=None)` skips increment for `MarketClosedError` |
| `backend/brokers/kis.py` | `place_order()`: add `CalendarService.is_session_active()` check |
| `backend/strategy/base.py` | `_live_trade_allowed()`: add session-active check |
| `api/routers/quick_trade.py` | Add session check; return HTTP 422 on market-closed with `next_open` field |
| `backend/quant/risk/engine.py` | Replace `date.today()` with `_seoul_today()` in `_write_db()` |
| `backend/worker/runner.py` | `trade_date` in `_persist_order()`: use `market_date(market)` helper |
| `bot/scheduler.py` | Fix US session to `CronTrigger(hour=9, minute=30, timezone="America/New_York")` or retire |
| `backend/api/server.py` | Add `GET /api/market-status` endpoint |
| `backend/database/models.py` | Optional: add `market_calendar_cache` table for offline fallback |

---

## 11. Verification (for TASK 3-1B)

```bash
# CalendarService: KRX holidays excluded
pytest tests/execution/test_calendar.py -v -k krx_holiday

# NYSE full holidays excluded
pytest tests/execution/test_calendar.py -v -k nyse_holiday

# NYSE early-close half-day returns correct window
pytest tests/execution/test_calendar.py -v -k nyse_early_close

# Scheduler does not publish session signal on holiday
pytest tests/worker/test_scheduler.py -v -k no_fire_on_holiday

# Order placement blocked outside session (MarketClosedError raised)
pytest tests/brokers/test_kis.py -v -k market_closed_blocks_order

# MarketClosedError does NOT increment circuit breaker
pytest tests/execution/test_circuit_breaker.py -v -k market_closed_not_counted

# Risk reset fires at 06:01 KST (after US session ends at ~05:00 KST)
pytest tests/worker/test_scheduler.py -v -k risk_reset_after_us_close

# DB date writes use _seoul_today() not UTC date.today()
pytest tests/quant/test_risk_engine.py -v -k db_uses_seoul_date

# on_market_close() invoked at correct time
pytest tests/worker/test_runner.py -v -k market_close_invoked

# DST spring-forward: us_session still fires at 09:30 ET
pytest tests/worker/test_scheduler.py -v -k dst_spring_forward

# DST fall-back: us_session still fires at 09:30 ET (not 08:30 or 10:30)
pytest tests/worker/test_scheduler.py -v -k dst_fall_back

# quick_trade API returns 422 with next_open when market is closed
pytest tests/api/test_quick_trade.py -v -k market_closed_returns_422
```
