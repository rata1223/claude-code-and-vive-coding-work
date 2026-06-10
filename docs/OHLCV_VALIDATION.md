# OHLCV Validator — Design Specification

## 1. Purpose and Scope

This document specifies the design of `backend/data/validator.py`, a structured
OHLCV validation module that classifies incoming market-data DataFrames into
`VALID` / `WARNING` / `INVALID` tiers. It supersedes the simple drop-and-raise
sketch (`validate_ohlcv_dataframe()`) proposed in
[`docs/OHLCV_DATA_VALIDATION.md`](./OHLCV_DATA_VALIDATION.md) §5.1, while
preserving its fail-closed guarantees and its `BadOHLCVError` exception
contract.

**In scope:** daily OHLCV `pd.DataFrame` objects with columns
`Open, High, Low, Close, Volume` and a `DatetimeIndex` (UTC-naive,
exchange-local time), as produced by:

- `backend/quant/data/loader.py` — `DataLoader.fetch()` / `_fetch_us()` /
  `_fetch_kr_pykrx()` / `fetch_from_broker()`
- `backend/quant/live/safeguards.py` — `OHLCVRecovery.fetch()` (4-tier
  KIS → yfinance → pykrx → cache waterfall)

**Out of scope:** intraday/tick data, backtest simulation inputs, and
order/fill validation. Those remain covered by the separate point-guards
V3-V9 from the TASK 3-2A gate map (see §12).

This is a **design document** — `backend/data/validator.py` and
`tests/data/test_validator.py` are specified here but not implemented in
this task.

---

## 2. Design Principles

1. **Three-tier classification, not binary drop/keep.** Every check produces
   `VALID`, `WARNING`, or `INVALID` — both per-issue and for the dataframe as
   a whole. `WARNING` means "usable but flagged — log and continue."
   `INVALID` means "do not trade on this data."

2. **"Worst wins" aggregation.** The overall `ValidationReport.status` is the
   maximum severity across all `ValidationIssue.status` values, where
   `INVALID > WARNING > VALID`, via `combine_status()`. A single `INVALID`
   issue makes the whole report `INVALID` even if every other row is clean.

3. **Fail-closed for `INVALID`.**
   `OHLCVValidator.validate(df, symbol, raise_on_invalid=True)` (the default)
   raises `BadOHLCVError` when the overall status is `INVALID`. Callers that
   want a non-raising "give me the report and decide" mode pass
   `raise_on_invalid=False`.

4. **Validator returns `(cleaned_df, report)` — never mutates in place.**
   - Auto-fixable issues (unsorted index, duplicate timestamps) are applied
     to a copy and reported as `WARNING`.
   - Non-fixable bad rows (NaN/inf, OHLC-inconsistent, negative price/volume)
     are dropped and reported.
   - Spikes and missing candles are **flagged only, never dropped or
     fabricated** — dropping a spike candle could itself corrupt the
     timestamp continuity that strategies rely on for `iloc[-252]`-style
     lookbacks; fabricating a missing candle would inject fake prices into
     SMA/RSI/momentum.

5. **Compatibility first.** The cleaned DataFrame retains the exact column
   set `["Open", "High", "Low", "Close", "Volume"]` (uppercase, same order),
   a sorted-ascending `DatetimeIndex` with no duplicates, and unchanged
   dtypes/timezone — i.e. exactly what `loader.py` already guarantees on the
   happy path. `df["Close"]`, `.iloc[-1]`, `.iloc[-252]` all continue to work
   unchanged. The validator never adds extra columns to the returned df —
   per-row issue metadata lives only in `ValidationReport`.

6. **Stateless class + thin module function.** `OHLCVValidator` holds only a
   `ValidatorConfig` and has no caches or singletons — safe to share across
   threads/symbols. `validate_ohlcv()` is a module-level convenience wrapper
   for one-off calls.

---

## 3. Data Structures

### 3.1 `ValidationStatus`

```python
class ValidationStatus(str, Enum):
    """Three-tier outcome of a validation check or overall report.

    Ordering for aggregation purposes (worst wins):
        VALID < WARNING < INVALID
    """
    VALID = "valid"
    WARNING = "warning"
    INVALID = "invalid"


_STATUS_RANK: dict[ValidationStatus, int] = {
    ValidationStatus.VALID: 0,
    ValidationStatus.WARNING: 1,
    ValidationStatus.INVALID: 2,
}


def combine_status(*statuses: ValidationStatus) -> ValidationStatus:
    """Return the most severe status among the given statuses ("worst wins").

    combine_status() with no arguments returns VALID (identity element).

    Examples:
        combine_status(VALID, WARNING)  -> WARNING
        combine_status(WARNING, INVALID) -> INVALID
    """
    if not statuses:
        return ValidationStatus.VALID
    return max(statuses, key=lambda s: _STATUS_RANK[s])
```

Follows the existing `XxxStatus(str, Enum)` lowercase-snake-case convention
used by `SessionType`, `OrderStatus`, and `BlockReason`. `combine_status` is a
free function (not a method) so `ValidationReport` and any future per-symbol
multi-report aggregation can both use it.

### 3.2 `ValidationIssue`

```python
@dataclass(frozen=True)
class ValidationIssue:
    """A single finding from one validation check.

    Attributes:
        check: Stable machine-readable check identifier — one of:
            "timestamp_order", "timestamp_duplicate", "null_check",
            "ohlc_consistency", "volume_validity", "price_spike",
            "missing_candle", "missing_columns", "empty_input".
            Used for log filtering/metrics — must NOT change across
            releases without a migration note.
        status: WARNING or INVALID for this specific issue. (VALID issues
            are never created — absence of an issue for a check implies
            that check passed.)
        message: Human-readable detail, English, suitable for logs and
            operator dashboards. Symbol names and counts are interpolated
            values, kept out of the template for grep-ability.
        timestamps: Sorted list of affected row timestamps (pd.Timestamp).
            Empty for dataframe-level issues (e.g. "too few rows").
        row_count: Number of affected rows. May exceed len(timestamps) if
            timestamps is truncated for log brevity (see
            ValidatorConfig.max_logged_timestamps).
        detail: Optional structured payload for programmatic consumers,
            e.g. {"high": 10.0, "low": 12.0} for an OHLC inconsistency, or
            {"pct_change": 0.42, "threshold": 0.30} for a spike.
    """
    check: str
    status: ValidationStatus
    message: str
    timestamps: list[pd.Timestamp] = field(default_factory=list)
    row_count: int = 0
    detail: dict = field(default_factory=dict)
```

