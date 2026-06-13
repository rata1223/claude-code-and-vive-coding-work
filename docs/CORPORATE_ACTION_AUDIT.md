# Corporate Action Audit (TASK 3-4A)

> **Read-only audit.** No `.py` files were modified to produce this document.
> Every claim below is backed by a `file:line` citation, verified against the
> `claude/trading-platform-philosophy-yNHQK` branch as of 2026-06-13. Where the
> codebase does not provide enough information to make a claim (e.g. pykrx's
> adjustment behavior), this is stated explicitly as an **open question**
> rather than guessed.
>
> This audit cross-references `docs/RECONCILIATION_ENGINE.md`,
> `docs/STALE_DATA_AUDIT.md` / `docs/STALE_DATA_DETECTOR.md`, and
> `docs/OHLCV_DATA_VALIDATION.md` / `docs/OHLCV_VALIDATION.md`. It introduces
> the **CA-01 .. CA-13** finding-ID convention, mirroring the
> `SD-01..SD-13` (3-3A) and `DS-01..DS-NN` (3-2A) numbering precedents.

---

## 1. Purpose & Scope

This audit traces every code path that handles, or fails to handle, the
following **corporate actions**:

1. Stock split
2. Reverse split
3. Dividend
4. Ticker change
5. Merger
6. Spinoff
7. Unknown / unclassified events

...across the following data and execution layers:

- **Price history** (`backend/quant/data/loader.py`, OHLCV ingestion)
- **Positions** (`backend/execution/position_tracker.py`,
  `backend/database/models.py::Position`)
- **Portfolio valuation** (`backend/brokers/kis.py`, `kis_adapter/portfolio.py`,
  `backend/database/models.py::EquitySnapshot`)
- **Backtest data** (`backend/strategy/indicator/backtest.py`,
  `backend/strategy/runtime/simulator.py`)
- **Execution data** (`backend/execution/{order_machine,reconciler}.py`,
  `backend/database/models.py::{Order,Fill,Trade}`)
- **Market data ingestion** (`backend/quant/data/loader.py`,
  `backend/quant/live/safeguards.py`, `kis_adapter/market_data.py`)

### In Scope

- KIS (live price + portfolio), Kiwoom (stub), OpenBB (integration status),
  yfinance, pykrx, and "기타" (other) sources.
- Every layer where a price- or quantity-denominated value is stored,
  compared, or acted upon, since corporate actions change *both* price and
  quantity simultaneously (in inverse proportion for splits) or quantity
  alone (mergers/spinoffs) or cash alone (dividends).

### Out of Scope

- **Implementation** — this audit specifies *where* corporate-action handling
  should live (§6) and *who* should own it (§7), but writes no code. A future
  TASK 3-4B would design the concrete module (see §11).
- **Kiwoom internals** beyond confirming it remains a stub with no live data
  path (per `CLAUDE.md`: "키움증권: 아직 스텁 없음").
- **OpenBB** beyond re-confirming it is not integrated (§5.3, consistent with
  the 3-3A finding).

### Relationship to Prior Audits (Orthogonality Note)

This audit is **orthogonal** to two prior audits, and the boundary matters for
future work:

- `docs/STALE_DATA_AUDIT.md` / `STALE_DATA_DETECTOR.md` (3-3A/3-3B) address
  **wall-clock freshness** — "is this data old?" A corporate-action price gap
  is not a freshness problem; the data can be perfectly fresh and still
  contain a legitimate ~50% single-day move (a 2:1 split).
- `docs/OHLCV_DATA_VALIDATION.md` / `OHLCV_VALIDATION.md` (3-2A/3-2B) address
  **structural validity** — NaN, `high < low`, price spikes, etc. A future
  3-2B price-spike validator could **misclassify a real split as bad data**
  (both look like a large single-bar return), and conversely a real data
  error could be **misclassified as a split**. This audit does not resolve
  that ambiguity — it is flagged here (CA-12, §10) as a future
  reconciliation point between the 3-2B validator and any 3-4B
  corporate-action detector.

---

## 2. Current Structure Analysis

### 2.1 Data Sources & Price-Adjustment Ownership

| Source | File:Line | Adjustment Call | Adjustment Behavior |
|---|---|---|---|
| **yfinance (US OHLCV, primary loader)** | `backend/quant/data/loader.py:74-76` | `ticker.history(..., auto_adjust=True)` | **Split- and dividend-adjusted.** The entire returned series is re-based to the query-time basis. No `actions=True`, so no event metadata. |
| **yfinance (4-tier recovery, tier 2)** | `backend/quant/live/safeguards.py:214` | `yf.Ticker(f"{symbol}{suffix}").history(period=period, auto_adjust=True)` | Same as above — consistent `auto_adjust=True` with the primary loader. |
| **yfinance (backtest fetch)** | `backend/strategy/indicator/backtest.py:17` | `yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)` | Same. |
| **yfinance (optimizer)** | `backend/strategy/optimizer.py:20` | `auto_adjust=True` | Same. |
| **yfinance (MultiTimeframeSignals)** | `strategy/signals.py:39` | `auto_adjust=True` | Same. |
| **yfinance (indicator API router)** | `api/routers/indicators.py:163` | `auto_adjust=True` | Same. |
| **pykrx (KR OHLCV)** | `backend/quant/data/loader.py:107-125` | `krx.get_market_ohlcv_by_date(s, e, symbol)` — **no adjustment parameter** | **UNVERIFIED** — see CA-06. No code comment or parameter indicates whether returned prices are split-adjusted. |
| **KIS live price** | `kis_adapter/market_data.py` — `get_price_us`/`get_price_kr` | N/A — single current-price float/int | Real-time market price; inherently reflects post-action reality. No "adjustment" concept applies, but also no historical series. |
| **KIS portfolio (live positions)** | `kis_adapter/portfolio.py`, `backend/brokers/kis.py:71-104` | N/A — broker reports current qty/avg_price | The exchange/broker auto-adjusts qty and avg cost basis for real corporate actions (this is standard brokerage behavior, not something this codebase implements). |
| **Kiwoom** | `kiwoom_adapter/` | N/A | Stub — no live data, no historical data, no adjustment of any kind. |
| **OpenBB** | — | N/A | Not integrated (§5.3). |

**Six confirmed `auto_adjust=True` call sites** for US/yfinance data. All six
are consistent with each other (no mixed adjusted/unadjusted US fetches). The
**only** unverified source is pykrx for KR symbols (CA-06).

### 2.2 Data Flow Diagram

```
Data Sources
  yfinance (auto_adjust=True, US)        pykrx (KR, adjustment UNVERIFIED)
  KIS live price (real-time, no history) Kiwoom (stub)        OpenBB (N/A)
        │                                        │
        ▼                                        ▼
backend/quant/data/loader.py  ─────────────────────────────────  ← fetch + EN column rename, no CA awareness
        │
        ▼
backend/quant/live/safeguards.py (OHLCVRecovery 4-tier: KIS→yfinance→pykrx→cache)
        │
        ├──────────────────────────────────────┐
        ▼                                       ▼
backend/strategy/indicator/strategy.py   backend/strategy/indicator/backtest.py
  (live scan loop)                          (single auto_adjust=True fetch — §2.4)
        │                                       │
        ▼                                       ▼
strategy/signals.py / backend/quant/signals/fusion.py   backend/strategy/runtime/simulator.py
  (SMA, 12-1 momentum, RSI, regime)              (SimulatedBroker — backtest-only position tracking)
        │
        ▼
backend/quant/risk/engine.py
  TrailingStopManager / PositionStop  ← peak_price, trailing_stop, hard_stop, entry_price (§2.3 "copy 3")
  PersistentLossTracker._evaluate()   ← daily/weekly/MDD kill-switch (CA-01)
        │
        ▼
backend/execution/order_machine.py → backend/execution/position_tracker.py
  PositionTracker._positions[symbol]  ← Fill-driven avg_price/qty (§2.3 "copy 1")
        │                                       │
        │                                       ▼
        │                              backend/brokers/kis.py / kis_adapter/portfolio.py
        │                                  get_positions() — broker-side, auto-adjusted (ground truth)
        ▼                                       │
backend/execution/reconciler.py  ◄──────────────┘
  qty_mismatch / missing_in_db / stale_db_position gaps
  _QTY_TOLERANCE=1, unconditional auto-repair (CA-03)
        │
        ▼
backend/database/models.py
  Position / Trade / Order / Fill / EquitySnapshot  ← no corporate-action fields (CA-04)
```

