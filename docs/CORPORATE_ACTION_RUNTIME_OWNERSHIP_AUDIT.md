# Corporate Action Runtime Ownership Audit — TASK P2-02A (2026-06-23)

**Read-only runtime-integration audit. No code is changed; nothing is implemented.**

This audit traces the live runtime path

```
Market Data → PositionTracker → Reconciler → Portfolio → Performance
```

identifies **every adjustment owner and every adjustment insertion point**, determines for each
data **source** whether the broker, the provider, or a corporate-action *processor* must own the
adjustment, and concludes **where exactly one adjustment authority should live**.

It builds on `docs/CORPORATE_ACTION_AUDIT.md` (TASK 3-4A + §13 P2-01A re-audit) and
`docs/CORPORATE_ACTION_PROCESSOR.md` (as-built, P2-01B). Where those use the CA-01…CA-13 failure
IDs, this document cross-references them. Every claim below is grounded in a `file:line` citation
read directly from the tree.

---

## 1. Runtime flow analysis

### 1.1 The pipeline, as wired today

| Stage | Module | What it holds / does | CA awareness |
|---|---|---|---|
| Market Data (prices) | `backend/quant/data/loader.py` | yfinance (US), pykrx (KR), broker live OHLCV | **provider-side** (`auto_adjust=True`) |
| Market Data (positions) | `backend/brokers/kis.py:77-106` | `get_positions()` reads `pchs_avg_pric`, qty | **broker-side** (KIS adjusts) |
| PositionTracker | `backend/execution/position_tracker.py` | in-memory `{symbol→Position}` from fills | **none** |
| Reconciler | `backend/execution/reconciler.py` | broker↔DB position/order repair | **implicit only** (absorbs as `qty_mismatch`) |
| Portfolio | `backend/quant/risk/portfolio.py` | weights from `price_history` Close | consumes provider-adjusted prices |
| Performance | `backend/quant/analysis/performance.py` | `equity.pct_change()` / returns | sensitive to *unadjusted* discontinuities |
| Persistence | `backend/database/models.py` | positions, fills, audit_logs, … | **no corporate_actions / adjustment_history table** |

### 1.2 Two independent adjusted bases coexist at runtime

The system carries position state and price state on **two separately-adjusted bases** that never
reconcile against each other:

- **Positions plane** — Copy0 (broker) → reconciler → DB `positions` → `PositionTracker`
  (`restore_positions`). The broker auto-adjusts qty/avg on corporate actions; the reconciler
  copies broker values into the DB.
- **Prices plane** — provider (yfinance `auto_adjust=True`) → `DataLoader` → Portfolio / Performance
  / indicators. The provider re-bases the entire price series.

These are the live divergence sources CA-01/CA-02/CA-07 in the parent audit: a split moves the
broker position basis at time *T* while the in-memory tracker and any cost-basis-derived P&L lag
until a reconcile, and KR price series may not be adjusted on the same basis as US.

### 1.3 PositionTracker behavior (current)

`PositionTracker` (`position_tracker.py:26-133`) is a pure fill-folding cache:
- `on_fill()` (`:80-115`) — buy averages cost (`avg_price = (avg*qty + price*fillqty)/total`), sell
  reduces qty, deletes at `qty<=0`.
- `restore_positions()` (`:118-124`) — replaces the map from a DB list at startup.
- `update_prices()` (`:127-132`) — updates `current_price` only.

There is **no method that adjusts qty/avg for a corporate action**. A 2-for-1 split makes the
broker report 200 shares while the tracker still holds 100 until a reconcile overwrites the DB and
the tracker is restored/refreshed. (CA-01)

### 1.4 Reconciler behavior (current) — the de-facto owner

`PositionReconciler._reconcile_positions()` (`reconciler.py:167-277`) treats the broker as ground
truth and **silently rewrites the DB to match**:
- `qty_diff > _QTY_TOLERANCE (1)` (`:96`, `:210`) → `qty_mismatch` gap → sets `row.qty = bp.qty`,
  `row.avg_price = bp.avg_price` (`:225-231`).
- avg-only drift (`price_changed and qty_diff<=tol`) → silent `fix_avg_price` (`:232-245`).
- the fix is **deferred** when `_has_pending_order(sym)` is true (`:211-214`).

This is the **only place the runtime "absorbs" a split** — but it does so as a generic quantity
mismatch (CA-03): no classification that it *was* a corporate action, no value-preservation check
(qty×avg before vs after), and no adjustment-history row beyond a generic `reconcile_fix_qty`
AuditLog entry (`:219-224`).

### 1.5 Restart recovery flow

`StartupRecovery.run()` (`recovery.py:83-108`) runs 8 steps before `SAFE_MODE.enable()`. Step 6
`_step_reconcile()` (`:190-226`) runs the reconciler; `worker/runner.py:407` →
`_restore_positions()` (`:633-642`) loads DB positions into the tracker. Net effect: a split that
happened while the worker was down is **self-healed by accident** — the reconciler absorbs it as a
`qty_mismatch` and the tracker restores the broker-adjusted DB row. It works, but it is
unlabelled, unaudited as a CA, and indistinguishable from absorbing a real anomaly.