`frozen=True` — issues are immutable facts about a point-in-time validation
run.

### 3.3 `ValidationReport`

```python
@dataclass
class ValidationReport:
    """Aggregate result of validating one OHLCV DataFrame for one symbol.

    Attributes:
        symbol: Ticker/code that was validated (e.g. "AAPL", "005930").
        status: combine_status(*[i.status for i in issues]).
            VALID if issues is empty.
        issues: All findings, in the order checks were executed
            (see OHLCVValidator._CHECK_ORDER). May be empty.
        rows_in: Row count of the input DataFrame (before cleaning).
        rows_out: Row count of the returned (possibly cleaned) DataFrame.
        rows_dropped: rows_in - rows_out. Always >= 0.
        dropped_reasons: {check_name: dropped_row_count}, summing to
            rows_dropped. Lets log lines say
            "dropped 3 rows (2 null_check, 1 ohlc_consistency)".
        validated_at: UTC timestamp when validate() ran.
    """
    symbol: str
    status: ValidationStatus
    issues: list[ValidationIssue] = field(default_factory=list)
    rows_in: int = 0
    rows_out: int = 0
    rows_dropped: int = 0
    dropped_reasons: dict[str, int] = field(default_factory=dict)
    validated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_valid(self) -> bool:
        return self.status == ValidationStatus.VALID

    @property
    def is_invalid(self) -> bool:
        return self.status == ValidationStatus.INVALID

    @property
    def has_warnings(self) -> bool:
        return any(i.status == ValidationStatus.WARNING for i in self.issues)

    def summary_line(self) -> str:
        """One-line human-readable summary for log lines, e.g.:
        "AAPL: WARNING (2 issues, 501 rows, 3 dropped: timestamp_duplicate=2, null_check=1)"
        """
```

### 3.4 `BadOHLCVError`

```python
class BadOHLCVError(Exception):
    """Raised when an OHLCV DataFrame is classified INVALID and
    raise_on_invalid=True (the default) was passed to validate().

    NOT a subclass of RuntimeError or ValueError — intentional, mirrors
    MarketClosedError in backend/data/calendar.py (lines 63-69). Bad
    upstream data is an *expected* operating condition (corporate actions,
    vendor outages, delisted tickers), not a programming error or a
    broker-side failure. ConsecutiveFailureBreaker
    (backend/execution/circuit_breaker.py) must NOT count a BadOHLCVError
    as a broker failure — the broker/data vendor may be working fine; the
    *data* failed quality checks. Counting it as a broker failure could
    trip the circuit breaker on a single bad ticker and block trading for
    unrelated symbols.

    Note: loader.py's existing StaleDataError(ValueError) is a narrower,
    older check (single-symbol staleness only) and is left as-is for
    backward compatibility — see §12. New structural validation failures
    raise BadOHLCVError, not StaleDataError or bare ValueError.

    Attributes:
        symbol: The symbol being validated.
        report: The full ValidationReport (status == INVALID), so the
            caller can log/inspect *why* without re-running validation.
    """

    def __init__(self, symbol: str, report: "ValidationReport") -> None:
        self.symbol = symbol
        self.report = report
        invalid_checks = [i.check for i in report.issues
                          if i.status == ValidationStatus.INVALID]
        super().__init__(
            f"OHLCV validation failed for {symbol}: "
            f"{', '.join(invalid_checks)} "
            f"({report.rows_out}/{report.rows_in} rows survived)"
        )
```

---

## 4. `ValidatorConfig`

```python
@dataclass(frozen=True)
class ValidatorConfig:
    """Configurable thresholds for OHLCVValidator.

    All fields default to values tuned for *daily* bars on liquid US/KR
    equities and ETFs.

    Attributes:
        min_rows: Minimum row count for a usable dataframe. Below this,
            overall status is INVALID regardless of per-row cleanliness
            (mirrors strategy.py's existing `len(df) < 50` gate — set to
            50 by default so the validator and the strategy gate agree).
        spike_threshold_pct: Maximum allowed absolute bar-over-bar %
            change in Close (see §7). Default 0.30 (30%) — matches KRX's
            +/-30% daily price-limit band, wide enough to allow legitimate
            large moves, narrow enough to catch decimal/unit data errors
            (10x/100x misprints).
        spike_status: Status assigned to spike issues. Default WARNING —
            spikes are flagged, not dropped (see §7).
        max_missing_ratio: Maximum allowed fraction of expected trading
            days missing from the dataframe's date range (see §6).
            Default 0.05 (5%). Above this -> WARNING.
        missing_candle_invalid_ratio: Optional stricter ratio above which
            missing-candle issues escalate to INVALID. Default None
            (disabled) — most callers should not hard-fail on gaps caused
            by e.g. a newly-listed symbol's short history.
        max_logged_timestamps: Cap on how many timestamps are embedded in
            a single ValidationIssue.timestamps list before truncation
            with a "+N more" summary. Default 10.
        null_check_columns: Columns checked for NaN/None/inf. Default all
            of ("Open","High","Low","Close","Volume").
        require_columns: Columns that MUST be present (else INVALID
            immediately, no further checks run). Default
            ("Open","High","Low","Close","Volume").
        dedup_keep: Which duplicate-timestamp row to keep: "last"
            (default, matches "latest correction wins" vendor semantics)
            or "first".
        market: Optional explicit market for the missing-candle check —
            "KR" or "US" (backend.brokers.models.Market values). If None,
            OHLCVValidator infers from the symbol via the same KR-symbol
            regex used in loader.py (see §6.2).
    """
    min_rows: int = 50
    spike_threshold_pct: float = 0.30
    spike_status: ValidationStatus = ValidationStatus.WARNING
    max_missing_ratio: float = 0.05
    missing_candle_invalid_ratio: Optional[float] = None
    max_logged_timestamps: int = 10
    null_check_columns: tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume")
    require_columns: tuple[str, ...] = ("Open", "High", "Low", "Close", "Volume")
    dedup_keep: str = "last"
    market: Optional[str] = None  # "KR" | "US" | None (auto-detect)


DEFAULT_CONFIG = ValidatorConfig()
```