### 2.3 Position/PnL State — Three Independent Copies

A single held position's price/quantity state exists in **three places**
that can each independently drift after a corporate action, with **no
mechanism that synchronizes all three together**:

**Copy 1 — `PositionTracker._positions[symbol]`** (in-memory, process
lifetime, restored from DB on startup via `restore_positions`). Updated
*only* by `Fill` events via a weighted-average formula:

```python
# backend/execution/position_tracker.py:99-102
total_qty = pos.qty + fill.qty
pos.avg_price = (pos.avg_price * pos.qty + fill.price * fill.qty) / total_qty
pos.qty = total_qty
```

This formula has no concept of "the share count changed without a fill." A
2:1 split doubles `qty` and halves `avg_price` at the broker — Copy 1 sees
neither change until the next `Fill` (which won't happen) or the next
reconciliation (CA-03).

**Copy 2 — DB `Position` row** (`backend/database/models.py:78-89`),
synchronized with Copy 1 via `Fill` persistence, and with the broker
("Copy 0", below) only by `backend/execution/reconciler.py`.

**Copy 0 — Broker-reported position** (`backend/brokers/kis.py:71-104` →
`kis_adapter/portfolio.py`). This is the *true* post-corporate-action state —
the exchange/broker has already applied the split/dividend/merger adjustment
to `qty`/`avg_price` by the time `get_positions()` is called.

**Copy 3 — `TrailingStopManager._positions[symbol]`**
(`backend/quant/risk/engine.py:95`, pure in-memory, **never persisted, never
reconciled**):

```python
# backend/quant/risk/engine.py:58-79
@dataclass
class PositionStop:
    symbol: str
    entry_price: float
    entry_date: str
    peak_price: float
    trailing_stop: float
    hard_stop: float
    qty: int
    trailing_stop_pct: float = 0.07

    def update_peak(self, current_price: float) -> None:
        if current_price > self.peak_price:
            self.peak_price = current_price
            self.trailing_stop = current_price * (1 - self.trailing_stop_pct)

    def is_stopped(self, current_price: float) -> tuple[bool, str]:
        if current_price <= self.hard_stop:
            return True, "hard_stop"
        if current_price <= self.trailing_stop:
            return True, "trailing_stop"
        return False, ""
```

Four price-denominated fields (`entry_price`, `peak_price`,
`trailing_stop`, `hard_stop`) are captured once at `open()` time and updated
only by `update_peak()`. **Nothing in the codebase ever rewrites these fields
to reflect a corporate action** — confirmed by grep (§12): zero references to
`TrailingStopManager`/`PositionStop` exist in `reconciler.py`.

### 2.4 Backtest Data Flow — Internal Consistency Caveat

`backend/strategy/indicator/backtest.py:16-20`:

```python
def _fetch(symbol: str, start: str, end: str) -> pd.DataFrame:
    df = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"데이터 없음: {symbol}")
    return df
```

This is a **single** `auto_adjust=True` call covering the entire
`[start, end]` range. yfinance's `auto_adjust=True` re-bases the **entire**
returned series to a single (query-time) adjustment basis — there is **no
price discontinuity at a historical split date within this single
DataFrame**. A 200-day SMA, 12-1 momentum ratio, or trailing-stop check
computed entirely from this one `df` will **not** see a split-induced jump.

This is an important correction to a naive reading of the codebase: it is
**not true** that "AAPL's Aug 9 2020 close is $145 and Aug 31 2020 close is
$36 in the same backtest `df`" — `auto_adjust=True` makes both values
post-split-equivalent (~$36-ish) in a single fetch. `SimulatedBroker`
(`backend/strategy/runtime/simulator.py`) computes `avg_price` and
`current_price` from this *same* internally-consistent `df`, so **within one
backtest run**, Copy 1/Copy 0/Copy 3-equivalent values never diverge.

The actual risk is the inverse: **backtests can never exercise the failure
modes (CA-01, CA-02) that depend on cross-time divergence between
previously-stored state and newly-fetched/broker-reported state**, because a
backtest never re-fetches mid-run under a *different* adjustment basis. This
is formalized as CA-13.

### 2.5 Existing Corporate-Action Awareness: None

Grep across `backend/`, `kis_adapter/`, `kiwoom_adapter/` for
`split|dividend|corporate|adjust|merger|spinoff|ticker_change|symbol_change`
(case-insensitive) returns **zero hits related to corporate-action handling**.
Every hit is one of:

1. The six `auto_adjust=True` yfinance call sites (§2.1) — a *price*
   adjustment flag, not corporate-action *event* handling.
2. `adjust=False` in **technical-indicator EMA calculations**
   (`backend/quant/indicators/base.py:32`, `volatility.py:36`,
   `backend/quant/signals/regime.py:221-225`) — this is pandas'
   `ewm(adjust=False)` parameter controlling exponential-weighting
   normalization, **completely unrelated to corporate actions**. Flagged
   here explicitly so a future reader does not chase this as a false lead.
3. `adjusted_score` in `backend/quant/signals/fundamental.py:64` — an
   unrelated variable name for a weighting-filter output.
4. The `# Workaround for the pandas-ta-openbb fork` comment in
   `backend/__init__.py` (and `api/__init__.py`, `strategy/__init__.py`) —
   refers to a **technical-indicator library fork**, not the OpenBB
   data-provider SDK (§5.3).
5. `backend/data/calendar.py:603` — `"adjusted for early-close"` comment,
   about **market session times**, not price adjustment.

One acknowledgment of **dividends** exists, but purely as an
out-of-scope/observability note:

> `docs/RECONCILIATION_ENGINE.md:302-305` — "Portfolio drift can reflect
> legitimate in-flight settlement (T+2 cash), **dividend payments**, FX
> movements, or fee deductions not captured in fill records." (CA-10)

No code anywhere defines a `split_ratio`, `adjustment_factor`,
`corporate_action_type`, `ex_date`, or any symbol-rename mapping.

---

## 3. Event Support Matrix

| Source | Stock Split | Reverse Split | Dividend | Ticker Change | Merger | Spinoff | Unknown |
|---|---|---|---|---|---|---|---|
| **KIS** (live price + portfolio) | Implicit only — broker auto-adjusts `qty`/`avg_price` in `get_positions()` (`kis.py:71-104`); **no explicit event flag or calendar endpoint** | Same as split (implicit, no flag) | Implicit — dividend cash credited to broker cash balance; **no event flag** (acknowledged drift, CA-10) | Broker reports the **new** symbol in `get_positions()`; our DB row under the **old** symbol becomes orphaned (CA-08) | Same shape as ticker change — target shares converted to acquirer shares or cash, broker reflects new state, our DB does not | New symbol appears in broker's position list with no DB/Fill counterpart (CA-09) | No detection path — `kis_adapter/market_data.py` exposes only `get_price_us`/`get_price_kr`/`get_pending_us`, no calendar/events endpoint |
| **Kiwoom** | N/A — confirmed stub, no live data path (`kiwoom_adapter/`) | N/A | N/A | N/A | N/A | N/A | N/A |
| **OpenBB** | Not integrated | Not integrated | Not integrated | Not integrated | Not integrated | Not integrated | Not integrated |
| **yfinance** | Price retroactively re-based via `auto_adjust=True` (§2.1); `Ticker.splits` / `actions=True` exist in the yfinance API but are **never called** | Same mechanism (a reverse split is just a split with ratio < 1) | Price retroactively re-based for dividends too; `Ticker.dividends` exists but **never called** | Ticker symbol resolution is whatever yfinance's own symbol table maps **today** — an old ticker (e.g. pre-rename) may return empty/404 | N/A — no merger metadata fetched | N/A — no spinoff metadata fetched | N/A |
| **pykrx** | **UNVERIFIED** (CA-06) — `loader.py:107-125` passes no adjustment parameter to `krx.get_market_ohlcv_by_date()` and no comment documents the behavior | UNVERIFIED | UNVERIFIED | N/A — KR 6-digit codes occasionally change after a merger; no detection | N/A | N/A | N/A |
| **기타 (other)** | none present | none | none | none | none | none | none |

**Key takeaway:** yfinance is the **only** source with *any* latent
corporate-action metadata capability in its API surface
(`actions=True`/`Ticker.splits`/`Ticker.dividends`), and it is unused. This
is the cheapest possible future event-source integration (§6.4, §11) — but
covers US symbols only, leaving KR (pykrx, CA-06) and all live-broker-side
events (KIS, §3 row 1) without any event source at all.

---

## 4. Failure Scenarios (CA-01 .. CA-13)

### CA-01 — CRITICAL: Stale internal `avg_price`/`qty` causes kill-switch false-fire after a split

**Files:** `backend/execution/position_tracker.py:99-102`,
`backend/brokers/models.py` (`Position.unrealized_pnl`/`unrealized_pnl_pct`),
`backend/quant/risk/engine.py:233-274` (`PersistentLossTracker.record_pnl`/`_evaluate`)

**Trigger:** A held position (e.g. 100 shares @ avg_price=$50) undergoes a
2:1 stock split.

**Propagation path:**
```
Broker (Copy 0):       qty 100→200, avg_price $50→$25   (auto-adjusted by exchange)
PositionTracker (Copy 1): qty=100, avg_price=$50         (unchanged — no Fill occurred)
Live price (post-split): ~$25

unrealized_pnl = (current_price - avg_price) * qty
               = (25 - 50) * 100
               = -$2,500   ← using Copy 1's stale avg_price/qty

Correct value (Copy 0 basis):
               = (25 - 25) * 200 = $0
```

If this `-$2,500` flows into `PersistentLossTracker.record_pnl(pnl, equity)`
→ `_evaluate()` (`risk/engine.py:248-274`), it is compared against
`daily_loss_limit_pct` (3%) / `weekly_loss_limit_pct` (6%) / `mdd_limit_pct`
(15%) of `peak_equity`. For a ₩2,000,000 account, a single mispriced
position can trivially exceed all three thresholds.

**Result:** `kill_switch = True`, `SAFE_MODE.disable(...)` fires
(`_fire_kill_switch_alert`, `risk/engine.py:276+`), and the system halts all
new orders — for a corporate action that caused **zero real economic loss**.

**Severity:** CRITICAL — false kill-switch activation requires manual
operator intervention (per `docs/RECONCILIATION_ENGINE.md` §8.3,
`EmergencyStop` "does not automatically re-enable trading").

---

### CA-02 — CRITICAL: `TrailingStopManager`/`PositionStop` false-triggers a stop-loss after a split

**Files:** `backend/quant/risk/engine.py:69-79` (`PositionStop.update_peak`/
`is_stopped`), `backend/quant/live/pipeline.py` (trailing-stop check call
site)

**Trigger:** Entry at $145 (pre-split). `PositionStop.open()` sets
`entry_price=145`, `peak_price=145`,
`trailing_stop = 145 * (1 - 0.07) = 134.85`, `hard_stop = 145 * (1 - 0.10) =
130.5`. A 4:1 split then occurs.

**Propagation path:**
```
Pre-split:  current_price=145 → peak_price=145, trailing_stop=134.85, hard_stop=130.5

Post-split (4:1): broker live price ≈ 36.25 (= 145/4, real market price, no real loss)

is_stopped(36.25):
    36.25 <= hard_stop (130.5)      → True, "hard_stop"
```

**Result:** `check_stops()` (`risk/engine.py:120-131`) reports
`(symbol, "hard_stop")`. The live pipeline (`pipeline.py`) executes a
real **sell** order for a position whose underlying value is unchanged —
an unintended liquidation, likely realizing a large *apparent* loss that
feeds back into CA-01's `record_pnl()`.

**Why this is a *live-only* risk (distinct from backtest, §2.4/CA-13):**
`PositionStop.entry_price`/`peak_price` are captured from **real, live
prices at entry time** and never re-derived from a re-fetched series — there
is no `auto_adjust=True` "re-basing" safety net for this in-memory state at
all.

**Severity:** CRITICAL — causes an actual order placement and realized loss,
compounding into CA-01.

---

### CA-03 — HIGH: Reconciler auto-repairs split-shaped `qty_mismatch` unconditionally — design-doc vs. implementation gap

**Files:** `backend/execution/reconciler.py:96` (`_QTY_TOLERANCE = 1`),
`:206-231` (`qty_mismatch` gap+repair), `docs/RECONCILIATION_ENGINE.md:168-171`
(§6 severity table), `:191-197` (§8.1 auto-repair preconditions)

**Trigger:** Same 2:1 split as CA-01. On the next reconciliation cycle
(periodic, every 30 minutes per `reconciler.py`'s own docstring), the
reconciler compares DB `Position` (Copy 2: qty=100, avg=$50) against broker
(Copy 0: qty=200, avg=$25).

**Propagation path:**
```python
# backend/execution/reconciler.py:206-231 (as implemented)
qty_diff = abs(dp["qty"] - bp.qty)        # = abs(100 - 200) = 100
price_changed = abs(dp["avg_price"] - bp.avg_price) > 0.01   # True

if qty_diff > self._QTY_TOLERANCE:        # 100 > 1 → True
    if self._has_pending_order(sym, db):
        result.gap("qty_mismatch_pending", sym, ...)
    else:
        result.gap("qty_mismatch", sym, f"DB qty={dp['qty']} vs 브로커 qty={bp.qty}")
        if not dry_run:
            row.qty = bp.qty            # 100 → 200
            row.avg_price = bp.avg_price  # 50 → 25
            result.repaired("fix_qty", sym, f"DB qty {dp['qty']}→{bp.qty}")
```

**Design-doc-vs-implementation gap:** `docs/RECONCILIATION_ENGINE.md`
describes a richer model:

- §6 (line 170): *"qty diff > 5 shares OR qty diff > 5% of position →
  CRITICAL"*
- §8.1 (lines 191-197): auto-repair is **prohibited** when severity is
  CRITICAL/HIGH, **or** "the position change represents > 5% of total
  portfolio equity"
- §8.3 (lines 402-417): `EmergencyStop` fires on "CRITICAL gap AND broker
  snapshot is confirmed fresh"

**None of this severity/threshold logic exists in `reconciler.py`** —
confirmed by grep (§12): zero matches for
`severity|CRITICAL|0.05|large_position|_classify` in
`backend/execution/reconciler.py`. The actual code performs an
**unconditional** `fix_qty`/`fix_avg_price` whenever `qty_diff > 1` and no
pending order exists, regardless of how large the change is.

**Result:** A 2:1 split (100% qty change — by the design doc's own
threshold, deeply CRITICAL) is **silently repaired** with a single generic
log line `"DB qty 100→200"`. There is no severity escalation, no
`EmergencyStop`, and (per CA-04) no record of *why* the qty doubled.

**Severity:** HIGH — the silent repair itself is "correct" in outcome (DB now
matches broker), but the **absence of any signal** that a corporate action
occurred means CA-01/CA-02 may have already fired *before* this repair runs
(reconciliation is periodic, not instantaneous), and no one is alerted to
investigate.

---

### CA-04 — HIGH: No `adjustment_factor`/`corporate_action_type`/`ex_date` field anywhere in the schema

**Files:** `backend/database/models.py:13-100` (`Trade`, `Order`, `Fill`,
`StrategyRun`, `EquitySnapshot`, `Position`, `DailyRiskState`, `Command`)

**Finding:** None of the eight tables in `database/models.py` contain any
field for:

- `adjustment_factor` / `split_ratio`
- `corporate_action_type` (split / reverse_split / dividend / ticker_change /
  merger / spinoff)
- `ex_date` / corporate-action effective date
- A historical snapshot of `qty`/`avg_price` *before* an automatic
  reconciler repair

`Position` (lines 78-89) has only `symbol, qty, avg_price, market, broker,
updated_at`. After CA-03's repair overwrites `qty`/`avg_price`, the
**previous** values are gone — there is no audit row capturing "qty was 100
@ $50 before this repair, now 200 @ $25, repaired at <timestamp>" *with a
corporate-action label*. (The reconciler does call `_audit_position_change`
with a generic `reconcile_fix_qty` event — but this records the mechanical
fact of the repair, not *why* it happened.)

**Result:** Forensic analysis after the fact cannot distinguish "this was a
2:1 split" from "this was a tracking bug that got corrected" — both produce
an identical `reconcile_fix_qty` audit entry with `db_qty`/`broker_qty`.

**Severity:** HIGH — blocks any future automated or manual root-cause
analysis of position-history anomalies.

---

### CA-05 — HIGH: `TrailingStopManager`/`PositionStop` state (Copy 3) is never re-adjusted, even if Copy 1/Copy 2 are fixed

**Files:** `backend/quant/risk/engine.py:58-95` (`PositionStop`,
`TrailingStopManager.__init__`), `backend/execution/reconciler.py` (full file)

**Finding:** Even in the best case — CA-03's reconciler repair runs
*before* CA-02's false stop fires, correctly updating DB `Position.qty`/
`avg_price` (Copy 2) to the post-split basis — **`TrailingStopManager.
_positions[symbol]`** (`risk/engine.py:95`, Copy 3) is a separate in-memory
dict that the reconciler **never touches**. Confirmed by grep (§12): zero
references to `TrailingStopManager` or `PositionStop` in
`reconciler.py`.

**Propagation path:** `PositionStop.entry_price=145`, `peak_price=145`,
`trailing_stop=134.85`, `hard_stop=130.5` remain in the **pre-split** price
basis indefinitely. The next `update()` call
(`risk/engine.py:113-118`) with a post-split `price_map` (~$36) will see
`36 < 134.85` and immediately mark `is_stopped=True` on the *very next
cycle* — CA-02 is not a one-time race, it is a **standing false-positive**
until the process restarts (re-running `restore_positions` does not
re-`open()` `TrailingStopManager` entries either — confirmed no such call
exists).

**Severity:** HIGH (independently of CA-02 — this is the *root cause* that
makes CA-02 persistent rather than transient).

---

### CA-06 — HIGH: pykrx KR historical-data adjustment status is UNVERIFIED

**Files:** `backend/quant/data/loader.py:107-125`

```python
def _fetch_kr_pykrx(self, symbol, start, end, period) -> pd.DataFrame:
    from pykrx import stock as krx
    # ... date formatting ...
    df = krx.get_market_ohlcv_by_date(s, e, symbol)
    if df.empty:
        raise ValueError(f"PyKRX returned no data for {symbol}")
    df = df.rename(columns={"시가": "Open", "고가": "High", "저가": "Low",
                             "종가": "Close", "거래량": "Volume"})
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
```

**Finding:** No parameter or comment indicates whether
`krx.get_market_ohlcv_by_date()` returns split-adjusted or raw historical
prices for KR symbols. This is an **open question** that this audit
deliberately does not resolve by speculation — it requires checking pykrx's
upstream source/changelog or empirically testing against a known KR split
date.

**Why this matters more for KR than US:** If pykrx returns **raw**
(unadjusted) prices, then unlike the US case (§2.4 — internally consistent
within one `auto_adjust=True` fetch), a **single KR fetch spanning a split
date would contain a genuine embedded discontinuity**. SMA/momentum/regime
calculations (`backend/quant/indicators/{trend,momentum}.py`,
`backend/quant/signals/regime.py`) computed over such a `df` would see a
real jump *within one backtest run* — a KR-specific variant of what CA-13
shows does **not** happen for US.

**Fallback path:** If pykrx fails, `loader.py` falls back to yfinance with a
`.KS` suffix (`auto_adjust=True`, §2.1) — meaning the *fallback* path is
adjusted even if the *primary* KR path is not, a potential **mismatch
between primary and fallback** for the same KR symbol.

**Severity:** HIGH — affects every KR symbol in `KR_ETF`
(`backend/quant/data/universe.py:10`) and is currently unverified in either
direction.

---

### CA-07 — MEDIUM: Cross-time adjustment-basis drift for US symbols (re-basing on each fetch)

**Files:** `backend/quant/data/loader.py:74-76`, `backend/quant/risk/engine.py:58-79`

**Trigger:** A position is opened (entry_price recorded at $145, "basis B1" —
before any *new* split). Some time later, a *new* split occurs (e.g. a
*second* split on the same symbol). The next `loader.py` fetch with
`auto_adjust=True` re-bases the **entire** series to "basis B2" (which now
also accounts for the second split).

**Propagation path:** `PositionStop.entry_price` (Copy 3, $145, basis B1) was
never re-fetched and is **not** automatically re-based to B2. Any
calculation that mixes a *previously-stored* derived value (entry_price,
avg_price, peak_price — all captured under B1) with a *newly-fetched* series
(under B2) introduces a silent discrepancy proportional to the *second*
split's ratio.

**Distinction from CA-01:** CA-01 is "internal tracker (Copy 1) vs. broker
(Copy 0)" — a single-event divergence triggered by *one* split. CA-07 is
"previously-computed/stored values vs. a newly-fetched yfinance series" — a
*compounding* divergence that grows with **every** subsequent split on a
held/tracked symbol, independent of whether the broker-side reconciliation
(CA-03) has run.

**Severity:** MEDIUM — smaller per-event magnitude than CA-01/CA-02 for a
single split, but compounds, and is **not addressed by fixing CA-01/CA-03/
CA-05 alone** since those operate on broker-vs-tracker state, not on
re-fetched-series-vs-stored-state.

---

### CA-08 — HIGH: Ticker change / merger orphans positions

**Files:** `backend/execution/position_tracker.py:41-43` (`get_position`),
`backend/strategy/indicator/strategy.py` (`on_bar`/`_check_exit`),
`backend/execution/reconciler.py:248-274` (`stale_db_position`)

**Trigger:** A held position's symbol is renamed at the broker (e.g.
`"FB"` → `"META"`-style rename, or a KR 6-digit code reissued after a
merger). The broker's `get_positions()` now reports the **new** symbol; the
DB `Position` row and `PositionTracker._positions` key remain the **old**
symbol.

**Propagation path:**
```
PositionTracker.get_position("META")  → None   (still keyed under "FB")
  → on_bar()'s `_check_exit` for "META" never runs — NO stop-loss management
    for the renamed position (unmanaged risk)

Reconciler (next cycle):
  "FB" in DB, not in broker.get_positions()
    → reconciler.py:248-274: stale_position_pending (if pending order)
                              stale_position_too_young (if age < threshold)
                              stale_db_position → auto-delete (if old enough)
  "META" in broker, not in DB
    → reconciler.py:187-205: missing_in_db → insert_position (new DB row,
       qty/avg_price = broker-reported, but with NO acquisition history,
       NO Fill, NO Trade — see CA-09 for the spinoff-shaped variant)
```

**Result:** The renamed position is **unmanaged** (no stop-loss/trailing-stop
applied — `TrailingStopManager` has no entry for "META" either) for at least
one reconciliation cycle, and potentially indefinitely if
`_STALE_MIN_AGE_HOURS` and the `missing_in_db`/`stale_db_position` paths
don't both fire in the same cycle. The old "FB" row's deletion produces a
phantom "position closed" with **no matching `Fill`/`Trade`** — breaking any
PnL attribution that assumes `Fill` records explain all `Position` changes.

**Severity:** HIGH — a real, currently-held position becomes invisible to
risk management.

---

### CA-09 — MEDIUM: Spinoff creates an unmanaged, contextless broker position

**Files:** `backend/execution/reconciler.py:187-205` (`missing_in_db` →
`insert_position`)

**Trigger:** A spinoff creates a **new** symbol in the broker's position list
(shares of the spun-off entity), with **no** corresponding DB row, no `Fill`,
no `Trade`, and a broker-reported cost basis that may be $0 or an
exchange-allocated basis unrelated to the parent position's history.

**Propagation path:**
```python
# backend/execution/reconciler.py:187-205
if dp is None:   # broker has it, DB doesn't
    result.gap("missing_in_db", sym, f"브로커 qty={bp.qty} avg={bp.avg_price:.2f} — DB 없음")
    if not dry_run:
        new_row = DBPosition(symbol=sym, qty=bp.qty, avg_price=bp.avg_price,
                              market=bp.market, broker=self._broker_name)
        db.add(new_row)
        result.repaired("insert_position", sym, f"DB에 포지션 추가: qty={bp.qty}")
```

**Result:** The spun-off position is inserted into the DB **with no link to
the parent position it came from** — `PositionTracker` only learns of it on
the next `restore_positions()` (worker restart) or live reconcile, not
immediately. `TrailingStopManager.open()` is never called for it, so it has
**no stop-loss/trailing-stop** from the moment it appears until some future
code path explicitly opens one (none currently exists).

**Severity:** MEDIUM — less acute than CA-08 (no *existing* risk management
is broken, since none existed for this symbol before), but the position is
silently un-risk-managed from creation.

---

### CA-10 — MEDIUM: Dividend cash drift is unattributed (acknowledged, observability-only)

**Files:** `docs/RECONCILIATION_ENGINE.md:280-305` (§6 Portfolio drift
thresholds and rationale)

**Finding:** `RECONCILIATION_ENGINE.md` already documents:

> §6.1 thresholds: Cash KRW drift > ₩1,000 → `PORTFOLIO_CASH_DRIFT`; Cash USD
> drift > $1.00 → `PORTFOLIO_CASH_USD_DRIFT`.
>
> §6.2 (lines 302-305): *"Portfolio drift can reflect legitimate in-flight
> settlement (T+2 cash), **dividend payments**, FX movements, or fee
> deductions not captured in fill records. Auto-correcting equity or cash can
> mask real losses and invalidate the kill-switch evaluation."*

**Result:** A dividend payment appears as an unattributed `cash_krw`/
`cash_usd` increase, flagged as a generic `PORTFOLIO_CASH_DRIFT`/
`PORTFOLIO_CASH_USD_DRIFT` gap with **no indication that it was a dividend**
specifically (vs. a fee, FX movement, or settlement timing artifact). This is
explicitly "by design" per the doc's own rationale (don't auto-correct cash,
since that could mask a real loss) — but it means dividend income is
permanently lumped into an unexplained-drift bucket rather than being
recognized as portfolio income.