---

## 2. Ownership map (per source)

For each market-data **source**, who *already* adjusts, and whether a corporate-action processor
*must*:

| Source | Plane | Broker already adjusts? | Provider already adjusts? | Processor must adjust? | Evidence |
|---|---|---|---|---|---|
| **KIS — positions** | positions | **YES** (qty + `pchs_avg_pric` are post-action) | n/a | **NO** for qty/avg — broker is the value authority. Processor should **detect / classify / record / gate** only | `brokers/kis.py:77-106` |
| **KIS — OHLCV (live)** | prices | **UNVERIFIED** (daily-chart adjust flag not set explicitly) | n/a | verify; must **not** double-adjust | `kis_adapter/market_data.py`, `loader.py:116-128` |
| **Kiwoom** | both | **N/A — full stub** (`get_positions/get_balance/get_price` all `NotImplementedError`) | n/a | **YES, if ever enabled** — no broker-side source would exist | `brokers/kiwoom.py:18-53`, `capabilities.py:53-54` (`supports_portfolio=False`) |
| **yfinance (US)** | prices | n/a | **YES** — `auto_adjust=True` (split + dividend) | **NO** — provider is the price authority; re-adjusting = double-adjust | `loader.py:73,75`; `quant/live/safeguards.py:214`; `strategy/indicator/backtest.py:17` |
| **pykrx (KR)** | prices | n/a | **UNVERIFIED** — `get_market_ohlcv_by_date(...)` called with **no `adjusted=` arg**; depends on the pykrx version default (CA-06) | verify; if raw, adjust **at load**, never in the position layer | `loader.py:96-114` |
| **OpenBB** | — | — | **N/A — not integrated** anywhere in the tree (phantom source) | N/A until adopted | grep: no `openbb`/`obb.` usage |
| **기타** (SimulatedBroker; yfinance `.KS` KR fallback) | both | sim is internally self-consistent | `.KS` fallback = `auto_adjust=True` | NO | `loader.py:88-94`; `brokers/capabilities.py:72-102` |

**Capabilities gap:** `BrokerCapabilities` (`brokers/capabilities.py`) encodes 30+ flags but **none
declares whether the broker auto-adjusts corporate actions** — so "who adjusts" is implicit
tribal knowledge, not a queryable contract.

---

## 3. The seven required findings

1. **Current runtime structure** — §1. Two independently-adjusted planes (broker-adjusted
   positions; provider-adjusted prices) that never cross-check. The reconciler is the only runtime
   component that moves a position basis in response to a corporate action, and it does so
   implicitly.

2. **Adjustment-ownership violations** — there is **no declared owner**. The reconciler *acts* as
   the position authority but mislabels corporate actions as `qty_mismatch`; the price authority is
   the provider but only by side-effect of `auto_adjust=True`; `CorporateActionService` *models*
   ownership but is **unwired** (zero runtime call sites — confirmed: it appears only in its own
   module and tests). No capability flag, no schema, no single module "owns" CA classification.

3. **Double-adjustment risks** — the dominant hazard for any future wiring:
   - wiring the processor to *also* adjust position qty/avg **on top of** the already-adjusted
     broker value ⇒ **double count** (e.g. 100→200 by broker, →400 by processor);
   - applying the processor's `PriceAdjuster` to yfinance bars that are **already** `auto_adjust`ed
     ⇒ double-adjusted prices;
   - reconciler **and** processor both writing DB `positions` ⇒ two writers racing on one row.

4. **Provider-adjusted-data risks** — mixing **adjusted** US (yfinance) with **possibly-raw** KR
   (pykrx, unverified) inside one portfolio means Portfolio covariance and Performance
   `pct_change()` (`performance.py:28,44,171,345`) can see a phantom ±50% return on a KR split day
   while US symbols are clean (CA-07). The single boundary that should enforce a uniform price
   basis is the **loader**, which currently does not.

5. **Broker-adjusted-position risks** — because the reconciler absorbs a split as a generic
   `qty_mismatch`, a *legitimate* corporate action is indistinguishable from a *bug* or an
   *unexpected external fill*; the `_has_pending_order` defer (`reconciler.py:211-214`) can leave
   the DB stale across the split window; and the `_QTY_TOLERANCE=1` (`:96`) means a 1-share
   reverse-split edge could be silently ignored.

6. **Restart risks** — the split self-heal on restart is accidental and unlabelled (§1.5). More
   importantly, **if the processor were wired as designed**, its `_pending` (a CONFIRMED-but-
   unapplied block) and `_history` live **in memory only** — no `corporate_actions` /
   `adjustment_history` table exists (`database/models.py` has none). A restart therefore would
   **fail open**: a symbol that should be CA-blocked becomes tradeable, and adjustment-audit
   continuity is lost.