`frozen=True` so a single shared `DEFAULT_CONFIG` instance can be reused
across threads/calls without accidental mutation.

**Hardcoded (not configurable):** rows dropped by the null/OHLC/volume
checks escalate `WARNING` → `INVALID` if the dropped fraction exceeds 5% of
`rows_in`, OR if `rows_out < min_rows`. Keeping this off the config surface
avoids a proliferation of tuning knobs for a threshold that should rarely
need adjustment.

---

## 5. The Seven Check Categories

Each check is implemented as a private method
`_check_xxx(df, config) -> tuple[pd.DataFrame, list[ValidationIssue]]`
returning the (possibly modified) dataframe and any issues found. Checks run
in a fixed order (`_CHECK_ORDER`, §8.2) because later checks depend on
earlier cleanup — e.g. the spike detector's bar-over-bar diff and the
missing-candle detector's date-range walk are both meaningless on an
unsorted or duplicated index.

| # | `check=` | Rule | WARNING | INVALID | Row treatment |
|---|---|---|---|---|---|
| 1 | `timestamp_order` / `timestamp_duplicate` | index ascending & unique | always (auto-fixed) | never | sort + dedup(keep=`dedup_keep`) — runs **first** |
| 2 | `null_check` | NaN/None/±inf in any of `null_check_columns` | drop ≤5% of rows | drop >5% of rows, or `rows_out < min_rows` | drop row — never interpolate/forward-fill |
| 3 | `ohlc_consistency` | High≥Low, High≥Open, High≥Close, Low≤Open, Low≤Close, **and** O/H/L/C > 0 | drop ≤5% | drop >5% or `rows_out < min_rows` | drop row |
| 4 | `volume_validity` | Volume ≥ 0 (Volume==0 is **valid**, not flagged) | drop ≤5% | drop >5% or `rows_out < min_rows` | drop row only if Volume<0 |
| 5 | `price_spike` | `abs(Close.pct_change()) > spike_threshold_pct` (first row excluded) | flagged (per `spike_status`) | only if `spike_status=INVALID` configured | flag only, never drop |
| 6 | `missing_candle` | gaps in `df.index` vs CalendarService trading-day set | `missing_ratio > max_missing_ratio` | only if `missing_candle_invalid_ratio` set & exceeded | flag only — never fabricate or drop |
| 7 | `missing_columns` / `empty_input` | required OHLCV columns absent, or `df.empty` | — | always `INVALID`, short-circuits | no further checks run |

### 5.1 Null / Infinite Validation (`null_check`)

Detection: `~np.isfinite(df[col])` per column in `config.null_check_columns`
(covers NaN and ±inf in one call for numeric dtypes; `None` in an
object-dtype column is caught by `pd.isna()` as a fallback). Directly
addresses DS-01/DS-02/DS-03's root cause (NaN propagating into PnL,
trailing-stop, and quantity calculations).

Never auto-fixed — interpolating or forward-filling a price would silently
invent market data, which is worse than dropping the bar.

### 5.2 OHLC Consistency Validation (`ohlc_consistency`)

For each row, all of:

- `High >= Low`
- `High >= Open`
- `High >= Close`
- `Low <= Open`
- `Low <= Close`
- `Open > 0`, `High > 0`, `Low > 0`, `Close > 0`

(Equivalently: `0 < Low <= min(Open, Close) <= max(Open, Close) <= High`.)

Negative/zero prices are folded into this same check
(`detail.reason="non_positive_price"`) rather than a separate category —
both represent "this candle's OHLC values are nonsensical" and both result
in row-drop with the same 5%/`min_rows` escalation rule.

Directly fixes DS-08 (`high < low` deflating ATR): by the time
`position_sizer._calc_atr` runs, the offending row is gone entirely, so
`high - low` is never negative in the ATR input.

### 5.3 Volume Validation (`volume_validity`)

`Volume >= 0`. **`Volume == 0` is explicitly allowed** — a trading-halt day
with `Volume == 0` is valid market data, not an error. DS-12's "volume=0
deflates the 20-day average" is a *signal-layer* concern, not a
data-validity concern, and is intentionally left to a separate point-guard
(see §12, V9 disposition).

Only `Volume < 0` rows are dropped (vendor corruption — never legitimate).

### 5.4 Timestamp Validation (`timestamp_order` / `timestamp_duplicate`)

Three sub-checks against `df.index` (a `DatetimeIndex`):

1. **Ordering** — index must be monotonically increasing.
2. **Duplicates** — no two rows share the same timestamp.
3. **Reversal** — subsumed by #1 (a "reversal" is just a non-monotonic
   point).

Both are `WARNING`, not `INVALID`, because both are mechanically and
unambiguously auto-correctable with no information loss beyond "which
duplicate copy to discard":

- **Unsorted index:** auto-fix `df = df.sort_index()`. Fixes DS-11 (stale
  `iloc[-1]`). Message: "index was not sorted ascending; sorted N rows".
- **Duplicate timestamps:** auto-fix
  `df = df[~df.index.duplicated(keep=config.dedup_keep)]`. Fixes DS-09.
  Message: "dropped N duplicate-timestamp rows (kept={dedup_keep})".
  Counted in `dropped_reasons["timestamp_duplicate"]`.

This check runs **first** in `_CHECK_ORDER` — every later check assumes a
sorted, unique index.

### 5.5 Price Spike Detector (`price_spike`)

See §7.

### 5.6 Missing Candle Detector (`missing_candle`)

See §6.

### 5.7 Schema / Empty-Input Guards (`missing_columns` / `empty_input`)

If `df.empty` on entry, or any of `config.require_columns` is absent from
`df.columns`, the validator short-circuits immediately: returns
`(df.copy(), report)` where `report.status == INVALID` and
`report.issues == [single ValidationIssue(check=..., status=INVALID, ...)]`.
No further checks run (they would `KeyError` on a missing column, or operate
on an empty frame meaninglessly).

---

## 6. Missing Candle Detector

### 6.1 Rule

Given the validated dataframe's date range
`[df.index[0].date(), df.index[-1].date()]`, compute the set of expected
trading days for the resolved market (§6.2) within that range using
`CalendarService`. Compare against the set of dates actually present in
`df.index`:

```
missing_ratio = len(expected_trading_days - present_dates) / len(expected_trading_days)
```