**Severity:** MEDIUM — no safety risk (the existing design correctly refuses
to auto-correct cash), but a permanent gap in PnL attribution/reporting
accuracy.

---

### CA-11 — LOW/MEDIUM: Hardcoded universe has no delisting/rename detection

**Files:** `backend/quant/data/universe.py:1-19`,
`backend/strategy/indicator/strategy.py` (`_scan_and_trade` exception
handling)

```python
# backend/quant/data/universe.py:1-19
"""
매매 유니버스 상수 — 공통 import 지점.
이 파일이 KR_ETF, EXCD_MAP의 canonical source.
strategy/signals.py 와 backend/brokers/kis.py 모두 여기서 import.
"""
US_ETF = ["SPY", "QQQ", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE"]
US_LARGE = ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "JPM", "V"]
KR_ETF = ["069500", "360750", "091160"]  # KODEX200, TIGER S&P500, KODEX반도체

UNIVERSE = US_ETF + US_LARGE + KR_ETF

EXCD_MAP: dict[str, str] = {s: "NASD" for s in [...]}
EXCD_MAP.update({s: "NYSE" for s in [...]})
```

**Finding:** This is a **static, hardcoded** list, confirmed as the
"canonical source" by its own docstring, imported by both
`strategy/signals.py` and `backend/brokers/kis.py`. There is no mechanism to:

