# OHLCV Data Validation — Audit & Design Specification

## 1. Purpose and Scope

This document audits the full OHLCV market data flow from raw data sources through
strategy, indicator computation, signal fusion, risk engine, and execution layers.
No systematic data validation exists at any layer boundary. The audit maps every
point where malformed data (NaN, out-of-range OHLCV, spikes, duplicates) can
propagate and identifies the exact crash sites and silent failure modes.

**Scope:** Market Data → Loader → Safeguards → Strategy → Indicators → Signal Fusion
→ Risk Engine → Position Sizer → Order Machine → Position Tracker → Runner (PnL).

**Not in scope:** WebSocket real-time tick data, backtest simulation path,
Kiwoom stub (no live data path).

---

## 2. Current Structure Analysis

### 2.1 Data Sources

| Source | File | Markets | Entry Point |
|---|---|---|---|
| **yfinance** | `backend/quant/data/loader.py:24-50` | US (primary) | `download(tickers, period, interval, auto_adjust=True)` |
| **pykrx** | `backend/quant/data/loader.py:55-80` | KR (primary) | `stock.get_market_ohlcv(date, date, ticker)` |
| **KIS live price** | `kis_adapter/market_data.py` | KR+US (real-time) | `get_price_kr()` / `get_price_us()` — single float, not OHLCV |
| **Kiwoom** | `kiwoom_adapter/market_data.py` | KR | `get_price_kr()` — stub |
| **4-tier recovery** | `backend/quant/live/safeguards.py:220-260` | KR+US | KIS→yfinance→pykrx→cache waterfall |

**Critical gap:** `kis_adapter.market_data.get_ohlcv()` is called at
`backend/quant/live/safeguards.py:242` but **does not exist**. The `AttributeError`
is swallowed by the outer `try/except Exception`; execution falls through to tier 3
(yfinance) silently. See DS-06.

### 2.2 Data Flow

```
Data Sources (yfinance / pykrx / KIS / cache)
    │
    ▼
backend/quant/data/loader.py           ← fetch; rename KR columns to EN; no validation
    │
    ▼
backend/quant/live/safeguards.py       ← 4-tier recovery; StaleDataWatchdog (dead code)
    │
    ▼
backend/strategy/indicator/strategy.py ← DataFrame consumed directly
    │  ├─ _scan_and_trade()            (line 100: only length check)
    │  └─ _execute_buy/sell()          (line 159: price <= 0 only — NaN passes)
    │
    ▼
strategy/signals.py                    ← MultiTimeframeSignals: SMA, RSI, momentum
backend/quant/signals/fusion.py        ← weighted score combiner
    │
    ▼
backend/quant/risk/engine.py           ← daily/weekly/MDD loss tracking; trailing stop
backend/quant/risk/position_sizer.py   ← ATR/Kelly/vol sizing
    │
    ▼
backend/execution/order_machine.py     ← avg_fill_price accumulation
backend/execution/position_tracker.py ← weighted avg_price from fill events
    │
    ▼
backend/worker/runner.py               ← realized PnL = (fill.price - entry) × qty
```

### 2.3 Column Naming Convention

| Layer | Format | Example |
|---|---|---|
| DataFrames (yfinance native) | Uppercase | `Open`, `High`, `Low`, `Close`, `Volume` |
| pykrx output (after rename) | Uppercase | Korean `시가→Open`, `고가→High`, etc. |
| `Bar` dataclass (`base.py`) | Lowercase | `bar.open`, `bar.close` |

Mixed-case references fail silently: `df["close"]` on a yfinance DataFrame returns
a NaN-filled Series rather than raising a `KeyError`.

### 2.4 Existing Validation

| File | Check | Gap |
|---|---|---|
| `loader.py` | `if df.empty` | NaN values, out-of-range, duplicates |
| `strategy.py:100` | `if df is None or len(df) < 50` | NaN rows, malformed values |
| `strategy.py:159` | `if price <= 0: return` | `NaN <= 0` → `False` — NaN passes through |
| `position_sizer.py` | Kelly: `if avg_loss == 0` | NaN avg_loss, NaN avg_win |
| `position_sizer.py` | Vol: `if realized_vol <= 0` | NaN realized_vol |
| `engine.py:269` | `max(peak, 1.0)` | NaN peak not guarded |

No existing check for: `high < low`, `close outside [low, high]`, negative prices,
negative volume, duplicate timestamps, timestamp reversal, extreme price spikes,
or `np.inf` values.

---