### 6.2 Resolving the `Market` enum collision

The codebase has **two unrelated `Market(str, Enum)` definitions**:

- `backend/data/calendar.py::Market` — `KRX`, `NYSE`, `NASDAQ` (used by
  `CalendarService`)
- `backend/brokers/models.py::Market` — `KR`, `US` (used by broker
  capability declarations)

`OHLCVValidator` must call
`CalendarService.is_trading_day(market: calendar.Market, d: date)`, but the
validator's natural input is a stock symbol string (e.g. `"AAPL"`,
`"005930"`).

**Resolution strategy** — explicit, three-step, documented in the
validator's docstring:

1. **Symbol → KR/US:** a **locally duplicated** regex
   `_KR_SYMBOL_RE = re.compile(r"^\d{6}$")`, mirroring `loader.py::_is_kr()`.
   Not imported from `backend.quant.data.loader`, to avoid a
   `backend.data → backend.quant.data` dependency edge that could become
   circular. *(Tradeoff documented as a follow-up TODO: if `loader.py`
   changes its KR-detection heuristic, this needs a matching update.)*

2. **KR/US → `calendar.Market`:** KR maps to `calendar.Market.KRX` (single
   Korean equity exchange in scope — KOSPI/KOSDAQ share the same holiday
   calendar). US maps to `calendar.Market.NYSE` (NYSE and NASDAQ share the
   same holiday calendar in this codebase's `CalendarDataSource`
   implementations, so the NYSE/NASDAQ distinction is immaterial for
   trading-day purposes — NYSE is the canonical default).

3. **Override hook:** `ValidatorConfig.market: Optional[str]` accepts `"KR"`
   or `"US"` (the `brokers.models.Market` *values*, as plain strings — the
   vocabulary callers like `loader.py` and `strategy.py` already use) to
   bypass auto-detection entirely.

```python
# Local, intentionally duplicated from backend/quant/data/loader.py::_is_kr
# to avoid backend.data -> backend.quant.data import coupling. Keep in sync.
_KR_SYMBOL_RE = re.compile(r"^\d{6}$")


def _resolve_calendar_market(symbol: str, config: ValidatorConfig) -> "calendar.Market":
    """Resolve a backend.data.calendar.Market for the missing-candle check.

    Resolution order:
      1. config.market == "KR" -> calendar.Market.KRX
      2. config.market == "US" -> calendar.Market.NYSE
      3. config.market is None -> regex symbol match (^\\d{6}$) -> KRX else NYSE

    Note: this returns backend.data.calendar.Market (KRX/NYSE/NASDAQ), which
    is DIFFERENT from backend.brokers.models.Market (KR/US). config.market
    uses the brokers.models vocabulary ("KR"/"US") because that is what
    calling code (loader.py, strategy.py) already has on hand; this function
    performs the KR/US -> KRX/NYSE translation internally.
    """
```

### 6.3 Computing expected trading days

`CalendarService` has no bulk `get_trading_days(start, end)` — only
`is_trading_day(market, d)` (single date) and `prev_trading_day(market,
before)` (walk backward). For a ~2-year validation window (~730 calendar
days), iterating `is_trading_day()` once per calendar day is acceptable: no
I/O, and `CalendarService` caches holiday sets in its internal index after
the first `_ensure_holidays_loaded()` call per `(market, year)`.

```python
def _expected_trading_days(market: "calendar.Market", start: date, end: date) -> set[date]:
    """Return the set of trading days in [start, end] inclusive for `market`,
    using CalendarService.is_trading_day() per calendar day.

    Cost: O(days) calls to is_trading_day(), each O(1) after the first call
    per (market, year) triggers _ensure_holidays_loaded(). For a 2-year
    daily-bar validation (~730 calendar days), this is ~730 dict lookups —
    negligible.
    """
    svc = get_calendar_service()
    days: set[date] = set()
    d = start
    while d <= end:
        if svc.is_trading_day(market, d):
            days.add(d)
        d += timedelta(days=1)
    return days
```

### 6.4 Classification

- `missing_ratio <= max_missing_ratio` (default 5%): no issue (`VALID`). A
  handful of gaps (a single back-filled outage day, or a recently-listed
  symbol whose pre-listing days are correctly absent) is normal.
- `missing_ratio > max_missing_ratio`: `WARNING`. Issue lists the missing
  dates (capped at `max_logged_timestamps`).
- `missing_ratio > missing_candle_invalid_ratio` (if configured, default
  `None` = disabled): `INVALID`.

