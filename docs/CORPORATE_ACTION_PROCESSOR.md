# Corporate Action Processor — As-Built Specification

> **Supersession note (TASK P2-01B, 2026-06-22).** This file previously held a
> 1781-line *design* specification (TASK 3-4B) for a `CorporateActionProcessor`
> class using the names `CorporateActionType` / `CorporateActionStatus` /
> `AdjustmentRatio`. **That design was never built under those names.** The
> module that actually shipped (`backend/data/corporate_actions.py`) is a
> `CorporateActionService` using `ActionType` / `ActionStatus` /
> `AdjustmentFactor`. `docs/CORPORATE_ACTION_AUDIT.md` §13.4 flagged this
> design↔code naming drift as a defect. This document is rewritten to describe
> the **as-built** module (post-P2-01B) so the doc and the code agree. The prior
> design spec remains in git history (this file, before the P2-01B commit).

This document specifies the corporate-action processor implemented in
`backend/data/corporate_actions.py` and tested in
`backend/data/tests/test_corporate_actions.py` (74 tests). It is a **standalone,
pure-by-default** library: it detects/registers corporate actions, computes
price/position adjustments that preserve portfolio value, retains an adjustment
history, persists a lifecycle audit trail, and fails closed on anything it does
not understand.

> **Scope boundary (still unwired).** As of P2-01B the module has **zero runtime
> call sites** outside its own tests — it is not yet invoked by `PositionTracker`,
> the reconciler, the strategy engine, or the brokers. Wiring it in is a
> separate, paper-validated task (see *Remaining risks* and
> `docs/CORPORATE_ACTION_AUDIT.md` §13.6). P2-01B deliberately does **not**
> redesign `PositionTracker` or touch strategy logic.

---

## 1. Architecture

The module is a small pipeline of single-responsibility components. Everything
below `CorporateActionService` is pure (no I/O, no mutation of inputs); the
service is the only stateful/orchestrating piece, and the audit log is the only
component that touches the database.

```
                         ┌─────────────────────────────────────────────┐
   raw bars / events ───▶│            CorporateActionService            │
   external announcements│  (orchestrator: pending registry + history) │
                         └───┬───────────┬───────────┬─────────────┬────┘
                             │           │           │             │
                   detect_from_bars  register_   apply / apply_   assert_
                             │        action      chain            tradeable
                             ▼           │           ▼             ▼
                  CorporateActionDetector│   PriceAdjuster   CorporateActionGate
                  (heuristic, →PENDING)  │   PositionAdjuster (fail-closed block)
                             │           │           │             │
                             └───────────┴─────┬─────┴─────────────┘
                                               ▼
                                       AdjustmentAuditLog
                                  (DETECTED/REGISTERED/APPLIED/BLOCKED → AuditLog)
```

### Components

| Component | Kind | Responsibility |
|---|---|---|
| `ActionType` | `str, Enum` | `SPLIT`, `REVERSE_SPLIT`, `CASH_DIVIDEND` (alias **`DIVIDEND`**), `TICKER_CHANGE`, **`UNKNOWN`** |
| `ActionStatus` | `str, Enum` | `CONFIRMED`, `PENDING`, `UNKNOWN` |
| `CorporateAction` | `dataclass(frozen)` | The action model: type, symbol, effective date, status, ratio / cash_amount / new_symbol, source, detail. `is_valid()`, `classified()` (fail-closed downgrade) |
| `CorporateActionPendingError` | `Exception` | A symbol has a blocking pending action (not a `RuntimeError`, so the failure breaker ignores it) |
| `UnsupportedCorporateActionError` | `Exception` | `apply()` was asked to apply an UNKNOWN/unsupported/invalid action (fail-closed; not a `RuntimeError`) |
| `AdjustmentFactor` | `dataclass(frozen)` | `price_factor`, `qty_factor`, `cash_per_share`, `new_symbol` |
| `PriceAdjuster` | pure class | `factor_for()`, `adjust_bar()/adjust_bars()` (new dicts, raw untouched), `combine()` |
| `PositionSnapshot` / `PositionAdjustmentResult` | `dataclass(frozen)` | Immutable position basis + before/after value and `value_preserved` |
| `PositionAdjuster` | pure class | `adjust()` — qty/avg-price adjustment + dividend cash delta |
| `CorporateActionDetector` | class | `detect_from_price_jump()` — heuristic split/reverse detection, **always** `PENDING` |
| `AdjustmentAuditLog` | class | Fire-and-forget persistence of the 4 lifecycle events to `AuditLog` |
| `CorporateActionGate` | class | `is_blocking()` / `assert_tradeable()` — fail-closed trading gate |
| `AdjustmentRecord` | `dataclass(frozen)` | **One immutable adjustment-history entry** (action, factor, before/after position, cash_delta, applied_at, `value_preserved`) |
| `CorporateActionService` | class | Orchestrator: detect/register → pending registry; `apply`/`apply_chain`; `history_for`; `assert_tradeable`; `reset`/`clear_history` |