## 3. Failure Scenarios

### DS-01 — CRITICAL: Kill-switch bypass via NaN equity

**Files:** `backend/quant/risk/engine.py:248-274`; `backend/worker/runner.py:466`

**Trigger:** yfinance returns NaN close for a corporate action day; strategy sells
the position; the fill price is NaN (inherited from bar data).

**Propagation path:**
```
fill.price = NaN (from bar)
runner.py:472:  realized_pnl = (NaN - entry_price) * fill.qty  → NaN
engine.py:248:  record_pnl(NaN)
engine.py:255:  self._daily_pnl += NaN                         → NaN
engine.py:260:  NaN / peak = NaN;  NaN < -0.03                 → False
                ↳ kill-switch never fires
```

**Result:** Daily loss limit, weekly limit, and MDD check all silently pass when
PnL is NaN. The system continues trading through unlimited losses. This is the
highest-severity failure mode in the codebase.

### DS-02 — CRITICAL: Trailing stop and stop-loss bypass via NaN price

**Files:** `backend/quant/risk/engine.py:72-75`; `backend/strategy/indicator/strategy.py:229`

**Trigger:** Live price fetch returns NaN (API timeout caught, NaN default returned).

**Trailing stop path:**
```
engine.py:72:  trailing_stop = max(trailing_stop, NaN * 0.93)  → NaN
engine.py:75:  current_price < trailing_stop
               NaN < NaN                                        → False
               ↳ trailing stop never triggers
```

**Fixed stop-loss path** (`strategy.py:229`):
```python
change = current_price / entry_price   # NaN / entry_price = NaN
change < 0.93                          # NaN < 0.93 → False → no sell
```

**Result:** Both trailing and fixed stop-loss fire on `False` for NaN prices.
Position is held indefinitely while declining.

### DS-03 — CRITICAL: Division-by-zero crash at order quantity calculation

**File:** `backend/strategy/indicator/strategy.py:168, 229`

```python
qty = int(amount_krw / price)           # line 168 — ValueError if price=NaN
change = current_price / entry_price    # line 229 — ZeroDivisionError if entry=0
```

**Trigger:** Data source returns price=0 (corporate action, penny stock) or NaN.

`int(amount / NaN)` raises `ValueError`. The exception propagates through
`_execute_buy()`, crashes that strategy session, and leaves `try_mark_pending()`
locked (symbol permanently excluded from new orders until process restart — see
EX-08 in the ORDER_POLLING_RELIABILITY audit).

**Note:** The existing guard `if price <= 0: return` (line 159) does NOT prevent
this because `NaN <= 0` evaluates to `False` in Python.

### DS-04 — HIGH: NaN fill price corrupts avg_fill_price in OrderStateMachine

**File:** `backend/execution/order_machine.py:93`

```python
avg_fill_price = (prev_total + fill_price * fill_qty) / order.filled_qty
```

If `fill_price = NaN`, `avg_fill_price = NaN`. All downstream PnL calculations
(`runner.py`, `position_tracker.py`, `models.py:unrealized_pnl_pct`) produce NaN
silently. The reconciler comparison `abs(avg_price - value) > 0.01`
(`reconciler.py:208`) silently returns `False` for NaN, masking the corruption.

### DS-05 — HIGH: Signal fusion silent no-buy via NaN score

**File:** `backend/quant/signals/fusion.py`

```python
weighted_score = sum(w * s for w, s in zip(weights, scores))
# Any NaN component → weighted_score = NaN
buy_signal = weighted_score >= buy_threshold    # NaN >= 0.6 → False
```

**Result:** Any NaN in any single signal component causes the symbol to be
permanently excluded from buy signals for the session. No warning is logged.
An operator looking at the strategy would see it running normally but producing
no buys — an extremely difficult bug to diagnose.

### DS-06 — HIGH: Missing `get_ohlcv()` function causes silent data-tier fallback

**File:** `backend/quant/live/safeguards.py:242`; `kis_adapter/market_data.py`

```python
ohlcv = kis_market_data.get_ohlcv(symbol, ...)   # AttributeError — not implemented
```

The `AttributeError` is caught by the outer `try/except Exception`. A warning
is logged as "KIS OHLCV failed" (not "function missing"). The 4-tier recovery
silently proceeds to tier 2 (yfinance). Live sessions unknowingly use yfinance
data rather than KIS real-time data, without any operator notification of the
difference.

### DS-07 — HIGH: 12-1 momentum calculation divide-by-zero