**Row treatment:** flag only — never fabricate or drop. There is nothing to
drop (the rows don't exist), and synthesizing placeholder candles would
silently feed fake prices into SMA/RSI/momentum.

```python
def _check_missing_candles(self, df: pd.DataFrame, symbol: str, config: ValidatorConfig
                            ) -> list[ValidationIssue]:
    """Compare df.index dates against expected trading days (CalendarService)
    for the resolved market over [df.index[0].date(), df.index[-1].date()].

    Returns issues only -- never modifies df (flag-only check).
    check="missing_candle". status=WARNING if missing_ratio >
    config.max_missing_ratio; escalates to INVALID if
    config.missing_candle_invalid_ratio is set and exceeded; otherwise no
    issue returned.

    detail = {"missing_ratio": float, "missing_count": int,
              "expected_count": int, "market": "KRX"|"NYSE"}
    timestamps = sorted list of missing dates (as pd.Timestamp at midnight),
    truncated to config.max_logged_timestamps.

    Fail-soft: if CalendarService raises (e.g. a holiday-data-source
    failure), this check catches the exception, logs at logger.warning(),
    and returns [] (no issue) -- a calendar-service hiccup must never make
    an otherwise-clean dataframe INVALID.
    """
```

---

## 7. Price Spike Detector

### 7.1 What is compared

**Bar-over-bar percentage change in `Close`**:
`df["Close"].pct_change().abs()`, with the first row excluded (its
`pct_change` is NaN by definition).

Chosen over alternatives for three reasons:

1. **Simplicity and determinism.** ATR requires a 14-bar rolling window and
   is undefined for the first 14 rows — a spike in row 2 of a freshly
   fetched dataframe would be invisible to an ATR-relative check.
   `Close.pct_change()` is defined from row 2 onward unconditionally.
2. **Directly addresses DS-10.** The audit's example
   (`close = 999999` for a $100 stock) is a `Close`-value error — a
   bar-over-bar `Close` percentage check catches it on both the spike bar
   (huge jump in) and the bar after (huge jump back out).
3. **Avoids circularity with `position_sizer`'s ATR.** The validator runs
   upstream of `position_sizer`; depending on the same ATR formula computed
   downstream would couple two modules in different packages
   (`backend.data` vs `backend.quant.risk`).

**Rejected alternatives:** ATR-relative range (undefined for first 14 rows,
couples to `position_sizer`'s formula); intrabar `(High-Low)/Close` range
(false-positives on legitimately high-volatility-but-correct days, e.g.
earnings).

### 7.2 Threshold

`spike_threshold_pct = 0.30` (30%), compared against `abs(pct_change)`.

KRX enforces a ±30% daily price limit on regular equities, so a legitimate
KR limit-up/-down day produces `pct_change` very close to but not exceeding
0.30 — flagged as `WARNING` ("extreme but real"), not rejected. US equities
have no exchange-wide daily limit, but a single-day ±30% move on a liquid
large-cap (the typical strategy universe — `SPY`, `QQQ`, etc.) is itself an
extremely rare, newsworthy event that *should* be flagged for operator
awareness. Setting the threshold at exactly the KR limit means typical
data-vendor decimal/unit errors (10x = +900%, 100x = +9900%) are flagged
with enormous margin.

### 7.3 Classification and row treatment

`abs(pct_change) > spike_threshold_pct` → `WARNING` (per
`config.spike_status`, default `WARNING`). **Flag only — never drop.**

Why `WARNING`, not `INVALID`, by default: the validator cannot distinguish
"vendor decimal error" from "genuine 35% one-day move" without external
context. Defaulting to `WARNING`:

- Preserves the bar (no information loss if it's real).
- Surfaces the event in logs for operator review.
- Lets downstream RSI/momentum/SMA naturally react to the (possibly real)
  move.
- Avoids `BadOHLCVError` storms on legitimate KR limit days across an entire
  portfolio.

A caller that wants stricter behavior (e.g. a backtest ingestion pipeline
with zero tolerance for vendor errors) can set
`spike_status=ValidationStatus.INVALID` in its `ValidatorConfig`.

```python
def _check_price_spikes(self, df: pd.DataFrame, config: ValidatorConfig
                         ) -> list[ValidationIssue]:
    """Flag bars where abs(Close.pct_change()) > config.spike_threshold_pct.

    Returns issues only -- never modifies df (flag-only check).
    check="price_spike". One ValidationIssue (status=config.spike_status)
    per dataframe if any spikes found, with timestamps = the spike bars
    (capped at config.max_logged_timestamps) and
    detail={"max_pct_change": float, "spike_count": int,
            "threshold": config.spike_threshold_pct}.
    """
```

---

## 8. `OHLCVValidator` — Main Class and `validate()` API

### 8.1 Class vs. module functions

**Decision:** a class, `OHLCVValidator`, plus a thin module-level
convenience function `validate_ohlcv()`.

- A class lets `ValidatorConfig` be bound once (e.g.
  `OHLCVValidator(config=ValidatorConfig(spike_threshold_pct=0.50))` for a
  crypto-specific instance) and reused across many `validate()` calls in a
  hot loop (`strategy.py`'s per-symbol scan loop).
- Mirrors `CalendarService` (class + module-level singleton getter) and
  `DataLoader` (class instantiated by callers) — established patterns in
  this codebase.
- `OHLCVValidator` is **stateless** (no caches needed, unlike
  `CalendarService`) — so there is no `get_validator()` singleton; callers
  just do `OHLCVValidator()` or `OHLCVValidator(config=...)` cheaply.
- `validate_ohlcv()` covers the common case (loader.py, safeguards.py call
  sites — §9) where a one-line call with default config suffices.

### 8.2 Class skeleton

```python
class OHLCVValidator:
    """Validates and cleans OHLCV DataFrames before they reach strategy,
    risk, and execution layers.

    Stateless and thread-safe -- safe to share a single instance across
    threads/symbols. Construct once with a ValidatorConfig (or use
    defaults) and call validate() per dataframe.

    Usage:
        validator = OHLCVValidator()  # DEFAULT_CONFIG
        df_clean, report = validator.validate(df, symbol="AAPL")
        if report.has_warnings:
            logger.warning("OHLCV warnings for AAPL: %s", report.summary_line())
        # df_clean is safe to pass to fusion.evaluate(), strategy logic, etc.

    Usage (fail-soft, e.g. inside OHLCVRecovery's per-tier try/except):
        df_clean, report = validator.validate(df, symbol="AAPL",
                                                raise_on_invalid=False)
        if report.is_invalid:
            continue  # try next data-source tier
    """

    # Order matters: timestamp fix must run before nulls/ohlc/volume/spike
    # /missing-candle, since those assume sorted+deduplicated index.
    _CHECK_ORDER = (
        "_check_timestamps",
        "_check_nulls",
        "_check_ohlc_consistency",
        "_check_volume",
        "_check_price_spikes",
        "_check_missing_candles",
    )

    def __init__(self, config: Optional[ValidatorConfig] = None) -> None:
        self.config = config or DEFAULT_CONFIG

    def validate(
        self,
        df: pd.DataFrame,
        symbol: str,
        raise_on_invalid: bool = True,
        config: Optional[ValidatorConfig] = None,
    ) -> tuple[pd.DataFrame, ValidationReport]:
        """Validate and clean an OHLCV DataFrame.

        Args:
            df: Input DataFrame with columns Open/High/Low/Close/Volume and
                a DatetimeIndex. Not mutated -- a copy is returned.
            symbol: Ticker/code, for logging and the report.
            raise_on_invalid: If True (default) and the resulting
                ValidationReport.status == INVALID, raises BadOHLCVError
                instead of returning. If False, always returns
                (df_clean, report) and the caller MUST check
                report.is_invalid before trusting df_clean.
            config: Per-call override of self.config. If None, uses
                self.config.

        Returns:
            (df_clean, report):
                df_clean: copy of df with timestamp sort/dedup applied and
                    INVALID-classified rows (null/inf, OHLC-inconsistent,
                    negative volume) dropped. WARNING-classified issues
                    (spikes, missing candles, minor drops under the 5%
                    threshold) do not remove additional rows beyond what's
                    stated above. Same column set/dtypes as input.
                report: ValidationReport (see SS3.3).

        Raises:
            BadOHLCVError: if raise_on_invalid=True and report.status ==
                ValidationStatus.INVALID. report.report carries the same
                report object that would otherwise have been returned.

        Special case -- missing required columns:
            If any of config.require_columns is absent from df.columns,
            short-circuits immediately: returns (df.copy(), report) where
            report.status == INVALID and report.issues == [single
            ValidationIssue(check="missing_columns", status=INVALID, ...)].
            No further checks run. Subject to the same raise_on_invalid
            behavior as any other INVALID report.

        Special case -- empty input:
            If df.empty on entry, short-circuits: report.status == INVALID,
            check="empty_input" (rows_in=0, rows_out=0).
        """
```

### 8.3 Module-level convenience function

```python
def validate_ohlcv(
    df: pd.DataFrame,
    symbol: str,
    config: Optional[ValidatorConfig] = None,
    raise_on_invalid: bool = True,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Module-level convenience wrapper:
    OHLCVValidator(config).validate(df, symbol, raise_on_invalid).

    Prefer constructing a shared OHLCVValidator instance in hot loops
    (e.g. strategy.py's per-symbol scan) to avoid repeated config lookups;
    use this function for one-off calls (scripts, tests, REPL).
    """
    return OHLCVValidator(config=config).validate(df, symbol, raise_on_invalid=raise_on_invalid)
```

---

## 9. Integration Points

### 9.1 V1 — `backend/quant/data/loader.py`

Each fetch path (`_fetch_us`, `_fetch_kr_pykrx`, `fetch_from_broker`)
currently ends with `df.sort_index(inplace=True)` (or
`.set_index("date").sort_index()`) and returns. **Insert a single validation
call at the end of `DataLoader.fetch()`** (the public dispatch method), not
inside each private `_fetch_*`, so the KR-fallback-to-US-via-`.KS` path
inside `_fetch_kr` is validated exactly once at the outer boundary:

```python
def fetch(self, symbol, start=None, end=None, period="2y", interval="1d") -> pd.DataFrame:
    if _is_kr(symbol):
        df = self._fetch_kr(symbol, start, end, period, interval)
    else:
        df = self._fetch_us(symbol, start, end, period, interval)

    from backend.data.validator import validate_ohlcv
    df, report = validate_ohlcv(df, symbol, raise_on_invalid=True)
    if report.has_warnings:
        logger.warning("[loader] OHLCV warnings for %s: %s", symbol, report.summary_line())
    return df
```

- **On WARNING:** log via `logger.warning()` (batch summary, §10.1) and
  return `df_clean` — caller proceeds normally. Warnings never block.
- **On INVALID:** `validate_ohlcv` raises `BadOHLCVError`. `DataLoader.fetch()`
  does **not** catch it — it propagates. `fetch_multi()`'s existing
  `except Exception as e: logger.warning("DataLoader.fetch_multi failed %s: %s", sym, e)`
  already catches `BadOHLCVError` (a plain `Exception` subclass) and excludes
  that symbol from the batch result — **no code change needed in
  `fetch_multi`**. Likewise, `strategy.py`'s
  `except Exception as e: logger.warning(...); continue` catches it
  generically — **no code change needed there either**, though the log
  message now says "OHLCV validation failed for AAPL: ohlc_consistency
  (...)" instead of a generic fetch error, a strict diagnosability
  improvement.
- **`fetch_from_broker`'s fallback path**
  (`except Exception: return self.fetch(symbol, period="1y")`) — a
  `BadOHLCVError` raised by the broker branch's own `validate_ohlcv` call
  triggers this fallback to `self.fetch()` (which performs its own
  validation), i.e. **a broker-tier INVALID dataframe causes an automatic
  fallback to yfinance/pykrx** — exactly the desired "INVALID → try next
  tier" behavior, with no new code beyond the validation call itself.

### 9.2 V2 — `backend/quant/live/safeguards.py` `OHLCVRecovery.fetch()`

This is the **primary "INVALID → try next tier" integration point**. Each
`_try_*` method already returns `Optional[pd.DataFrame]` (`None` on failure)
inside its own try/except. Insert validation **inside each `_try_*` method**,
immediately before the success-path `return`, with `raise_on_invalid=False`
(these methods communicate failure via `None`, not exceptions):

```python
def _try_yfinance(self, symbol: str, period: str) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        suffix = ".KS" if (symbol.isdigit() and len(symbol) == 6) else ""
        df = yf.Ticker(f"{symbol}{suffix}").history(period=period, auto_adjust=True)
        if df.empty:
            raise ValueError("empty")
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()

        df, report = self._validator.validate(df, symbol, raise_on_invalid=False)
        if report.is_invalid:
            logger.warning("yfinance OHLCV invalid for %s: %s -- trying next tier",
                           symbol, report.summary_line())
            return None
        if report.has_warnings:
            logger.warning("yfinance OHLCV warnings for %s: %s", symbol, report.summary_line())
        return df
    except Exception as e:
        logger.warning("yfinance 실패 %s: %s", symbol, e)
        return None
```

- `OHLCVRecovery.__init__` gains `self._validator = OHLCVValidator()`
  (constructed once, shared across `_try_*` calls and across symbols —
  stateless per §8.1).
- **On WARNING:** log and return `df` (the cleaned frame) — tier succeeds,
  recovery stops here.
- **On INVALID:** log and `return None` — `OHLCVRecovery.fetch()`'s existing
  waterfall (`if df is not None: ...`) automatically proceeds to the next
  tier. **Zero changes to `fetch()`'s control flow** — only the four
  `_try_*` methods gain a validation call each.
- The final tier (`_get_cache`) is **not** validated — cached data was
  already validated when stored via `_update_cache` (only called after a
  successful, validated `_try_*`). The existing "신선도 불보장" (freshness
  not guaranteed) warning already signals reduced trust for this tier.

### 9.3 Summary table

| Layer | Call | `raise_on_invalid` | On WARNING | On INVALID |
|---|---|---|---|---|
| `loader.py: DataLoader.fetch()` | `validate_ohlcv(df, symbol, raise_on_invalid=True)` | `True` | log + return cleaned df | raise `BadOHLCVError` -> propagates to caller's existing `except Exception` (strategy.py, fetch_multi) |
| `safeguards.py: OHLCVRecovery._try_*()` | `self._validator.validate(df, symbol, raise_on_invalid=False)` | `False` | log + return cleaned df (tier succeeds) | log + `return None` (waterfall proceeds to next tier) |

**Blocking policy for execution:** the loader/recovery layer (V1/V2) is
where hard stops are enforced — `BadOHLCVError` raised at V1 means "no
usable data this cycle for this symbol," which `strategy.py`'s existing
`except Exception: continue` turns into "skip this symbol this cycle" (not a
process crash, not a circuit-breaker trip — guaranteed by `BadOHLCVError`'s
non-`RuntimeError` ancestry). No execution-layer code
(`order_machine.py`, `risk/engine.py`) needs to know about
`ValidationReport` at all — by the time a dataframe reaches those layers, it
has already passed V1/V2 and is `VALID` or `WARNING`-with-cleaned-rows.
Execution-layer NaN guards (V4 `runner.py`, V5 `order_machine.py`, V8
`engine.py` from the 3-2A gate map) remain a **second line of defense** for
NaN values that originate *after* the OHLCV stage — e.g. a live tick price
from `broker.get_price()`, a single `float` the validator never sees.

**Warning policy:** `WARNING` never blocks. The cleaned df is returned and
used normally; warnings exist purely for observability/logging.

---

## 10. Logging Conventions

Module-level `logger = logging.getLogger(__name__)` (`backend.data.validator`).

### 10.1 Warning batch summary (one line per `validate()` call with warnings)

```python
logger.warning("[validator] %s: %s", symbol, report.summary_line())
```

```
[validator] AAPL: WARNING (2 issues, 501 rows, 3 dropped: timestamp_duplicate=2, null_check=1)
[validator] 005930: WARNING (1 issue, 248 rows, 0 dropped: price_spike=1 max=34.2% on 2026-01-09)
```

### 10.2 Per-issue debug detail (opt-in verbosity)

Emitted only when `report.has_warnings or report.is_invalid` (avoid debug
spam on the fully-clean common case):

```python
for issue in report.issues:
    logger.debug("[validator] %s.%s: %s (rows=%d, ts=%s)",
                  symbol, issue.check, issue.message, issue.row_count,
                  [t.date().isoformat() for t in issue.timestamps[:3]])
```

```
DEBUG [validator] 005930.ohlc_consistency: dropped 1 row with High < Low (rows=1, ts=['2026-03-12'])
DEBUG [validator] 005930.price_spike: 1 bar exceeded 30.0% threshold (rows=1, ts=['2026-01-09'])
```

### 10.3 Error on raise (`BadOHLCVError`)

For V1 (`loader.py`), log at `logger.error()` **before** raising, so the
full detail isn't lost if the exception is caught several frames up with a
generic `%s`:

```python
if report.is_invalid:
    logger.error("[loader] OHLCV INVALID for %s: %s", symbol, report.summary_line())
    # validate_ohlcv() raises BadOHLCVError here (raise_on_invalid=True)
```

The catch site logs `BadOHLCVError`'s `__str__` via existing patterns —
`BadOHLCVError`'s `__str__` slots naturally into `%s`:

```
WARNING [IndicatorStrategy] OHLCV 로드 실패 AAPL: OHLCV validation failed for AAPL: ohlc_consistency, null_check (47/52 rows survived)
```

This gives two log lines for an INVALID dataframe — one `ERROR` at the
source (loader, full detail) and one `WARNING` at the catch site (strategy,
exception summary) — consistent with existing dual-logging patterns
elsewhere (e.g. `CalendarService._ensure_holidays_loaded`'s
`logger.warning(...)` at primary-source failure plus `logger.error(...)` at
total failure).

---

## 11. Compatibility Notes

| Pattern | Location | Compatibility guarantee |
|---|---|---|
| `df["Close"]`, `df["Open"]`, etc. | strategy.py, signals.py, fusion.py | Column names/case unchanged — validator never renames columns. |
| `if df is None or len(df) < 50` | strategy.py:100 | `min_rows=50` aligns with this exactly; the existing check remains as harmless redundant defense-in-depth. |
| `df.iloc[-1]`, `df.iloc[-252]` | signals.py:198-199 | The timestamp fix (sort + dedup) makes `iloc[-1]` == most recent calendar date (fixes DS-11). Spike/missing-candle checks never drop rows, so positional offsets like `iloc[-252]` are not shifted by *those* checks; null/ohlc/volume row-drops *can* shift positional offsets by a few rows in pathological cases — the same tradeoff the original 3-2A `validate_ohlcv_dataframe()` sketch already accepted, and unavoidable (the alternative — keeping bad rows — is worse). |
| `close.iloc[-252] == 0` divide-by-zero (DS-07) | signals.py:198-199 | **Incidentally fixed** — `Close > 0` is enforced by the OHLC-consistency check (§5.2), so a *validated* dataframe cannot contain `Close == 0`. If dropping such rows shifts `iloc[-252]` to a different (valid, nonzero) row, DS-07's `ZeroDivisionError` cannot occur post-validation. `signals.py`'s own `if len(close) < 252: return 0.0` guard remains as the length-side defense. The 3-2A V7 point-guard (`if base == 0` check) is still recommended as defense-in-depth — see §12. |
| `weighted_score >= self.buy_threshold` (NaN comparison, DS-05) | fusion.py | **Not solved by the validator** — `fusion.py` computes `weighted_score` from *signal outputs* (RSI, momentum, etc.), not directly from OHLCV cells. Even a fully `VALID` OHLCV dataframe can produce a NaN signal output if a signal's internal math divides by something that becomes zero *after* validation removed rows. V6 (fusion NaN-score guard) from the 3-2A gate map remains **necessary and separate** — see §12. |

**Index dtype/timezone:** the validator does not alter `df.index`'s dtype or
timezone (UTC-naive exchange-local, per `loader.py` convention) — only sorts
and deduplicates. `df.index[-1].date()` and similar accessors used by
`strategy.py`'s staleness check continue to work unchanged.

---

## 12. Reconciliation with TASK 3-2A Gate Map (V1-V10)

| Gate | 3-2A Description | 3-2B Disposition |
|---|---|---|
| **V1 — Loader output** | Call `validate_ohlcv_dataframe()` after each fetch | **Subsumed.** Implemented as `OHLCVValidator.validate()` called once in `DataLoader.fetch()` (§9.1). Covers DS-08, DS-09, DS-11, and the volume=0 *detection* part of DS-12 (the *signal-layer* SMA-deflation effect of DS-12 is out of scope — see V9 below). |
| **V2 — Live bar fetch (safeguards)** | Call `validate_ohlcv_dataframe()` on 4-tier output | **Subsumed.** Implemented inside each `OHLCVRecovery._try_*()` with `raise_on_invalid=False` (§9.2). Covers DS-06 (indirectly — INVALID broker-tier data now correctly triggers fallback instead of being silently used), DS-08, DS-09, DS-11. |
| **V3 — Strategy price guard** (`strategy.py:159`, `if price <= 0`) | Fix `NaN <= 0` bypass for *live tick price* | **Remains a separate point-guard.** `price` here is a single `float` from `broker.get_price()` (a live quote), not a dataframe cell — `OHLCVValidator` never sees it. Still required: `if not math.isfinite(price) or price <= 0: ...` per 3-2A §5.3. |
| **V4 — PnL guard** (`runner.py`, `record_pnl`) | Guard `realized_pnl` NaN before kill-switch accumulation | **Remains a separate point-guard.** `realized_pnl` is computed from `fill.price` (order-fill data), not OHLCV. Out of validator's scope — still required per 3-2A. |
| **V5 — Fill price guard** (`order_machine.py:93`) | Guard `fill_price` NaN before `avg_fill_price` calc | **Remains a separate point-guard.** Fill events are not OHLCV dataframes. Still required. |
| **V6 — Signal score guard** (`fusion.py`) | Return HOLD on NaN `weighted_score` | **Remains a separate point-guard.** Validated OHLCV does not guarantee NaN-free *signal outputs* (computed by `signals.py`/individual `SignalBase` implementations on top of the cleaned df). Still required per 3-2A. |
| **V7 — Momentum base guard** (`signals.py:199`) | Guard `close.iloc[-252] == 0` | **Weakened to defense-in-depth.** The validator's `Close > 0` enforcement (via OHLC-consistency, §5.2) makes `close.iloc[-252] == 0` impossible *for validated data* — but the explicit `if base == 0 or not math.isfinite(base): return None` guard is still recommended, since it's cheap and protects against any future code path that bypasses the validator. |
| **V8 — Trailing stop guard** (`engine.py:72`) | Guard `current_price` NaN | **Remains a separate point-guard.** `current_price` is a live tick, not OHLCV. Still required. |
| **V9 — ATR candle filter** (`position_sizer.py`) | `df = df[df['High'] >= df['Low']]` before ATR | **Subsumed** for the `High >= Low` part — by the time `position_sizer` runs, no `High < Low` rows remain (removed upstream at V1/V2). Recommend keeping V9 as a no-op regression assertion (`assert (df['High'] >= df['Low']).all()`) to verify V1/V2 are wired correctly. The *volume=0-deflates-rolling-average* aspect of DS-12 is **not** addressed (`Volume == 0` is valid data, §5.3) and would need its own point-guard if the team decides DS-12 still warrants a fix. |
| **V10 — Fix missing `get_ohlcv()`** (`kis_adapter/market_data.py`) | Implement the function | **Unrelated to validator — separate workstream.** Still required per 3-2A; once implemented, its output flows through V2 (`OHLCVRecovery._try_broker`) and gets validated like any other tier. |

**Net effect:** V1 and V2 collapse from "call a drop-only function" into
"call `OHLCVValidator.validate()`", with richer reporting. V3-V8 and V10 are
unchanged/separate. V9 is downgraded to a regression-check role.

---

## 13. Testing Plan

For `tests/data/test_validator.py`, following the `tests/data/test_calendar.py`
convention (pure pytest, no network calls; mock `CalendarService` via
`configure_calendar_service(data_source=_MockSource(...))`):

- `test_bad_ohlcv_error_not_runtime_error()` — mirrors
  `test_market_closed_error_not_runtime_error`: asserts
  `not issubclass(BadOHLCVError, RuntimeError)` and
  `issubclass(BadOHLCVError, Exception)`.
- `test_combine_status_worst_wins()` —
  `combine_status(VALID, WARNING, INVALID) == INVALID`,
  `combine_status() == VALID`, `combine_status(VALID, VALID) == VALID`.
- `test_clean_dataframe_returns_valid()` — well-formed df → `status ==
  VALID`, `issues == []`, `df_clean.equals(df)` (modulo copy).
- `test_nan_row_dropped_as_warning_under_threshold()` — 1 NaN row in 100 →
  `WARNING`, `rows_out == 99`.
- `test_high_low_inversion_dropped()` — single `High < Low` row → dropped,
  `dropped_reasons["ohlc_consistency"] == 1`.
- `test_negative_volume_dropped()`, `test_zero_volume_not_flagged()`.
- `test_unsorted_index_autofixed_warning()` — shuffled index → `WARNING`,
  `df_clean.index.is_monotonic_increasing`.
- `test_duplicate_timestamp_deduped_keep_last()`.
- `test_price_spike_flagged_not_dropped()` — 40% jump → `WARNING`, row count
  unchanged.
- `test_missing_candle_ratio_warning()` — using mocked `CalendarService` data
  source (per `tests/data/test_calendar.py::_MockSource` pattern), df missing
  10% of expected trading days → `WARNING`.
- `test_market_resolution_kr_symbol()` / `test_market_resolution_us_symbol()`
  — `_resolve_calendar_market("005930", config) == calendar.Market.KRX`,
  `_resolve_calendar_market("AAPL", config) == calendar.Market.NYSE`.
- `test_invalid_raises_by_default()` /
  `test_invalid_no_raise_when_disabled()`.
- `test_empty_dataframe_invalid()`, `test_missing_columns_invalid()`.
- `test_too_few_rows_invalid()` — `min_rows` boundary.
- `test_calendar_service_failure_does_not_invalidate()` — missing-candle
  check fails soft on `CalendarDataError`.

---

## Future Work (TASK 3-2C — not this task)

Implement `backend/data/validator.py` per this design plus
`tests/data/test_validator.py`, then wire the V1/V2 integration points
described in §9.
