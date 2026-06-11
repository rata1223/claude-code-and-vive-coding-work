# Stale Market Data Audit (TASK 3-3A)

> **Phase: M1-A — audit only. No code is modified by this document.**
> This is a read-only trace of the market-data pipeline focused on **data
> freshness / staleness** — as distinct from TASK 3-2A/3-2B's audit of
> **malformed data** (NaN, OHLC inconsistency, duplicates, etc.).
> Deliverable for a future implementation task (tentatively "TASK 3-3B").

---

## 1. Purpose & Scope

**Purpose.** Trace the full market-data flow —

```
market data source → recovery/loader → (validator, design-only) → indicators/strategy → fusion → execution/risk
```

— and identify every point where **stale data** (wall-clock-old data, a
degraded/failed source silently substituted, a worker or feed that has
stopped updating, a partially-formed bar, or a timestamp that drifts across
markets/timezones) can reach a trading decision undetected.

**Phase statement.** This is **M1-A: audit only**. No `.py` files are
created or modified. The output is this document
(`docs/STALE_DATA_AUDIT.md`), which a future task ("TASK 3-3B") would use as
its design input — mirroring how `docs/OHLCV_DATA_VALIDATION.md` (3-2A) fed
`docs/OHLCV_VALIDATION.md` (3-2B).

**In scope.**
- KIS REST price/OHLCV (`kis_adapter/`)
- Kiwoom (`backend/brokers/kiwoom.py`, `kiwoom_adapter/`)
- OpenBB (inspected — see §4.3)
- yfinance / pykrx (`backend/quant/data/loader.py`)
- The `OHLCVRecovery` 4-tier waterfall (`backend/quant/live/safeguards.py`)
- FX-rate caching (`backend/brokers/kis.py`)
- Strategy/indicator/fusion consumption (`backend/strategy/`,
  `backend/quant/signals/fusion.py`)
- Execution/risk consumption (`backend/quant/live/pipeline.py`,
  `strategy/risk.py`, `backend/quant/risk/engine.py`, `backend/execution/`)
- Worker-process liveness (`backend/worker/heartbeat.py`,
  `backend/worker/emergency.py`)

**Out of scope.**
- Malformed-data validation (NaN/OHLC-consistency/duplicates) — covered by
  TASK 3-2A (`docs/OHLCV_DATA_VALIDATION.md`) and TASK 3-2B
  (`docs/OHLCV_VALIDATION.md`). These are an **orthogonal axis**: a row can
  be `INVALID` per 3-2B's validator AND independently "stale" per this
  document.
- Backtest data path (historical, not live).
- Calendar/holiday/session-window gating — covered by TASK 3-1A
  (`docs/MARKET_CALENDAR_AUDIT.md`) and TASK 3-1B's `CalendarService`. Only
  referenced here where it intersects with timestamp-drift (§3, SD-11).