**File:** `strategy/signals.py:198-199`

```python
momentum = close.iloc[-1] / close.iloc[-252] - 1
```

If `close.iloc[-252] = 0` (penny stock delisted to zero): `ZeroDivisionError`
crashes the signal computation for the entire scan. If `= NaN`: momentum = NaN
→ signal NaN → silent no-buy (DS-05).

**Frequency:** Any symbol with < 252 trading days of history (newly listed) where
pykrx backfills missing rows with 0.

### DS-08 — MEDIUM: `high < low` candle inverts ATR → oversized position

**File:** `backend/quant/risk/position_sizer.py` (ATR calculation path)

ATR uses `max(high-low, |high-prev_close|, |low-prev_close|)`. A single candle
with `high < low` (data vendor error, pre-market data corruption) makes `high-low < 0`.
This deflates the ATR reading, causing the volatility-adjusted position size to
exceed the 5% per-symbol cap without triggering any guard.

### DS-09 — MEDIUM: Duplicate candles cause wrong indicator windows

**Trigger:** Exchange API or pykrx returns two rows with the same timestamp (common
on trading halt days or data vendor corrections).

**Effect:** Rolling SMA(200) over 201 physical rows covers fewer than 200 unique
calendar days. RSI, ATR, and 12-1 momentum all compute on windows that are shorter
than intended, producing signals based on less history than the strategy assumes.
No deduplication exists anywhere in the pipeline.

### DS-10 — MEDIUM: Extreme price spike produces false buy signal

**Trigger:** Data vendor error inserts `close = 999999` for a $100 stock.

**Path:**
```
RSI(14) may saturate to 100         → should block (RSI < 70 check fails) ✓
BUT: close > SMA_200 satisfied       ✓
     3-month return > 0 satisfied    ✓
     volume check may pass           ✓
→ 3/4 conditions met depending on RSI timing
→ order placed at spike price
→ broker rejects (price out of band)
→ ConsecutiveFailureBreaker counts as failure
```

**Note:** RSI > 70 correctly blocks the buy in this case IF the spike candle
dominates the RSI window. But the spike also corrupts the momentum and SMA
calculations for subsequent sessions.

### DS-11 — LOW: Unsorted timestamps cause stale `iloc[-1]` reference

**Trigger:** pykrx (or yfinance after multi-ticker concat) returns rows not sorted
by date.

`df.iloc[-1]` is the last physical row, not the most recent date. Indicators
reading "the latest" close read a stale value. No `sort_index()` call exists in
`loader.py`.

### DS-12 — LOW: Volume=0 candles deflate 20-day average volume threshold

**Trigger:** Trading halt day included in the OHLCV window (volume=0).

If a halt day enters the 20-day rolling volume mean, the threshold is lowered.
Subsequent low-volume days that should be excluded pass the `volume > 20d_avg`
filter, generating false buy signals on thinly-traded sessions.

---

## 4. Risk Classification

| ID | Severity | Crash? | Silent? | Impact |
|---|---|---|---|---|
| DS-01 | **CRITICAL** | No | **Yes** | Loss limits never trigger; unlimited loss exposure |
| DS-02 | **CRITICAL** | No | **Yes** | Stop-loss disabled; position held through continuous decline |
| DS-03 | **CRITICAL** | **Yes** | No | Strategy session crash; symbol permanently locked |
| DS-04 | HIGH | No | **Yes** | All downstream PnL calculations corrupted |
| DS-05 | HIGH | No | **Yes** | Symbol silently excluded from all buy signals |
| DS-06 | HIGH | No | **Yes** | Wrong data source used without operator awareness |
| DS-07 | HIGH | **Yes** | No | Signal computation crash; entire scan aborted |
| DS-08 | MEDIUM | No | **Yes** | 5% position size limit violated silently |
| DS-09 | MEDIUM | No | **Yes** | Indicators computed on wrong historical window |
| DS-10 | MEDIUM | No | No | Broker rejection → circuit breaker incremented |
| DS-11 | LOW | No | **Yes** | Wrong bar processed for signal generation |
| DS-12 | LOW | No | **Yes** | False buys on illiquid days |

**Summary:** 3 CRITICAL (2 silent risk bypasses + 1 crash), 4 HIGH, 3 MEDIUM, 2 LOW.

**Most dangerous pattern:** DS-01 + DS-02 together — NaN prices disable both the
kill-switch (DS-01) and the stop-loss (DS-02) simultaneously, leaving open positions
exposed to unlimited decline with no protective mechanism active.