1. Detect a delisted symbol (e.g. a KR ETF code discontinued after a
   merger of the underlying index).
2. Detect a renamed symbol (e.g. the FB→META-style case from CA-08, applied
   to the *universe definition itself*, not just an open position).
3. Retire or replace a ticker automatically.

**Propagation path:** A delisted/renamed symbol causes `loader.fetch()` to
raise (`ValueError`, e.g. `"PyKRX returned no data for {symbol}"` from
`loader.py:117`, or an empty yfinance frame). This is caught by the existing
`except Exception: ... continue` in `_scan_and_trade`
(`backend/strategy/indicator/strategy.py`), which silently skips the symbol
**forever**, with the only trace being a per-cycle warning log line.

**Severity:** LOW/MEDIUM — no immediate financial risk (the symbol is simply
never scanned), but represents permanent silent degradation of the trading
universe with zero operator-facing alert distinguishing "transient fetch
error" from "this symbol is permanently gone."

---

### CA-12 — LOW: No corporate-action event source exists anywhere — "UNKNOWN" has nothing to classify from

**Files:** All of §3 (Event Support Matrix)

**Finding:** Every cell in §3's matrix for "event detection" is either
`N/A`, `Implicit (no flag)`, `UNVERIFIED`, or `Not integrated`. There is
**no** source — KIS, Kiwoom, OpenBB, yfinance (unused `actions=True`), or
pykrx (CA-06) — that currently surfaces an explicit corporate-action
**event** (type + date + ratio/amount).