**ID conventions.**
- `SD-XX` — **S**tale **D**ata failure scenarios (this document's primary
  catalog; a new series, distinct from 3-2A's `DS-XX`).
- `SG-XX` — **S**taleness **G**ate, the Gate Map of proposed blocking/
  annotation insertion points (§5; distinct from 3-2A's `V-XX`, which
  addresses malformed-data validation, not staleness).

---

## 2. Current Structure Analysis

### 2.1 Data Sources

| Source | File | Markets | Entry Point | Freshness Metadata? |
|---|---|---|---|---|
| KIS price | `kis_adapter/market_data.py` (`get_price_us`/`get_price_kr`) | KR/US | `bot/main.py` (legacy), `backend/quant/live/pipeline.py` (current) | **None** — bare `float`/`int`, no timestamp |
| KIS OHLCV | — | — | missing entirely (`get_ohlcv()` does not exist; cf. 3-2A DS-06) | N/A |
| Kiwoom | `backend/brokers/kiwoom.py` (29-line `NotImplementedError` stub); `kiwoom_adapter/` (broken KIS-copy) | — | none — no method is callable | N/A |
| yfinance | `backend/quant/data/loader.py::_fetch_us` | US | `DataLoader.fetch()` | `DatetimeIndex`; 26h staleness check, **daily only**, WARN-only |
| pykrx | `backend/quant/data/loader.py::_fetch_kr_pykrx` | KR | `DataLoader.fetch()` | `DatetimeIndex`; **no staleness check at all** |
| OHLCVRecovery cache | `backend/quant/live/safeguards.py` (`_get_cache`/`_update_cache`) | KR/US | tier-4 fallback of `OHLCVRecovery.fetch()` | in-memory write timestamp; 26h TTL, pure wall-clock |
| FX rate cache | `backend/brokers/kis.py` (`_FX_CACHE`) | — (KRW/USD) | `_get_fx()` | in-memory timestamp; 1h TTL, stale-fallback with WARN if >30min |

### 2.2 Data Flow Diagram

Each arrow is annotated with the **freshness check that exists today** on
that hop. "NONE" means the hop performs zero recency check of any kind.

```
┌──────────────────┐
│ KIS REST price    │ ──(NONE — value is always "now" at call time,
│ get_price_kr/us   │     but no timestamp is attached downstream)──┐
└──────────────────┘                                                │
                                                                     v
┌──────────────────┐                                    ┌────────────────────────┐
│ KIS OHLCV         │ ── MISSING (get_ohlcv() absent)   │ strategy/risk.py        │
└──────────────────┘                                    │ (legacy RiskManager) /  │
                                                         │ engine.py Trailing-     │
                                                         │ StopManager (current)   │
                                                         │ — both bare float price │
                                                         └────────────────────────┘

┌──────────────────┐
│ yfinance / pykrx  │
│ DataLoader.fetch()│ ──(26h WARN-only, DAILY ONLY;
└──────────────────┘     NONE for intraday 1h/5m/15m)──┐
                                                         v
                                           ┌──────────────────────────────┐
                                           │ OHLCVRecovery 4-tier waterfall │
                                           │ tier1 KIS (broken, no OHLCV)   │
                                           │ tier2 yfinance (no add'l check)│
                                           │ tier3 pykrx (no check)         │
                                           │ tier4 cache (26h TTL,          │
                                           │   wall-clock only)             │
                                           │ -- tier degradation INVISIBLE  │
                                           │    to caller --                │
                                           └──────────────────────────────┘
                                                         │
                                                         v
                                  ┌───────────────────────────────────────────┐
                                  │ [no validator yet — backend/data/validator │
                                  │  .py is design-only per TASK 3-2B; its     │
                                  │  checks are sortedness/dedup, not          │
                                  │  wall-clock freshness]                     │
                                  └───────────────────────────────────────────┘
                                                         │
                                                         v
                          ┌────────────────────────────────────────────────────┐
                          │ strategy/signals.py, IndicatorStrategy               │
                          │   ._scan_and_trade()                                 │
                          │ ── 3-CALENDAR-DAY gate, bare except: pass ──         │
                          │ on_bar() ── _is_bar_stale(), 600s, DORMANT path ──   │
                          └────────────────────────────────────────────────────┘
                                                         │
                                                         v
                          ┌────────────────────────────────────────────────────┐
                          │ SignalFusion / RobustFusion.evaluate()               │
                          │ ── NO age check at all ──                            │
                          └────────────────────────────────────────────────────┘
                                                         │
                                                         v
                          ┌────────────────────────────────────────────────────┐
                          │ pipeline.py buy-loop / risk.py stop-loss /           │
                          │ order_machine.py / position_tracker.py               │
                          │ ── NO freshness check on get_price() OR the          │
                          │    df["Close"].iloc[-1] fallback ──                  │
                          └────────────────────────────────────────────────────┘
```

### 2.3 The Four Fragmented Staleness Mechanisms

The codebase already contains **four independent** staleness checks. None
share a threshold, a config object, an exception type, or even a notion of
"context" (daily vs. intraday vs. live-bar vs. cache). This fragmentation is
itself a risk (SD-06, §3).

| # | Mechanism | Location | Threshold | Scope | Block / Warn | Status |
|---|---|---|---|---|---|---|
| (a) | Loader daily-bar check | `backend/quant/data/loader.py:84-97` (`_fetch_us`) | 26 hours | Daily (`interval == "1d"`) bars, **US path only** | **WARN-only** — logs and returns the stale `df` unchanged | Active |
| (b) | `StaleDataWatchdog` | `backend/worker/emergency.py:134-171` | 2 hours (`max_age_hours`, configurable) | `is_stale(df)` / `check_all(dfs)` | n/a — never invoked | **Dead code** — zero call sites outside its own file |
| (c) | `_is_bar_stale()` | `backend/strategy/base.py:80-99` | 600s (`BAR_STALE_SECONDS` env, default 10min) | `IndicatorStrategy.on_bar()` only — the live-bar-streaming path | Returns `True` → caller skips the bar | Active code, but the path is **dormant** (no live bar producer exists — see §4.4 / SD-10) |
| (d) | `_scan_and_trade()` inline gate | `backend/strategy/indicator/strategy.py:103-118` | 3 calendar days (`age_days > 3`) | Main scan-and-trade loop, all symbols | `continue` (skip symbol); wrapped in **bare `except Exception: pass`** | Active — this is the *only* staleness check on the path that actually places live orders today |

### 2.4 Existing "Safe Blocking" Precedent

`backend/worker/heartbeat.py` already implements a **process**-level
"detect bad condition → cross-process kill switch → alert" pattern that is a
strong template for a future **data**-level equivalent:

- `WorkerHeartbeat` writes a Redis key `worker:heartbeat` every 30s with a
  90s TTL.
- `WorkerWatchdog._check()` (60s interval) calls
  `HeartbeatMonitor.is_alive()`; on a dead worker it:
  1. Sets `DailyRiskState.kill_switch = True` in Postgres (chosen because
     `SAFE_MODE` is a process-local singleton and cannot be toggled
     cross-process — DB is the only safe cross-process channel).
  2. Calls `bot.notifier.alert_emergency(...)` (Telegram).
  3. Calls `backend.websocket.server.publish_alert(..., level="critical")`
     (UI alert).

This monitors **process liveness**, not **data freshness** — a worker can be
perfectly alive while every market-data source it depends on is silently
stale (SD-05, §3). §5 proposes a `MarketDataFreshnessWatchdog` that mirrors
this exact three-step pattern for data health.

---

## 3. Stale-Risk Map (SD-01 .. SD-13)

### Summary Table

| ID | Scenario | Trigger | Where (file:location) | Current behavior | Impact | Severity |
|---|---|---|---|---|---|---|
| SD-01 | Daily-bar 26h staleness check is WARN-only | Weekend/holiday gap, or fetch delayed >26h since last close | `backend/quant/data/loader.py:83-97` (`_fetch_us`, `stale_hours=26`) | Logs `logger.warning(...)`, returns the stale `df` unchanged — no exception, no propagated flag | Strategy computes signals/indicators on a day-old (or older) bar with no caller-visible marker | HIGH |
| SD-02 | Intraday timeframes (1h/5m/15m) have zero staleness check | Any intraday `interval` passed to `DataLoader.fetch()` | `backend/quant/data/loader.py:84` (`if interval == "1d"` guard) | No check at all | A 5m-interval fetch returning data from 3 hours ago is indistinguishable from data from 3 minutes ago — silent today, becomes HIGH if intraday strategies activate | MEDIUM (HIGH if intraday activated) |
| SD-03 | OHLCVRecovery tier-degradation invisible | yfinance and pykrx both fail; `OHLCVRecovery.fetch()` falls through to tier-4 cache | `backend/quant/live/safeguards.py:201-205` (`fetch`, tier-4 cache fallback) and `:254-263` (`_get_cache`) | Returns cached `df` if `age <= max_cache_age_hours (26)`; logs `"OHLCV 캐시 사용: %s (신선도 불보장)"` but the **return value carries no metadata** | Caller cannot distinguish "fresh tier-2 data" from "25h59m-old tier-4 cache"; no operator alert that 3 of 4 tiers failed | HIGH |
| SD-04 | FX rate cache silent-stale fallback >30min | `yf.Ticker("KRW=X")` fetch fails or errors repeatedly | `backend/brokers/kis.py:300-317` (`_get_fx`) | Returns last cached `_FX_CACHE["rate"]` regardless of age; only logs a WARNING if `age_min > 30` — never blocks, never raises | KRW/USD conversion feeds the kill-switch equity calc (the warning text itself says "킬스위치 계산 부정확 가능") — a stale FX rate can silently skew the daily-loss/MDD threshold comparison | MEDIUM |
| SD-05 | Worker dies mid-session — heartbeat checks process liveness, not data freshness | Worker process crash/hang while data fetches continue to be attempted by a different process | `backend/worker/heartbeat.py` (`WorkerWatchdog._check`, 60s interval) | On dead-worker detection, sets DB `kill_switch=True` — a **process-health** signal, fully orthogonal to data freshness; if the worker is alive but its data sources are all stale, heartbeat is green | "Worker alive, trading on hour-old data" produces zero alerts | MEDIUM |
| SD-06 | Four fragmented staleness checks — inconsistent thresholds/scopes; fixing one doesn't fix the others | Any staleness condition occurring on a code path not covered by the specific mechanism active there | `loader.py:84-97` (26h, WARN, daily/US-only); `emergency.py:134-171` (2h, dead code); `strategy/base.py:80-99` (600s, dormant `on_bar` path); `indicator/strategy.py:103-118` (3-day, bare `except: pass`) | Each mechanism operates in isolation — different threshold (10min / 2h / 26h / 3 days), different scope, different block/warn semantics, no shared config or exception type | A future fix to the loader's 26h check would NOT fix `_scan_and_trade`'s independent 3-day gate, and vice versa — a structural meta-risk that makes the *other* SD-XX items individually unfixable without a unifying layer | HIGH |
| SD-07 | Stop-loss / PnL calc on bare-float price with no timestamp | (legacy path) `enforce_stop_losses()` calls `get_price_fn(symbol)`; (current path) `pipeline.py` collects `price_map` via `broker.get_price()` for `TrailingStopManager` | `strategy/risk.py:88-110` (`RiskManager.is_stop_loss`/`enforce_stop_losses`, legacy `bot/main.py` path); `backend/quant/risk/engine.py:56-130` (`PositionStop`/`TrailingStopManager`, current `pipeline.py` path); `backend/quant/live/pipeline.py:164-174` (`price_map` collection via `broker.get_price()`); `kis_adapter/market_data.py:22-30` (`get_price_us`/`get_price_kr` — bare `float`/`int`, zero metadata) | Both `is_stop_loss()` and `PositionStop.is_stopped()` are pure numeric comparisons; if the underlying `current_price`/`price_map` value reflects a delayed/cached upstream quote (e.g. a KIS REST quote that lags during high load), there is no way to detect it | A stop-loss (legacy hard-stop or current trailing/hard stop) could fail to fire on a price that has already moved past the threshold in reality, or fire on a transient bad quote — no defense in either direction, on either path | HIGH |
| SD-08 | Fusion / regime filter has zero data-age check | `SignalFusion.evaluate(df, symbol)` is called from `_scan_and_trade()` with a `df` that may itself be stale (SD-01/SD-03) | `backend/quant/signals/fusion.py:59-104` (`evaluate`) | `regime_filter(df)` and each `SignalBase.compute(df, ...)` operate on `df.iloc[-1]` / `.rolling(200)` etc. with no recency check on `df.index[-1]` | Even when the loader *knows* the data is stale (SD-01's WARNING), that information never reaches `fusion.evaluate()` — fusion has no parameter or hook for "this df is N hours stale" | HIGH |
| SD-09 | `_scan_and_trade`'s 3-day gate is calendar-day granularity + bare `except: pass` swallows real errors | Any exception inside the staleness-check block (e.g. `df.index[-1]` is an unexpected type after a vendor change) | `backend/strategy/indicator/strategy.py:103-118` | `age_days = (datetime.now(utc) - last_dt).days; if age_days > 3: continue` — `.days` truncates, so a bar 23h59m old (`age_days=0`) and a bar 71h59m old (`age_days=2`) are both "fresh enough"; AND if the timestamp-parsing logic itself raises, the bare `except Exception: pass` means the **entire staleness check is silently skipped** ("non-fatal" per the inline comment) | A genuinely broken/garbage timestamp index causes the staleness check to be silently bypassed entirely (worse than no check — false confidence); even on the happy path, "3 calendar days" is far too coarse to catch an intraday-stale-cache scenario (SD-03) | MEDIUM |
| SD-10 | No market-data websocket exists yet — future KIS-websocket-disconnect risk undesigned | (hypothetical — no current trigger) | `backend/websocket/server.py` (UI-push only — Redis Pub/Sub → Flask-SocketIO; channels `order:update`/`position:update`/`equity:update`/`alert`); KIS websocket price-streaming capability is unused | N/A today — `_is_bar_stale()` (`backend/strategy/base.py:80-99`) is the only mechanism shaped for a streaming-bar context, but `on_bar()` has no live producer | Forward-looking gap: if a future task adds a KIS websocket price feed, "websocket disconnect" becomes a live staleness vector with NO existing detection — `_is_bar_stale`'s 600s threshold would need to be wired to an actual disconnect signal from the socket layer | LOW (informational / future-risk) |
| SD-11 | Timestamp drift: KR vs US market timezones + UTC-naive index | A US-market bar (`df.index[-1]` = e.g. `2026-06-10 16:00 ET`, stored UTC-naive) evaluated by a check that assumes KR-local "now", or vice versa | `backend/quant/data/loader.py:85-90` (`last_ts.tz_localize("UTC")` — treats a naive timestamp as UTC regardless of source exchange); `backend/strategy/indicator/strategy.py:108-113` (similar naive→UTC coercion) | Both the loader's 26h check and `_scan_and_trade`'s 3-day check `tz_localize`/coerce naive timestamps to UTC — for a KR stock (pykrx, KST-midnight-anchored dates) vs a US stock (yfinance, ET-midnight-anchored dates), "midnight" represents a different UTC instant depending on source, but both get identical UTC-coercion treatment | A genuinely fresh KR daily bar (dated "today" KST) could compute as ~9h "older" than a US bar dated "today" ET when both are naively coerced to UTC — near either threshold's margin, this could cause a fresh KR bar to be misjudged stale, or a stale US bar to appear fresher than it is. Cross-references `docs/MARKET_CALENDAR_AUDIT.md` (3-1A) | MEDIUM |
| SD-12 | Cache refresh failure on date-rollover — yesterday's last bar served as "fresh" early in a new session | New trading session begins (e.g. KR market opens 09:00 KST); `OHLCVRecovery._cache` still holds yesterday's last-fetched `df` with a recent `_update_cache` timestamp from late yesterday | `backend/quant/live/safeguards.py:254-263` (`_get_cache`) — `age = (now_utc - cached_ts).total_seconds()/3600`; cache key is `symbol` only (`self._cache: dict[str, tuple[pd.DataFrame, datetime]]`, line 173), with **no trading-day/session component** | If the day's first real fetch (tier 1/2/3) fails right at session open, a cache written at 15:30 KST yesterday is only ~17.5h old at 09:00 KST today — well under the 26h `max_cache_age_hours`, so it passes as "not expired" even though it predates today's session entirely and contains zero of today's price action | Strategy could scan-and-trade at market-open using yesterday's closing bar as if it were "fresh" data for today — exactly the moment fresh data matters most | HIGH |
| SD-13 | Partial/incomplete intraday bar treated as a complete bar | A live or near-real-time fetch returns the current, still-forming bar for the in-progress interval (e.g. a "1d" bar for today, fetched mid-session before market close) | `backend/quant/data/loader.py` (`_fetch_us`/`_fetch_kr_pykrx` — no "is this the final/closed bar" check); `backend/strategy/indicator/strategy.py::_scan_and_trade` (`df.iloc[-1]` consumed as "the latest closed bar") | No mechanism distinguishes a closed historical bar from a partial/in-progress bar; `df.index[-1]` for "today" during market hours could be today's partial OHLCV if the vendor includes it | Indicators (SMA/RSI/momentum) computed on a partial bar produce values that will change once the bar closes — signals generated mid-session on `df.iloc[-1]` could flip after the bar finalizes; "data not yet final" is staleness-adjacent | LOW-MEDIUM |

### Coverage of the 8 Requested Risk Categories

| Category (from task prompt) | Covered by |
|---|---|
| API delay | SD-01, SD-07 |
| Websocket disconnect | SD-10 |
| Polling stop | SD-05, SD-12 |
| Cache refresh failure | SD-03, SD-04, SD-12 |
| Worker stop | SD-05 |
| Partial update | SD-13 |
| Source outage | SD-03, SD-12 |
| Timestamp drift | SD-11 |

SD-06 and SD-09 are meta/structural findings specific to this codebase's
history of incremental, never-reconciled fixes — they amplify nearly every
other row above.

---

## 4. Source-Specific Health Flow

### 4.1 KIS

- **Token health**: solid. `kis_adapter/auth.py` caches the access token in
  Redis (`kis:access_token`/`kis:token_expiry`), 24h TTL, refreshes 15
  minutes (`_TOKEN_REFRESH_BUFFER = 900`) before expiry, with an in-memory
  fallback if Redis is unreachable. **No change recommended here.**
- **Price (`get_price_kr`/`get_price_us`)**: synchronous REST, on-demand —
  the value returned is always "now" at call time, so staleness here is
  purely a function of *API latency*, not cached data. Today this latency is
  completely untracked: the function returns a bare `float`/`int` with zero
  timestamp.
- **OHLCV**: **N/A — `get_ohlcv()` does not exist** (this is DS-06 from
  3-2A). Its absence forces `OHLCVRecovery` to skip tier-1 entirely for
  every fetch, which is itself a contributing cause of SD-03 (more frequent
  reliance on lower, less-fresh tiers).
- **Proposed health-flow shape (forward-looking, not built here)**: wrap
  `get_price_kr`/`get_price_us` to return a small `PriceQuote(value, ts,
  source="kis")` structure, where `ts` is captured at call-return time. This
  gives SG-07/SG-08 (§5) something concrete to check against.

### 4.2 Kiwoom — Not Applicable

`backend/brokers/kiwoom.py` is a 29-line stub where every `BrokerAdapter`
method raises `NotImplementedError`. The separate `kiwoom_adapter/` package
exists but is rated CRITICAL-broken in `docs/KIWOOM_AUDIT_REPORT.md` (a
KIS-copy with wrong endpoints/field names, paper-trading flag never
consulted, POST retries that risk duplicate orders).

**Conclusion**: Kiwoom contributes **zero live market data today** — staleness
is moot until Kiwoom is functionally wired up at all. This is noted as a
**future source**: once `kiwoom_adapter/` is fixed (a separate workstream per
`KIWOOM_AUDIT_REPORT.md`), it should be wired into the same
`FreshnessChecker`/`FreshnessConfig` proposed in §5 rather than getting its
own bespoke staleness logic.

### 4.3 OpenBB — Not Applicable / Red Herring

OpenBB (the OpenBB SDK/platform) is **not integrated anywhere** in this
codebase. The only OpenBB-related artifact is `pandas-ta-openbb`, a fork of
the `pandas-ta` indicator library used for technical-indicator computation —
unrelated to data fetching or to the OpenBB SDK's data-source capabilities.

This is flagged explicitly so a future reader does not waste time searching
for an OpenBB data adapter that does not exist. If OpenBB integration is ever
planned as an additional data source, it should be designed to flow through
the same `OHLCVRecovery`/`FreshnessChecker` path as yfinance/pykrx (§5),
rather than introducing a fifth bespoke staleness mechanism.

### 4.4 yfinance / pykrx

This is the most-developed source, and the one with the most existing (if
incomplete) freshness logic.

**Current flow:**
```
DataLoader.fetch(symbol, interval)
  └─ _fetch_us (yfinance) or _fetch_kr_pykrx (pykrx, falls back to
     yfinance "<code>.KS" on any exception)
       └─ sort_index()
       └─ if interval == "1d":
             check df.index[-1] age vs 26h  → WARN only, never raises
          else (intraday):
             NO CHECK AT ALL
       └─ return df
```

- pykrx itself has **zero** staleness check, and its silent fallback to
  yfinance `.KS` on *any* exception means a pykrx-specific staleness issue
  (e.g., KRX feed delay) would never even be detected — it would just
  silently switch data providers.
- The 26h threshold was chosen to tolerate weekend/holiday gaps for daily
  bars, but it is not session-aware (SD-12) and not timezone-aware across
  KR/US (SD-11).

**Proposed health-flow shape (forward-looking, not built here):**
```
fetch → FreshnessChecker.check(df, symbol, context=DAILY_SCAN | INTRADAY_BAR)
  → if stale:
       hard path (SG-01/SG-02): raise DataFreshnessError
       soft path (SG-06/SG-07): attach df.attrs["freshness"] = FreshnessReport
  → consumer decides block vs. annotate per the
    entry-vs-exit asymmetry principle (§5)
```

---

## 5. Blocking Insertion Points — Gate Map (SG-01 .. SG-09)

### Design Principle

> **Block new entries on stale data. Never block protective exits
> (stop-losses) on stale data — annotate/log staleness on exit paths
> instead.**

This directly answers the task's "where can trading be blocked most
safely" question. The safest blocking points are at **new-position entry
decisions** (SG-06 fusion, SG-07 buy-loop) — a blocked buy simply means "no
new risk is taken this cycle," a fully reversible, low-cost outcome. Blocking
an **exit** (stop-loss) on the same staleness signal would be actively
dangerous: it could leave a losing position open specifically *because* the
mechanism meant to add safety fired. SG-08 therefore proposes
*annotation-only* for the stop-loss/exit path.

### Proposed Unified Components (named here for §11; not built in this task)

- **`DataFreshnessError(Exception)`** — explicitly **not** a subclass of
  `RuntimeError`, mirroring `BadOHLCVError` (3-2B) and `MarketClosedError`
  (`backend/data/calendar.py`). This ensures
  `ConsecutiveFailureBreaker` (`backend/execution/circuit_breaker.py`) never
  counts a stale-but-otherwise-valid fetch as a broker/API failure.
- **`FreshnessConfig`** — single dataclass holding per-context thresholds:
  `intraday_max_age_seconds` (replaces `_BAR_STALE_SECONDS`'s 600s),
  `daily_max_age_hours` (replaces the loader's 26h, default kept at 26 to
  preserve weekend/holiday tolerance), `scan_max_age_hours` (replaces the
  3-calendar-day gate with hour-precision — fixes SD-09's truncation),
  `cache_max_age_hours` (replaces `OHLCVRecovery.max_cache_age_hours`), and
  `session_aware: bool` (addresses SD-12 by computing cache age relative to
  the last session boundary via `CalendarService`, not pure wall-clock).
- **`FreshnessChecker`** — stateless class (mirrors `OHLCVValidator`'s shape
  from `docs/OHLCV_VALIDATION.md`): `check(df, symbol, context, now=None) ->
  FreshnessReport`, where `context ∈ {DAILY_SCAN, INTRADAY_BAR,
  CACHE_FALLBACK, LIVE_PRICE}` selects which threshold applies.
  `FreshnessReport` carries `is_stale`, `age`, `threshold`, and (for
  `OHLCVRecovery`) `tier` — addressing SD-03's visibility gap.
- **`MarketDataFreshnessWatchdog`** — new periodic watchdog mirroring
  `WorkerWatchdog` (§2.4): tracks `last_successful_fetch` per symbol/source
  in a small registry populated on each successful fetch; on "all sources
  stale beyond `cache_max_age_hours` for N consecutive checks," sets
  `DailyRiskState.kill_switch = True` with
  `kill_reason="시장 데이터 전체 소스 stale — 거래 중단"`, then alerts via
  `bot.notifier.alert_emergency` + `publish_alert(level="critical")` — the
  same three-step pattern as `WorkerWatchdog._alert_dead_worker`.

### Gate Map

| Gate | File:Location | Action | Addresses |
|---|---|---|---|
| **SG-01** Loader daily-bar gate | `backend/quant/data/loader.py:84-97` (`_fetch_us`) | Replace the WARN-only block with `FreshnessChecker.check(df, symbol, context=DAILY_SCAN)`; on stale, raise `DataFreshnessError` (configurable hard-raise vs. WARN via `FreshnessConfig`) | SD-01 |
| **SG-02** Loader intraday gate | `backend/quant/data/loader.py:84` (new branch for `interval != "1d"`) | Add `FreshnessChecker.check(df, symbol, context=INTRADAY_BAR)` — currently entirely absent | SD-02 |
| **SG-03** OHLCVRecovery tier/cache gate | `backend/quant/live/safeguards.py:175-263` (`fetch`/`_try_*`/`_get_cache`/`_update_cache`) | Attach `df.attrs["freshness"]` recording which tier served the data + cache age (pandas `.attrs` preserves column/index access, so this is non-breaking); `_get_cache` uses `context=CACHE_FALLBACK, session_aware=True` (fixes SD-12's date-rollover gap) | SD-03, SD-12 |
| **SG-04** Strategy scan gate | `backend/strategy/indicator/strategy.py:103-118` | Replace the inline 3-day bare-except gate with `FreshnessChecker.check(df, symbol, context=DAILY_SCAN)` (same `FreshnessConfig` instance as SG-01, so loader and scanner agree); on `DataFreshnessError`, log and `continue` — visible/loggable instead of silently swallowed | SD-09, SD-06 |
| **SG-05** Live-bar gate (reconcile, no behavior change) | `backend/strategy/base.py:80-99` (`_is_bar_stale`) | Migrate `_BAR_STALE_SECONDS` into `FreshnessConfig.intraday_max_age_seconds`; have `_is_bar_stale` delegate to `FreshnessChecker` for consistency. No behavior change today (path is dormant), but prevents future drift | SD-06, SD-10 |
| **SG-06** Fusion entry gate | `backend/quant/signals/fusion.py:59` (`SignalFusion.evaluate`, before the regime filter) | Add an optional `freshness: FreshnessReport = None` parameter; if `freshness.is_stale`, short-circuit to `FusionResult(signal=0, ..., regime_blocked=True, blocked_reason="stale_data")` — reuses the `regime_blocked` early-return shape that already exists | SD-08 |
| **SG-07** Pipeline buy-loop gate | `backend/quant/live/pipeline.py:277-279` (`try: price = self.broker.get_price(symbol) except Exception: price = df["Close"].iloc[-1]`) | Before using either price source, `FreshnessChecker.check(..., context=LIVE_PRICE)`; if both the live-price call AND the dataframe fallback are stale, set `blocked_reason="stale_price"` and skip the buy — mirrors the existing `blocked_reason="daily_loss_limit"` pattern at `pipeline.py:224-240` | SD-01, SD-03, SD-07 |
| **SG-08** Stop-loss freshness annotation (**not a block**) | `strategy/risk.py:88-110` (`enforce_stop_losses`, legacy `bot/main.py:73-98` path); `backend/quant/risk/engine.py:113-130` (`TrailingStopManager.update`/`check_stops`) + `backend/quant/live/pipeline.py:164-174` (`price_map`, current path) | `get_price_fn`/`price_map` also carries a timestamp/`FreshnessReport`; both stop-loss paths **log** if the price is stale but still **execute the stop** — staleness here is a logged risk annotation, never a gate, per the entry-vs-exit asymmetry principle above | SD-07 |
| **SG-09** Periodic freshness watchdog | NEW `backend/worker/freshness_watchdog.py` (mirrors `WorkerWatchdog`) | `MarketDataFreshnessWatchdog`: periodic check of `last_successful_fetch` registry across actively-traded symbols/sources; on "all sources stale" → DB `kill_switch=True` + `alert_emergency` + `publish_alert(level="critical")` | SD-03, SD-05, SD-06 |

---

## 6. Reconciliation of the Fragmented Staleness Mechanisms

| Existing Mechanism | Location | Threshold | Scope | Disposition under unified design |
|---|---|---|---|---|
| Loader 26h daily check | `loader.py:84-97` | 26h | Daily bars, US-only (`_fetch_us`) | **Migrate** → SG-01; extend to KR (`_fetch_kr_pykrx`) via the shared `FreshnessConfig.daily_max_age_hours` |
| `StaleDataWatchdog` | `backend/worker/emergency.py:134-171` | 2h, configurable | Dead code — zero call sites | **Delete** the unused class, but salvage its `is_stale`/`check_all` shape as the basis for `FreshnessChecker` — the logic itself is reasonable, only its lack of integration is the problem |
| `_is_bar_stale` | `backend/strategy/base.py:80-99` | 600s (`BAR_STALE_SECONDS` env) | `on_bar()` — dormant live-streaming path | **Migrate** → SG-05; fold the threshold into `FreshnessConfig.intraday_max_age_seconds` |
| `_scan_and_trade` 3-day gate | `backend/strategy/indicator/strategy.py:103-118` | 3 calendar days, bare `except: pass` | Main active scan-and-trade path | **Replace** → SG-04; fixes the bare-except and the day-truncation issue simultaneously |

**Unifying principle**: a single `FreshnessConfig` instance (env-configurable,
similar to how `STOP_LOSS_PCT` is read via `os.environ.get`), a single
`FreshnessChecker`, and a single `DataFreshnessError` — used consistently by
SG-01 through SG-08. This guarantees that changing "what counts as stale for
a daily bar" in one place (`FreshnessConfig.daily_max_age_hours`) applies
identically to the loader (SG-01) **and** the scanner (SG-04), closing the
SD-06 meta-gap. Today, fixing one of the four mechanisms in isolation would
leave the other three (and their inconsistencies) untouched.

---

## 7. Risk Classification

| ID | Severity | Crash? | Silent? | Impact |
|---|---|---|---|---|
| SD-01 | HIGH | No | Yes (WARN-only, easily missed) | Stale daily bar feeds signal generation unflagged |
| SD-02 | MEDIUM (HIGH if intraday activated) | No | Yes (no check at all) | Intraday data age completely unknown |
| SD-03 | HIGH | No | Yes (no return-value metadata) | 4th-tier degraded cache indistinguishable from fresh fetch |
| SD-04 | MEDIUM | No | Mostly (WARN only past 30min) | FX-driven kill-switch math can be skewed |
| SD-05 | MEDIUM | No | Yes (heartbeat green, data stale) | Process-health monitoring gives false confidence about data |
| SD-06 | HIGH | No | Yes (structural, not surfaced anywhere) | Any single-mechanism fix is incomplete by construction |
| SD-07 | HIGH | No | Yes (bare float, no timestamp) | Stop-loss can mis-fire or fail to fire on stale price |
| SD-08 | HIGH | No | Yes (no hook for staleness info) | Buy/sell signals inherit upstream staleness with zero extra gating |
| SD-09 | MEDIUM | No | Yes (bare except swallows errors) | Coarse + fail-open staleness check on the live order path |
| SD-10 | LOW (informational) | No | N/A (hypothetical) | Future websocket-disconnect risk currently undesigned |
| SD-11 | MEDIUM | No | Yes (silent UTC coercion) | Cross-market timestamp comparisons can be off by ~9h |
| SD-12 | HIGH | No | Yes (passes 26h check despite being pre-session) | Yesterday's close can be traded as "today's fresh data" at market open |
| SD-13 | LOW-MEDIUM | No | Yes (no closed-bar marker) | Mid-session signals computed on a partial bar may flip post-close |

All entries are `Crash? = No` — every finding in this audit is a **silent
data-quality/degradation** risk, not a process crash. This is consistent with
the staleness theme: the system keeps running and keeps trading, just
possibly on data that no longer reflects reality.

### Most Dangerous Pattern

The combination of **SD-07 + SD-08 + SD-06** is the most dangerous compound
risk: a stale price can simultaneously (a) **fail to trigger** a protective
stop-loss (SD-07, because the comparison is a bare-float check with no
freshness awareness), and (b) **produce a fresh-looking buy signal** from
fusion on the same underlying stale `df` (SD-08, because fusion has no age
check at all) — and because the four existing staleness mechanisms are
fragmented and non-overlapping (SD-06), **no single existing check is
positioned to catch either side of this**. A position could be opened on
stale-but-signal-positive data, and its stop-loss could simultaneously fail
to protect it if the live price feed is also stale.

---

## 8. Affected Modules

Files a future TASK 3-3B implementation would touch:

| File | Line Range | SD-IDs | Nature of Change |
|---|---|---|---|
| `backend/quant/data/loader.py` | 83-97 | SD-01, SD-02, SD-11 | Add SG-01 (daily) and SG-02 (intraday) gates; fix UTC-coercion (SD-11) |
| `backend/quant/live/safeguards.py` | 164-263 | SD-03, SD-12 | Add SG-03 — tier/cache freshness metadata, session-aware cache age |
| `backend/strategy/indicator/strategy.py` | 60-68, 103-118 | SD-09, SD-06 | Replace inline 3-day gate with SG-04 |
| `backend/strategy/base.py` | 80-99 | SD-06, SD-10 | Migrate `_is_bar_stale` to SG-05 (`FreshnessConfig`-backed) |
| `backend/quant/signals/fusion.py` | 59-104 | SD-08 | Add SG-06 — optional freshness param + `blocked_reason="stale_data"` |
| `backend/quant/live/pipeline.py` | 277-279 | SD-01, SD-03, SD-07 | Add SG-07 — pre-buy freshness check on price sources |
| `strategy/risk.py` | 88-110 | SD-07 | Add SG-08 — freshness annotation (logging only, no block), legacy path |
| `backend/quant/risk/engine.py` | 56-130 | SD-07 | Add SG-08 — freshness annotation (logging only, no block), current path |
| `bot/main.py` | 73-98 | SD-07 | Legacy path — apply SG-08 here too **if** this path is still in active use (cross-ref `bot/scheduler.py`'s superseded status from TASK 3-1A) |
| `backend/brokers/kis.py` | 300-317 | SD-04 | FX-cache freshness — no dedicated SG gate proposed; flagged as a follow-up for `FreshnessConfig` extension |
| `backend/worker/emergency.py` | 134-171 | SD-06 | Delete dead `StaleDataWatchdog`; salvage its check logic into `FreshnessChecker` |
| `backend/worker/freshness_watchdog.py` (**new**) | — | SD-03, SD-05, SD-06 | New SG-09 — `MarketDataFreshnessWatchdog`, mirrors `WorkerWatchdog` |
| `backend/data/freshness.py` (**new**, suggested location) | — | all | New `DataFreshnessError`, `FreshnessConfig`, `FreshnessChecker`, `FreshnessReport` — alongside `backend/data/validator.py` and `backend/data/calendar.py` |

---

## 9. Unsafe Assumptions

These are assumptions implicitly baked into the current code that this audit
shows to be **false** or **unverified**:

1. *"`OHLCVRecovery`'s cache fallback means the data is recent enough to
   trade on."* — False. The cache can be up to 26 wall-clock hours old with
   zero visibility to the caller (SD-03), and can even predate the current
   trading session entirely (SD-12).
2. *"The loader's 26h WARNING means someone is watching the logs and will
   react."* — No alerting is wired to this WARNING; it is purely a log line
   (SD-01).
3. *"`_scan_and_trade`'s `try`/`except` around the staleness check means the
   check 'fails safe'."* — It fails **open**: any exception during the check
   silently skips it entirely, the opposite of "safe" (SD-09).
4. *"Coercing naive timestamps to UTC is timezone-neutral."* — False for
   cross-market comparison: KR (KST-anchored) and US (ET-anchored) "daily"
   timestamps differ by ~9h once both are naively treated as UTC (SD-11).
5. *"If the worker heartbeat is healthy, the data being traded on is
   healthy."* — These are orthogonal signals; heartbeat measures process
   liveness, not data freshness (SD-05).
6. *"A price returned by `get_price_kr`/`get_price_us` represents 'now'."* —
   True at the API-call boundary, but once it propagates into `risk.py`/
   `bot/main.py`/`position_tracker.py` as a bare float, there is no way to
   later verify how old it was when used (SD-07).

---

## 10. Cross-References to Prior Audits

- **TASK 3-1A** (`docs/MARKET_CALENDAR_AUDIT.md`) and **3-1B**
  (`backend/data/calendar.py`'s `CalendarService`): the timezone/session-window
  findings from 3-1A directly underpin SD-11 (timestamp drift). A future
  3-3B should reuse `CalendarService.is_trading_day`/`get_session_window`
  for the `session_aware` flag in `FreshnessConfig` (SG-03), rather than
  re-deriving session boundaries.
- **TASK 3-2A** (`docs/OHLCV_DATA_VALIDATION.md`) and **3-2B**
  (`docs/OHLCV_VALIDATION.md`): malformed-data validation and stale-data
  detection are **orthogonal axes** — a row can be `INVALID` per 3-2B's
  `OHLCVValidator` AND independently "stale" per this document, or `VALID`
  but stale, or `WARNING` but fresh, etc. The 3-2B `OHLCVValidator`'s
  `timestamp_order`/`timestamp_duplicate` checks address index
  **sortedness/uniqueness**, not wall-clock **freshness** — there is no
  overlap to reconcile, only a sequencing question for 3-3B (freshness check
  could run before or after the 3-2B validator; recommend **after**, since a
  duplicate/unsorted index could otherwise corrupt an `df.index[-1]`-based
  age calculation).
- **`docs/KIWOOM_AUDIT_REPORT.md`** (TASK 1-4): Kiwoom is broken for reasons
  entirely unrelated to staleness (wrong endpoints/field names). Staleness
  becomes relevant for Kiwoom only after that audit's findings are addressed
  — see §4.2.

---

## 11. Future Work / Not This Task (TASK 3-3B Preview)

A future implementation task ("TASK 3-3B") would, based on this audit:

1. Create `backend/data/freshness.py` with `DataFreshnessError`,
   `FreshnessConfig`, `FreshnessChecker`, `FreshnessReport` (§5) — following
   the same stateless-class/dataclass-config pattern as
   `backend/data/validator.py` (3-2B) and `backend/data/calendar.py` (3-1B).
2. Wire gates **SG-01 through SG-08** into `loader.py`, `safeguards.py`,
   `indicator/strategy.py`, `strategy/base.py`, `fusion.py`, `pipeline.py`,
   and `risk.py` per the Gate Map (§5).
3. Build **SG-09** — `backend/worker/freshness_watchdog.py`'s
   `MarketDataFreshnessWatchdog`, mirroring `WorkerWatchdog`.
4. Apply the **§6 reconciliation table**: delete `StaleDataWatchdog` (salvage
   its shape), migrate `_is_bar_stale` and the loader's 26h check, replace
   `_scan_and_trade`'s 3-day gate.
5. Resolve the **`bot/main.py` legacy-path question** (§8): determine whether
   `bot/main.py`'s `TradingEngine` is still actively run alongside (or
   instead of) `backend/quant/live/pipeline.py`; if active, apply SG-08
   there too.
6. Extend `FreshnessConfig`/`FreshnessChecker` to cover the FX-rate cache
   (SD-04, `backend/brokers/kis.py:300-317`) as a follow-up.
7. Document the result in a follow-on doc — suggested name (non-binding):
   `docs/MARKET_DATA_FRESHNESS.md`, mirroring the
   `OHLCV_DATA_VALIDATION.md` → `OHLCV_VALIDATION.md` precedent from 3-2A/3-2B.

None of the above is implemented in this task.

---

## 12. Verification (of this audit's claims)

Since this is an audit-only deliverable, "verification" means a checklist of
commands to re-derive each finding above against the current codebase:

```bash
# SD-01 / SD-02 — confirm 26h check is daily-only, US-only, WARN-only
grep -n "stale_hours\|interval == \"1d\"" backend/quant/data/loader.py

# SD-03 / SD-12 — confirm OHLCVRecovery cache has no metadata / session awareness
grep -n "max_cache_age_hours\|_get_cache\|_update_cache" backend/quant/live/safeguards.py

# SD-04 — confirm FX cache stale-fallback behavior
grep -n "_FX_CACHE\|stale\|age_min" backend/brokers/kis.py

# SD-05 — confirm WorkerWatchdog only checks process heartbeat, not data
grep -n "kill_switch\|HeartbeatMonitor\|is_alive" backend/worker/heartbeat.py

# SD-06 — confirm StaleDataWatchdog is dead code (expect 0 matches outside emergency.py)
grep -rn "StaleDataWatchdog" --include=*.py | grep -v "backend/worker/emergency.py"

# SD-06 / SG-05 — confirm _is_bar_stale scope and threshold
grep -n "_is_bar_stale\|BAR_STALE_SECONDS" backend/strategy/base.py backend/strategy/indicator/strategy.py

# SD-09 — confirm 3-day gate's day-truncation and bare except
grep -n "age_days\|except Exception" backend/strategy/indicator/strategy.py

# SD-08 — confirm fusion has no age/freshness parameter
grep -n "def evaluate\|freshness\|is_stale" backend/quant/signals/fusion.py

# SD-07 — confirm get_price_* return bare numerics
grep -n "def get_price_us\|def get_price_kr\|return" kis_adapter/market_data.py

# Confirm KIS OHLCV is still missing (cross-ref 3-2A DS-06)
grep -rn "def get_ohlcv" kis_adapter/ backend/brokers/

# Confirm OpenBB is not integrated (cross-ref §4.3)
grep -rn "openbb" --include=*.py -i | grep -v "pandas-ta-openbb\|pandas_ta_openbb"
```

Each command should reproduce the finding described in the corresponding
SD-XX entry above. If any command's output has changed (e.g. `get_ohlcv()`
now exists, or `_is_bar_stale` is called from a new location), the
corresponding SD-XX entry and Gate Map row should be re-evaluated before
TASK 3-3B proceeds.