---

## 5. Validation Insertion Points

### 5.1 Primary Gate — `validate_ohlcv_dataframe()` (new shared function)

A single validation function should be created in `backend/quant/data/loader.py`
(or `backend/data/ohlcv_validator.py`) and called at V1 and V2:

```python
def validate_ohlcv_dataframe(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Clean and validate an OHLCV DataFrame.
    - Sort by ascending timestamp index
    - Drop duplicate index entries (keep last)
    - Assert columns: Open, High, Low, Close, Volume present
    - Drop rows where any price is NaN, 0, negative, or non-finite
    - Drop rows where High < Low or Close not in [Low, High]
    - Drop rows where Volume < 0
    - Log count of dropped rows as WARNING if any dropped
    Returns cleaned DataFrame; raises BadOHLCVError if result is empty.
    """
```

**Exception:** `BadOHLCVError(Exception)` — not a `RuntimeError` (circuit breaker safe).

### 5.2 Validation Gate Map

| Gate | File:Location | Action | Addresses |
|---|---|---|---|
| **V1 — Loader output** | `loader.py` after each fetch block | Call `validate_ohlcv_dataframe(df, symbol)` | DS-08,09,11,12 (upstream) |
| **V2 — Live bar fetch** | `safeguards.py:_fetch_ohlcv()` return path | Call `validate_ohlcv_dataframe(df, symbol)` | DS-06,08,09,11 |
| **V3 — Strategy price guard** | `strategy.py:159` (before `int(amount/price)`) | `if not math.isfinite(price) or price <= 0: return` | DS-03 |
| **V4 — PnL guard** | `runner.py:472` (before `record_pnl()`) | `if not math.isfinite(realized_pnl): logger.warning(...); return` | DS-01 |
| **V5 — Fill price guard** | `order_machine.py:93` (before avg calc) | `if not math.isfinite(fill_price): raise ValueError(...)` | DS-04 |
| **V6 — Signal score guard** | `fusion.py` (before threshold compare) | `if not math.isfinite(weighted_score): return TradingSignal(HOLD, "nan_score")` | DS-05 |
| **V7 — Momentum base guard** | `signals.py:199` | `if base == 0 or not math.isfinite(base): return None` | DS-07 |
| **V8 — Trailing stop guard** | `engine.py:72` | `if not math.isfinite(current_price): return` | DS-02 |
| **V9 — ATR candle filter** | `position_sizer.py` (ATR input) | `df = df[df['High'] >= df['Low']]` before ATR calc | DS-08 |
| **V10 — Fix missing function** | `kis_adapter/market_data.py` | Implement `get_ohlcv(symbol, from_date, to_date)` | DS-06 |

### 5.3 Fix for `strategy.py:159` NaN price bypass

Current code:
```python
if price <= 0:   # NaN <= 0 → False — NaN passes
    return
```

Replace with:
```python
if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
    logger.warning("무효 가격 [%s]: %s — 매수 생략", symbol, price)
    return
```

### 5.4 Fix for `get_ohlcv()` missing function

Add to `kis_adapter/market_data.py`:
```python
def get_ohlcv(self, symbol: str, from_date: str, to_date: str,
              market: str = "KR") -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV via KIS inquire-daily-price API.
    Returns DataFrame with Open/High/Low/Close/Volume columns,
    indexed by date (ascending). Returns None on API failure."""
```

This makes the 4-tier recovery in `safeguards.py` work as intended at tier 1.

---

## 6. Affected Files

| File | Finding IDs | Nature |
|---|---|---|
| `backend/quant/data/loader.py` | DS-08,09,11,12 | Primary fetch; no output validation |
| `backend/quant/live/safeguards.py` | DS-06 | 4-tier recovery; calls missing function |
| `backend/strategy/indicator/strategy.py` | DS-02,03,10 | Consumes raw bars; div-by-zero at lines 168, 229 |
| `strategy/signals.py` | DS-05,07 | MultiTimeframeSignals; momentum div-by-zero |
| `backend/quant/signals/fusion.py` | DS-05 | Weighted score; NaN → False → silent no-buy |
| `backend/quant/risk/engine.py` | DS-01,02 | Kill-switch; trailing stop; NaN bypasses limits |
| `backend/quant/risk/position_sizer.py` | DS-08 | ATR sizer; inverted candle inflates position |
| `backend/execution/order_machine.py` | DS-04 | avg_fill_price corruption from NaN fill |
| `backend/execution/position_tracker.py` | DS-04 | Weighted avg_price from unvalidated fill.price |
| `backend/worker/runner.py` | DS-01 | Realized PnL not guarded against NaN |
| `kis_adapter/market_data.py` | DS-06 | Missing `get_ohlcv()` function |
| `backend/brokers/models.py` | DS-04 | `unrealized_pnl_pct` checks `==0` but not NaN |
| `api/routers/quick_trade.py` | DS-03,10 | User-supplied price not validated before broker |
| `backend/execution/reconciler.py` | DS-04 | NaN comparison in `abs(avg - val) > 0.01` silent |