**Implication for "unknown event" classification:** Any future detector must
infer "a corporate action of *some* type occurred" purely from
**discontinuities** in price (a large single-bar return) and/or quantity (a
broker-vs-tracker qty ratio that resembles a split/reverse-split factor, per
§6.4). This is the *same signal shape* that a future 3-2B price-spike
validator would use to flag bad data (§1's orthogonality note) — meaning a
3-4B detector and a 3-2B validator would need to **agree on disambiguation**
(e.g., "is a -75% single-bar move a 4:1 split or a fat-finger bad print?")
or risk one suppressing the other's correct signal.

**Severity:** LOW (no direct financial-loss path on its own), but it is the
structural reason CA-01..CA-09 cannot be *proactively* detected — only
*reactively* discovered after the fact via reconciliation (CA-03) or a
triggered stop (CA-02).

---

### CA-13 — MEDIUM: Backtest internal consistency masks live-only risk (false confidence)

**Files:** `backend/strategy/indicator/backtest.py:16-20`,
`backend/strategy/runtime/simulator.py`

**Finding:** As established in §2.4, a single `auto_adjust=True` backtest
fetch is internally consistent — **CA-01 and CA-02 cannot occur within a
backtest run**, because both depend on a *previously-stored* value (Copy 1's
`avg_price`, Copy 3's `peak_price`/`trailing_stop`) diverging from a
*newly-fetched or broker-reported* value under a *different* adjustment
basis. A backtest fetches once, under one basis, for the whole run.

**Result — false confidence:** A strategy that backtests cleanly through a
historical period containing real splits (e.g. AAPL's 2020 4:1 split) gives
**zero signal** about how that same strategy would behave **live** when that
split actually happens in real time — because the backtest's data pipeline
structurally cannot reproduce the cross-time divergence (CA-01/CA-02/CA-07)
that only arises when state captured *before* a split is compared against
data/prices/positions observed *after* it.

**Severity:** MEDIUM — not a direct loss path, but undermines the value of
backtesting as a pre-deployment safety check specifically for
corporate-action scenarios, and could lead operators to believe (incorrectly)
that "the strategy has been tested against splits" when it has not.

---

## 5. Source-Specific Health Flow

### 5.1 KIS

- **Live price** (`kis_adapter/market_data.py`): `get_price_us(symbol, excd)`
  → `float`, `get_price_kr(symbol)` → `int`, `get_pending_us(account_no)` →
  pending orders. **No** historical OHLCV, **no** corporate-action calendar,
  **no** dividend/split endpoint of any kind.
- **Portfolio** (`kis_adapter/portfolio.py` → `backend/brokers/kis.py:71-104`
  `get_positions()`): returns broker-reported `qty`/`avg_price`/
  `current_price` per position. This **is** the ground truth post-action
  (Copy 0, §2.3) — the broker/exchange has already applied any real
  split/dividend/merger/spinoff adjustment by the time this is called. But
  it provides **no explanation** of *why* a value changed since the last
  call — a corporate-action-driven change and a manual-trade-driven change
  are indistinguishable from this API alone.
- **Health verdict:** KIS is the most *authoritative* source (it reflects
  reality) but the least *informative* (zero event metadata). Any
  corporate-action detector relying on KIS alone must work by **diffing**
  successive `get_positions()` snapshots and **inferring** event type from
  the qty/avg_price ratio (§6.4).

### 5.2 Kiwoom

Confirmed stub (`kiwoom_adapter/`, per `CLAUDE.md`: "키움증권: 아직 스텁
없음"). No live data path of any kind exists. **N/A for all 7 event types** —
not because Kiwoom inherently lacks corporate-action support, but because no
Kiwoom integration exists at all yet.

### 5.3 OpenBB — Not Applicable / Red Herring

Re-confirming the 3-3A finding: grep for `openbb`/`OpenBB` across `backend/`,
`api/`, `strategy/` returns only the comment `# Workaround for the
pandas-ta-openbb fork (imported as pandas_ta)` in `backend/__init__.py` (and
identical comments in `api/__init__.py`, `strategy/__init__.py`). This refers
to a **technical-indicator library** fork (the `pandas-ta-openbb` PyPI
package, a maintained fork of `pandas-ta`), **not** the OpenBB Platform SDK
or any OpenBB data-provider integration. **No OpenBB corporate-action data of
any kind is available to this codebase.**

### 5.4 yfinance / pykrx

- **yfinance** — `auto_adjust=True` confirmed at all 6 sites (§2.1). The
  yfinance Python API additionally exposes `Ticker(symbol).splits` (a pandas
  Series of historical split ratios + dates) and `Ticker(symbol).dividends`
  (a pandas Series of dividend amounts + ex-dates), reachable via
  `history(..., actions=True)` as well. **None of these are called anywhere**
  in this codebase (confirmed via grep, §12). This is the single highest-
  leverage, lowest-effort future event source — but **US-symbols-only**.
- **pykrx** — adjustment behavior for `krx.get_market_ohlcv_by_date()`
  **UNVERIFIED** (CA-06). pykrx also exposes corporate-action-adjacent
  functions in its broader API (e.g. market-cap/shares-outstanding endpoints)
  that could theoretically help detect a split via a shares-outstanding
  step-change, but **none are used** here, and confirming pykrx's adjustment
  default is a prerequisite before designing around it.

---

## 6. Insertion Points — Where Corporate Actions Should Be Applied

### 6.1 Price-Adjustment Layer

**Already owned** (for US) by yfinance's `auto_adjust=True` at the loader
boundary (`backend/quant/data/loader.py:74-76` and the 5 other sites, §2.1).
No insertion needed for US historical price adjustment itself.

For KR, **pending CA-06's resolution**: if pykrx returns raw prices,
`loader.py:107-125` (`_fetch_kr_pykrx`) would need a new adjustment step —
applying split/dividend ratios sourced from *somewhere* (no current source
exists per §3 — this would itself require a new KR corporate-action feed,
which is out of scope to specify further here).

Live current price (`kis_adapter/market_data.py`) needs **no** adjustment —
the broker's real-time price is inherently post-action.

### 6.2 Position-Adjustment Layer

**`PositionTracker`** (`backend/execution/position_tracker.py`) is the
natural owner — it already owns Copy 1 (`_positions[symbol]`) and is the
in-memory source consulted by `on_bar`/`_check_exit`. It currently has
**zero** corporate-action logic (§2.3, Copy 1).

Needed:
- **(a) Detection** — either (i) reconciler-flagged qty-ratio-shaped
  mismatches (a `qty_diff` whose ratio to the prior qty is close to a small
  integer or its reciprocal — distinguishable from an arbitrary
  tracking-bug `qty_diff`, see §6.4), or (ii) an external split/dividend feed
  for held US symbols via `yf.Ticker(symbol).splits`/`.dividends` (§5.4),
  checked periodically for symbols with open positions.
- **(b) Application** — given a detected ratio `R` (e.g. `R=2` for a 2:1
  split), apply `qty *= R`, `avg_price /= R` to **both** Copy 1
  (`PositionTracker._positions[symbol]`) and Copy 2 (DB `Position` row) in
  the same operation, so they never observably diverge from each other (even
  if both temporarily diverge from Copy 0 until the broker's own update is
  observed/confirmed).

### 6.3 Risk-State Adjustment Layer

**`TrailingStopManager`/`PositionStop`** (`backend/quant/risk/engine.py:58-
135`) is a **separate** insertion point from 6.2 — this is the core finding
of CA-05. Whenever 6.2's detection fires a ratio `R` for `symbol`, the
corresponding `PositionStop` (if one exists in
`TrailingStopManager._positions[symbol]`) needs `entry_price`, `peak_price`,
`trailing_stop`, and `hard_stop` **all divided by `R`** (prices scale
inversely to a split ratio applied to quantity) — *in the same atomic step*
as 6.2's adjustment, otherwise CA-02 can still fire in the gap between the
two updates.

### 6.4 Reconciler — Detection + Audit Trail

`backend/execution/reconciler.py:206-231` is the natural **detection** site:
it already computes `qty_diff = abs(dp["qty"] - bp.qty)` and
`bp.qty`/`dp["qty"]` are both available, so the ratio
`bp.qty / dp["qty"]` (or its reciprocal) can be tested against a small set of
plausible split/reverse-split ratios (2, 3, 4, 5, 10, 0.5, 0.2, 0.25, 0.1,
...) with a small tolerance. A ratio match is a **split signature**,
distinguishable from an arbitrary tracking-bug `qty_diff` that is unlikely to
land near such a ratio.

Needed:
- **(a)** New audit fields — `adjustment_factor`, `corporate_action_type`,
  `ex_date` (CA-04) — populated when a split-signature match is found, so the
  existing `_audit_position_change("reconcile_fix_qty", ...)` call
  (`reconciler.py:219-224`) carries this context instead of (or alongside)
  the generic `db_qty`/`broker_qty` payload.
- **(b)** A severity bypass/annotation: per the *design doc's intended*
  model (§6/§8.1 of `RECONCILIATION_ENGINE.md`), a 100%-of-position
  `qty_diff` would be CRITICAL and block auto-repair / trigger
  `EmergencyStop`. A split-signature match should be **annotated** (not
  silently passed CA-03-style, but also not treated as an unexplained
  CRITICAL gap warranting `EmergencyStop`) — this is the disambiguation
  point between "real anomaly" and "known corporate action."

### 6.5 Universe / Symbol-Mapping Layer

**No current owner.** A new component is needed for CA-08 (ticker
change/merger), CA-09 (spinoff), and CA-11 (delisting): an
old-symbol→new-symbol translation table, consulted by:
- `backend/quant/data/universe.py`'s static lists (so the universe
  definition itself can be updated/retired),
- Open `Position.symbol` rows (so CA-08's orphaning doesn't occur — a rename
  is *translated*, not silently dropped-and-recreated),
- Historical `Order`/`Trade`/`Fill` rows (for consistent historical reporting
  under the symbol's current name, or at minimum a documented mapping for
  reporting tools).

### 6.6 Kill-Switch / EmergencyStop Gating

`backend/quant/risk/engine.py:233-274` (`PersistentLossTracker._evaluate`)
and the reconciler's `EmergencyStop` path
(`docs/RECONCILIATION_ENGINE.md` §8.3 — **currently unimplemented** per
CA-03) both need a **"corporate-action-pending"** suppression or annotation
hook: if 6.4 has just detected (or is in the process of confirming) a
split-signature match for a symbol, `_evaluate()`'s daily/weekly/MDD
calculations for that cycle should either (i) exclude the affected symbol's
contribution to `pnl`/`current_equity` until 6.2/6.3's adjustment completes,
or (ii) at minimum tag the resulting `kill_reason` with the pending
corporate-action context so an operator isn't debugging a "mystery" loss.

---

## 7. Ownership Boundaries

| Concern | Current Owner | Gap |
|---|---|---|
| Historical price adjustment (US) | yfinance `auto_adjust=True` (`loader.py:74-76` + 5 other sites) | No event metadata (`actions=True` unused); cross-time re-basing untracked (CA-07) |
| Historical price adjustment (KR) | Unknown — pykrx (`loader.py:107-125`) | **Unverified** whether adjusted (CA-06) |
| Live current price | KIS broker — inherently correct, real-time | N/A — no adjustment concept applies |
| Position qty/avg_price (broker-side, Copy 0) | KIS broker (`kis.py:71-104`, auto-adjusted by exchange) | N/A — source of truth, but zero event metadata (§5.1) |
| Position qty/avg_price (internal, Copy 1) | `PositionTracker` (`position_tracker.py:99-102`, fill-driven only) | **No corporate-action adjustment logic at all** (CA-01) |
| Position qty/avg_price (DB, Copy 2) | `database/models.py::Position`, synced only via reconciler | No CA fields (CA-04); synced to Copy 0 but not Copy 1 directly |
| Mismatch detection (Copy 1/2 vs Copy 0) | `reconciler.py:206-231` | Detects `qty_mismatch` but applies no severity/ratio-signature distinction (CA-03) |
| Risk-state (peak/trailing/hard stop, Copy 3) | `TrailingStopManager`/`PositionStop` (`risk/engine.py:58-135`) | Fully independent in-memory state; **never** touched by reconciler (CA-05/CA-02) |
| Symbol/ticker mapping | **None** | No owner — new component needed (CA-08/CA-09/CA-11) |
| Corporate-action event detection/feed | **None** | No source provides this (§3); yfinance `actions=True`/`.splits`/`.dividends` is the cheapest unused option (§5.4) |
| Dividend cash accounting | **None** (acknowledged drift, `RECONCILIATION_ENGINE.md:302-305`) | Observability-only by design (CA-10) — correct for safety, incomplete for attribution |
| Kill-switch corporate-action awareness | **None** | `_evaluate()` (`risk/engine.py:248-274`) has no way to distinguish a CA-driven PnL swing from a real one (CA-01/CA-06) |

---

## 8. Affected Modules

| File | Role | Corporate-Action Relevance |
|---|---|---|
| `backend/quant/data/loader.py` | Primary OHLCV fetch (yfinance US, pykrx KR) | §2.1 — `auto_adjust=True` (US, owns price adjustment); pykrx adjustment unverified (CA-06) |
| `backend/quant/live/safeguards.py` | `OHLCVRecovery` 4-tier fallback | Tier-2 yfinance fetch (`:214`) consistent `auto_adjust=True`; cache (tier 4) could mix bases across a split boundary if stale (CA-07-adjacent) |
| `backend/strategy/indicator/backtest.py` | Backtest OHLCV fetch | Single `auto_adjust=True` fetch — internally consistent (§2.4, CA-13) |
| `backend/strategy/runtime/simulator.py` | `SimulatedBroker` — backtest position tracking | Operates on the same internally-consistent `df` as backtest.py — no CA divergence within a run (CA-13) |
| `backend/quant/signals/fusion.py`, `strategy/signals.py` | SMA / 12-1 momentum / RSI / regime signal computation | Computed over `auto_adjust=True` series — consistent within a fetch (§2.4); KR variant unverified (CA-06) |
| `backend/quant/indicators/{trend,momentum}.py` | SMA cross, 12-1 momentum factor | Cited in §2.4/CA-13 framing — would show real discontinuities only if fed raw (unadjusted) data |
| `backend/quant/risk/engine.py` | `TrailingStopManager`/`PositionStop` (Copy 3), `PersistentLossTracker` kill-switch | CA-01 (`_evaluate`), CA-02/CA-05 (`PositionStop` fields never adjusted) |
| `backend/execution/position_tracker.py` | `PositionTracker` — Copy 1, fill-driven `avg_price`/`qty` | CA-01 root cause — `on_fill` (`:99-102`) has no CA logic |
| `backend/execution/reconciler.py` | Reconciliation engine — Copy 1/2 vs Copy 0 | CA-03/CA-04 (no severity/audit-trail for split-shaped `qty_mismatch`), CA-08/CA-09 (`stale_db_position`/`missing_in_db` paths for renames/spinoffs) |
| `backend/brokers/kis.py`, `kis_adapter/{market_data,portfolio}.py` | KIS broker adapter — Copy 0, live price | §5.1 — ground truth, zero event metadata |
| `backend/database/models.py` | ORM schema — `Position`/`Trade`/`Order`/`Fill`/`EquitySnapshot`/`DailyRiskState` | CA-04 — no adjustment/event fields in any of the 8 tables |
| `backend/quant/data/universe.py` | Canonical hardcoded universe (`US_ETF`/`US_LARGE`/`KR_ETF`/`EXCD_MAP`) | CA-11 — no delisting/rename detection |
| `backend/quant/live/pipeline.py` | End-to-end live pipeline (loader→fusion→risk→execution) | Carries CA-01/CA-02's divergent values through every stage |
| `docs/RECONCILIATION_ENGINE.md` | Reconciliation design doc | CA-03 (severity model not implemented), CA-10 (dividend acknowledgment) |

---

## 9. Risk Classification

| ID | Severity | Category | Description |
|---|---|---|---|
| CA-01 | CRITICAL | Position / Execution | Stale internal avg_price/qty post-split → spurious PnL → kill-switch false-fire |
| CA-02 | CRITICAL | Position / Execution | `TrailingStopManager` false-triggers stop-loss post-split → real unintended liquidation |
| CA-03 | HIGH | Execution / Reconciliation | Reconciler auto-repairs split-shaped `qty_mismatch` unconditionally — design-doc severity model not implemented |
| CA-04 | HIGH | Execution / Schema | No `adjustment_factor`/`corporate_action_type`/`ex_date` field anywhere — no forensic trail |
| CA-05 | HIGH | Position / Execution | `TrailingStopManager`/`PositionStop` state never re-adjusted — root cause of CA-02's persistence |
| CA-06 | HIGH | Market Data Ingestion | pykrx KR adjustment status UNVERIFIED — open question |
| CA-07 | MEDIUM | Price History | Cross-time adjustment-basis drift for US — re-basing on each fetch vs. stored state |
| CA-08 | HIGH | Position / Portfolio | Ticker change/merger orphans positions — unmanaged risk for renamed position |
| CA-09 | MEDIUM | Position / Portfolio | Spinoff creates unmanaged, contextless broker position |
| CA-10 | MEDIUM | Portfolio Valuation | Dividend cash drift unattributed (acknowledged, observability-only) |
| CA-11 | LOW/MEDIUM | Market Data Ingestion | Hardcoded universe has no delisting/rename detection |
| CA-12 | LOW | Market Data Ingestion | No corporate-action event source anywhere — "UNKNOWN" has nothing to classify from |
| CA-13 | MEDIUM | Backtest | Backtest internal consistency masks live-only risk — false confidence |

### Most Dangerous Pattern

**CA-01 → CA-02 → CA-03 → CA-04**, in sequence:

1. A real stock split occurs on a held position.
2. `PositionTracker` (Copy 1) goes stale; `_evaluate()`'s daily/weekly/MDD
   PnL calculation may **false-fire the kill-switch** (CA-01) — possibly
   *before* the next reconciliation cycle even runs.
3. Independently, `TrailingStopManager`/`PositionStop` (Copy 3) — which the
   reconciler never touches (CA-05) — sees the post-split live price fall
   below its stale `trailing_stop`/`hard_stop` and **executes a real sell
   order** (CA-02), realizing an apparent loss that feeds back into step 2.
4. When the reconciler eventually runs, it **silently "fixes"** the resulting
   DB/tracker `qty`/`avg_price` mismatch (CA-03) — with **no audit trail**
   explaining that any of this was caused by a corporate action (CA-04).

**End state:** the position has been incorrectly liquidated, the system may
be sitting in `SAFE_MODE` from a false kill-switch, and the only audit
evidence is a generic `reconcile_fix_qty` log entry with no corporate-action
context — an operator investigating this after the fact has **no way to
connect the liquidation, the kill-switch, and the qty-doubling to a single
root cause** without manually checking external split-calendar data.

---

## 10. Cross-References to Prior Audits

- **`docs/RECONCILIATION_ENGINE.md`** — §6 (lines 168-171) and §8.1 (lines
  191-197) describe a severity/threshold model that **does not exist** in
  `reconciler.py` (CA-03/CA-04); §6.2 (lines 302-305) already acknowledges
  dividends as an out-of-scope drift source (CA-10); §8.3 (lines 402-417)
  describes `EmergencyStop`, also not implemented per the same grep (CA-03).
- **`docs/STALE_DATA_AUDIT.md` / `docs/STALE_DATA_DETECTOR.md`** (3-3A/3-3B)
  — orthogonal per §1's framing (freshness vs. corporate-action
  discontinuity). The interaction to watch for in future work: a stale-data
  detector that flags "price hasn't moved in N days" could be **fooled** by
  a corporate action that *legitimately* causes a large jump right after a
  stale period (e.g. a dividend ex-date during a market holiday) — not a
  contradiction, but worth keeping in mind if/when both detectors are wired
  into the same pipeline.
- **`docs/OHLCV_DATA_VALIDATION.md` / `docs/OHLCV_VALIDATION.md`** (3-2A/3-2B)
  — DS-01/DS-02 (NaN propagation through PnL/kill-switch) describe a *similar
  shape* of failure to CA-01 (a bad numeric value silently defeating the
  kill-switch's comparison logic), but via NaN rather than via a stale
  adjustment basis. A future 3-2B price-spike validator and a future 3-4B
  corporate-action detector would both react to "large single-bar price
  change" (CA-12) and **must be designed to agree** on which one wins for a
  given event, or one will suppress the other's correct signal.

---

## 11. Future Work / Not This Task (TASK 3-4B Preview)

This audit specifies **where** (§6) and **who** (§7) — a future design task
would specify **how**. Candidate scope for TASK 3-4B:

1. Design a `backend/data/corporate_actions.py`-style module (detector +
   adjuster), per §6's six insertion points.
2. `PositionTracker` adjustment hook (§6.2) — detection + `qty`/`avg_price`
   rescaling for Copy 1 + Copy 2 atomically.
3. `TrailingStopManager`/`PositionStop` adjustment hook (§6.3) — rescale
   `entry_price`/`peak_price`/`trailing_stop`/`hard_stop` for Copy 3 in the
   same atomic step as #2.
4. Reconciler split-signature detection + new audit fields (§6.4) —
   `adjustment_factor`/`corporate_action_type`/`ex_date` additions to
   `database/models.py` (CA-04), severity-bypass annotation for
   split-shaped `qty_mismatch` (CA-03).
5. Symbol-mapping layer (§6.5) for ticker changes/mergers (CA-08) and
   universe maintenance (CA-11).
6. Kill-switch/EmergencyStop corporate-action-pending gate (§6.6).
7. Resolve CA-06 — verify pykrx's adjustment behavior empirically or via
   upstream docs/changelog; if unadjusted, design the KR adjustment step
   from §6.1.
8. Integrate yfinance `actions=True`/`Ticker.splits`/`Ticker.dividends` as
   the initial (US-only) corporate-action event source (§5.4, §6.4).
9. Spinoff handling for `missing_in_db`-inserted positions (CA-09) — link to
   parent position, open a `TrailingStopManager` entry.
10. Coordinate with the (still-unimplemented) 3-2B price-spike validator on
    disambiguating "real corporate action" vs. "bad data" for large
    single-bar moves (CA-12, §10).

---

## 12. Verification (of this audit's claims)

```bash
# Confirm auto_adjust=True at all 6 cited call sites
grep -rn "auto_adjust" backend/ api/ strategy/

# Confirm no corporate-action keywords anywhere in backend/ outside auto_adjust
# and unrelated EMA `adjust=False` parameters
grep -rniE "split|dividend|corporate_action|adjustment_factor|ticker_change|merger|spinoff" \
  backend/ kis_adapter/ kiwoom_adapter/

# Confirm reconciler.py has no severity/threshold check (CA-03)
grep -n "severity\|CRITICAL\|0.05\|large_position\|_classify" backend/execution/reconciler.py
# expect: no matches

# Confirm _QTY_TOLERANCE and unconditional auto-repair shape
sed -n '90,232p' backend/execution/reconciler.py

# Confirm database/models.py Position/Trade/Order/Fill schema has no CA fields
grep -n "class Position\|class Trade\|class Order\|class Fill\|class EquitySnapshot" -A 12 \
  backend/database/models.py

# Confirm PositionTracker fill-driven avg_price formula (CA-01 root cause)
sed -n '80,133p' backend/execution/position_tracker.py

# Confirm TrailingStopManager/PositionStop fields never touched by reconciler (CA-05)
grep -rn "TrailingStopManager\|PositionStop" backend/execution/reconciler.py
# expect: no matches

# Confirm universe.py is the canonical hardcoded source (CA-11)
sed -n '1,19p' backend/quant/data/universe.py

# Confirm yfinance actions=True/.splits/.dividends unused (§5.4)
grep -rn "actions=True\|\.splits\|\.dividends" backend/ strategy/ api/
# expect: no matches

# Confirm OpenBB still not integrated (§5.3)
grep -rni "openbb" backend/ api/ strategy/
```

---

## Verification (of this task's deliverable)

1. ✅ `docs/CORPORATE_ACTION_AUDIT.md` exists with all 12 sections + preamble,
   each with concrete content (no "TBD"/placeholder).
2. ✅ §3 Event Support Matrix covers KIS/Kiwoom/OpenBB/yfinance/pykrx/기타 ×
   all 7 requested event types.
3. ✅ §4 lists CA-01..CA-13, each with Files/Trigger/Propagation/Result/
   Severity, including the CA-13 backtest-consistency correction and the
   CA-03 design-doc-vs-implementation gap.
4. ✅ §6 Insertion Points covers all 6 sub-areas with file:line references.
5. ✅ §7 Ownership Boundaries table covers 12 concerns with current owner +
   gap.
6. ✅ §9 includes the CA-01→02→03→04 "Most Dangerous Pattern" callout.
7. ✅ §12 Verification commands re-confirm every major claim.
8. ⏳ Commit `docs/CORPORATE_ACTION_AUDIT.md`, push to
   `claude/trading-platform-philosophy-yNHQK`, open a draft PR (per repo
   convention established by PR #65/#66/#69/#70).

**Future (TASK 3-4B, not this task):** design
`backend/data/corporate_actions.py` (or similarly named module) per this
audit's §6 insertion points and §7 ownership boundaries.
