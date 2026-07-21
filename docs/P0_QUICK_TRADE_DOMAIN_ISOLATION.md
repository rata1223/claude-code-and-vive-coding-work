# P0-02: Quick Trade Domain Isolation — Design & Audit

> **Analysis and design only. No code, API, frontend, Compat, Execution, or schema file was changed to produce this document.** Every claim is grounded in a direct read of current source, cited as `file:line`. Anything not definitively confirmable from source is marked `UNVERIFIED`.

**Date:** 2026-07-21
**Precondition:** `docs/P0_QUICK_TRADE_EXECUTION_DECISION.md` (merged, PR #135) decided **SEPARATE** — Quick Trade becomes its own lightweight order-lifecycle domain, not merged into `backend/execution/*`. This document specifies that isolation: domain boundaries, responsibility split, `closePosition` ownership, UI-contract redesign, data ownership, migration, and the P0-03/04/05 roadmap. It does **not** re-argue SEPARATE; that decision is settled.

**Correction carried into this document:** the two prior merged docs (`P5_ORDER_LIFECYCLE_AUDIT.md`, `P0_QUICK_TRADE_EXECUTION_DECISION.md`) described `api/models.py`'s `Trade.strategy_id` as "already nullable." That is **factually wrong** against current source — `api/models.py:74` declares `strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False)`. This document supersedes that framing; the corrected fact drives §8 and §10.

---

## 1. Architecture Boundary

Two order pipelines exist and must stay separated. The boundary line is drawn at the **domain entry**, not at the broker.

```text
[Quick Trade pipeline — manual, multi-user, stateless-today]
Frontend (frontend/src/views/quick-trade/index.vue)
  → CompatMiddleware (api/compat.py — DTO reshaping ONLY, P5-02/P5-03B)
  → Quick Trade Domain (api/routers/quick_trade.py [+ future api/services/quick_trade_service.py])
  → KIS Broker SDK (kis_adapter/*) → KIS Open API
  → shared infra: RiskManager (strategy/risk.py), api/crypto, Credential/User (api/models.py)

[Execution pipeline — automated strategy, single-account, stateful daemon]
Frontend → CompatMiddleware
  → Execution Layer (backend/execution/*)
  → OrderStateMachine → OrderFillPoller → PositionTracker / PositionReconciler
  → StartupRecovery, IdempotencyStore, (dormant) KillSwitch, EmergencyFlattenManager
  → backend/database/models.py tables (orders/fills/positions/strategy_runs/trades)
```

**The isolation invariant:** the two pipelines may share **stateless, credential-agnostic infrastructure** (broker SDK, DTO middleware, the schema-independent risk manager) but must **never** share **stateful, single-account-scoped machinery** (the execution state machine, pollers, reconcilers, and the `backend/database/models.py` tables). Rationale is the credential-model incompatibility established in PR #135 §1: `backend/execution/*` has zero `credential_id`/`user_id` columns and is single-account-per-process; Quick Trade is inherently `user_id`/`credential_id`-scoped (`api/models.py:27-43`).

---

## 2. Quick Trade — Final Responsibility

Quick Trade is a **manual, per-request, multi-user convenience trading domain**. Its final, bounded responsibility:

| Owns (final) | Does NOT own |
|---|---|
| UI convenience endpoints (`balance`, `position`, `place-order`, `close-position`, `history`) | Automated strategy execution |
| Building a broker order request from an explicit, client-supplied DTO | Order-lifecycle state machine / async fill polling |
| Submitting a single synchronous broker order under a **request-scoped, credential-injected** client | Position tracking / reconciliation daemon |
| Live per-request broker reads (balance/position) | Multi-account emergency flatten |
| Persisting **its own** submitted orders/fills to a **dedicated** table (§8) | Kill-switch / MDD platform enforcement |
| A pre-submit risk gate that **wraps** the existing `RiskManager` (§4) | The `backend/database/models.py` schema |

**Current vs. to-remove responsibilities** (step-1 nine-factor split, each confirmed against `api/routers/quick_trade.py`):

| Factor | Currently does | Final design |
|---|---|---|
| UI 요청 변환 (request translation) | Yes — via CompatMiddleware DTO reshape (P5-03B) | **KEEP** in Compat (shape only, no logic) |
| 주문 의사결정 (order decision) | No — client supplies symbol/side/qty/price verbatim | **KEEP absent** — QT never decides qty/price (constraint) |
| 주문 제출 (submit) | Yes — one synchronous `orders.buy_*/sell_*` call (`quick_trade.py:134-147`) | **KEEP**, but move broker construction into a request-scoped factory (P0-03) |
| 주문 상태 (order status) | Fake — hardcoded `"status":"submitted"` (`quick_trade.py:159`) | **REMOVE the fake literal**. Store only a **submission outcome** (`submitted`/`failed`/`unknown`) — the broker-*submission* result, which **never means filled**. Real fill/lifecycle status is DEFER (§4) |
| 체결 (fill) | Not detectable — single call, no confirmation (`quick_trade.py:46` behavior) | **DEFER** — no async fill confirmation in P0 |
| Position 반영 | Live read per `GET /position` (`quick_trade.py:90-118`), nothing tracked | **KEEP** live-read; no tracked position |
| Fill Persistence | Zero — no row written (`quick_trade.py:149-150`) | **ADD** — QT persists its own to `quick_trade_orders` (§8) |
| Risk | Zero — `RiskManager`/`KillSwitch` unreachable from this file | **ADD** — wrap `RiskManager` as a pre-submit gate |
| Reconciliation | None — nothing tracked to reconcile | **KEEP absent** — stateless design, nothing to reconcile |

---

## 3. Execution Layer — Final Responsibility (unchanged)

`backend/execution/*` remains the **single-account, automated KIS-equity order-lifecycle owner**, exactly as today. This document changes nothing about it. Confirmed wired-and-active components (per PR #135 §3 and re-confirmed): `OrderStateMachine`, `OrderFillPoller`, `PositionTracker`, `PositionReconciler`, `IdempotencyStore`, `StartupRecovery` — all driven by `backend/worker/runner.py`'s single static-credential worker. `KillSwitch` and `EmergencyFlattenManager` exist but are dormant/paper-gated. The Execution Layer's scope is **not** extended to Quick Trade.

---

## 4. Shared Infrastructure (allowed coupling)

Only **stateless, credential-agnostic** components may be shared across both domains. **This table describes the post-P0-03 target.** The KIS SDK is only credential-agnostic once clients are built from **request-scoped** credentials; today `_load_kis` still injects them via process-wide `os.environ` (§5), so the request-scoped client factory (P0-03) is a **hard prerequisite** — multi-user Quick Trade must not be enabled until it lands.

| Component | Path | Why shareable |
|---|---|---|
| KIS broker SDK | `kis_adapter/*` (`KISClient`, `KISOrders`, `KISPortfolio`) | Pure per-call broker HTTP wrapper, no cross-request state of its own — **but only credential-agnostic after P0-03**; the current `os.environ` injection (§5) must be replaced first |
| Semantic mapper | `backend/brokers/semantic_mapper.py` (`extract_broker_order_id`) | Pure function over a broker response |
| Risk manager | `strategy/risk.py` `RiskManager` | **Schema-independent** — Redis halt-flag + env thresholds, no SQL dependency; the one execution-adjacent capability wire-able into QT with zero schema change |
| Credential decrypt | `api/crypto.decrypt` | Pure crypto helper |
| Identity/credential models | `api/models.py` `User`, `Credential` (`user_id`-scoped) | Already QT's own multi-user schema |
| DTO middleware | `api/compat.py` `CompatMiddleware` | Shape reshaping only — carries no trade logic |

**Step-4 KEEP/MOVE/WRAP/DEFER classification** (Quick Trade ownership of 8 execution capabilities):

| Capability | Verdict | Reason |
|---|---|---|
| Fill persistence | **KEEP** | SEPARATE requires QT to own its own fills; **planned** via the new `quick_trade_orders` table (§8), a fresh lightweight impl created in P0-04 — **not** the backend `Fill` table. Not yet implemented |
| Order lifecycle state | **DEFER** | QT is synchronous today; a full state machine is out of P0 scope. Store only **submission outcomes** (`submitted`/`failed`/`unknown`), explicitly not fill/lifecycle status; no async transitions |
| Kill switch | **DEFER** | Dormant platform-wide (zero call sites, PR #135 §3); wiring it is a platform decision, not QT's to make |
| MDD | **DEFER** | Part of the risk/kill-switch platform layer; deferred with it |
| Position reconciliation | **DEFER** | QT tracks no position state → nothing to reconcile. Likely never needed while QT stays stateless |
| Emergency flatten | **MOVE** | Remains Execution-Layer / single-account scope. QT has no concept of "flatten this user's positions"; explicitly not QT-owned |
| Broker reconciliation | **DEFER** | Same as position reconciliation — no tracked state to reconcile against the broker |
| Risk gate | **WRAP** | Reuse the schema-independent `RiskManager` as a thin pre-submit gate; do not reimplement |

---

## 5. Forbidden Coupling

Quick Trade must **never** couple to:

- **`OrderStateMachine`, `OrderFillPoller`, `PositionTracker`, `PositionReconciler`, `StartupRecovery`, `IdempotencyStore` (backend), `EmergencyFlattenManager`, `KillSwitch`** — all scoped to the single static-credential model; importing any of them re-creates the multi-tenancy incompatibility SEPARATE was decided to avoid.
- **`backend/database/models.py` tables** (`orders`, `fills`, `positions`, `strategy_runs`, and the backend `trades`) — they have no `credential_id`/`user_id` column and belong to the execution daemon.
- **Process-wide `os.environ` credential injection** (`api/routers/quick_trade.py:26-30`) — the active concurrency bug PR #135 §1 flagged as a **rollout gate**; the isolated domain must construct broker clients from **request-scoped** credentials instead (P0-03).
- **Any real trade-decision logic inside CompatMiddleware** — Compat stays DTO-shape-only (P0-01 principle); qty/price/market decisions never live there.

**Landmine — dual `trades` definition (record it, do not couple to it):** two ORM classes both map `__tablename__ = "trades"` with **incompatible column sets**:
- `api/models.py:70-83` `Trade`: `strategy_id` NOT NULL (FK→strategies), `pnl`, `fee`, `filled_at`.
- `backend/database/models.py:13-22` `Trade`: `strategy_run_id` (nullable), `market`, `broker`, `created_at` — matching Alembic `alembic/versions/1aad3f2df8d9_initial_schema.py:138-149` and carrying **none** of the api columns.

Both apps provision via **non-altering** `create_all` (`api/database.py:32-35`, `backend/database/models.py:190-200`); Alembic's canonical url is `postgresql://localhost/trading` (`alembic.ini:65`), api's default is `.../trading` (`api/database.py:5-8`). **`UNVERIFIED`:** whether both runtimes hit the same physical `trading` database — if they do, whichever provisioner runs first wins the `trades` schema and the loser's ORM would fail at query time on a missing column. The isolated Quick Trade domain sidesteps this entirely by owning a **new** table (§8), never writing to `trades`.

---

## 6. closePosition Decision

**Decision: `closePosition` stays in the Quick Trade domain** — it is **not** promoted to an Execution-Layer formal order. It is a plain reverse-direction broker `sell` (`api/routers/quick_trade.py:183-187` → `orders.sell_kr`/`sell_us`), semantically identical to a `place-order` sell; promoting it to the single-account Execution Layer would re-introduce the forbidden coupling for no benefit.

Seven-owner definition (constraint: **no compat/backend auto-calculation of qty or price**):

| Owner axis | Assignment | Grounding |
|---|---|---|
| Quantity owner | **Client / UI** | `ClosePositionRequest.qty` is a required field the frontend must supply; backend never derives it |
| Price owner | **Client / UI** | `price` is required and submitted verbatim; no server-side price sourcing |
| Order type | **Limit only** | `kis_adapter/orders.py` hardcodes `ORD_DVSN:"00"`; no market-order path exists in this integration |
| Position source | **Live broker read** | `GET /position` (`quick_trade.py:90-118`); QT holds no tracked position to close against |
| Risk gate | **QT `RiskManager` wrap** | The same pre-submit gate as `place-order` (§4 WRAP), on the QT side |
| Execution owner | **QT lightweight service** | The future `quick_trade_service`, not `backend/execution/*` |
| Fill owner | **QT (`quick_trade_orders`)** | Persisted like any QT order (§8) |

**Client-owned ≠ unvalidated.** "Client owns qty/price" means the domain never *derives or calculates* them (the no-auto-calc constraint) — it does **not** mean they are submitted unchecked. The current `PlaceOrderRequest`/`ClosePositionRequest` expose unconstrained `float` fields and the code submits them verbatim with no sanity guard. The final contract must specify a **server-side validation boundary** in the QT service (enforcement, not calculation): quantity > 0; price > 0 with valid precision/tick for the instrument; a maximum-exposure cap (qty × price); and scope enforcement that the target symbol/position and `credential_id` belong to the authenticated user. Validation rejects a bad request; it never invents a missing value.

**Consequence (UI contract, §7):** because qty and price are client-owned and must not be auto-filled, the current frontend — which sends neither — produces a guaranteed 422 (confirmed by P5-03B's `TestClosePositionRemainingGap`). The contract must be **redesigned** to require both explicitly; auto-injecting a stale `pchs_avg_pric` as a live limit price was rejected in P5-03B as a real-money pricing decision outside the adapter's scope, and that rejection stands.

---

## 7. UI Contract Decision

Current UI expectation vs. KIS-equity backend, eight items classified **유지 / 변환 / 제거 / 재설계**:

| Item | Verdict | Basis |
|---|---|---|
| buy | **유지** | `place-order` side=buy already works; P5-03B `amount→qty`, `market_type→market` reshape handles the DTO shape |
| sell | **유지** | Same path, side=sell |
| closePosition | **재설계** | Must require explicit client qty+price (§6); today sends neither ⇒ guaranteed 422 |
| leverage | **제거** | Crypto-only inherited concept; KIS equities are cash-settled/unleveraged; `PlaceOrderRequest` already drops it (Pydantic `extra="ignore"`). Formal removal per `CLAUDE.md` Stage 7 UI replacement |
| quantity | **변환** | Frontend `amount` → backend `qty` (P5-03B `body_remap`); keep as a conversion |
| price | **유지** | Passed through, but documented as a **real limit price** (no market path) — callers must send a sane price |
| position | **변환** | Backend singular `{symbol, position}` → frontend `positions[]` via P5-03B `response_transform`; keep |
| order status | **재설계** | No status endpoint exists; `"submitted"` is a hardcoded literal (`quick_trade.py:159`). The redesigned field is a **submission acknowledgement** (`submitted`/`failed`/`unknown`), explicitly **not** a fill/lifecycle status — `submitted` never implies filled, and an ambiguous broker result (timeout) is `unknown`. Real fill status is future work (DEFER, §4) |

---

## 8. Data Ownership

**Decision: Quick Trade persists to a new, dedicated table `quick_trade_orders`, owned solely by the `api/` app.** Rationale:

1. `api/models.py:74` `Trade.strategy_id` is **NOT NULL** (FK→strategies). Today **no trade row is persisted at all** for quick-trade orders — `quick_trade.py:149-150` explicitly skips it ("skip for simplicity"). But the comment's implied approach (`strategy_id=None`) would, if ever implemented, violate the NOT-NULL constraint: persisting a manual, non-strategy order into `trades` is **impossible without either a fake strategy or a schema change**. This is a reason to persist elsewhere, not a description of current behavior.
2. The `trades` table is **contested** (§5 landmine): reusing it entangles Quick Trade with the execution/Alembic schema-ownership conflict.
3. A dedicated table is the cleanest expression of SEPARATE: QT owns a table nothing else writes, `user_id`/`credential_id`-scoped from day one.

Proposed shape (design only; created in P0-04):

| Column | Type | Note |
|---|---|---|
| `id` | PK | |
| `user_id` | FK→users, NOT NULL | tenant scope |
| `credential_id` | FK→credentials, NOT NULL | which account placed it |
| `symbol`, `side`, `market` | strings | order identity |
| `qty`, `price` | numeric | client-supplied |
| `broker_order_id` | string, nullable | broker ODNO from `extract_broker_order_id` |
| `status` | string | **submission outcome** only: `submitted` / `failed` / `unknown` (never a fill/lifecycle status; `unknown` = ambiguous/timeout broker result, recoverable) |
| `idempotency_key` | string, **NOT NULL, unique per user** | reserved **before** the broker call (§9); a retry reuses the reservation and cannot double-submit |
| `created_at` | datetime | |

**Tenant-ownership invariant.** `user_id` and `credential_id` are independent FKs, which alone do **not** guarantee the credential belongs to the order's user. This invariant must be enforced explicitly, or cross-tenant attribution can be corrupted. Two layers: (1) at write time, the service resolves the credential via `_get_cred` (`quick_trade.py:38-43`), which already filters `Credential.user_id == current_user.id` — the new service path must preserve this and reject a mismatch; (2) every history/read query must filter on `user_id` (never on `credential_id` alone). Preferred belt-and-suspenders: a composite FK `(user_id, credential_id)` referencing a matching unique key on `credentials`, so the database rejects a mismatched pair outright.

**`get_history` implication:** the current handler INNER-JOINs `Trade.strategy_id == Strategy.id` (`quick_trade.py:216-221`), so it can only ever return strategy-attributed trades. Surfacing `quick_trade_orders` in history is therefore **explicit follow-up work** (part of P0-04) — it does not come for free by reusing `Trade`. This is a deliberate, acknowledged cost of the dedicated-table choice.

**Ownership summary:** `quick_trade_orders` → Quick Trade domain (exclusive writer). `trades`/`orders`/`fills`/`positions`/`strategy_runs` → Execution Layer (QT never writes). `credentials`/`users`/`strategies`/`watchlist_items` → shared `api/` identity schema (QT reads `credentials`/`users`).

---

## 9. Runtime Flow (target)

```text
[Quick Trade — target, after P0-03..P0-05]
Frontend
  → CompatMiddleware (DTO reshape only)
  → api/routers/quick_trade.py (thin endpoint)
  → api/services/quick_trade_service.py  ← NEW, minimal (no heavy abstraction):
      1. build request-scoped KIS client from the decrypted Credential
         (NO os.environ mutation)                        [P0-03]
      2. validate DTO (qty>0, price precision/tick, exposure cap,
         symbol/credential belongs to user)               [P0-04, §6]
      3. RiskManager pre-submit gate (halt-flag / thresholds) [P0-05, WRAP]
      4. RESERVE per-user idempotency_key + write a pending row
         BEFORE the broker call (crash-safety)            [P0-04/05]
      5. orders.buy_*/sell_* — single synchronous broker call
      6. finalize status: submitted / failed; an ambiguous or
         timed-out broker result → unknown (recoverable, never "filled")
         — a retry reuses the reservation, never double-submits [P0-04, KEEP]
  → KIS Open API
  → response (broker_order_id, status)

[Execution — unchanged]
backend/worker/runner.py (single static credential)
  → OrderStateMachine → OrderFillPoller → PositionTracker/Reconciler
  → backend/database/models.py tables
```

The one structural change vs. today is inserting a **thin service** between the router and the broker SDK. It does **not** adopt `OrderStateMachine`/`OrderFillPoller`; the Execution-Layer bypass stays **deliberate**. "Not reusing the state machine" means QT accepts it has no async lifecycle — a conscious scope choice, not an accidental omission.

---

## 10. Migration Plan

Incremental and additive; nothing in `backend/execution/*` is touched.

1. **P0-03 first — credential isolation (the rollout gate).** Replace `_load_kis`'s `os.environ` mutation with a request-scoped credential→client factory. No caller-visible behavior change; closes the active concurrency bug before any further QT hardening builds on it. Highest priority precisely because PR #135 declared it a rollout gate, not a backlog item.
2. **P0-04 — persistence (crash-safe).** Add `quick_trade_orders` via a **new Alembic migration** (not `create_all`, to avoid the non-altering-provisioner conflict in §5). Add the model with the tenant-ownership invariant (§8), write a **pending row and reserve the unique idempotency key *before* the broker call**, finalize to `submitted`/`failed`/`unknown` after, and extend `get_history` (or add a QT-scoped history path) to surface them — the inner-join limitation (§8) makes this explicit work. **Not production-complete without the reserve-before-submit guarantee**: doing the broker call first risks a duplicate on retry and an order missing from history if the process dies between submit and persist.
3. **P0-05 — risk gate + idempotency enforcement.** Wrap `RiskManager` as a pre-submit gate and enforce the per-user idempotency reservation from P0-04 (a retry reuses the reservation, never double-submits).

Sequencing rule: the credential fix (P0-03) lands **before** persistence and risk, so the hardened path is never built on top of the unsafe injection mechanism.

---

## 11. Remaining Risks

- **`os.environ` credential bleed is live until P0-03.** The rollout gate from PR #135 §1 remains open in current code; multi-credential concurrent use is unsafe until the request-scoped factory ships.
- **Dual `trades` definition (§5).** `UNVERIFIED` whether `api/` and `backend/` share the physical `trading` DB. If they do, the api `Trade` ORM and the Alembic `trades` schema disagree on columns and one side can fail at query time — independent of Quick Trade, but it must be resolved before anyone assumes `trades` is a reliable QT persistence target (the dedicated-table choice avoids depending on the answer).
- **No async fill confirmation.** QT records a **submission outcome** (`submitted`/`failed`/`unknown`) and never learns the real fill outcome; `submitted` must never be read as "filled." Accepted scope limit, not a bug to fix in P0 — but the `unknown` outcome and the reserve-before-submit ordering (§9/§10) are required so an ambiguous broker result is recoverable rather than silently lost or duplicated.
- **Stale-price closePosition.** Client must supply a live limit price. Today there is **no** server-side guard that the price is reasonable; §6 makes a validation boundary (positive/precision/tick/exposure/scope) a required part of the final contract, but until it is built the risk is live. Note validation can reject an unreasonable price but cannot *choose* a correct one — that remains the client's responsibility.
- **`RiskManager` currently unwired on both stacks.** Until P0-05, QT places orders with zero risk gating; the wrap is reuse, but the wiring is genuinely new surface.

---

## Sources

Every field name, line number, and behavior claim above is drawn from a fresh read of `api/routers/quick_trade.py`, `api/models.py`, `api/database.py`, `backend/database/models.py`, `alembic/versions/1aad3f2df8d9_initial_schema.py`, `alembic.ini`, and the merged `docs/P0_QUICK_TRADE_EXECUTION_DECISION.md` / `docs/P5_ORDER_LIFECYCLE_AUDIT.md`. The dedicated-table persistence decision (§8) was explicitly confirmed before writing; the `strategy_id` NOT-NULL correction was verified against `api/models.py:74` and Alembic `1aad3f2df8d9`.