---

## 7. Unsafe Assumptions

1. **"yfinance download has no NaN prices."** False: corporate actions, delisted
   tickers, and partial trading days produce NaN close values. `auto_adjust=True`
   can insert NaN for adjusted prices during restatements.

2. **"`price <= 0` prevents division-by-zero."** False: `NaN <= 0` returns `False`
   in Python, so NaN passes the guard unchanged.

3. **"Signal NaN propagation will raise an exception."** False: all pandas/numpy
   NaN comparisons return `False` silently. `NaN >= threshold` means "no signal"
   rather than an error — the system appears healthy while never generating buys.

4. **"The 4-tier recovery always provides valid data."** False: tier 1
   (`get_ohlcv()`) does not exist; the waterfall silently uses tier 2+ data
   without notifying the operator that the intended primary source failed.

5. **"pykrx returns well-ordered, deduplicated DataFrames."** Unverified: the
   pykrx API returns data indexed by date, but duplicate dates on trading halt
   days or data-vendor corrections have been observed in practice.

6. **"Momentum calculation is safe for all tickers in the universe."** False:
   newly-listed stocks and penny stocks with zero-price history produce
   `ZeroDivisionError` in the 12-1 momentum formula.

7. **"The kill-switch always triggers on loss threshold breach."** False: any NaN
   in the PnL chain (fill price, sell price, entry price) causes the daily loss
   accumulator to become NaN, after which all threshold comparisons return `False`.

---

## 8. New Code Requirements

| File | Change |
|---|---|
| `backend/data/ohlcv_validator.py` | **NEW** — `validate_ohlcv_dataframe()`, `BadOHLCVError` |
| `backend/quant/data/loader.py` | Call `validate_ohlcv_dataframe()` after each source fetch |
| `backend/quant/live/safeguards.py` | Call validator on 4-tier output; fix DS-06 tier-1 path |
| `backend/strategy/indicator/strategy.py` | Replace `price <= 0` with `math.isfinite(price) and price > 0` |
| `backend/worker/runner.py` | Guard `realized_pnl` NaN before `record_pnl()` |
| `backend/execution/order_machine.py` | Guard `fill_price` NaN before avg_fill_price calc |
| `backend/quant/signals/fusion.py` | Return HOLD signal when `weighted_score` is NaN |
| `strategy/signals.py` | Guard `close.iloc[-252]` zero/NaN in momentum |
| `backend/quant/risk/engine.py` | Guard `current_price` NaN in trailing stop update |
| `backend/quant/risk/position_sizer.py` | Drop `high < low` candles before ATR |
| `kis_adapter/market_data.py` | Implement `get_ohlcv(symbol, from_date, to_date)` |
| `tests/data/test_ohlcv_validation.py` | **NEW** — full test suite for validation function |

---

## 9. Verification

```bash
# Validate the validation function itself
pytest tests/data/test_ohlcv_validation.py -v

# NaN kill-switch: fill with NaN price still triggers loss limit
pytest tests/quant/test_risk_engine.py -v -k nan_pnl_triggers_kill_switch

# Division-by-zero guard at qty calculation
pytest tests/strategy/test_indicator_strategy.py -v -k zero_price_guard

# Trailing stop NaN safe: NaN price does not suppress the trailing stop
pytest tests/quant/test_risk_engine.py -v -k trailing_stop_nan_safe

# Momentum zero-base: no ZeroDivisionError
pytest tests/strategy/test_signals.py -v -k momentum_zero_base

# Signal fusion NaN: NaN score returns HOLD explicitly
pytest tests/quant/test_fusion.py -v -k nan_score_returns_hold

# Smoke: get_ohlcv exists and returns a DataFrame
python -c "
from kis_adapter.market_data import KISMarketData
df = KISMarketData().get_ohlcv('005930', '2026-01-01', '2026-01-31')
print('columns:', df.columns.tolist() if df is not None else 'None (offline ok)')
"
```
