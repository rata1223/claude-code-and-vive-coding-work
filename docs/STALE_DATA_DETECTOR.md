# Stale Data Detector — Design Specification

This document specifies the design of `backend/data/stale_detector.py`
(TASK 3-3B), a 4-state market-data freshness classifier with an explicit
**Trading Gate** and **Recovery Hook**. It is the implementation design that
follows from `docs/STALE_DATA_AUDIT.md` (TASK 3-3A, merged via PR #69), and
is structured to mirror `docs/OHLCV_VALIDATION.md` (TASK 3-2B, the design
doc for `backend/data/validator.py` — itself still unimplemented/design-only).

This is a **design document** — `backend/data/stale_detector.py` and
`tests/data/test_stale_detector.py` are specified here but **not
implemented** in this task (see "Future Work" at the end).

> **Naming supersession.** TASK 3-3A's audit (§5, §11) tentatively proposed
> `backend/data/freshness.py` with `FreshnessConfig` / `FreshnessChecker` as
> placeholder names for a future design. TASK 3-3B's task prompt explicitly
> mandates the module name `backend/data/stale_detector.py`. **This document
> supersedes those tentative names.** The canonical names going forward are:
>
> | Audit's tentative name | Canonical name (this doc) |
> |---|---|
> | `backend/data/freshness.py` | `backend/data/stale_detector.py` |
> | `FreshnessConfig` | `StaleDetectorConfig` |
> | `FreshnessChecker` | `StaleDetector` |
> | `FreshnessReport` | `FreshnessReport` (unchanged — still an apt name for a per-check result) |
> | `is_stale` (binary) | `FreshnessReport.state: StaleState` (4-state) + `FreshnessReport.is_stale` property (`state == STALE`) |
>
> All references to "FreshnessChecker"/"FreshnessConfig" elsewhere
> (`docs/STALE_DATA_AUDIT.md`) should be read as references to
> `StaleDetector`/`StaleDetectorConfig` as defined here.

---

## 1. Purpose and Scope

This document specifies `StaleState`, a 4-state (`FRESH` / `WARNING` /
`STALE` / `UNKNOWN`) classifier for **wall-clock data freshness** across the
market-data pipeline, plus the `StaleDetector` class that produces
`FreshnessReport`s, the `StaleDetectorConfig` thresholds, the `evaluate_gate`
Trading Gate, and the `RecoveryHook` protocol.

**Orthogonal to 3-2B.** This is a *different axis* from TASK 3-2B's
`ValidationStatus` (`VALID` / `WARNING` / `INVALID`), which addresses
**structural** validity — sortedness, duplicate timestamps, OHLC
consistency, NaN/inf, price spikes, missing candles. `StaleState` addresses
**age** — how long ago the data was last updated relative to "now". A
dataframe can be `VALID` per `OHLCVValidator` and `STALE` per
`StaleDetector`, or `INVALID` and `FRESH`, independently of each other
(audit §10, "Cross-References to Prior Audits").

**In scope:**

- KIS REST price quotes — `kis_adapter/market_data.py`
  (`get_price_us`/`get_price_kr`)
- yfinance/pykrx daily and intraday OHLCV — `backend/quant/data/loader.py`
- `OHLCVRecovery`'s 4-tier cache fallback — `backend/quant/live/safeguards.py`
- FX-rate cache — `backend/brokers/kis.py` (`_FX_CACHE`)
- Strategy/fusion/execution consumption of the above
  (`backend/strategy/`, `backend/quant/signals/fusion.py`,
  `backend/quant/live/pipeline.py`, `backend/quant/risk/engine.py`,
  `strategy/risk.py`)
- The SG-09 periodic freshness watchdog (new
  `backend/worker/freshness_watchdog.py`)

**Out of scope:**

- Structural OHLCV validation — `backend/data/validator.py` (TASK 3-2B,
  still unimplemented as of this writing).
- Calendar/session logic itself — `backend/data/calendar.py`'s
  `CalendarService` (TASK 3-1B/3-1C). `StaleDetector` consumes
  `CalendarService` as a dependency for `session_aware` (§5.3); it does not
  re-implement holiday/session-window logic.
- The backtest data path (historical, not live — staleness is meaningless
  for a fixed historical dataset).

**Sequencing with 3-2B.** Per audit §10, the freshness check should run
**after** the (future) 3-2B validator: a duplicate or unsorted
`DatetimeIndex` would corrupt `df.index[-1]`-based age extraction (§5.1)
before the freshness check ever sees it. Since `backend/data/validator.py`
does not exist yet, this design introduces **no import dependency** on it —
this is documented as an **ordering recommendation** for whoever wires up
both modules (likely TASK 3-3C and a future "3-2C"): when both are present,
call `OHLCVValidator.validate()` first, then `StaleDetector.check_df()` on
the cleaned result.

This document references `SD-01..SD-13` (stale-data scenarios) and
`SG-01..SG-09` (gate map) from `docs/STALE_DATA_AUDIT.md` throughout.

---

## 2. Design Principles

1. **Four-state classification, not binary.** The audit's proposed
   `FreshnessChecker.check()` returned a binary `is_stale: bool`. This
   design generalizes that into `StaleState` with four members:
   `FRESH` (recently updated, trade normally), `WARNING` (older than ideal
   but still usable — log it), `STALE` (too old to act on for new entries),
   and `UNKNOWN` (couldn't determine the age at all). The audit's binary
   contract is preserved as a derived property:
   `FreshnessReport.is_stale == (state == StaleState.STALE)`. `WARNING` is a
   new "old but tradeable, log it" tier with no audit precedent. `UNKNOWN` is
   a new "couldn't determine age" tier that directly fixes SD-09: today's
   bare `except Exception: pass` around the 3-day staleness check
   (`backend/strategy/indicator/strategy.py:117-118`) silently skips the
   check entirely on a parse failure. Under this design, the same failure
   produces an explicit `FreshnessReport(state=UNKNOWN, ...)` — logged, never
   silent.

2. **"Worst wins" aggregation via `combine_state()`**, mirroring
   `combine_status()` (`docs/OHLCV_VALIDATION.md` §3.1). **Resolved design
   question** (the hardest call in this doc): the aggregation ordering is
   `FRESH(0) < WARNING(1) < STALE(2) < UNKNOWN(3)` — i.e. for multi-source
   rollups (SG-09, §11.8), `UNKNOWN` is treated as **worse than `STALE`**.
   Rationale: "I don't know how old this data is" is at least as dangerous as
   "I know it's old" — an `UNKNOWN` report could be hiding a `STALE` (or
   worse) condition behind a parse failure or a source that has gone
   completely silent (Kiwoom, §6). However, the **Trading Gate** (§9) does
   **not** mechanically inherit this ranking for blocking decisions — it
   treats `UNKNOWN` via its own `fail_closed_on_unknown` config toggle (§4),
   decoupled from `STALE`'s unconditional entry-block. This split is
   deliberate: `combine_state()`'s ranking answers "what's the worst thing we
   know across N sources, for logging/reporting/kill-switch purposes",
   while the gate answers "should *this* check, by itself, block *this*
   action" — two different questions that happen to both involve `UNKNOWN`,
   but do not need the same answer. See §9 for the full gate matrix.

3. **Entry-vs-exit asymmetry is the central safety invariant**, carried
   directly from the audit's Design Principle (audit §5):
   `STALE` blocks **new entries** (SG-06 fusion gate, SG-07 buy-loop gate);
   it **never** blocks protective exits or stop-losses (SG-08 — annotate/log
   only, the stop always executes). This is formalized as the Trading Gate's
   decision matrix in §9, and is the single most load-bearing rule in this
   document.

4. **Fail-closed is configurable, conservative by default.** `STALE` + entry
   → block, **always**, regardless of config (there is no toggle to make a
   `STALE` entry pass — if you need that, don't call the gate). `UNKNOWN` +
   entry → block **iff** `config.fail_closed_on_unknown` (default `True`).
   Operators who find `UNKNOWN` too noisy (e.g. during initial rollout, when
   most sources haven't been instrumented with timestamps yet) can set
   `fail_closed_on_unknown=False` to downgrade `UNKNOWN`+entry to
   `ALLOW_WITH_LOG` — but the default is the safe choice.

5. **`DataFreshnessError(Exception)`** — explicitly **not** a `RuntimeError`
   or `ValueError`. This mirrors `MarketClosedError`
   (`backend/data/calendar.py:63-69`) and `BadOHLCVError`
   (`docs/OHLCV_VALIDATION.md` §3.4): stale data is an *expected* operating
   condition (weekend gap, vendor delay, cold cache), not a programming error
   or a broker-side failure. `ConsecutiveFailureBreaker`
   (`backend/execution/circuit_breaker.py:14-40`, with `record_failure()` /
   `record_success()` / `is_open()`) must never count a `DataFreshnessError`
   as a broker failure — doing so could trip the circuit breaker and block
   *all* trading because *one* symbol's data went stale.
   **Naming note**: `backend/quant/data/loader.py:16` already defines
   `class StaleDataError(ValueError)` — an older, narrower, single-symbol
   check. It is left as-is (§13). The new exception is deliberately named
   `DataFreshnessError`, distinct in both name and base class, so it is never
   accidentally caught by an existing `except StaleDataError` or
   `except ValueError` handler that predates this design.

6. **Compatibility first.** `StaleDetector` never mutates `df` — any
   dataframe-level annotation is soft, via `df.attrs["freshness"]` (a plain
   dict, following pandas' `.attrs` mechanism, which survives most
   operations and adds no columns). Entry-blocks reuse **existing** result
   shapes with **zero signature changes**: `fusion.py`'s `FusionResult`
   already has `regime_blocked: bool = False` and `meta: dict =
   field(default_factory=dict)` (lines 28-29) but **no `blocked_reason`
   field** — SG-06 (§11.5) carries `meta={"blocked_reason": "stale_data"}`
   through the existing `regime_blocked=True` early-return (lines 88-91).
   `pipeline.py` already uses `"blocked_reason"` as a **dict key** in
   trade-record dicts (lines 224, 237, e.g. `"blocked_reason":
   "daily_loss_limit"`) — SG-07 (§11.6) reuses the same key with value
   `"stale_price"`.

7. **Stateless class + thin module function**, mirroring `OHLCVValidator`
   (`docs/OHLCV_VALIDATION.md` §8.1) and `CalendarService`. `StaleDetector`
   holds only a `StaleDetectorConfig` and has no caches, no singletons, and
   is safe to share across threads/symbols. `check_freshness()` is a
   module-level convenience wrapper for one-off calls (§8.3).

8. **Single unified config/detector/exception closes SD-06.** The audit's
   §2.3/§6 documented **four fragmented, non-overlapping staleness
   mechanisms** (loader's 26h check, the dead `StaleDataWatchdog`,
   `_is_bar_stale`'s 600s check, and `_scan_and_trade`'s 3-day gate) —
   different thresholds, different scopes, different exception types, no
   shared config. This design's single `StaleDetectorConfig` +
   `StaleDetector` + `DataFreshnessError` is the unifying layer: changing
   "what counts as stale for a daily bar" in one place
   (`config.daily_warn`/`daily_stale`) applies identically everywhere
   `context=DAILY_SCAN` is used (SG-01 *and* SG-04), closing the meta-gap
   that made each individual SD-XX fix incomplete by construction (audit
   §6's "Unifying principle").

---

## 3. Data Structures

### 3.1 `StaleState` and `combine_state()`

```python
class StaleState(str, Enum):
    """Four-tier outcome of a single freshness check.

    Ordering for combine_state() aggregation purposes (worst wins):
        FRESH(0) < WARNING(1) < STALE(2) < UNKNOWN(3)

    UNKNOWN ranks WORST for aggregation -- "I don't know how old this is"
    is treated as at least as dangerous as "I know it's old" (see Section 2,
    item 2). The Trading Gate (Section 9) does NOT mechanically inherit this
    ranking for blocking decisions: it gates UNKNOWN via its own
    fail_closed_on_unknown toggle, independent of STALE's unconditional
    entry-block. Aggregation severity (this enum's ranking) and gate action
    (Section 9's matrix) are deliberately decoupled.
    """
    FRESH = "fresh"
    WARNING = "warning"
    STALE = "stale"
    UNKNOWN = "unknown"


_STATE_RANK: dict[StaleState, int] = {
    StaleState.FRESH: 0,
    StaleState.WARNING: 1,
    StaleState.STALE: 2,
    StaleState.UNKNOWN: 3,
}


def combine_state(*states: StaleState) -> StaleState:
    """Return the most severe state among the given states ("worst wins").

    combine_state() with no arguments returns FRESH (identity element),
    mirroring combine_status() in docs/OHLCV_VALIDATION.md SS3.1.

    Examples:
        combine_state(FRESH, WARNING)        -> WARNING
        combine_state(WARNING, STALE)        -> STALE
        combine_state(STALE, UNKNOWN)        -> UNKNOWN
        combine_state(FRESH, FRESH, UNKNOWN) -> UNKNOWN
    """
    if not states:
        return StaleState.FRESH
    return max(states, key=lambda s: _STATE_RANK[s])
```

Follows the existing `XxxStatus(str, Enum)` lowercase-snake-case convention
used by `SessionType`, `ValidationStatus`, and `BlockReason`. `combine_state`
is a free function (not a method) so `MarketDataFreshnessWatchdog` (SG-09,
§11.8) and any future per-symbol multi-source rollup can both use it without
needing a `StaleDetector` instance.

**On the UNKNOWN-ranking resolution** (restated in full, since this is the
doc's hardest call): a naive design might rank `UNKNOWN` *between* `WARNING`
and `STALE`, reasoning "we don't know, so assume the middle". This design
rejects that for `combine_state()` specifically, because the dominant
real-world cause of `UNKNOWN` in this codebase is **a source that has gone
completely silent** — Kiwoom (always `UNKNOWN`, §6), a `df.index[-1]` parse
failure on a vendor schema change (SD-09), or a cache that was never written
(`_get_cache` miss). In every one of these cases, "we don't know" is
operationally closer to "assume the worst" than to "assume average". Ranking
`UNKNOWN` last means a single silent source poisons a multi-source
`combine_state()` rollup to `UNKNOWN` — which is exactly the signal SG-09's
watchdog (§11.8) needs to escalate. The Trading Gate's separate
`fail_closed_on_unknown` toggle (§9) exists precisely so that this
"assume-the-worst" aggregation choice does not *also* force every `UNKNOWN`
report to hard-block every entry by default in deployments where, say, KIS
price quotes are simply not yet `PriceQuote`-wrapped (today's reality, §6) —
operators can tune that independently.

### 3.2 `PriceQuote`

```python
@dataclass(frozen=True)
class PriceQuote:
    """A price value with an attached observation timestamp and source tag.

    Rationale: kis_adapter/market_data.py:21-32 (get_price_us/get_price_kr)
    return a bare float/int with zero timestamp (audit SD-07) -- a
    stop-loss or buy-loop comparison against this value has no way to know
    how stale the underlying REST response was by the time it's used.
    Wrapping the return value at the call site --
        PriceQuote(value=151.23, ts=datetime.now(timezone.utc), source="kis")
    -- gives StaleDetector.check_quote() something concrete to evaluate for
    SG-07 (buy-loop, SS11.6) and SG-08 (stop-loss annotation, SS11.7).

    Attributes:
        value: The price.
        ts: UTC-aware timestamp captured immediately after the broker call
            returns -- i.e. "when this value was observed by our process",
            not "when the underlying exchange trade occurred" (the latter is
            unknowable for a REST quote and not needed here).
        source: Free-text source tag, e.g. "kis", "yfinance", "pykrx",
            "cache". Used only for FreshnessReport.source / logging.
    """
    value: float
    ts: datetime
    source: str
```

This is **forward-looking**: no call site constructs a `PriceQuote` today.
`StaleDetector.check_quote()` (§8.2) accepts either a `PriceQuote` or a raw
`(value, ts)` tuple, so adoption can be incremental — a call site can start
by passing `(price, datetime.now(timezone.utc))` without first refactoring
`kis_adapter/market_data.py`'s return types, and migrate to returning
`PriceQuote` from `get_price_us`/`get_price_kr` later as a separate,
non-blocking change.

### 3.3 `FreshnessReport`

```python
@dataclass(frozen=True)
class FreshnessReport:
    """Result of one StaleDetector freshness check.

    Attributes:
        symbol: Ticker/code that was checked (e.g. "AAPL", "005930").
        source: Data source tag -- "yfinance", "pykrx", "kis", "cache",
            "kiwoom", etc. (see Section 6's per-source table).
        context: One of "DAILY_SCAN", "INTRADAY_BAR", "CACHE_FALLBACK",
            "LIVE_PRICE" (StaleDetectorConfig's per-context threshold keys,
            Section 4). Stable string, not an Enum, so new contexts can be
            added without an enum migration.
        state: The classified StaleState (Section 5).
        last_updated_at: The timestamp the age was computed from, or None if
            state == UNKNOWN because no timestamp could be determined.
        age: now - last_updated_at (after any session-aware/KR-drift
            adjustment, Sections 5.3/5.4), or None if state == UNKNOWN.
        warn_threshold: The warn cutoff used for this check (from
            StaleDetectorConfig, resolved via _thresholds_for(context)).
        stale_threshold: The stale cutoff used for this check.
        recovery_possible: Whether RecoveryHook.attempt_recovery() is
            meaningful for this source (False for permanently-dead sources
            like Kiwoom, Section 6). Default True.
        checked_at: UTC timestamp when this report was produced.
        detail: Optional structured payload, e.g. {"tier": 4} for an
            OHLCVRecovery cache report (SD-03 visibility), or
            {"session_boundary": "2026-06-10T00:00:00+09:00"} for a
            session-aware cache check (SD-12, Section 5.3).

    Two thresholds (warn_threshold AND stale_threshold) are carried per
    report -- unlike the audit's single-threshold binary model, the 4-state
    model needs two cut points to distinguish FRESH/WARNING/STALE (Section
    5's table). Carrying both on the report (rather than requiring the
    caller to re-look-up StaleDetectorConfig) makes summary_line() and log
    lines self-contained.
    """
    symbol: str
    source: str
    context: str
    state: StaleState
    last_updated_at: Optional[datetime]
    age: Optional[timedelta]
    warn_threshold: timedelta
    stale_threshold: timedelta
    recovery_possible: bool = True
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    detail: dict = field(default_factory=dict)

    @property
    def is_stale(self) -> bool:
        """True iff state == STALE. Mirrors the audit's binary `is_stale`."""
        return self.state == StaleState.STALE

    @property
    def is_fresh(self) -> bool:
        return self.state == StaleState.FRESH

    def summary_line(self) -> str:
        """One-line human-readable summary for log lines, e.g.:

            "AAPL[yfinance/DAILY_SCAN]: STALE (age=51.2h, stale>50h)"
            "005930[cache/CACHE_FALLBACK]: STALE (session boundary crossed)"
            "KIWOOM[kiwoom/LIVE_PRICE]: UNKNOWN (no timestamp available)"
        """
```

### 3.4 `DataFreshnessError`

```python
class DataFreshnessError(Exception):
    """Raised by StaleDetector when a check classifies as STALE (or UNKNOWN
    with config.fail_closed_on_unknown=True) and raise_on_stale=True (the
    default, from StaleDetectorConfig.raise_on_stale or a per-call override)
    was in effect.

    NOT a subclass of RuntimeError or ValueError -- intentional, mirrors
    MarketClosedError (backend/data/calendar.py:63-69) and BadOHLCVError
    (docs/OHLCV_VALIDATION.md SS3.4). Stale data is an *expected* operating
    condition (weekend gap, vendor delay, cold cache, a source that hasn't
    refreshed yet), not a programming error or a broker-side failure.
    ConsecutiveFailureBreaker (backend/execution/circuit_breaker.py:14-40,
    record_failure/record_success/is_open) must NOT count a
    DataFreshnessError as a broker failure -- the broker/data vendor may be
    working perfectly; the *data* is simply old. Counting it as a broker
    failure could trip the circuit breaker on a single stale ticker and
    block trading for unrelated symbols.

    Naming-collision note: backend/quant/data/loader.py:16 already defines
    StaleDataError(ValueError) -- an older, narrower, single-symbol
    staleness check, predating this design. It is left as-is (Section 13)
    for backward compatibility with any existing `except StaleDataError` or
    `except ValueError` handlers. The new exception introduced here is
    deliberately named DataFreshnessError -- distinct in both name and base
    class -- so it is never accidentally caught by code written against the
    older StaleDataError. New staleness-related raises in this design always
    use DataFreshnessError, never StaleDataError or bare ValueError.

    Attributes:
        symbol: The symbol being checked.
        report: The FreshnessReport (state == STALE, or state == UNKNOWN
            with fail_closed_on_unknown) that triggered the raise, so the
            caller can log/inspect *why* without re-running the check.
    """

    def __init__(self, symbol: str, report: "FreshnessReport") -> None:
        self.symbol = symbol
        self.report = report
        super().__init__(f"Stale data for {symbol}: {report.summary_line()}")
```

---

## 4. `StaleDetectorConfig`

```python
@dataclass(frozen=True)
class StaleDetectorConfig:
    """Configurable thresholds for StaleDetector.

    Each context (DAILY_SCAN, INTRADAY_BAR, CACHE_FALLBACK, LIVE_PRICE) has
    a (warn, stale) threshold pair -- the 4-state model needs two cut points
    per context, unlike the audit's single-threshold binary sketch.
    """
    # DAILY_SCAN -- yfinance/pykrx daily bars (SG-01, SG-04)
    daily_warn: timedelta = timedelta(hours=26)
    daily_stale: timedelta = timedelta(hours=74)

    # INTRADAY_BAR -- live-bar streaming, on_bar() (SG-05)
    intraday_warn: timedelta = timedelta(minutes=10)
    intraday_stale: timedelta = timedelta(minutes=30)

    # CACHE_FALLBACK -- OHLCVRecovery tier-4 cache (SG-03)
    cache_warn: timedelta = timedelta(hours=26)
    cache_stale: timedelta = timedelta(hours=74)

    # LIVE_PRICE -- KIS REST quote latency (SG-07, SG-08)
    live_warn: timedelta = timedelta(seconds=30)
    live_stale: timedelta = timedelta(minutes=5)

    session_aware: bool = True            # SD-12 -- cache age vs. last session boundary
    fail_closed_on_unknown: bool = True   # UNKNOWN + entry -> block
    kr_drift_margin: timedelta = timedelta(hours=9)  # SD-11 KR/US UTC-coercion drift
    raise_on_stale: bool = True


DEFAULT_CONFIG = StaleDetectorConfig()
```

`frozen=True` so a single shared `DEFAULT_CONFIG` instance can be reused
across threads/symbols without accidental mutation, mirroring
`ValidatorConfig`'s style (`docs/OHLCV_VALIDATION.md` §4).

**Default justification, tied to the audit:**

- **`daily_warn=26h`** reproduces today's loader behavior
  (`backend/quant/data/loader.py:84-97`, the "주말·휴장 제외: 26h 허용" check)
  exactly at the `WARNING` boundary — `age <= 26h` is `FRESH`/`WARNING`
  exactly as before, so SG-01's migration (§11.1) is **zero regression** on
  the happy/warn path. The only behavioral change at this threshold is that
  the *previous* WARN-only behavior becomes a `WARNING`-tier
  `FreshnessReport` instead of a bare log line — still non-blocking.

- **`daily_stale=74h`**: a Friday US market close (16:00 ET) scanned the
  following Monday before market open (e.g. 08:30 ET) is `~64.5h` old and is
  **legitimately fresh** — it is simply the most recent available daily bar,
  exactly as expected over a weekend. `74h` clears this with margin (≈10h of
  slack) while still catching a genuine multi-day data outage (e.g. three
  consecutive failed fetch attempts spanning a long weekend would be
  `>74h`). **This wall-clock fallback is secondary.**
  `session_aware=True` (default) is the **canonical** staleness computation
  for `DAILY_SCAN`/`CACHE_FALLBACK` — it asks "how many trading sessions have
  been missed?" via `CalendarService.prev_trading_day()`
  (`backend/data/calendar.py:770`), not "how many wall-clock hours have
  elapsed?". `daily_stale`/`cache_stale` are the fallback thresholds used
  only when `session_aware=False` (e.g. in unit tests that don't configure a
  `CalendarService`, or for a hypothetical future market with no
  `CalendarService` coverage).

- **`intraday_warn=10min`** is an **exact migration** of
  `_BAR_STALE_SECONDS` (`backend/strategy/base.py:44`,
  `int(os.environ.get("BAR_STALE_SECONDS", "600"))` = 600s = 10min) — SG-05
  (§11.4) is behavior-neutral by construction. `intraday_stale=30min` is new
  (the old mechanism was binary: `age > 600s` → skip the bar, with no
  intermediate tier).

- **`cache_warn`/`cache_stale`** mirror
  `OHLCVRecovery.__init__(max_cache_age_hours=26)`
  (`backend/quant/live/safeguards.py:171`) at the `WARNING` boundary, for the
  same zero-regression reason as `daily_warn`. The `session_aware` override
  (§5.3) is the actual fix for SD-12 — a cache that is wall-clock-fresh
  (`<26h`) but predates the current trading session is reclassified `STALE`
  regardless of `cache_warn`/`cache_stale`.

- **`live_warn=30s` / `live_stale=5min`** are new — there is no existing
  threshold for KIS REST quote latency (SD-07: `kis_adapter/market_data.py`
  returns a bare float with zero timestamp today, so this context is
  presently unmeasurable; see §6's "KIS price" row). `30s` covers normal
  REST round-trip variance plus rate-limit backoff
  (`kis_adapter/client.py`'s retry-3 behavior); `5min` is conservative enough
  that exceeding it likely indicates a stuck/hung broker session rather than
  ordinary latency.

- **`session_aware=True`** (SD-12) — see `daily_stale` above and §5.3's
  worked example. Uses `CalendarService.prev_trading_day`/`get_session_window`
  (`backend/data/calendar.py:770`/`:602`).

- **`fail_closed_on_unknown=True`** — see §2 item 4 and §9's gate matrix.

- **`kr_drift_margin=9h`** (SD-11): when a symbol resolves to the KR market
  (reuse the `_is_kr` regex pattern — **locally duplicated**, not imported,
  per the 3-2B precedent at `docs/OHLCV_VALIDATION.md` §6.2's
  `_KR_SYMBOL_RE = re.compile(r"^\d{6}$")`) and the dataframe index is
  UTC-naive, subtract `kr_drift_margin` from the computed age before
  threshold comparison. This mitigates SD-11: a KR daily bar's index is
  KST-midnight-anchored while a US daily bar's index is ET-midnight-anchored,
  but both get naively `tz_localize("UTC")`'d
  (`backend/quant/data/loader.py:86-87`,
  `backend/strategy/indicator/strategy.py:108-113`) — a difference of ~9
  hours (KST = UTC+9, ET ≈ UTC-5, difference ≈14h in absolute terms, but the
  *trading-day-boundary* drift relevant to "is this today's bar" is bounded
  by KST's UTC+9 offset). Subtracting `9h` from a KR symbol's naively-UTC age
  before comparing against `daily_warn`/`daily_stale` prevents a genuinely
  fresh KR bar (dated "today" KST) from being computed as artificially
  older than an equivalent US bar at the same wall-clock instant.
  `session_aware=True` (when a `CalendarService` is configured) is the more
  precise fix and takes precedence — `kr_drift_margin` is the fallback for
  `session_aware=False` deployments or for `LIVE_PRICE`/`INTRADAY_BAR`
  contexts where session-window math is less directly applicable.

---

## 5. State Resolution

| Condition | `StaleState` |
|---|---|
| `last_updated_at is None`, or the timestamp/index could not be parsed | `UNKNOWN` |
| `age <= warn_threshold` | `FRESH` |
| `warn_threshold < age <= stale_threshold` | `WARNING` |
| `age > stale_threshold` | `STALE` |

`age` here is the (possibly session-aware- and/or KR-drift-adjusted) value —
see §5.3/§5.4. `warn_threshold`/`stale_threshold` are resolved from
`StaleDetectorConfig` via an internal `_thresholds_for(context)` lookup
(§8.2).

### 5.1 Age extraction and `UNKNOWN`

`StaleDetector` exposes two entry points for obtaining `last_updated_at`
(§8.2):

- **`check(last_updated_at, ...)`** — the direct path. Caller already has a
  `datetime` (or `None`). If `None`, `state=UNKNOWN` immediately
  (`detail={"reason": "no_timestamp"}`).

- **`check_df(df, ...)`** — extracts `last_updated_at = df.index[-1]`,
  coercing it to a UTC-aware `datetime` via the **same pattern** already used
  at `backend/worker/emergency.py:154-157` and
  `backend/strategy/indicator/strategy.py:107-112`
  (`to_pydatetime()` if available, else `datetime.combine(...)`, then
  `tzinfo=timezone.utc` if naive). **Unlike SD-09's bare `except: pass`**, a
  coercion failure here does **not** silently skip the check — it returns
  `FreshnessReport(state=UNKNOWN, last_updated_at=None, age=None,
  detail={"reason": "index_coercion_failed", "error": str(exc)})` and logs
  at `logger.warning(...)` (§12). The caller (e.g. SG-04, §11.3) then handles
  `UNKNOWN` exactly like any other non-`FRESH` state via the Trading Gate
  (§9) — never via a bare `except: pass`.

Both paths converge on the same state-resolution table above once
`last_updated_at` (or `None`) is known.

### 5.2 `combine_state` worst-wins

Defined fully in §3.1. Referenced here for SG-09's multi-source rollup
(§11.8): `MarketDataFreshnessWatchdog` calls `combine_state(*reports)` across
all `(symbol, source)` pairs it tracks, and a single `UNKNOWN` source (e.g.
Kiwoom, always `UNKNOWN` per §6) makes the rollup `UNKNOWN` — the worst
classification, by design (§3.1's "On the UNKNOWN-ranking resolution").

### 5.3 Session-aware cache age (SD-12)

When `context == "CACHE_FALLBACK"` and `config.session_aware` is `True`, age
is computed **relative to the last trading-session boundary**, not
`now - cached_ts`. Concretely:

1. Resolve the symbol's market (`KR`/`US`, same heuristic as §4's
   `kr_drift_margin` resolution) and map to `calendar.Market.KRX`/`NYSE` (the
   3-2B `_resolve_calendar_market` precedent, `docs/OHLCV_VALIDATION.md`
   §6.2).
2. Call `CalendarService.get_session_window(market, today)` /
   `CalendarService.prev_trading_day(market, today)`
   (`backend/data/calendar.py:602`/`:770`) to determine the start of the
   **current or most recently opened** trading session relative to `now`.
3. If `cached_ts < session_start` — i.e. the cache was written **before**
   the most recent session boundary — the cache predates the current
   session and is classified `STALE` (`detail={"session_boundary":
   session_start.isoformat(), "reason": "predates_session"}`), **regardless**
   of how the wall-clock `cache_warn`/`cache_stale` thresholds would classify
   it.
4. Otherwise, fall back to the wall-clock `cache_warn`/`cache_stale`
   comparison from §5's table.

**Worked example (from audit SD-12):** `OHLCVRecovery._cache` (`safeguards.py:173`,
`self._cache: dict[str, tuple[pd.DataFrame, datetime]]`) was last written by
`_update_cache` (`safeguards.py:251-252`) at **15:30 KST yesterday**. Today,
`_get_cache` (`safeguards.py:254-263`) is consulted at **09:00 KST** (the
moment KR market opens) because tiers 1–3 all failed. Wall-clock age =
`~17.5h` — well under `cache_warn=26h`, so a wall-clock-only check would
classify this `FRESH`. But the KRX session boundary for "today" is
`09:00 KST today`, which is **after** the cache's `15:30 KST yesterday`
write — the cache predates today's session entirely. Session-aware logic
therefore yields `STALE` (`detail={"session_boundary": "...T00:00:00+09:00",
"reason": "predates_session"}`), correctly flagging "this is yesterday's
close being offered as if it were usable for today's open" — exactly the
SD-12 scenario.

### 5.4 KR/US drift adjustment (SD-11)

When `session_aware` is `False` (or `CalendarService` is unavailable) and the
symbol resolves to `KR`, subtract `config.kr_drift_margin` (default `9h`)
from the naively-computed `age` before comparing against
`warn_threshold`/`stale_threshold`, per §4's justification. This adjustment
applies to `DAILY_SCAN` and `CACHE_FALLBACK` contexts (where `df.index[-1]`
is a date-anchored timestamp); it does not apply to `INTRADAY_BAR` or
`LIVE_PRICE` (where timestamps are already UTC-aware wall-clock instants with
no date-anchoring ambiguity).

---

Note: `docs/OHLCV_VALIDATION.md`'s §6 (Missing-Candle Detector) and §7
(Price-Spike Detector) deep-dives have no freshness analog — there is no
"missing candle" or "price spike" concept in a wall-clock-age model. The
equivalent depth for this design lives in §5.3 (session-aware cache age) and
§6 below (per-source health semantics).

---

## 6. Per-Source Health Semantics ("Data Source Health")

| Source | `last_updated_at` source | `FRESH` means | `STALE` means | `UNKNOWN` when | Recovery action | Ref |
|---|---|---|---|---|---|---|
| KIS price | `PriceQuote.ts` | REST round-trip latency ≤ `live_warn` (30s) | latency > `live_stale` (5min) | `PriceQuote` not yet wrapped — today's `get_price_us`/`get_price_kr` return bare numerics with no timestamp | retry the REST call (bounded, §10) | `kis_adapter/market_data.py:21-32`; audit §4.1 |
| Kiwoom | none | — | — | **always `UNKNOWN`** — `backend/brokers/kiwoom.py` is a 29-line `NotImplementedError` stub; no method is callable | none — `recovery_possible=False` | `backend/brokers/kiwoom.py`; audit §4.2 |
| OpenBB | N/A | — | — | N/A — **not integrated anywhere** in this codebase | N/A | audit §4.3 |
| yfinance | `df.index[-1]` | `age <= daily_warn` (26h) | `age > daily_stale` (74h) | unparseable index (coercion failure, §5.1) | re-fetch via `DataLoader.fetch()` | `loader.py:83-97`; audit §4.4 |
| pykrx | `df.index[-1]` | same as yfinance | same as yfinance | unparseable index, **or** the silent `.KS`-fallback-to-yfinance path masking the real pykrx failure | re-fetch via `DataLoader.fetch()` | `safeguards.py:224-238` |
| OHLCVRecovery cache | `_update_cache` timestamp | session-fresh (§5.3) | session-stale (SD-12, §5.3), or wall-clock `> cache_stale` | cache miss (`_get_cache` returns `None` because no entry exists), or no timestamp recorded | re-run tiers 1–3 (`_try_broker`/`_try_yfinance`/`_try_pykrx`) | `safeguards.py:251-263` |
| FX cache | `_FX_CACHE["ts"]` — **`time.monotonic()`**, NOT wall-clock `datetime` | age ≤ `_FX_TTL` (1h) | `age_min > 30` past TTL (existing `kis.py:315` WARN condition) | — (always has *a* value once initialized) | re-fetch `KRW=X` via yfinance | `kis.py:300-317`; SD-04 |

**Per-source notes:**

- **KIS price**: staleness here is pure REST-call latency — the value
  itself always represents "now" at the moment the call returns, so there is
  nothing to be stale *about* except how long ago that "now" was by the time
  a downstream comparison happens. Today this is **completely invisible**:
  `kis_adapter/market_data.py:21-32`'s `get_price_us`/`get_price_kr` return a
  bare `float`/`int`. Until call sites wrap the return value in a
  `PriceQuote` (§3.2), every KIS price check is `UNKNOWN` by construction —
  this is an accurate reflection of today's reality, not a defect in this
  design. `config.fail_closed_on_unknown` (default `True`) means that, if
  `StaleDetector` were wired up today with zero other code changes, **every**
  KIS-price-based entry would be blocked — which is why §11.6 (SG-07)
  describes `PriceQuote`-wrapping as a co-requirement, not optional polish.

- **Kiwoom**: contributes **zero live market data today**. Every
  `BrokerAdapter` method on `backend/brokers/kiwoom.py` raises
  `NotImplementedError`. A `StaleDetector.check(None, source="kiwoom", ...)`
  call therefore always produces `state=UNKNOWN, recovery_possible=False` —
  there is nothing to retry. This is the canonical example of
  `recovery_possible=False` driving SG-09's kill-switch condition (§10):
  Kiwoom alone going `UNKNOWN` should **never** trip the kill-switch (it has
  *always* been `UNKNOWN` — that's not new information), but if Kiwoom
  *and* every other configured source go `STALE`/`UNKNOWN` with
  `recovery_possible=False`, that **is** new information.

- **OpenBB**: explicitly **N/A** — flagged here, as the audit did (§4.3), so
  a future reader does not waste time searching for an OpenBB data adapter
  that does not exist. The only OpenBB-related artifact in this codebase is
  `pandas-ta-openbb`, an indicator-library fork unrelated to data fetching.
  If OpenBB integration is ever added as a real data source, it should flow
  through the same `OHLCVRecovery`/`StaleDetector` path as yfinance/pykrx,
  not introduce a sixth bespoke staleness mechanism.

- **FX cache is special-cased.** `_FX_CACHE["ts"]`
  (`backend/brokers/kis.py:300-317`) is set via `time.monotonic()` at lines
  302/310/314 — a **process-uptime-relative** clock, not comparable to
  `datetime.now(timezone.utc)`. `StaleDetector` cannot call `check_df()` or
  `check()` with a `datetime` for this source; it would need a variant that
  accepts a **monotonic-seconds age directly** (e.g.
  `check_monotonic_age(age_seconds, *, symbol="FX", source="fx_cache",
  context="LIVE_PRICE", ...)`). **No SG number is assigned to the FX cache**
  in the audit's gate map (audit §8: "no dedicated SG number proposed; flagged
  as a follow-up for `FreshnessConfig` extension") — this row is documented
  here as an **integration nuance for a future FX-cache gate**, not wired
  into §11's SG-01..SG-09 integration points. Implementing
  `check_monotonic_age()` is listed under "Future Work".

---

## 7. Template Alignment Note

`docs/OHLCV_VALIDATION.md`'s §6 (Missing Candle Detector) and §7 (Price Spike
Detector) are deep-dive sections specific to **structural** validation
concepts (gaps in a date range; bar-over-bar percentage jumps) that have no
direct analog in a **wall-clock freshness** model — there is no "missing
candle" or "spike" concept when the question is simply "how old is the most
recent data point?". The equivalent depth for this design is distributed
across §5.3 (session-aware cache age, the closest analog to "missing candle"
in spirit — both ask "did we miss something we should have received?") and
§6 (per-source health semantics, the closest analog to "what does this
specific data source's failure mode look like?"). Section numbering above
this point (§1–§5) and below (§8 onward) otherwise tracks
`docs/OHLCV_VALIDATION.md`'s structure 1:1 to ease cross-referencing for
readers familiar with both documents.

---

## 8. `StaleDetector` — Main Class and Module Function

### 8.1 Class vs. module function

**Decision**: a class, `StaleDetector`, plus a thin module-level convenience
function `check_freshness()` — mirroring `OHLCVValidator`/`validate_ohlcv()`
(`docs/OHLCV_VALIDATION.md` §8.1) and `CalendarService`.

- `StaleDetector` is **stateless** (holds only a `StaleDetectorConfig`) and
  **thread-safe** — no singleton, no `get_stale_detector()`. Callers
  construct `StaleDetector()` (default config) or
  `StaleDetector(config=StaleDetectorConfig(daily_stale=timedelta(hours=48)))`
  cheaply, once, and reuse the instance across symbols/calls (e.g. one shared
  instance inside `OHLCVRecovery.__init__`, mirroring how `OHLCVValidator`
  is proposed to be constructed once inside `OHLCVRecovery.__init__` at
  `docs/OHLCV_VALIDATION.md` §9.2).
- `check_freshness()` covers one-off calls (scripts, tests, REPL) where
  constructing an instance is unnecessary ceremony.

### 8.2 Class skeleton

```python
class StaleDetector:
    """Classifies market data freshness into FRESH/WARNING/STALE/UNKNOWN.

    Stateless and thread-safe -- safe to share a single instance across
    threads/symbols. Construct once with a StaleDetectorConfig (or use
    DEFAULT_CONFIG) and call check()/check_df()/check_quote() per data point.

    Usage:
        detector = StaleDetector()  # DEFAULT_CONFIG
        report = detector.check_df(df, symbol="AAPL", source="yfinance",
                                    context="DAILY_SCAN")
        if report.is_stale:
            ...  # see Section 9, evaluate_gate()

    Usage (non-raising, e.g. inside OHLCVRecovery's per-tier waterfall):
        report = detector.check_df(df, symbol="AAPL", source="cache",
                                    context="CACHE_FALLBACK",
                                    raise_on_stale=False)
        if report.is_stale:
            return None  # waterfall: signal "no usable data this tier"
    """

    def __init__(self, config: Optional[StaleDetectorConfig] = None) -> None:
        self.config = config or DEFAULT_CONFIG

    def check(
        self,
        last_updated_at: Optional[datetime],
        *,
        symbol: str,
        source: str,
        context: str,
        now: Optional[datetime] = None,
        recovery_possible: bool = True,
        raise_on_stale: Optional[bool] = None,
    ) -> FreshnessReport:
        """Classify a single (symbol, source) pair given an explicit
        last_updated_at (or None -> UNKNOWN immediately).

        now defaults to datetime.now(timezone.utc) -- overridable for tests.

        raise_on_stale: per-call override of self.config.raise_on_stale.
        When the effective value is True, raises DataFreshnessError if
        state == STALE, or if state == UNKNOWN and
        self.config.fail_closed_on_unknown is True.
        """

    def check_df(
        self,
        df: "pd.DataFrame",
        *,
        symbol: str,
        source: str,
        context: str,
        now: Optional[datetime] = None,
        raise_on_stale: Optional[bool] = None,
    ) -> FreshnessReport:
        """Extracts last_updated_at = df.index[-1] (Section 5.1) and
        delegates to check(). On index-coercion failure, returns
        state=UNKNOWN (logged at WARNING) -- never silent (fixes SD-09).

        Applies session-aware (Section 5.3, context=="CACHE_FALLBACK") and
        KR-drift (Section 5.4) adjustments before classification.
        """

    def check_quote(
        self,
        quote: "PriceQuote | tuple[float, datetime]",
        *,
        symbol: str,
        source: str = "kis",
        context: str = "LIVE_PRICE",
        now: Optional[datetime] = None,
        raise_on_stale: Optional[bool] = None,
    ) -> FreshnessReport:
        """Classify a PriceQuote (Section 3.2) or raw (value, ts) tuple.
        Delegates to check() using quote.ts / tuple[1] as last_updated_at.
        """

    def _thresholds_for(self, context: str) -> tuple[timedelta, timedelta]:
        """Internal: (warn_threshold, stale_threshold) for the given
        context, looked up from self.config. Raises ValueError for an
        unrecognized context string -- a programming error, not a data
        condition, so this is a plain ValueError (not DataFreshnessError).
        """
```

`raise_on_stale=None` (the default for all three `check*` methods) means
"use `self.config.raise_on_stale`". Passing `True`/`False` explicitly
overrides the config for that one call — e.g. SG-03's `_get_cache` (§11.2)
always passes `raise_on_stale=False` because it communicates "no usable data"
via `return None`, not via an exception, regardless of what the shared config
says.

### 8.3 Module-level convenience function

```python
def check_freshness(
    df_or_ts,
    *,
    symbol: str,
    source: str,
    context: str,
    config: Optional[StaleDetectorConfig] = None,
    now: Optional[datetime] = None,
    raise_on_stale: Optional[bool] = None,
) -> FreshnessReport:
    """Module-level convenience wrapper, mirroring validate_ohlcv()
    (docs/OHLCV_VALIDATION.md SS8.3):
        StaleDetector(config).check_df(df_or_ts, ...) if df_or_ts is a
        DataFrame, else StaleDetector(config).check(df_or_ts, ...).

    Prefer constructing a shared StaleDetector instance in hot loops
    (e.g. strategy.py's per-symbol scan, or inside OHLCVRecovery.__init__)
    to avoid repeated config lookups; use this function for one-off calls
    (scripts, tests, REPL).
    """
```

---

## 9. Trading Gate

This section defines the task's core "Trading Gate" requirement:
`evaluate_gate()`, a pure function from `(FreshnessReport, is_exit, config)`
to a `GateDecision`.

```python
class GateDecision(str, Enum):
    """What the Trading Gate says to do with this report."""
    ALLOW = "allow"
    ALLOW_WITH_LOG = "allow_with_log"
    BLOCK = "block"


def evaluate_gate(
    report: FreshnessReport,
    *,
    is_exit: bool,
    config: Optional[StaleDetectorConfig] = None,
) -> GateDecision:
    """Decide whether `report`'s data may be used for a trading action.

    is_exit=False -> a new-position entry decision (SG-06 fusion, SG-07
        buy-loop).
    is_exit=True  -> a protective exit / stop-loss decision (SG-08).

    See the decision matrix below. This function never raises -- raising
    DataFreshnessError (if desired) is the caller's responsibility, driven
    by check()/check_df()/check_quote()'s own raise_on_stale handling
    (Section 8.2). evaluate_gate() is the *non-raising* decision API for
    callers (like fusion.evaluate(), Section 11.5) that need to fold the
    decision into an existing result shape rather than catching an
    exception.
    """
```

### Decision matrix

| `state` | Entry (`is_exit=False`) | Exit / Stop-loss (`is_exit=True`) |
|---|---|---|
| `FRESH` | `ALLOW` | `ALLOW` |
| `WARNING` | `ALLOW_WITH_LOG` | `ALLOW_WITH_LOG` |
| `STALE` | **`BLOCK`** — `DataFreshnessError`, or `meta={"blocked_reason": "stale_data"}` (SG-06) / dict-key `"blocked_reason": "stale_price"` (SG-07) | `ALLOW_WITH_LOG` — **never block** |
| `UNKNOWN` | `BLOCK` if `config.fail_closed_on_unknown` (default `True`), else `ALLOW_WITH_LOG` | `ALLOW_WITH_LOG` — **never block** |

> **The load-bearing safety invariant.** The bottom-right cell —
> `STALE`/`UNKNOWN` + exit = `ALLOW_WITH_LOG`, **never** `BLOCK` — is the
> single most important rule in this entire design, restated verbatim from
> the audit's Design Principle (audit §5):
>
> > Blocking a new entry is reversible and low-cost: "no new risk is taken
> > this cycle." Blocking a protective exit (stop-loss) on the *same*
> > staleness signal would be **actively dangerous** — it could leave a
> > losing position open *specifically because* the safety mechanism fired.
>
> Every integration point in §11 that touches an exit/stop-loss path
> (SG-08, §11.7) must route through `evaluate_gate(..., is_exit=True)`,
> which **structurally cannot return `BLOCK`** — there is no config flag,
> override, or code path in this design that makes an exit `BLOCK`. If a
> future change ever makes that possible, it has broken this invariant.

`ALLOW_WITH_LOG` means: proceed exactly as `ALLOW` would, but emit a
`logger.warning()` (or `.info()` for `WARNING`-state, see §12) line via
`report.summary_line()` first. It carries no other behavioral difference —
deliberately, so that `ALLOW_WITH_LOG` sites need no special-case code beyond
"log, then continue".

---

## 10. Recovery Hook

No direct `docs/OHLCV_VALIDATION.md` analog — the task names Recovery as a
first-class capability of `stale_detector.py`. Defined as a `Protocol`
(structural typing), **not** a concrete implementation:

```python
class RecoveryHook(Protocol):
    """A pluggable per-source recovery action, invoked when StaleDetector
    (or MarketDataFreshnessWatchdog, Section 11.8) determines that a source
    is STALE/UNKNOWN and recovery_possible=True.
    """

    def attempt_recovery(self, source: str, report: FreshnessReport) -> bool:
        """Attempt to refresh `source`'s data.

        Returns True if a refresh was *attempted* (the result is unknown
        until the next check() call observes an updated last_updated_at) --
        NOT a guarantee that the refresh succeeded. Returns False if no
        recovery action exists for this source (e.g.
        report.recovery_possible is False) or the attempt itself could not
        be initiated (e.g. retry budget exhausted, Section 10's policy).
        """
```

**Per-source recovery behavior** (cross-referencing §6's table):

- **Cache/loader sources** (yfinance, pykrx, OHLCVRecovery cache) → retry by
  re-calling the relevant fetch tier (`DataLoader.fetch()`,
  `OHLCVRecovery._try_yfinance`/`_try_pykrx`/`_try_broker`).
- **KIS price** → retry the REST call (`get_price_us`/`get_price_kr`).
- **A future websocket source** (SD-10, currently hypothetical — no live bar
  producer exists today) → trigger a reconnect.
- **Kiwoom / any fully-down source** → `recovery_possible=False`,
  `attempt_recovery()` returns `False` immediately, no retry attempted.

**Recovery policy:**

- **Bounded retries with exponential backoff** — default **3 retries**,
  mirroring `ConsecutiveFailureBreaker`'s cooldown style
  (`backend/execution/circuit_breaker.py:17-19`,
  `__init__(threshold=3, cooldown_minutes=30)`). Concrete backoff schedule
  (e.g. 1s/2s/4s, or per-source) is left to the concrete `RecoveryHook`
  implementation — this design specifies the *interface and policy*, not the
  schedule.
- **`recovery_possible` gates SG-09's escalation.** The kill-switch trigger
  condition for `MarketDataFreshnessWatchdog` (§11.8) is: **all tracked
  sources report `combine_state(...) >= STALE` AND `recovery_possible ==
  False` for all of them.** If *any* source is `STALE`-but-recoverable
  (`recovery_possible=True`), the watchdog calls `attempt_recovery()` and
  re-checks on its next interval instead of escalating to the kill-switch —
  recovery is attempted before the system gives up.
- **Concrete implementations are future work.** `KISRecoveryHook`,
  `LoaderRecoveryHook`, `CacheRecoveryHook`, etc. are explicitly **not**
  designed here — this section specifies the `RecoveryHook` interface and
  the bounded-retry/`recovery_possible` policy that any concrete
  implementation must satisfy.

---

## 11. Integration Points (SG-01..SG-09 mapping)

### 11.1 SG-01 / SG-02 — Loader daily and intraday gates

**File**: `backend/quant/data/loader.py:83-97` (`_fetch_us`, the existing 26h
check), `:84` (`if interval == "1d"` guard, where SG-02's new branch is
inserted).

Replace the existing WARN-only `stale_hours` block with:

```python
report = detector.check_df(df, symbol=symbol, source="yfinance",
                            context="DAILY_SCAN")
```

(or `source="pykrx"` on the KR path). On `state == STALE`:
`detector.check_df(..., raise_on_stale=True)` (the default) raises
`DataFreshnessError` — configurable to non-raising via
`StaleDetectorConfig(raise_on_stale=False)` for callers that prefer the
return-value style. On `WARNING`: `ALLOW_WITH_LOG` — log via
`report.summary_line()` and return `df` unchanged, exactly as today.

**SG-02** (new): for `interval != "1d"`, add a parallel
`detector.check_df(df, ..., context="INTRADAY_BAR")` branch — today there is
**no check at all** for intraday intervals (audit SD-02). Same STALE/WARNING
handling as SG-01, using `intraday_warn`/`intraday_stale` thresholds.

Both SG-01 and SG-02 use the **same shared `StaleDetector` instance** (and
hence the same `StaleDetectorConfig`) as SG-04 (§11.3) — this is what closes
SD-06's "fixing the loader doesn't fix the scanner" meta-gap. Extends to KR
(`_fetch_kr_pykrx`) via the same config — today's 26h check is US-only
(`_fetch_us`); under this design `context="DAILY_SCAN"` applies uniformly to
both `source="yfinance"` and `source="pykrx"`, with `kr_drift_margin`
(§5.4)/`session_aware` (§5.3) handling the KR-specific timestamp nuance.
Fixes SD-01, SD-02, SD-11.

### 11.2 SG-03 — `OHLCVRecovery` tier/cache freshness

**File**: `backend/quant/live/safeguards.py:175-263`
(`fetch`/`_try_*`/`_get_cache`/`_update_cache`).

Two changes:

1. **Soft annotation on every successful fetch tier**: after a successful
   `_try_yfinance`/`_try_pykrx`/`_try_broker`, set
   `df.attrs["freshness"] = {"tier": N, "report": report}` (where `report`
   is the `FreshnessReport` from a `context="DAILY_SCAN"` or
   `"INTRADAY_BAR"` check, as appropriate to the symbol's requested
   interval). This is **non-breaking** — `df.attrs` is a plain dict that
   pandas preserves through most operations and adds no columns. Addresses
   SD-03's visibility gap: callers can now inspect `df.attrs["freshness"]`
   to see which tier served the data and how fresh it was, without changing
   any existing column-based access pattern.

2. **`_get_cache` (lines 254-263) becomes session-aware**:

   ```python
   report = detector.check_df(cached_df, symbol=symbol, source="cache",
                               context="CACHE_FALLBACK", raise_on_stale=False)
   if report.is_stale:
       return None  # preserve the Optional[pd.DataFrame] waterfall contract
   ```

   `raise_on_stale=False` is **mandatory** here — `_get_cache`'s contract is
   `Optional[pd.DataFrame]`, communicated via `return None`, never via an
   exception. On `report.is_stale` (which, with `session_aware=True`,
   includes the SD-12 "predates today's session" case from §5.3 even when
   wall-clock age `< cache_warn`), return `None` exactly as the existing
   "tier failed" path does — `OHLCVRecovery.fetch()`'s caller sees "all 4
   tiers exhausted" and can react accordingly (today: returns `None` up the
   stack; SG-09's watchdog, §11.8, is the longer-term answer to "all tiers
   exhausted").

Fixes SD-03, SD-12.

### 11.3 SG-04 — Strategy scan-and-trade gate

**File**: `backend/strategy/indicator/strategy.py:103-118` (the inline
3-calendar-day gate, currently wrapped in `except Exception: pass` at lines
117-118).

Replace the entire inline block with:

```python
report = detector.check_df(df, symbol=symbol, source="yfinance",
                            context="DAILY_SCAN", raise_on_stale=False)
if report.is_stale or (report.state == StaleState.UNKNOWN
                        and detector.config.fail_closed_on_unknown):
    logger.warning("[%s] %s", self.name, report.summary_line())
    continue
```

This **degrades into the existing `except Exception: ...; continue` pattern**
already present at the surrounding lines (97-99 for the fetch itself,
121-123 for the fusion-evaluation step) — i.e. "skip this symbol this cycle"
remains the outcome, but it is now driven by an explicit, loggable
`FreshnessReport` instead of (a) a coarse `.days`-truncated comparison and
(b) a bare `except: pass` that could silently bypass the check entirely.
Fixes SD-09 (both the truncation and the silent-fail-open) and contributes to
SD-06 (same `StaleDetector`/`StaleDetectorConfig` instance as SG-01/SG-02).

### 11.4 SG-05 — Live-bar gate (`_is_bar_stale`)

**File**: `backend/strategy/base.py:80-99` (`_is_bar_stale`).

```python
def _is_bar_stale(self, bar: dict) -> bool:
    if not getattr(self._broker, "is_live", True):
        return False
    ts = bar.get("ts")
    report = self._stale_detector.check(ts, symbol=bar.get("symbol"),
                                          source="live_bar",
                                          context="INTRADAY_BAR",
                                          raise_on_stale=False)
    if report.is_stale:
        logger.warning("[%s] %s", self.name, report.summary_line())
    return report.is_stale
```

`_BAR_STALE_SECONDS` (`base.py:44`, env-configurable, default 600s) folds
into `config.intraday_warn` (§4 — exact value preserved, `10min == 600s`).
**Behavior-neutral**: this path is dormant today (no live bar producer
exists, audit §4.4/SD-10), so this is purely a reconciliation move that
prevents future drift between this threshold and SG-02's `INTRADAY_BAR`
threshold. Note: `report.is_stale` only covers the `STALE` tier — the old
binary check had no `WARNING`-equivalent; under this design a `WARNING`-tier
bar (`age` between `intraday_warn` and `intraday_stale`) is **not** skipped
(`_is_bar_stale` returns `False`), only logged via `ALLOW_WITH_LOG`-style
`report.summary_line()` if desired — preserving the "only `STALE` causes
`_is_bar_stale() == True`" contract that `on_bar()`'s caller depends on.
Addresses SD-06, SD-10.

### 11.5 SG-06 — Fusion entry gate

**File**: `backend/quant/signals/fusion.py:59-104` (`SignalFusion.evaluate`),
regime-blocked early return at lines 88-91.

Add an **optional** parameter:

```python
def evaluate(self, df, symbol, *, freshness: Optional[FreshnessReport] = None) -> FusionResult:
    ...
    if freshness is not None:
        decision = evaluate_gate(freshness, is_exit=False, config=self._stale_config)
        if decision == GateDecision.BLOCK:
            return FusionResult(
                symbol=symbol, signal=0, score=weighted_score,
                strength=abs(weighted_score), individual=individual,
                regime_blocked=True,
                meta={"blocked_reason": "stale_data"},
            )
    ...
```

**`FusionResult` gets no new field** — it already has `regime_blocked: bool
= False` and `meta: dict = field(default_factory=dict)` (lines 28-29) and
**no `blocked_reason` field**. The freshness block reuses the existing
`regime_blocked=True` early-return shape (lines 88-91) verbatim, carrying
`meta={"blocked_reason": "stale_data"}` — a caller that already inspects
`result.regime_blocked` to detect "fusion declined to signal" sees no change
in shape, and a caller that additionally inspects `result.meta` gets the
*reason*. `freshness=None` (the default) preserves today's behavior exactly
— this parameter is purely additive and optional, so **no existing call site
needs to change** unless it opts in. Addresses SD-08.

### 11.6 SG-07 — Pipeline buy-loop price gate

**File**: `backend/quant/live/pipeline.py:277-279`:

```python
try:
    price = self.broker.get_price(symbol)
except Exception:
    price = df["Close"].iloc[-1]
```

Before this fallback chain runs, check both candidate price sources:

```python
quote_report = detector.check_quote((price_from_broker, broker_call_ts),
                                      symbol=symbol, source="kis",
                                      context="LIVE_PRICE", raise_on_stale=False)
df_report = detector.check_df(df, symbol=symbol, source="yfinance",
                                context="DAILY_SCAN", raise_on_stale=False)
combined = combine_state(quote_report.state, df_report.state)
if evaluate_gate(FreshnessReport(..., state=combined, ...), is_exit=False) == GateDecision.BLOCK:
    trade_record["blocked_reason"] = "stale_price"
    continue  # skip the buy
```

If **both** the live-price call and the dataframe-fallback are
`STALE`/`UNKNOWN`-blocking, set the dict-key `"blocked_reason": "stale_price"`
— matching the **existing** pattern at `pipeline.py:224`
(`"blocked_reason": "daily_loss_limit"`) and `:237`
(`"blocked_reason": f"regime_{regime.regime}"`) — and skip the buy for this
symbol this cycle. **Co-requirement**: as noted in §6, `get_price()`'s return
value needs a `(value, ts)` tuple or `PriceQuote` (§3.2) for `check_quote()`
to be meaningful — wrapping the call-return timestamp at this call site
(`broker_call_ts = datetime.now(timezone.utc)` immediately around the
`self.broker.get_price(symbol)` call) is the minimal change needed; it does
not require modifying `kis_adapter/market_data.py`'s return type. Addresses
SD-01, SD-03, SD-07.

### 11.7 SG-08 — Stop-loss freshness annotation (never blocks)

**Files**: `strategy/risk.py:88-110` (legacy `RiskManager.is_stop_loss`/
`enforce_stop_losses`, the `bot/main.py` path) and
`backend/quant/risk/engine.py:56-130` (`PositionStop`/`TrailingStopManager`,
the current path via `pipeline.py:164-174`'s `price_map` collection).

Carry a `FreshnessReport` (or raw timestamp) alongside each price in
`price_map` / `get_price_fn`'s return value. At the point `is_stop_loss()` /
`PositionStop.is_stopped()` / `TrailingStopManager.check_stops()` is called:

```python
decision = evaluate_gate(report, is_exit=True, config=detector.config)
if decision == GateDecision.ALLOW_WITH_LOG:
    logger.warning("[stop-loss] %s", report.summary_line())
# decision is ALLOW or ALLOW_WITH_LOG -- NEVER BLOCK (Section 9).
# The stop-loss check below executes unconditionally either way.
is_stopped, reason = position_stop.is_stopped(current_price)
```

Per §9's decision matrix, `evaluate_gate(..., is_exit=True)` **structurally
cannot return `BLOCK`** — `STALE`/`UNKNOWN` both map to `ALLOW_WITH_LOG`. The
stop-loss comparison itself is **completely unaffected**; the only addition
is a log line when the price backing it is stale. This is the asymmetry's
load-bearing case (§9's callout). Addresses SD-07.

### 11.8 SG-09 — Periodic freshness watchdog (new)

**File**: new `backend/worker/freshness_watchdog.py`,
`MarketDataFreshnessWatchdog`, mirroring `WorkerWatchdog`
(`backend/worker/heartbeat.py:86-174`) **exactly**:

1. A registry of `{(symbol, source): FreshnessReport}`, populated on each
   successful (or attempted) fetch across all actively-traded symbols.
2. On its periodic interval, compute
   `combine_state(*(r.state for r in registry.values()))`.
3. **Kill-switch trigger condition**: `combine_state(...) in (STALE,
   UNKNOWN)` for the rollup, **AND** every report in the registry has
   `recovery_possible == False` (§10's policy — if *any* source is
   `STALE`-but-recoverable, `attempt_recovery()` is called instead and the
   watchdog waits for the next interval).
4. On trigger, the **same 3-step pattern** as `WorkerWatchdog._alert_dead_worker`
   (`heartbeat.py:128-174`):
   1. `DailyRiskState.kill_switch = True`,
      `kill_reason = "시장 데이터 전체 소스 stale — 거래 중단"` written to
      Postgres (`backend/database/models.py:92-100`'s `kill_switch`/
      `kill_reason` columns) — the cross-process channel, per
      `heartbeat.py:129-131`'s rationale (`SAFE_MODE` is a process-local
      singleton and cannot be toggled cross-process).
   2. `bot.notifier.alert_emergency(...)` (Telegram).
   3. `backend.websocket.server.publish_alert(message, level="critical")`
      (`websocket/server.py:122-123`).

Addresses SD-03, SD-05, SD-06.

### 11.9 Summary table

| Gate | Site | API call | Context | On `STALE` | On `WARNING` | On `UNKNOWN` |
|---|---|---|---|---|---|---|
| SG-01 | `loader.py:83-97` (`_fetch_us`/`_fetch_kr_pykrx`, daily) | `check_df(..., context="DAILY_SCAN")` | `DAILY_SCAN` | raise `DataFreshnessError` (configurable) | log + return `df` | per `fail_closed_on_unknown` |
| SG-02 | `loader.py:84` (new intraday branch) | `check_df(..., context="INTRADAY_BAR")` | `INTRADAY_BAR` | raise `DataFreshnessError` (configurable) | log + return `df` | per `fail_closed_on_unknown` |
| SG-03 | `safeguards.py:175-263` (`OHLCVRecovery`) | `df.attrs["freshness"]` (annotate); `_get_cache`: `check_df(..., context="CACHE_FALLBACK", raise_on_stale=False)` | `DAILY_SCAN`/`INTRADAY_BAR` (annotate); `CACHE_FALLBACK` (`_get_cache`) | `_get_cache` returns `None` (next-tier) | annotate only | annotate only |
| SG-04 | `indicator/strategy.py:103-118` (`_scan_and_trade`) | `check_df(..., context="DAILY_SCAN", raise_on_stale=False)` | `DAILY_SCAN` | log + `continue` | log + proceed | log + `continue` (per `fail_closed_on_unknown`) |
| SG-05 | `strategy/base.py:80-99` (`_is_bar_stale`) | `check(bar_ts, context="INTRADAY_BAR", raise_on_stale=False)` | `INTRADAY_BAR` | return `True` (skip bar) | return `False` (log only) | return `True` (per `fail_closed_on_unknown`) |
| SG-06 | `fusion.py:59-104` (`evaluate`, optional `freshness=`) | `evaluate_gate(freshness, is_exit=False)` | (caller-supplied) | `regime_blocked=True`, `meta={"blocked_reason": "stale_data"}` | log only, proceed | per `fail_closed_on_unknown` |
| SG-07 | `pipeline.py:277-279` (buy-loop price fallback) | `check_quote(...)` + `check_df(..., context="DAILY_SCAN")`, `combine_state` | `LIVE_PRICE` + `DAILY_SCAN` | `blocked_reason="stale_price"`, skip buy | log only, proceed | per `fail_closed_on_unknown` |
| SG-08 | `risk.py:88-110` / `engine.py:56-130` (stop-loss) | `evaluate_gate(report, is_exit=True)` | `LIVE_PRICE` | `ALLOW_WITH_LOG` — stop **always executes** | `ALLOW_WITH_LOG` | `ALLOW_WITH_LOG` — **never `BLOCK`** |
| SG-09 | new `backend/worker/freshness_watchdog.py` | `combine_state(*registry.values())` | all | + `recovery_possible=False` everywhere → kill-switch (3-step alert) | no action | counts toward kill-switch rollup (worst-wins) |

**Blocking policy, summarized**: hard stops only at entry gates (SG-01,
SG-02, SG-04, SG-06, SG-07); annotate-only at the exit gate (SG-08);
next-tier-on-`None` at the recovery layer (SG-03); kill-switch at the
watchdog (SG-09). No execution-layer module (`order_machine.py`,
`backend/quant/risk/engine.py`'s order-placement paths) needs to import
`FreshnessReport` directly — entry-blocks ride the **existing**
`regime_blocked`/`meta` (SG-06) and `blocked_reason` dict-key (SG-07) shapes,
per §2 item 6's "zero signature changes" principle.

---

## 12. Logging Conventions

Mirrors `docs/OHLCV_VALIDATION.md` §10. Module-level
`logger = logging.getLogger("backend.data.stale_detector")`.

**One-line summary per non-`FRESH` report**, via `report.summary_line()`:

```python
if report.state != StaleState.FRESH:
    logger.warning("[stale_detector] %s", report.summary_line())
```

**Debug-level per-field detail**, opt-in (avoid spam on the common
`FRESH` case):

```python
if report.state != StaleState.FRESH:
    logger.debug("[stale_detector] %s.%s context=%s age=%s detail=%s",
                  report.symbol, report.source, report.context,
                  report.age, report.detail)
```

**Error before raising** (`DataFreshnessError`), dual-log pattern mirroring
the loader's existing style and `docs/OHLCV_VALIDATION.md` §10.3:

```python
if report.is_stale:
    logger.error("[stale_detector] STALE for %s: %s", report.symbol, report.summary_line())
    # check()/check_df()/check_quote() raises DataFreshnessError here
    # (raise_on_stale=True, the effective default)
```

**Example log lines:**

```
WARNING [stale_detector] AAPL[yfinance/DAILY_SCAN]: WARNING (age=27.3h, warn>26h)
ERROR   [stale_detector] STALE for 005930: 005930[cache/CACHE_FALLBACK]: STALE (session boundary crossed)
WARNING [stale_detector] KIWOOM[kiwoom/LIVE_PRICE]: UNKNOWN (no timestamp available, recovery_possible=False)
```

---

## 13. Compatibility & Reconciliation

### Compatibility

| Pattern | Location | Guarantee |
|---|---|---|
| `df["Close"]`, `.iloc[-1]`, `.iloc[-252]` | `signals.py`, `fusion.py`, `strategy.py` | Unaffected — `StaleDetector` never mutates `df`; all annotation is via `df.attrs["freshness"]` (SG-03), which adds no columns and changes no values. |
| `if df is None or len(df) < 50` | `indicator/strategy.py:100` | Unchanged and independent — this length gate runs *before* the freshness check (SG-04, §11.3) and is unaffected by it. |
| `regime_blocked` / `meta` (`FusionResult`) | `fusion.py:21-29`, `:88-91` | SG-06 reuses these existing fields verbatim (`meta={"blocked_reason": "stale_data"}`) — no new dataclass field. |
| `"blocked_reason"` dict key | `pipeline.py:224, 237` | SG-07 reuses this key with a new value (`"stale_price"`) — same dict shape, no schema change. |
| `except Exception: ...; continue` | `indicator/strategy.py:97-99, 121-123` | SG-04's degrade target — staleness now produces an explicit, loggable skip via the same `continue` outcome. |
| `StaleDataError(ValueError)` | `loader.py:16` | Left as-is, untouched by this design — distinct from the new `DataFreshnessError(Exception)` (§3.4/§2 item 5). |

### Reconciliation of the 4 fragmented mechanisms (audit §6)

| Existing mechanism | Location | Disposition |
|---|---|---|
| Loader 26h daily check | `loader.py:83-97` | **Migrate** → SG-01 (`check_df(context="DAILY_SCAN")`). `daily_warn=26h` preserves the existing WARNING boundary exactly — zero regression. |
| `StaleDataWatchdog` (dead code) | `emergency.py:134-171` | **Delete.** Salvage its `is_stale(df)`/`check_all(dfs)` *shape* into `StaleDetector.check_df()` (single-symbol) and `combine_state()` (multi-symbol rollup, SG-09) — the underlying logic was reasonable, only its zero-call-sites integration was the problem. |
| `_is_bar_stale` (600s) | `strategy/base.py:80-99` | **Migrate** → SG-05. `_BAR_STALE_SECONDS` → `config.intraday_warn` (exact value preserved: 600s = 10min). Behavior-neutral (dormant path). |
| `_scan_and_trade` 3-day gate | `indicator/strategy.py:103-118` | **Replace** → SG-04. Fixes both the `.days`-truncation (SD-09) and the bare-except-swallows-errors anti-pattern (SD-09) — a parse failure now yields `UNKNOWN`, never silence. |

**Closing note**: the single `StaleDetectorConfig` / `StaleDetector` /
`DataFreshnessError` triad closes SD-06's meta-gap — a future change to "what
counts as stale for a daily bar" (`config.daily_warn`/`daily_stale`) is made
**once** and applies identically to SG-01, SG-02, and SG-04, because they all
construct their `FreshnessReport`s from the same `StaleDetectorConfig`
instance. Today, the equivalent change would require editing four unrelated
files with four different threshold representations (hours vs. seconds vs.
calendar-days) and four different block/warn semantics.

---

## 14. Testing Plan

`tests/data/test_stale_detector.py`, pure pytest, no network calls — follows
the `tests/data/test_calendar.py` convention (`_MockSource` /
`configure_calendar_service`, lines ~14/30/45):

1. **`test_data_freshness_error_not_runtime_error()`** — mirrors
   `test_market_closed_error_not_runtime_error`: asserts
   `not issubclass(DataFreshnessError, RuntimeError)` and
   `issubclass(DataFreshnessError, Exception)`.
2. **`test_state_boundary_thresholds()`** — for each context
   (`DAILY_SCAN`/`INTRADAY_BAR`/`CACHE_FALLBACK`/`LIVE_PRICE`), construct
   `last_updated_at` at `age == warn_threshold` (→ `FRESH`), `age ==
   warn_threshold + epsilon` (→ `WARNING`), `age == stale_threshold +
   epsilon` (→ `STALE`).
3. **`test_unknown_on_missing_or_unparseable_timestamp()`** — `check(None,
   ...)` → `UNKNOWN`; `check_df(df_with_garbage_index, ...)` → `UNKNOWN`,
   logged at WARNING (SD-09 fix — never silent).
4. **`test_combine_state_worst_wins_including_unknown()`** —
   `combine_state(FRESH, WARNING, STALE) == STALE`,
   `combine_state(STALE, UNKNOWN) == UNKNOWN`,
   `combine_state() == FRESH`.
5. **`test_gate_matrix_entry_vs_exit()`** — table-driven over §9's full
   matrix: `STALE`+entry → `BLOCK`; `STALE`+exit → `ALLOW_WITH_LOG` (never
   `BLOCK`); `UNKNOWN`+entry → `BLOCK` iff `fail_closed_on_unknown`;
   `UNKNOWN`+exit → `ALLOW_WITH_LOG` always, regardless of
   `fail_closed_on_unknown`.
6. **`test_session_aware_cache_age_sd12()`** — reproduces §5.3's worked
   example using `configure_calendar_service(data_source=_MockSource(...))`:
   cache written 15:30 KST yesterday, checked 09:00 KST today → wall-clock
   age `~17.5h` (`< cache_warn`), but `session_aware=True` → `STALE`
   (`detail["reason"] == "predates_session"`).
7. **`test_kr_drift_margin_sd11()`** — `session_aware=False`, KR symbol,
   naive-UTC index → age adjusted by `kr_drift_margin` before threshold
   comparison; assert a borderline timestamp classifies differently with vs.
   without the margin.
8. **`test_recovery_possible_flag()`** — Kiwoom-shaped input (`source="kiwoom"`,
   `last_updated_at=None`) → `state=UNKNOWN, recovery_possible=False`;
   `RecoveryHook.attempt_recovery()` returns `False` immediately for such a
   report.
9. **`test_per_source_health_table_driven()`** — table-driven over §6's rows
   (KIS price, yfinance, pykrx, cache, FX cache via
   `check_monotonic_age`-style stub) verifying each source's
   FRESH/STALE/UNKNOWN conditions match the table.
10. **`test_blocked_reason_shapes_unchanged_sg06_sg07()`** — `FusionResult`
    from `evaluate(df, symbol, freshness=stale_report)` has
    `regime_blocked=True` and `meta["blocked_reason"] == "stale_data"`, with
    **no new field** on `FusionResult`; a pipeline-style trade-record dict
    gains `"blocked_reason": "stale_price"` under SG-07's combined-staleness
    condition, matching the existing key used for `"daily_loss_limit"`.

---

## Future Work (TASK 3-3C — not this task)

1. Implement `backend/data/stale_detector.py` per this design:
   `StaleState`, `combine_state()`, `PriceQuote`, `FreshnessReport`,
   `DataFreshnessError`, `StaleDetectorConfig`/`DEFAULT_CONFIG`,
   `StaleDetector` (`check`/`check_df`/`check_quote`/`_thresholds_for`),
   `check_freshness()`, `GateDecision`/`evaluate_gate()`, `RecoveryHook`.
2. Implement `tests/data/test_stale_detector.py` per §14.
3. Wire **SG-01 through SG-08** into `loader.py`, `safeguards.py`,
   `indicator/strategy.py`, `strategy/base.py`, `fusion.py`, `pipeline.py`,
   `strategy/risk.py`, and `backend/quant/risk/engine.py` per §11.
4. Build **SG-09** — new `backend/worker/freshness_watchdog.py`,
   `MarketDataFreshnessWatchdog`.
5. Apply §13's reconciliation: delete `StaleDataWatchdog`
   (`backend/worker/emergency.py:134-171`), migrate `_is_bar_stale`'s
   threshold and the loader's 26h check, replace `_scan_and_trade`'s 3-day
   gate.
6. Resolve the `bot/main.py` legacy-path question (audit §8): determine
   whether `bot/main.py`'s `TradingEngine` is still actively run alongside
   (or instead of) `backend/quant/live/pipeline.py`; if active, apply SG-08
   there too.
7. Wrap `kis_adapter/market_data.py`'s `get_price_us`/`get_price_kr` return
   values in `PriceQuote` (§3.2) — a co-requirement for SG-07/SG-08 to be
   meaningful (today, every KIS price check is `UNKNOWN` by construction,
   §6).
8. Extend `StaleDetector` to cover the FX-rate cache (SD-04,
   `backend/brokers/kis.py:300-317`) via a `check_monotonic_age()` variant
   that accepts a `time.monotonic()`-relative age directly (§6's "FX cache is
   special-cased" note) — no dedicated SG number, follow-up only per audit
   §8.

None of the above is implemented in this task.
