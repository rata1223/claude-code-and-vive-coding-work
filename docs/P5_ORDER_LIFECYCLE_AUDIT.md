# docs/P5_ORDER_LIFECYCLE_AUDIT.md

> **Analysis and design audit only. No code was changed to produce this document.** No router, middleware, frontend, schema, or business-logic file was modified. Every claim below is cited to an actual file and, where practical, a line range, verified by direct reads and repo-wide greps — not inferred. Items that could not be definitively confirmed from source are explicitly marked `UNVERIFIED`.

**Date:** 2026-07-20
**Precondition milestones treated as complete:** P5-01 API Audit, P5-01B Compatibility Adapter, P5-02 Compatibility Middleware Refactor (P5-02E, PR #131), P5-03A Orders Compatibility (audit, PR #132), P5-03B Orders Compatibility Implementation (PR #133).

---

## 1. Executive Summary

This repository contains **two entirely separate, non-integrated order-execution stacks**, not one system with a thin compatibility gap:

1. **`api/main.py`** (FastAPI, port 8000) — the app the Vue3 frontend actually talks to. Its `api/routers/quick_trade.py` places orders by calling `kis_adapter` **directly**, with zero involvement from any state machine, position tracker, reconciler, risk engine, or idempotency store. It is architecturally a bare pass-through to the broker.
2. **`backend/execution/*`** + **`backend/worker/runner.py`** + **`backend/api/server.py`** (a separate Flask app, `kis-api`/`kis-worker` services) — a materially more complete execution engine with an `OrderStateMachine` (explicit legal-transition table, terminal states enforced), an `OrderFillPoller` (broker polling with backoff, timeout-triggered cancel, single-writer locking), a `PositionReconciler` (broker-priority reconciliation), an `IdempotencyStore` (Redis-backed, fingerprinted order dedup), and dedicated `Order`/`Fill`/`Position` DB tables (`backend/database/models.py`).

**These two stacks do not communicate.** `grep -rn "backend\.execution" api/` returns zero matches. The frontend's Orders/Quick-Trade screen exercises only stack (1). P5-02/P5-03's compatibility-adapter work correctly and safely reshaped DTOs at the boundary of stack (1) — that was in-scope, low-risk, reversible work. But stack (1) is missing entire *capabilities* (cancel, order detail, order status, risk checks, idempotency, fill persistence) that stack (2) already implements correctly. **The P5 Compatibility Layer cannot safely absorb any of these missing capabilities** — doing so would mean re-implementing execution-domain logic (position lookups, pricing decisions, state tracking) inside a component whose entire design contract is "reshape requests/responses, no business logic." Every gap identified below is either (a) something stack (2) already solves and merely needs to be wired to the live frontend-facing app — an **Execution Layer** integration task, not a Compat fix — or (b) a genuine new capability/domain decision that neither stack currently owns.

**Additionally, `frontend/src/views/quick-trade/index.vue` is architecturally a crypto spot/swap exchange UI** (USDT balance semantics, spot/swap toggle, a "leverage" field) bolted onto a KIS-equities-only backend. P5-03B's field-rename adapters fixed the *naming* mismatches; they did not and could not fix this deeper *conceptual* mismatch — "leverage" and "market order" have no corresponding meaning in this backend at all.

---

## 2. Current Runtime Flow

**Path actually used by the live frontend (Orders / Quick Trade screen):**

```text
frontend/src/views/quick-trade/index.vue
  → POST/GET /api/quick-trade/{balance,position,place-order,close-position,history}
  → api/main.py (FastAPI, CompatMiddleware reshapes DTOs — P5-02/P5-03B)
  → api/routers/quick_trade.py
  → kis_adapter/orders.py, kis_adapter/portfolio.py  (direct broker HTTP calls)
  → KIS Open API
```
No `OrderStateMachine`, `OrderFillPoller`, `PositionTracker`, `PositionReconciler`, `IdempotencyStore`, or `RiskManager` is touched anywhere in this path. `place_order`/`close_position` never write to any `Order`/`Fill` table (`api/routers/quick_trade.py:149-150`, explicit comment: *"Record trade in DB ... skip for simplicity"*). `Trade` (`api/models.py`) is read-only in this router (`get_history`, line 217) and is never written by it.

**Path implemented but disconnected from the frontend (the "real" execution engine):**

```text
backend/worker/runner.py (kis-worker service, docker-compose.yml:146)
  → backend/execution/order_poller.py (OrderFillPoller: broker polling, backoff, timeout→cancel)
  → backend/execution/order_events.py (apply_terminal_event: shared CANCELED/REJECTED/EXPIRED convergence)
  → backend/execution/position_tracker.py (in-memory qty/avg_price, restore_positions() from DB)
  → backend/worker/runner.py:_persist_fill (Fill + Order DB write, one commit)
  → AuditLog (separate, best-effort commit)

backend/api/server.py (Flask, kis-api service)
  → backend/execution/reconciler.py (PositionReconciler: broker-priority repair, periodic + startup + manual /api/admin/reconcile)
  → backend/execution/reconciliation.py (FillReconciler: SUM(Fill.qty)==Order.filled_qty backfill; ReconciliationEngine orchestrator)
```
`backend.brokers.kis.KISBroker` wraps `kis_adapter` for this stack. `RiskManager` (`strategy/risk.py`) is imported **only** by the disabled legacy `bot/main.py` (`kis-bot` service, explicitly commented out in `docker-compose.yml` — *"Running it simultaneously with kis-worker would place duplicate orders"*). It is not imported by `backend/execution/*` or `api/routers/quick_trade.py`. **`RiskManager` is currently wired to nothing that runs.**

---

## 3. closePosition Semantic Options

**End-to-end trace:** `frontend/src/views/quick-trade/index.vue` → `quickTradeApi.closePosition(payload)` (`frontend/src/api/index.js:~570`) → `POST /api/quick-trade/close-position` → `CompatMiddleware` (no `_ORDERS_PATH_CONFIG` entry for this path — P5-03B deliberately left it uncovered) → `api/routers/quick_trade.py:169-202` (`close_position`) → `_load_kis(cred)` → `orders.sell_us`/`sell_kr` (`kis_adapter/orders.py`) → KIS Open API. No `PositionTracker`, `OrderStateMachine`, `RiskManager`, or `Fill` persistence anywhere in this path.

**Direct answers:**

- **Market or limit?** Always **limit**. `kis_adapter/orders.py`'s `sell_us`/`sell_kr` hard-code `ORD_DVSN: "00"` (KIS's limit-order code) — confirmed, no market-order path exists anywhere in this adapter.
- **Who decides the closing price?** `ClosePositionRequest` (`api/schemas.py`) requires `price: float` with no default — the **caller** must supply it. The frontend sends **no price at all** (confirmed by P5-03A/P5-03B audits: `closePosition`'s actual payload omits `qty`/`price` entirely) — so today, literally nobody decides it; the request 422s before reaching this question. If a price-sourcing adapter were built (the option investigated and rejected during P5-03B planning), the only value available from `GET /position` is `pchs_avg_pric` (**average purchase price**, a stale historical value), not a live market quote.
- **Who owns quantity calculation?** Same answer — `qty: float` is a required, caller-supplied field with no default and no server-side derivation from the actual open position size.
- **Auto-cancel of existing open orders on close?** `UNVERIFIED` as "no" — no code in `close_position` (`api/routers/quick_trade.py:169-202`) references any cancel call, any order lookup, or any `OrderStateMachine`. Since no order tracking exists in this path at all, there is nothing *to* auto-cancel from this router's perspective.
- **Reverse-order offset instead of a native close instruction?** Yes — `close_position` is literally a plain `sell_us`/`sell_kr` call (`quick_trade.py:183-187`), the same primitive `place_order` uses for a sell. KIS has no distinct "close position" order type in this integration; it is synthesized entirely as a regular sell order.
- **Partial close support?** Technically unenforced rather than "supported" — `qty` is a free numeric field with no validation against the actual open position size (no position lookup happens inside `close_position` at all). Any `qty` value, including one exceeding the real position, would be forwarded to the broker unchanged.
- **Already-closed-position exception handling?** None exists — `close_position` never queries the current position before submitting the sell order (confirmed: no `get_position`/`portfolio.get_us_balance` call anywhere in `close_position`'s body, `quick_trade.py:169-202`). Whatever is submitted goes straight to the broker regardless of whether a matching position exists.

**Authority structure (each axis, current owner):**

| Axis | Current Authoritative Source |
|---|---|
| Position | **Nobody**, in this path — no `PositionTracker`/DB lookup occurs. The real `Position` table (`backend/database/models.py`) exists but is never read or written by `quick_trade.py`. |
| Quantity | Client (frontend), required field, zero server-side validation |
| Price | Client (frontend), required field, zero server-side validation, submitted verbatim as a live broker limit price |
| Order creation | `api/routers/quick_trade.py` itself — a direct, synchronous broker call, not routed through `OrderStateMachine`/`OrderFillPoller` |
| Risk validation | **None** — `RiskManager` is unreachable from this path (§2) |
| Execution | `kis_adapter/orders.py` — a raw broker HTTP call with zero pre-flight validation (no min-qty, tick-size, or market-hours check — confirmed by full read of the file) |
| Final position confirmation | **None** — no reconciliation, no post-order state check, fire-and-forget |

**Adapter safety analysis (why the Compat Layer cannot own this):**

- **Business-logic duplication**: the only way to make `close-position` "work" via request/response reshaping alone would be for the adapter itself to look up the current position and inject `qty`/`price` — i.e., the adapter making a position-sizing and pricing decision. That is order-lifecycle business logic by definition, not DTO translation.
- **Risk bypass**: this path already bypasses `RiskManager` entirely; an adapter-injected close order would inherit that bypass while *additionally* making an un-reviewed pricing call (stale average-purchase-price as a live limit price).
- **Execution bypass**: even if the adapter successfully placed a "working" close order, it would still never touch `OrderStateMachine`/`PositionTracker`/`OrderFillPoller` — it would not close the architectural gap, it would add a second, ungoverned order-placement path parallel to the one real execution engine already built for this purpose.
- **Position-state race (TOCTOU)**: a two-step "read position, then place order" sequence with no locking (nothing in `api/routers/quick_trade.py` or `CompatMiddleware` provides atomicity across two HTTP round-trips) is structurally exposed to a concurrent broker-side position change between the read and the write — e.g., another fill landing on the same account in between.
- **Duplicate-order risk**: the `IdempotencyStore` (`backend/execution/idempotency.py`) exists and is designed for exactly this class of problem, but is unreachable from `api/routers/quick_trade.py`. A double-click would place two real broker orders with zero deduplication, adapter-built or not.
- **Partial-fill handling failure**: since no `OrderStateMachine`/`PositionTracker`/`OrderFillPoller` ever registers this order, a partial fill at the broker would never be detected, tracked, or reconciled — the system would have zero visibility into whether a close order fully executed, partially executed, or failed at all.
- **Reconciliation conflict**: `PositionReconciler` operates against `backend/database/models.py`'s `Position`/`Order` tables. Orders placed via `quick_trade.py` never touch these tables, so they are invisible to reconciliation — but if the underlying KIS account is shared with the "real" execution stack (structurally plausible; both target the same credential model), a resulting fill would surface to the reconciler as an unexplained broker-only position (its documented "external buy/sell" case), with no correlation back to any known order, strategy, or user action.

**Classification:** `Domain redesign needed` — the required capability (position-aware, risk-checked, state-tracked order closing) has no safe home in either existing stack as currently wired; it requires either integrating `quick_trade.py`'s traffic into the existing `backend/execution/*` engine, or building an equivalent lifecycle-aware layer inside `api/`. Neither is a DTO-reshaping task.

**Recommended architecture owner:** `Execution Layer` — the correctness primitives (`OrderStateMachine`, `PositionTracker`, `IdempotencyStore`) already exist there; the work needed is integration, not invention.

---

## 4. closePosition Final Owner Decision

| Decision axis | Owner |
|---|---|
| Final architecture owner | **Execution Layer** |
| Rationale | `backend/execution/*` already implements every primitive `closePosition` needs correctly (position tracking, state transitions, idempotent submission); the gap is that `api/routers/quick_trade.py` was built as an independent, disconnected shortcut and was never wired to it. |
| Explicitly rejected owner | **Compat Layer** — confirmed unsafe per §3's adapter-safety analysis; would require the adapter to make trading decisions (price, quantity, position sizing) it has no business making. |
| Explicitly rejected owner | **UI** — the frontend already collects no `qty`/`price` for this action by design (it expects the backend to know the open position); asking the frontend to collect and send them does not resolve the deeper missing-integration problem, only shifts where the missing data is sourced from. |

---

## 5. Cancel / Order Detail / Order Status Gap Analysis

### Cancel

- **No client-facing HTTP cancel endpoint exists anywhere in the repository** — confirmed by grep across `api/`, `backend/api/server.py`, `backend/execution/`. `backend/api/server.py` defines only `GET /api/orders` (a list). `api/routers/quick_trade.py` has no cancel route. The frontend has zero calls to any cancel endpoint (only unrelated Vant UI-dismiss `@cancel` handlers).
- Cancellation exists **internally only**, from two triggers:
  - `backend/execution/order_poller.py:452-487` (`_handle_timeout`) — **checks terminal state first** (`entry.order.status in (FILLED, CANCELED, REJECTED, EXPIRED)`, returns early if already terminal), removes internal tracking, *then* calls the broker.
  - `backend/execution/reconciler.py:402-425` (`_mark_order_lost`) — calls the broker cancel **unconditionally**, then sets `row.status = CANCELED` afterward, with **no re-check of current status immediately before the broker call** (only pre-filtered earlier in a batch query). This is a different check-order pattern than the poller's, inconsistent within the same codebase.
- `kis_adapter/orders.py:83-96`'s `cancel_us` and `backend/brokers/kis.py:171-210`'s `cancel_order` both send whatever they're given with **zero state validation of their own** — the state check, where it exists at all, lives entirely in the caller (`order_poller.py`), not the broker adapter.
- **Authoritative source of a cancel request today: none is client-triggerable.** Users cannot cancel their own orders through this platform via any UI action — this is a functional gap independent of any DTO mismatch.
- **Poller/Reconciler overwrite risk on cancel is real**: see Order Status below.
- **Compat Layer safety**: not applicable — there is no existing endpoint to adapt. Building one is new capability, not reshaping.

### Order Detail

- **No single-order-detail endpoint exists anywhere in the repo** — confirmed by grep for path-parameterized order routes across `api/`, `backend/api/server.py`. Every existing order-related `GET` returns a **list**: `GET /api/orders` (Flask, `backend/api/server.py:146-155`), `GET /api/quick-trade/history` (`api/routers/quick_trade.py:207`), `GET /api/dashboard/pendingOrders` (`api/routers/dashboard.py:139-172`, confirmed dead code on the frontend per the P5-03A audit).
- **DB vs. broker source authority: not answerable as posed — no such feature exists to examine.** The closest existing analogs already disagree with each other: `GET /history` is DB-sourced (queries `Trade`); `GET /pendingOrders` is broker-sourced (live `KISMarketData.get_pending_us` call). This split is itself evidence that a real order-detail endpoint would inherit an unresolved DB-vs-broker authority question, not a clean precedent to follow.
- **Fill info inclusion**: not applicable, no such endpoint exists. The only Fill-level detail anywhere in the `api/` app's reachable schema is `Trade` (strategy-scoped, never populated by quick-trade); the real `Fill` table (`backend/database/models.py`) is not reachable from `api/main.py` at all.

### Order Status

- **No client-facing HTTP status-query endpoint exists anywhere.** `get_order_status(order_id, symbol)` is defined only as an internal broker-adapter method (`backend/brokers/base.py`, `kis.py:212`, `kiwoom.py:40`, `router.py:74-75`, `paper_broker.py:230`), called only by `backend/execution/reconciler.py:372` and `backend/execution/order_poller.py:292` — never from any route.
- **State authority is conditional, not singular** — this is the most concrete, code-confirmed structural weak point found in this audit:
  - For an order currently **owned** (live-registered) by `OrderFillPoller`, `reconciler.py:_sync_order_status` (lines 427-498) routes through `poller.resync()` (line 442-444), which re-enters the poller's own `_apply_update` pipeline under `entry.processing_lock` (`order_poller.py:148`) — a genuine single-writer guarantee.
  - For an order **not owned** (e.g., after a worker restart, or after the poller's own timeout already removed it — `owned=False` from `resync()`, `order_poller.py:326-328`), `_sync_order_status` falls through to its **own independent DB write** (`reconciler.py:473-498`), guarded only by a coarse `self._reconcile_lock` (serializes reconciler runs against each other) plus a soft `old_status != FILLED` application-level check — **not** a hard mutex against a poller thread that might concurrently re-register the same order. The code's own inline comment (`reconciler.py:485-486`) calls this check the "dedup guard against a simultaneous poller callback" — an acknowledged, accepted soft guard, not a structural guarantee.
  - **Net effect: whether the Poller or the Reconciler has final say over an order's status depends on that order's live-registration lifecycle state at the moment of conflict** — a real, code-confirmed race window for un-owned orders, not a speculative concern.

**Compat Layer safety verdict for all three:** none of Cancel, Order Detail, or Order Status can be safely delivered by the Compat Layer — none has an existing endpoint whose shape merely needs realigning; all three are missing capabilities or (for Order Status) an existing subsystem's internal race condition.

---

## 6. Fill Persistence Ownership Map

| Pipeline stage | Current role | Creates/receives Fill? | Writes DB? | Changes state? | Recommended responsibility |
|---|---|---|---|---|---|
| Broker (KIS) | Source of truth for what actually executed | — | — | — | (external system, no change) |
| `OrderFillPoller` (`order_poller.py`) | Polls broker status on backoff schedule; computes incremental qty vs. watermark (`entry.last_reported_qty`) | Computes fill deltas, does **not** construct/insert `Fill` rows itself | **No** — only writes `AuditLog` (lines 489-508) | Invokes injected `on_filled` callback; on timeout, transitions to CANCELED via broker call | Detection + orchestration (current role is correct) |
| `on_filled` callback (`backend/worker/runner.py:_make_fill_callback`, ~lines 470-564) | The actual fill-processing owner, registered externally to `backend/execution/` | Constructs the `Fill` object | **Yes** — `_persist_fill` (runner.py:616-641), one `db.commit()` with the `Order` status update | Calls `tracker.on_fill(fill)` **before** persisting | Persistence + position update owner (current role is correct, but only for orders registered with this worker) |
| `PositionTracker.on_fill()` (`position_tracker.py:106-141`) | In-memory qty/avg-price update | Consumes a `Fill`, doesn't create one | **No** — pure in-memory; `restore_positions()` rehydrates from caller-supplied data, issues no DB queries itself | Yes, in-memory only | Correct as designed, but has no transactional relationship to the Fill DB write that precedes/follows it |
| `PositionReconciler._sync_order_status` (`reconciler.py:427-498`) | Backfill/repair for orders whose live poller entry is gone | **Yes, when un-owned** — direct `Fill` insert (line ~491) | **Yes** — separate commit (line 496), independent of the runner's path | Yes, `row.status`/`filled_qty`/`avg_fill_price` | A legitimate fallback, but a **second, independently-guarded write path into the same table** as the runner's |
| `FillReconciler` (`reconciliation.py:30-107`) | Batch catch-up across all open orders | **Yes** — inserts the arithmetic gap `Order.filled_qty - SUM(existing Fill.qty)` | **Yes** — `Fill` insert (line 106) | No (fill-only, doesn't touch `Order.status`) | Correct as a batch-reconciliation sweep, but shares the table with two other writers with no unified dedup key |
| `AuditLog` | Immutable trail | — | **Yes**, but always a **separate transaction** from the Fill/Order write, in every path examined (`runner.py:646-658` and `reconciler.py`'s `_audit_position_change`), and failures are swallowed (best-effort) | — | Currently non-authoritative and not guaranteed complete even when the Fill/Order write succeeds |
| `api/routers/quick_trade.py` | Frontend-facing order placement | **No** — confirmed zero `Fill(`/`Trade(` calls in `place_order`/`close_position` | **No** | **No** | Currently orphaned from this entire pipeline; quick-trade fills are invisible to the whole system described in this table |

**Persistence single-Owner verdict:** there is **no single owner**. `backend/database/models.py`'s `Fill`/`Order` tables are the schema owner, but **write authority is split three ways** — the real-time callback path (`runner.py`), the un-owned-order reconciler path (`reconciler.py`), and the batch backfill path (`reconciliation.py`'s `FillReconciler`) — each with a different trigger condition and a different (non-ID-based) dedup strategy.

---

## 7. Fill Idempotency Analysis

- **`Fill` table schema** (`backend/database/models.py:48-54`): `id` (PK, autoincrement), `order_id` (indexed FK, **not unique**), `qty`, `price`, `filled_at`. **No `UniqueConstraint`, no broker-issued fill-ID column, no composite unique key of any kind.** The database itself cannot reject a duplicate `Fill` insert — confirmed by direct read of the model.
- **Real-time path dedup**: relies on `entry.processing_lock` (single-writer mutual exclusion for the *owned* case) plus watermark advancement (`entry.last_reported_qty`) — a **concurrency guard**, not a persisted uniqueness constraint. It is correct under normal operation but enforces nothing at the storage layer.
- **Backfill path dedup** (`FillReconciler`, `reconciliation.py:89-100`): an **arithmetic invariant** — `increment = broker_filled_qty - SUM(Fill.qty WHERE order_id = X)`, insert only if `increment > 0`. This is not an identity check ("does this specific fill already exist") but a sum-matching check ("does our total match the broker's total"). It would not detect, for example, two smaller fills that happen to sum to the same total as one larger fill recorded differently by another path.
- **Explicit dedup key used, verbatim from code:** `Fill.order_id` (for the sum-based invariant only — not a uniqueness key). **No true idempotency/dedup key (e.g. a broker fill ID) exists anywhere in the `Fill` persistence pipeline.** This is stated plainly, not as `UNVERIFIED`, because the schema was read in full and contains no such column.
- **Separate, unrelated idempotency mechanism**: `IdempotencyKey` (`backend/execution/idempotency.py:42-99`) — fingerprint (SHA256) over `(strategy_run_id, symbol, side, qty, price, order_type, date, time_bucket)`, Redis-backed (`idem:{fingerprint}` + `idem_lock:{fingerprint}` via `SET NX EX`), `AuditLog` fallback when Redis is unavailable. **This governs duplicate order *submission*, not duplicate *fill recording*** — a materially different concern, and it is unreachable from `api/routers/quick_trade.py` (§2), so quick-trade order placement has **zero** duplicate-submission protection today.
- **Transactional ordering**: `PositionTracker.on_fill()` runs **before** `_persist_fill()` in the real-time callback (`runner.py:507` then `554`), same thread, but the two are **not** part of the same DB transaction — the in-memory mutation has no rollback/compensation path if the subsequent DB write fails. `UNVERIFIED`: whether any downstream reconciliation pass specifically detects and repairs this exact partial-failure shape (in-memory position updated, Fill/Order write failed) — no such targeted check was found, though `PositionReconciler`'s general periodic broker-priority repair would likely self-correct it eventually as a side effect, not by design.

---

## 8. Quick Trade Domain Mismatch Analysis

- **Price**: form supports both a market/limit order-type selector (`order_type` field sent by the frontend), but `PlaceOrderRequest` has no `order_type` field — it is silently dropped. The **actual** broker call (`kis_adapter/orders.py`) is hard-coded to `ORD_DVSN: "00"` (limit) in every code path with zero exceptions. **"Market order" as a UI concept is fully non-functional against this backend** — every order that reaches the broker is a limit order regardless of what the user selected.
- **Quantity**: frontend field is a generic numeric `form.amount` (`index.vue:69-74`), sent as `amount: Number(...)`. `UNVERIFIED` whether this was originally designed as a share count or a notional currency amount in the crypto-exchange UI this screen was inherited from — the code contains no label or comment clarifying intent, only a generic numeric input. P5-03B's adapter already renames `amount`→`qty` at the DTO level, but that fixes the *field name*, not this deeper *semantic* ambiguity, which remains unresolved and is worth flagging separately.
- **Risk**: `RiskManager` (`strategy/risk.py`) is not imported by `api/routers/quick_trade.py` — confirmed by full-file grep (zero matches for "risk"). No `KillSwitch` class exists in `strategy/risk.py`; a separate `backend/risk/kill_switch.py` module exists but was not read in this audit pass (out of the explicit investigation scope) — `UNVERIFIED` whether it is wired to quick-trade, though circumstantial evidence (quick_trade.py imports nothing from `backend.risk` or `backend.execution` at all) strongly suggests it is not. No stale-market-data gate was found anywhere in `strategy/risk.py`; `UNVERIFIED` whether one exists elsewhere reachable by this path.
- **Broker**: `kis_adapter/orders.py` performs **zero** validation of any kind before sending an order — no minimum quantity, no tick-size rounding, no market-hours check (confirmed by full-file read). Market-hours gating exists **only** as cron-scheduled invocation in the legacy `bot/scheduler.py` (`09:05`/`22:35` KST triggers) — a scheduling mechanism, not a live open/closed gate, and it is structurally bypassed entirely by quick-trade's synchronous HTTP path. **A user can submit a quick-trade order at any hour and it will be forwarded to the broker unconditionally**; what KIS itself does with an off-hours order is `UNVERIFIED` from this codebase (depends on KIS's own server-side behavior, outside this repo's control).
- **Final verdict**: **Not currently fit for either domain as implemented.** The frontend (`quick-trade/index.vue`) is architecturally a crypto spot/swap exchange screen (USDT balance semantics, spot/swap toggle, a `leverage` field meaningful only for perpetual swaps) that happens to POST to a KIS-equities-only backend with no margin/leverage concept at all. P5-03B's field-rename adapters closed the *naming* gap (`amount`→`qty`, `market_type`→`market`) but cannot and did not close this *conceptual* gap — "leverage" has no corresponding backend meaning today (dropped silently by Pydantic), and this is consistent with `CLAUDE.md`'s own Stage 7 roadmap item ("Mobile 브로커 UI 교체... KIS + 키움 2개로 교체"), which already earmarks this screen for a future frontend replacement rather than a backend accommodation.

---

## 9. Remaining Gap Classification

| # | Gap / Problem | Evidence Path | Current Behavior | Risk | Classification | Recommended Action |
|---|---|---|---|---|---|---|
| 1 | `closePosition` has no position-aware sizing/pricing | `api/routers/quick_trade.py:169-202`, `api/schemas.py` (`ClosePositionRequest`) | Requires client-supplied `qty`/`price`; frontend sends neither; 422 today | Financial: a naive fix would submit a stale-price limit order on a live account | Domain redesign needed | Wire quick-trade order placement into `backend/execution/*`, or design a stateful gateway with explicit pricing policy — not an adapter fix |
| 2 | `closePosition` never checks for an existing position before submitting | `api/routers/quick_trade.py:169-202` | No lookup call exists in this handler at all | Could submit a sell against a non-existent or already-closed position | Domain redesign needed | Same as #1 |
| 3 | No client-facing Cancel endpoint anywhere | grep across `api/`, `backend/api/server.py` (no matches) | Users cannot cancel their own orders via any UI path | Functional gap; stuck orders have no user-triggered remedy | Execution Layer problem | Expose a cancel route wired to the existing state-check-before-cancel pattern already proven in `order_poller.py:_handle_timeout` |
| 4 | Inconsistent state-check ordering between the two existing internal cancel triggers | `order_poller.py:452-487` (checks first) vs. `reconciler.py:402-425` (`_mark_order_lost`, no re-check at write time) | Two different check-order patterns in the same codebase | Small race window in the reconciler's lost-order path | Execution Layer problem | Align `_mark_order_lost` to re-verify status immediately before the broker call, matching the poller's pattern |
| 5 | No Order Detail (single-order) endpoint anywhere | grep across `api/`, `backend/api/server.py` (no matches) | All existing GETs return lists only | Frontend cannot show per-order detail even if it wanted to | Domain redesign needed | Decide DB-vs-broker source authority before building; existing analogs (`/history` DB-sourced, `/pendingOrders` broker-sourced) already disagree |
| 6 | No Order Status query endpoint anywhere | grep across `api/`, `backend/api/server.py` (no matches); `get_order_status` only called internally (`reconciler.py:372`, `order_poller.py:292`) | No client can ask "what is the status of order X" | Same as #5 | Domain redesign needed | Same as #5 |
| 7 | Poller/Reconciler race for orders not currently "owned" by a live poller entry | `reconciler.py:427-498` (`_sync_order_status`, independent write path at 473-498) vs. `order_poller.py:148` (`processing_lock`, only held for owned orders) | Un-owned orders' status writes are guarded only by a coarse reconciler-level lock + a soft `old_status != FILLED` check, not a hard mutex | Genuine, code-confirmed race window between concurrent poller re-registration and reconciler write | Execution Layer problem | Extend lock coverage (or an equivalent guard) to the un-owned-order write path |
| 8 | `Fill` table has no unique constraint or broker-fill-ID column | `backend/database/models.py:48-54` | Dedup is 100% procedural (locks, sum-matching), not enforced by the database | A bug or unanticipated race could insert a true duplicate fill with nothing to stop it | Execution Layer problem | Add a broker-issued fill identifier column + unique constraint |
| 9 | Three independent Fill-write paths into the same table with different dedup strategies | `backend/worker/runner.py:_persist_fill`, `reconciler.py:473-498`, `reconciliation.py:30-107` (`FillReconciler`) | Real-time (lock+watermark), reconciler-fallback (soft check), backfill (sum invariant) — three different mechanisms | Latent inconsistency risk; no single path is unambiguously "the" writer | Execution Layer problem | Consolidate to a single write path (e.g. all paths route through the poller's `resync()`, as already partially done for the owned case) |
| 10 | Quick-trade order placement bypasses `RiskManager` entirely | `api/routers/quick_trade.py` (zero risk-related imports/calls, confirmed by grep) | No daily-loss, MDD, or stop-loss check on any quick-trade order | A user can place unlimited-risk orders with no platform-level guardrail | Execution Layer problem | Route quick-trade placement through the same risk-check call `bot/main.py` already demonstrates, or integrate with `backend/execution/*` |
| 11 | Quick-trade order placement bypasses `IdempotencyStore` entirely | `api/routers/quick_trade.py` (no import of `backend.execution.idempotency`) | A double-click/double-submit places two real broker orders with zero deduplication | Real financial risk (duplicate live orders) | Execution Layer problem | Same as #10 |
| 12 | Quick-trade never persists any `Fill`/`Trade` row for its own orders | `api/routers/quick_trade.py:149-150` (explicit "skip for simplicity" comment), confirmed by P5-03A/B audits | `GET /history` is permanently empty for quick-trade activity | Users have no record of their own quick-trade order history | Execution Layer problem | Requires full execution-lifecycle integration — registering the order with `OrderFillPoller` and routing its fill callback through `_persist_fill` (`backend/worker/runner.py`) — not merely wiring `RiskManager`/`IdempotencyStore` (#10/#11), which govern pre-submission checks only and have no path to a `Fill`/`Order` DB write on their own |
| 13 | Quick Trade frontend is conceptually a crypto spot/swap UI on a KIS-equities-only backend | `frontend/src/views/quick-trade/index.vue` (spot/swap toggle, `leverage` field, USDT semantics) vs. `api/schemas.py` (`PlaceOrderRequest`, no `leverage`/margin concept) | "Leverage"/"market order" selections are silently discarded; no functional effect | User-facing confusion — selections that appear to do something do nothing | UI problem | Per `CLAUDE.md`'s own Stage 7 roadmap: replace this screen with a KIS/Kiwoom-native UI rather than continue adapting it |
| 14 | No market-hours gate on the quick-trade order-placement path | `api/routers/quick_trade.py`, `kis_adapter/orders.py` (zero hour/session logic, confirmed by grep); `bot/scheduler.py` (cron-only, structurally bypassed) | Orders can be submitted to the broker at any hour with no platform-side gate | Off-hours order behavior depends entirely on KIS's own server-side handling (`UNVERIFIED` from this repo) | Execution Layer problem | Add a live market-hours check to whichever execution path ultimately serves quick-trade |
| 15 | Two entirely separate, non-integrated order-execution stacks exist in this repository | `api/main.py`+`api/routers/quick_trade.py` vs. `backend/execution/*`+`backend/worker/runner.py`+`backend/api/server.py`; `grep -rn "backend\.execution" api/` → no matches | The frontend-facing app and the "real" execution engine have never been connected | Root cause underlying nearly every other gap in this table | Domain redesign needed | The single highest-leverage next step: decide whether to integrate quick-trade into `backend/execution/*`, or formally scope quick-trade as a permanently separate, lighter-weight domain with its own (currently-nonexistent) risk/idempotency/persistence guarantees |

---

## 10. Final Architecture Decision

| Domain | Final Owner |
|---|---|
| `closePosition` | **Execution Layer** |
| `Cancel` | **Execution Layer** |
| `Order Detail` | **Order Lifecycle Domain** |
| `Order Status` | **Execution Layer** |
| `Fill Persistence` | **Execution Layer** |
| `Quick Trade` | **UI** |

Rationale for the two domains not already justified in §3/§9: **Order Detail** is assigned to `Order Lifecycle Domain` rather than `Execution Layer` specifically because its central open question — DB-sourced vs. broker-sourced authority — is a data-modeling/API-contract decision that should be settled before any execution-engine work is built on top of it, not a mechanical extension of existing `backend/execution/*` code the way Cancel/Status/Fill-Persistence are. **Quick Trade** is assigned to `UI` because the root mismatch (a crypto-exchange-shaped screen on a KIS-equities backend) originates in, and can only be fully resolved by, replacing the frontend — consistent with `CLAUDE.md`'s own existing Stage 7 roadmap language, not a new judgment introduced by this audit.

---

## 11. Recommended Next Implementation Phase

Priorities reflect structural dependency, not effort — later items depend on earlier ones being decided first, and none of this implies immediate implementation (this document is analysis-only).

- **P0 — Decide whether to integrate `api/main.py`'s quick-trade path into `backend/execution/*`, or formally scope quick-trade as a separate, permanently lighter-weight domain.** This is Gap #15, the root cause underlying #1, #2, #3, #8, #9, #10, #11, #12, #14 — each of these genuinely requires this decision before it can be resolved. Gaps #4 and #7 are *related symptoms* of the same disconnection (both are internal-correctness weaknesses inside the already-existing `backend/execution/*` engine) but their fixes do **not** depend on this decision — see the P1 item below for #7, and note #4's fix (aligning `reconciler.py:_mark_order_lost`'s state-check ordering with `order_poller.py:_handle_timeout`'s) is equally independent, an internal-correctness tightening that exposing Cancel to clients (P2, which *does* depend on P0) is separate from. Every other recommendation in this document assumes an answer to this question; attempting any of them in isolation risks building integration work twice or in the wrong direction.
- **P1 — If integration is chosen: wire `api/routers/quick_trade.py`'s order placement through `RiskManager` and `IdempotencyStore`.** This closes gaps #10 and #11, the two with the most direct real-money risk (unbounded-risk orders, duplicate live orders), and are lower-lift than full state-machine integration since both components already exist and only need a call site added.
- **P1 — Harden the Poller/Reconciler race for un-owned orders** (Gap #7) — a correctness fix entirely internal to `backend/execution/*`, independent of the quick-trade integration decision, and worth doing regardless of P0's outcome since it affects the *existing* execution engine's own correctness today.
- **P2 — Design and expose Cancel + Order Status endpoints**, reusing `order_poller.py`'s already-correct terminal-state-check pattern (Gaps #3, #4, #6). Depends on P0's outcome for where these endpoints ultimately route.
- **P2 — Add a broker-fill-ID column + unique constraint to the `Fill` table** (Gap #8), and consolidate the three independent fill-write paths (Gap #9) — a schema and data-flow change squarely in `Execution Layer` territory, not urgent but foundational for any future work that assumes fill data is trustworthy.
- **P3 — Design Order Detail's DB-vs-broker source-of-truth policy** (Gap #5) — lower urgency since no current UI element requests this (confirmed by the P5-03A audit), but worth deciding before any future feature assumes an answer either way.
- **P3 — Replace `frontend/src/views/quick-trade/index.vue`** with a KIS/Kiwoom-native screen (Gap #13), per `CLAUDE.md`'s existing Stage 7 roadmap — the lowest-urgency item here specifically because it's a frontend rewrite, already planned independently of this audit, and not blocking any backend correctness work.

---

## 12. Remaining Risks

- **This audit's scope was read-only and static.** No runtime tracing, load testing, or live-traffic observation was performed — all race-condition and ordering findings (§5's Order Status conflict, §7's transactional-ordering note) are derived from reading lock/guard code, not from observing an actual race occur. They are code-confirmed as *possible*, not confirmed as *observed in production*.
- **Whether the `kis-worker`/`kis-api` Flask stack is actually deployed and running in any current environment is `UNVERIFIED`** — it is fully defined in `docker-compose.yml` and its code is real and substantial, but this audit did not confirm it is presently live anywhere, only that it exists and is architecturally complete relative to `api/main.py`'s quick-trade path.
- **`backend/risk/kill_switch.py` was not read in this pass** (out of the explicit scope handed to the investigating agent) — if it contains a genuine kill-switch mechanism, its relationship to quick-trade (or lack thereof) is `UNVERIFIED`, not confirmed absent.
- **Whether `frontend/src/views/quick-trade/index.vue`'s `amount` field was originally designed as a share count or a notional currency amount is `UNVERIFIED`** — no code-level evidence settles this, and it materially affects how any future fix to Gap #13 should be scoped.
- **Whether `Trade.pnl` (`api/models.py`) is ever populated in the live system is `UNVERIFIED`** — no writer was found in non-test code during this audit; if genuinely unpopulated, this is an additional, previously-undocumented gap outside this audit's core six domains, noted here rather than silently omitted.
- **No compensating/rollback path was found for the case where `PositionTracker.on_fill()` succeeds but the subsequent `Fill`/`Order` DB write fails** (§7) — whether `PositionReconciler`'s periodic broker-priority sweep reliably self-corrects this specific partial-failure shape, by design or by coincidence, is `UNVERIFIED`.