7. **Affected modules** — `backend/execution/position_tracker.py`,
   `backend/execution/reconciler.py`, `backend/worker/recovery.py`, `backend/worker/runner.py`,
   `backend/quant/data/loader.py`, `backend/quant/risk/portfolio.py`,
   `backend/quant/analysis/performance.py`, `backend/brokers/{kis,kiwoom,capabilities}.py`,
   `backend/data/corporate_actions.py`, `backend/database/models.py`.

---

## 4. Failure scenarios

| # | Scenario | Mechanism | Result | Parent ID |
|---|---|---|---|---|
| F-1 | 2:1 split, worker live | broker reports 200 @ ½ price; tracker still 100 until reconcile | sizing/PnL on stale 100 between split and next reconcile | CA-01 |
| F-2 | Split absorbed | reconciler `qty_mismatch` → DB qty 100→200 | "healed" but logged as generic mismatch, not a CA; no value-preservation assertion | CA-03 |
| F-3 | Split coincident with bug/external fill | reconciler can't tell CA from anomaly | a real discrepancy is silently absorbed as if a CA | CA-03 |
| F-4 | KR split, pykrx raw | `pct_change()` on raw series | phantom −50% return in Performance / Portfolio cov | CA-07 |
| F-5 | Future wiring, naïve | processor adjusts qty on top of broker-adjusted value | **double-count** position | new |
| F-6 | Future wiring, naïve | processor adjusts already-`auto_adjust`ed yfinance bars | **double-adjust** prices | new |
| F-7 | Restart with pending CA | in-memory `_pending`/`_history` lost | gate **fails open**; CA-blocked symbol trades | CA restart |
| F-8 | Kiwoom enabled | `get_positions()` raises `NotImplementedError` | no broker-side source ⇒ no adjustment authority at all | new |

---

## 5. Double-adjust risk register (explicit)

Any future integration **must** guarantee *exactly one* adjuster per value, per plane:

1. **Position qty/avg** — owned by **broker (Copy0)**. The processor must **not** write qty/avg.
2. **Price/OHLCV (US)** — owned by **provider (`auto_adjust`)**. The processor must **not** re-scale.
3. **Price/OHLCV (KR)** — owner **must be made explicit** at the loader (verify/force pykrx
   adjustment). Today: ambiguous.
4. **DB `positions` row** — today written by reconciler and recovery (`recovery.py:333-366`); a
   processor must **not** become a third writer.

---

## 6. Insertion points (where a single authority would attach — not implemented)

- **Position-CA authority (detect/classify/record/gate):** the reconciler's qty/avg branch,
  `reconciler._reconcile_positions` (`reconciler.py:206-245`). This is the one place a broker-side
  quantity jump is already observed; classifying it there (split signature vs. anomaly) upgrades
  the existing implicit absorb into an explicit, audited corporate action — **without** adding a
  second writer of qty/avg.
- **Price-CA authority (uniform basis):** the loader boundary, `DataLoader._fetch_us` /
  `_fetch_kr` (`loader.py:69-114`) — the single choke point where every price series enters the
  system.
- **Gate insertion:** before order submission in the strategy/worker path (the same place
  `SAFE_MODE.can_trade` is checked), calling `CorporateActionService.assert_tradeable(symbol)`.
- **Persistence insertion:** new `corporate_actions` + `adjustment_history` tables in
  `database/models.py`, written by the reconciler-side detector and restored during
  `StartupRecovery` so the gate is fail-closed across restarts.

---

## 7. Recommendation — where the single authority should live

**Establish one authority per data plane; do not introduce a parallel adjuster.**

1. **Positions plane → broker (Copy0) remains the value authority, surfaced through a CA-aware
   reconciler.** Wire `CorporateActionService` at the reconciler insertion point as
   **detector + classifier + recorder + gate only**: when a broker quantity jump matches a
   split/reverse signature, label and persist it as a corporate action (not a `qty_mismatch`),
   write an adjustment-history row, and gate trading on the symbol — **without** the processor ever
   writing qty/avg itself. This removes the double-write hazard (F-5) by construction.

2. **Prices plane → provider remains the authority.** Keep `auto_adjust=True`; the processor must
   **never** re-adjust provider-adjusted bars (F-6). Close the KR gap by verifying/forcing pykrx
   adjustment **at the loader**, giving every symbol a uniform basis (fixes F-4).

3. **Restart → make the gate durable.** Persist `_pending`/`_history` to the new tables and restore
   them in `StartupRecovery`, so a CA hold survives a restart (fixes F-7).

4. **Make ownership explicit.** Add an `adjusts_corporate_actions` capability flag to
   `BrokerCapabilities` so "who adjusts" is a queryable contract, not tribal knowledge (and so a
   future Kiwoom — F-8 — is forced to declare it has no broker-side adjustment).

**Single-sentence answer:** the one adjustment authority for **positions** is the **broker, made
explicit through a CA-aware reconciler that classifies/records/gates but never re-writes qty/avg**;
the one authority for **prices** is the **provider, enforced uniformly at the loader** — and the
corporate-action processor's correct role is **detector/recorder/gate**, never a second adjuster.

> **Do not implement yet.** This is analysis only. Each insertion point above is a separate,
> paper-validated, safety-critical change to be approved on its own.