### Lifecycle

1. **Detect or register.** `detect_from_bars()` runs the price-jump heuristic and,
   on a match, files a `PENDING` action and writes `EVENT_DETECTED`.
   `register_action()` files an externally-known action (a confirmed split, a
   dividend, a ticker change), running `classified()` first (fail-closed) and
   writing `EVENT_REGISTERED`. Both land in the per-symbol `_pending` registry.
2. **Block while pending.** `assert_tradeable(symbol)` raises
   `CorporateActionPendingError` (and writes `EVENT_BLOCKED`) while any pending
   action is blocking. Default gate (`block_on_unconfirmed=True`) blocks
   `PENDING`/`UNKNOWN`; a `CONFIRMED`-but-unapplied action **always** blocks
   because the price/position basis is about to change.
3. **Apply.** `apply()` computes the `AdjustmentFactor`, produces new adjusted
   bars and/or a new position snapshot, removes the action from `_pending`,
   appends an `AdjustmentRecord` to the history, and writes `EVENT_APPLIED`.
   `apply_chain()` threads each step's output into the next (split → dividend →
   ticker change for one symbol).
4. **Fail closed.** If `apply()` is handed an `UNKNOWN`/unsupported type or an
   invalid action, it does **not** adjust: it writes `EVENT_BLOCKED`, leaves the
   action pending (symbol stays blocked), and raises
   `UnsupportedCorporateActionError`.

---

## 2. Adjustment rules

Let `ratio` be stored as **new-shares-per-old-share** for splits (a 2-for-1
split is `ratio = 2.0`; a 1-for-10 reverse split is `ratio = 0.1`).

| Action | `price_factor` | `qty_factor` | Position effect | Cash |
|---|---|---|---|---|
| `SPLIT` | `1/ratio` | `ratio` | `qty *= ratio`, `avg_price *= 1/ratio` | — |
| `REVERSE_SPLIT` | `1/ratio` | `ratio` | `qty *= ratio`, `avg_price *= 1/ratio` | — |
| `CASH_DIVIDEND` / `DIVIDEND` | `1.0` | `1.0` | unchanged | `cash_delta = qty * cash_amount` |
| `TICKER_CHANGE` | `1.0` | `1.0` | symbol remapped to `new_symbol` | — |
| `UNKNOWN` / unsupported / invalid | — | — | **not applied** | — (fail closed) |

Bars are scaled by `PriceAdjuster.adjust_bar()`: OHLC × `price_factor`, volume ×
`qty_factor`, and `symbol` remapped on a ticker change. The raw bar is never
mutated — a new dict is returned, keeping raw and adjusted series distinct.

### Portfolio-value preservation

For split / reverse-split / ticker-change the position **value is invariant**:

```
value_before = qty * avg_price
value_after  = (qty * qty_factor) * (avg_price * price_factor)
             = qty * avg_price * (qty_factor * price_factor)
             = qty * avg_price * (ratio * 1/ratio) = value_before
```

`PositionAdjustmentResult.value_preserved` (and `AdjustmentRecord.value_preserved`)
assert this within a tight tolerance (`max(1e-6, |value| * 1e-9)`). For a cash
dividend the position basis is intentionally **unchanged**; the economic value
moves into `cash_delta = qty * cash_amount` rather than the position — so total
(position + cash) value is preserved, while average cost is **not** disturbed.

### Average-cost preservation

Average cost is rebased multiplicatively (`avg_price *= price_factor`) for
splits/reverse-splits so that cost-basis-derived P&L is continuous across the
event, and is left **untouched** for dividends and ticker changes. The processor
never recomputes or resets average cost from market price.

### Fail-closed semantics

`SUPPORTED_ACTION_TYPES = {SPLIT, REVERSE_SPLIT, CASH_DIVIDEND, TICKER_CHANGE}`.
`apply()` refuses anything outside this set or any action failing `is_valid()`
(e.g. a `SPLIT` with no ratio, a `DIVIDEND` with no amount). On refusal the
symbol stays blocked and `UnsupportedCorporateActionError` is raised — the
processor never silently treats an unrecognized action as a no-op.

---

## 3. Adjustment history & audit trail

Two independent records, by design:

- **In-process history** — `CorporateActionService` keeps an append-only list of
  `AdjustmentRecord`s. Query synchronously with `history_for(symbol=None)`
  (per-symbol, keyed on the action's *original* symbol so a ticker change is
  found under the old ticker) or `history_for()` for all. `reset()` clears only
  the pending registry; `clear_history()` clears the history. This is the
  authoritative, queryable "what did we adjust" record for callers/tests.
- **Durable audit log** — `AdjustmentAuditLog` writes `corporate_action_detected`
  / `_registered` / `_applied` / `_blocked` rows to the `AuditLog` table
  (fire-and-forget, mirrors `KillReasonLog`/`StaleDataDetectionService`; a DB
  failure is logged and swallowed, never breaks adjustment). `EVENT_APPLIED`
  carries `cash_delta` / `value_before` / `value_after`.

---

## 4. Remaining risks

1. **Unwired (highest).** The module has zero runtime call sites; production
   corporate-action handling is still provider/broker-side only (yfinance
   `auto_adjust=True`, KIS broker-side position auto-adjust). Until
   `CorporateActionService` is wired into `PositionTracker`/reconciler/strategy
   (a separate paper-validated task), every failure mode CA-01…CA-13 in
   `docs/CORPORATE_ACTION_AUDIT.md` remains live. Its mere existence risks
   *false confidence* that runtime CA handling exists.
2. **Double-adjust hazard on wiring.** Because provider/broker data is **already**
   adjusted, naively applying this module on top would double-count. Wiring must
   define a single adjustment owner per data path (audit §13.4).
3. **Unsupported action types.** Mergers, spin-offs, rights issues, and any
   non-1:1 reorganization are **not modeled** — they funnel to `UNKNOWN` and fail
   closed (safe: trading blocks), but are not *handled*. Adding them is future
   work.
4. **KR adjustment unverified (CA-06).** Korea-market (pykrx/KIS) split/dividend
   adjustment semantics are not yet validated against the factor math here.
5. **No persistence/restore.** `_pending` and `_history` are in-memory only; a
   process restart loses unapplied pending actions and the history (the durable
   `AuditLog` rows survive, but are not reloaded into state). A restart-safe
   store is required before this gates live trading.
6. **Detector is heuristic.** `detect_from_price_jump()` infers splits from
   close-to-close ratios against a fixed table and always returns `PENDING` —
   it cannot *confirm* an action and can be fooled by genuine large moves.
   Confirmation needs an authoritative corporate-action feed, not price alone.

---

## 5. Verification

```bash
pytest backend/data/tests/test_corporate_actions.py -q     # 74 tests
```

Covers all five action categories (split, reverse split, dividend, ticker
change, UNKNOWN), the `DIVIDEND`↔`CASH_DIVIDEND` alias, fail-closed `apply()`
(direct and mid-chain), value/average-cost preservation, the adjustment-history
API, and the full detect→block→apply audit lifecycle.

## 6. Recommended next task

Wire `CorporateActionService` into the runtime — `PositionTracker` and the
reconciler first (per `docs/CORPORATE_ACTION_AUDIT.md` §13.6), establishing a
single adjustment owner per data path and adding restart-safe persistence of
`_pending`/`_history`. That is where the actual CA-01…CA-13 risk reduction
happens; this module only makes it *possible*.
