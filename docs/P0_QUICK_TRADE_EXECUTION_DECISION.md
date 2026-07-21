# P0: Quick Trade — Execution Architecture Decision (INTEGRATE vs. SEPARATE)

> **Analysis and design decision only. No code was changed to produce this document.** No router, middleware, frontend, schema, or business-logic file was modified. `COMMANDS-QUICK-REF.md` (referenced in the task brief) does not exist anywhere in this repository — this audit instead uses the same source-grounded, file:line-cited methodology as `docs/P5_ORDER_LIFECYCLE_AUDIT.md`. Items that could not be definitively confirmed from source are explicitly marked `UNVERIFIED`.

**Date:** 2026-07-21
**Precondition:** `docs/P5_ORDER_LIFECYCLE_AUDIT.md` (merged, PR #134) established that `api/main.py`+`api/routers/quick_trade.py` and `backend/execution/*` are two disconnected order-execution stacks and recommended P0 resolve which model quick-trade should adopt. This document is that P0 decision.

---

## 1. Executive Summary

**Decision: SEPARATE.** Quick Trade should remain — and be deliberately hardened as — its own lightweight order-lifecycle domain, not merged into `backend/execution/*`.

New evidence gathered for this decision, beyond what the prior audit established, makes this a stronger and more concrete conclusion than "the two stacks happen to be disconnected":

- `backend/execution/*`'s data model (`backend/database/models.py`) has **zero `credential_id`/`user_id`/`account_id` column anywhere** in its `Order`, `Fill`, `Position`, or `StrategyRun` tables. It is architecturally a **single-account-per-process** system: `backend/worker/runner.py` reads one static broker credential from process environment variables (`KIS_APP_KEY` etc., `backend/brokers/kis.py:47`), not from a database. Quick Trade, by contrast, is inherently multi-user and multi-credential (`api/models.py`'s `Credential` table, `user_id`-scoped). **Integrating would not mean "wiring a call" — it would mean re-architecting `backend/execution/*`'s core multi-tenancy assumption**, a foundational schema and process-model change to a system independently confirmed as **currently active** in production (not dormant), not a scoped adapter task.
- `docs/SYSTEMS_AUDIT_RISK_ANALYSIS.md` and `docs/P4_OPERATIONAL_READINESS.md` (both pre-existing, read in full for this audit) independently confirm `backend/worker/runner.py` is the live/active execution path today (paper-mode by default, one operator flag away from real trading) — this is a materially different risk posture than modifying dead code.
- A genuinely new, unrelated-but-relevant finding surfaced during this audit: `api/routers/quick_trade.py`'s `_load_kis` **mutates process-wide `os.environ["KIS_APP_KEY"]` etc. at request time** (`quick_trade.py:22-33`) to inject a per-user decrypted credential before constructing a broker client. **This is not a future-only concern — it is an active, currently-reachable concurrency bug in the code as it runs today**: under concurrent requests from different users in a multi-worker/async server, one request's credential mutation can leak into another in-flight request's broker call before that request constructs its own client, producing a real cross-request credential-bleed under ordinary production load. This is independent of the INTEGRATE/SEPARATE question and **requires urgent remediation on its own timeline**, not deferral to whatever pace P0-02's broader architectural redesign proceeds at (see §10). It also further confirms `quick_trade.py` and `backend/execution/*`'s env-var-based single-account model **cannot safely share a process or a credential-provisioning mechanism at all**, reinforcing that they are different architectures by design, not just by historical accident.

**This revises `docs/P5_ORDER_LIFECYCLE_AUDIT.md`'s final-owner assignments** for `closePosition`, `Cancel`, `Order Status`, and `Fill Persistence` (all previously assigned to `Execution Layer`) — see §9 for the explicit reconciliation.

---

## 2. Quick Trade Runtime Trace

```text
Frontend (frontend/src/views/quick-trade/index.vue)
  → POST/GET /api/quick-trade/{balance,position,place-order,close-position,history}
  → CompatMiddleware (api/compat.py — P5-02/P5-03B DTO reshaping only)
  → api/routers/quick_trade.py
      _load_kis(cred): decrypts api/models.py Credential row (per-user),
      WRITES into process os.environ["KIS_APP_KEY"/"KIS_APP_SECRET"/
      "KIS_ACCOUNT_NO"/"KIS_HTS_ID"/"KIS_ENV"] at REQUEST TIME (quick_trade.py:22-33)
  → kis_adapter.KISOrders/KISPortfolio (constructed fresh per request from the
      just-mutated env vars)
  → KIS Open API (synchronous HTTP call)
  → response returned immediately; "order_id" = broker's own ODNO
      (mapper.extract_broker_order_id(result), quick_trade.py:154/192,
      backend/brokers/semantic_mapper.py:177-178/232-233) — no internal ID
```

**Verified responsibilities and gaps** (each independently confirmed by direct file reads):

- **Order creation/ID management**: no internal ID is ever generated. `order_id` in the response is purely the broker's returned order number. No `Order` row is written anywhere (`quick_trade.py:149-150`, explicit comment: *"skip for simplicity"*).
- **State management**: none. No in-memory dict, cache, or DB row tracks an order after the response returns. The response's `"status": "submitted"` (`quick_trade.py:159`) is a hardcoded literal, not derived from any tracked state.
- **Fill / partial fill / cancel / failure / timeout**: `place_order` calls the broker exactly once and returns synchronously. There is no mechanism to detect a partial fill — **the caller has no way to learn whether their order fully filled, partially filled, or didn't fill at all** through this endpoint. No cancel endpoint exists (confirmed by the prior audit, re-confirmed here). No order-lifecycle timeout/retry orchestration exists beyond whatever raw HTTP client timeout `kis_adapter` uses internally (`UNVERIFIED` exact value — not traced in this pass).
- **Position reflection**: `GET /position` performs a live broker read on each call; nothing is tracked or persisted between calls.
- **Fill persistence**: zero — confirmed across three independent audit passes now (P5-03A, P5-03B, P5 Order Lifecycle Audit, and this one).
- **Restart recovery**: not applicable by design — each request is fully independent and stateless; there is no daemon process holding state that could need recovery.
- **Reconciliation**: none — there is nothing tracked to reconcile against the broker.
- **Risk / kill-switch**: zero. `strategy/risk.py`'s `RiskManager` and `backend/risk/kill_switch.py`'s `KillSwitch` are both unreachable from this file (confirmed by grep, zero imports).

**Quick Trade's actual, honest responsibility scope**: a **stateless, per-request DTO-translating pass-through to the broker**, nothing more. It is not an order-lifecycle system at all today — it has no lifecycle to speak of.

---

## 3. Execution Layer Runtime Trace

```text
backend/worker/runner.py:main() [if __name__ == "__main__", line 875-876]
  → validates KIS_ENV/ENABLE_LIVE_TRADING consistency (829-841)
  → get_kis_broker() (backend/brokers/kis.py:25) reads STATIC process env vars
      (KIS_APP_KEY/SECRET/ACCOUNT_NO) — ONE hardcoded broker account for the
      entire process's lifetime, NOT selected from any database
  → StrategyWorker() starts: OrderFillPoller, PersistentLossTracker,
      WorkerHeartbeat, PositionReconciler, StartupRecovery (848-865),
      scheduler (867-869), then blocks in worker.run() (872)

backend/api/server.py (Flask, kis-api service)
  → POST /api/admin/reconcile → backend/execution/reconciler.py:PositionReconciler
  → POST /api/admin/flatten (server.py:329) → backend/worker/emergency.py:
      EmergencyFlattenManager.flatten_all — SUBMITS REAL closing orders unless
      dry_run=True; dry_run becomes False precisely when ENABLE_LIVE_TRADING=true
      (server.py:340)
```

**Strict distinction: "class exists" vs. "wired into a live/paper runtime that actually runs" — verified per component:**

| Component | Class exists? | Wired into a runtime that actually executes? |
|---|---|---|
| `OrderStateMachine` | Yes (`order_machine.py`) | **Yes** — used by `runner.py`'s worker/poller pipeline |
| `OrderFillPoller` | Yes | **Yes** — started at `runner.py` worker startup |
| `PositionTracker` | Yes | **Yes** — invoked from the fill callback in `runner.py` |
| `PositionReconciler` | Yes | **Yes** — periodic + startup + manual-trigger, confirmed active |
| `IdempotencyStore` | Yes | **Yes** — used within the poller/execution pipeline |
| `RiskManager` (`strategy/risk.py`) | Yes | **No** — imported only by the disabled legacy `bot/main.py`; zero references anywhere in `backend/execution/*` (re-confirmed this pass) |
| `KillSwitch` (`backend/risk/kill_switch.py`) | Yes, 700 lines, fully tested | **No** — `KillSwitch(` instantiated only in test files and `backend/testing/paper_harness.py` (itself pytest-only, see below); its own docstring states production wiring is "a deliberate follow-up task"; two independent existing docs (`SYSTEMS_AUDIT_RISK_ANALYSIS.md`, `P4_OPERATIONAL_READINESS.md`) corroborate it is dormant |
| `EmergencyFlattenManager` | Yes | **Partially** — wired to a real HTTP route (`POST /api/admin/flatten`), but structurally paper-only unless an operator sets `ENABLE_LIVE_TRADING=true` |
| `backend/testing/paper_harness.py` | Yes | **No, not a running service** — pytest-only fixture (`PaperHarness`), zero usages outside `backend/testing/tests/` and `tests/`; no CLI entrypoint, no docker-compose service; its own docstring confirms it is a test driver |

**Is `backend/worker/runner.py` itself actually live?** Yes, per the repository's own pre-existing operational docs, not just this audit's inference: `docs/SYSTEMS_AUDIT_RISK_ANALYSIS.md:16-19` explicitly labels it "**ACTIVE**" (alongside `api/main.py`, also active, separately) and `docs/P4_OPERATIONAL_READINESS.md:19-22,61` states *"only one is live... Strategy | **WIRED**, not aspirational."* `docker-compose.yml`'s `kis-worker`/`kis-api` services are defined, enabled (not commented out, unlike the legacy `kis-bot`), reference real KIS credential env vars, and default to `KIS_ENV=paper`/`ENABLE_LIVE_TRADING=false` — i.e., they run continuously in **shadow/paper mode by design**, not idle. `UNVERIFIED`: whether these services are literally running right now in any specific deployed environment (requires runtime observation, not static reading).

**Execution Layer's actual, honest scope**: a genuinely active, single-broker-account, continuously-running automated-strategy execution engine with real state tracking, reconciliation, and (unwired but built) risk/kill-switch gates. It has **no concept of multiple users or multiple credentials** anywhere in its schema.

---

## 4. Side-by-Side Contract Matrix

| # | Item | Quick Trade | Execution Layer | 계약 분류 | 비고 |
|---|---|---|---|---|---|
| 1 | Order ID | Broker's own ODNO only, no internal ID | DB PK (autoincrement `Integer`) + `broker_order_id` column + unique `idempotency_key` | **의미가 충돌함** | Even if adapted, Execution's `Order` table has no `credential_id` to attribute an order to a specific quick-trade user |
| 2 | Order State | None — ephemeral, unchecked hardcoded string | `OrderStateMachine`, explicit enum, enforced legal transitions, terminal states | **Quick Trade에 없음** | |
| 3 | Submit | Synchronous single broker call, no idempotency check | `IdempotencyStore` fingerprint+lock → broker call → `OrderStateMachine.submit()` → persisted row | **Execution Layer 수정 필요** | The idempotency piece (Redis fingerprint, schema-independent) is adapter-connectable on its own; full state-machine registration needs the credential-schema gap closed first |
| 4 | Cancel | Does not exist (no client-facing endpoint) | No client-facing endpoint either, but internal broker-cancel mechanics exist (`order_poller.py` timeout, `reconciler.py` lost-order) | **Quick Trade에 없음** | Execution's internal cancel mechanics are structurally reusable once a client-facing route is built, on either side |
| 5 | Reject | Synchronous error string (`Resp.err`), no state impact | Persisted `REJECTED` terminal state via `OrderStateMachine` | **Execution Layer 수정 필요** | Same credential-schema gap as Submit |
| 6 | Timeout | None (`UNVERIFIED` exact HTTP client timeout value) | Explicit 30-minute poll timeout → auto-cancel (`order_poller.py`) | **Quick Trade에 없음** | |
| 7 | Partial Fill | Not detectable by the caller at all | Incremental watermark-based tracking, `PARTIAL_FILLED` state | **Quick Trade에 없음** | |
| 8 | Full Fill | "submitted" returned unconditionally; fill never confirmed | Confirmed `FILLED` transition via poller/reconciler | **Quick Trade에 없음** | |
| 9 | Fill Persistence | Zero — confirmed across 3 independent prior audits | `Fill` table exists (no unique constraint — a pre-existing gap of its own) | **Quick Trade에 없음** | |
| 10 | Position Update | Live broker read per `GET /position` call, nothing tracked between calls | `PositionTracker` (in-memory) + `Position` DB table, broker-priority reconciliation | **Quick Trade에 없음** | |
| 11 | P&L | Not computed anywhere for quick-trade activity | Unrealized: computed on-read; Realized: computed at fill-time for loss-tracking, not persisted to any row either | **Quick Trade에 없음** | Execution's own P&L persistence is itself incomplete — not a fully solved contract to inherit |
| 12 | Risk Gate | Zero | `RiskManager` class exists, Redis-halt-flag + env-var thresholds, **no SQL schema dependency** | **Adapter로 연결 가능** | Genuinely callable directly from `quick_trade.py` with no Execution Layer schema change — but currently unwired on *both* sides (only the disabled legacy bot uses it today) |
| 13 | Kill Switch | Zero | `KillSwitch` fully built/tested, zero production call sites anywhere including inside `backend/execution/*` itself | **Quick Trade에 없음** | `check_order()`'s exact DB-coupling depth is `UNVERIFIED` in this pass; may be more adapter-connectable than assumed, but not confirmed |
| 14 | Reconciliation | None — nothing tracked to reconcile | `PositionReconciler`, confirmed active, broker-priority | **Quick Trade에 없음** | Scoped only to Execution's single hardcoded account |
| 15 | Restart Recovery | Not applicable by design (fully stateless per-request) | `StartupRecovery` restores worker state on process restart | **Quick Trade에 없음** | Structural difference, not a missing feature in the same sense |
| 16 | Emergency Flatten | Zero | `EmergencyFlattenManager.flatten_all`, real order submission, wired to `POST /api/admin/flatten` (paper-gated) | **Quick Trade에 없음** | Scoped only to the worker's single hardcoded account; no concept of "flatten this specific user's quick-trade positions" |

---

## 5. KIS Equity Domain Compatibility Analysis

(Restating and extending §8 of `docs/P5_ORDER_LIFECYCLE_AUDIT.md`, not re-deriving from scratch — those findings still hold and were spot-checked, not contradicted, in this pass.)

- **`closePosition`'s exact meaning**: a plain reverse-direction broker `sell` call (`orders.sell_us`/`sell_kr`), not a native "close" instruction — KIS has no such concept in this integration.
- **Qty calculation authority**: nobody today — `ClosePositionRequest.qty` is a required client field the frontend never sends.
- **Price decision authority**: nobody today — same for `price`; the only value derivable from `GET /position` is a stale average purchase price (`pchs_avg_pric`), and KIS's `sell_us`/`sell_kr` submit whatever price is given as an actual **limit-order price** (`ORD_DVSN: "00"`, hardcoded, confirmed both in the prior audit and unchanged in this pass).
- **Market vs. limit semantics**: always limit at the broker call level, regardless of the frontend's `order_type` selection (silently dropped, no field on `PlaceOrderRequest`).
- **Position direction / leverage**: `PlaceOrderRequest`/`ClosePositionRequest` have no leverage or margin concept at all — KIS equities here are cash-settled, unleveraged. The frontend's `leverage` field (meaningful only for its inherited crypto spot/swap UI) has zero backend representation.
- **Compatibility verdict, unchanged from the prior audit**: the frontend and backend are conceptually mismatched (crypto-exchange UI vs. KIS-equities-only backend), independent of the INTEGRATE/SEPARATE question — this mismatch exists regardless of which execution architecture eventually backs quick-trade, and its owner remains `UI` (see §9).

---

## 6. INTEGRATE vs. SEPARATE Comparison

| Evaluation axis | A안 — INTEGRATE | B안 — SEPARATE |
|---|---|---|
| 변경 파일/모듈 범위 | Large: `backend/database/models.py` (schema migration — add `credential_id`/`user_id` to `Order`/`Fill`/`Position`/`StrategyRun`), multiple `backend/execution/*` files (credential-scoping throughout), `backend/worker/runner.py` (single-account → multi-account process model), `api/routers/quick_trade.py` | **Not small** — per §9's Responsibility Boundary Redefinition, this introduces a substantial new domain boundary, not a single-file change: `api/routers/quick_trade.py` plus new persistence (Fill/Trade write path), idempotency checking, `RiskManager` risk-gating, and client-facing lifecycle endpoints (Cancel, Order Status, `closePosition` qty/price sourcing) — collectively a new "Order Lifecycle Domain." It reuses `api/models.py`'s existing `Trade` table and multi-user `Credential` schema as a starting point, but does not inherit a pre-built lifecycle system the way INTEGRATE would reuse `backend/execution/*`'s |
| 재사용 코드 | Substantial and genuine: `OrderStateMachine`, `OrderFillPoller`, `PositionTracker`, `PositionReconciler`, `IdempotencyStore`, `KillSwitch` (once wired) | `RiskManager` (directly, no adaptation needed — schema-independent), the *design pattern* of `IdempotencyStore`/`EmergencyFlattenManager`, not the classes themselves (both are scoped to the single-account model) |
| 신규 필요 코드 | A multi-tenancy layer across `backend/execution/*`'s foundational assumptions — not a small addition, a re-architecture | A lightweight, quick-trade-scoped order-tracking/risk-gate/idempotency layer within `api/`'s existing multi-user schema — moderate, well-scoped |
| 데이터 모델 영향 | **High** — schema migration on tables backing a currently-active production daemon | **Low-Medium** — extends `api/models.py`, already designed for multi-user/multi-credential |
| 런타임 위험 | **High** — `backend/worker/runner.py` is confirmed live (shadow/paper mode); any change here risks the automated-strategy execution path, not just quick-trade | **Low** — isolated to the `api/` FastAPI app; zero risk to the already-running execution stack |
| 주문 상태 일관성 위험 | Medium-High — quick-trade orders and strategy-run orders would share one `Order`/state-machine namespace, requiring careful scoping to avoid cross-contamination | Low — quick-trade stays in its own namespace, no collision surface |
| Fill 중복 처리 위험 | Medium — the `Fill` table already has 3 independent write paths with no unique constraint (a pre-existing gap, §7 of the prior audit); adding quick-trade as a 4th source worsens this before it's fixed | Low — a new, single, isolated write path into a table quick-trade would own exclusively |
| Restart/Recovery 위험 | Medium — `StartupRecovery` is built for one account; multi-account recovery is new, untested surface | Low/N/A — quick-trade is stateless by design, no daemon to recover |
| 향후 유지보수 비용 | Higher upfront (large migration + re-architecture), potentially lower long-term *if* the multi-tenancy redesign succeeds — a real but unproven bet | Two systems to maintain long-term (a genuine, acknowledged cost), but each stays simpler in isolation, and the higher-risk automated-strategy engine remains untouched by lower-priority manual-trade work |

---

## 7. 최종 결정 (Final Decision)

# **SEPARATE**

Quick Trade will **not** be integrated into `backend/execution/*`. It will be scoped and hardened as its own, deliberately lightweight order-lifecycle domain.

---

## 8. 결정 근거 (Rationale)

1. **`backend/execution/*`'s data model structurally cannot represent multiple users or credentials today** — confirmed by full-schema reads of `backend/database/models.py`'s `Order`/`Fill`/`Position`/`StrategyRun` tables (zero `credential_id`/`user_id`/`account_id` columns anywhere) and confirmed by `backend/worker/runner.py`'s single static env-var-sourced broker account. "Integration" would mean redesigning this foundational assumption, not writing an adapter.
2. **`backend/execution/*` is a confirmed-active production system**, not dead code — two pre-existing operational documents in this repository (`SYSTEMS_AUDIT_RISK_ANALYSIS.md`, `P4_OPERATIONAL_READINESS.md`) independently state this. Migrating its schema or process model to accommodate quick-trade carries real risk to the automated-strategy execution path this system already serves, for the benefit of a lower-volume, manual-only feature.
3. **The two systems' process models are fundamentally different shapes**: Quick Trade is a stateless, per-HTTP-request, multi-tenant adapter; `backend/execution/*` is a long-lived, single-tenant daemon built around one persistent broker connection. This is not an accidental gap to close — it is two different, both-legitimate architectures for two different problems (manual one-off orders vs. continuous automated strategy execution).
4. This newly-surfaced credential-model incompatibility is a stronger and more concrete argument than the KIS-equity-vs-crypto-UI semantic mismatch alone (§5, already established by the prior audit) — together they make SEPARATE the evidence-driven conclusion, not a default or assumed one.

---

## 9. 책임 경계 재정의 (Responsibility Boundary Redefinition)

| Component / capability | Category |
|---|---|
| `balance`/`position`/`history`/`place-order` DTO reshaping (already implemented, P5-02/P5-03B) | **P5 Compatibility Layer에 남길 것** |
| `RiskManager` wiring into `quick_trade.py`'s order placement | **Order Lifecycle Domain으로 새로 정의** (a new, quick-trade-scoped lightweight domain — reuses the existing `RiskManager` class directly, no schema change) |
| A lightweight idempotency check for quick-trade submission (double-click protection) | **Order Lifecycle Domain으로 새로 정의** |
| Fill/Trade persistence for quick-trade orders | **Order Lifecycle Domain으로 새로 정의** (extending `api/models.py`'s existing `Trade` table; `strategy_id`'s existing nullable design is only a starting point that permits a non-strategy-attributed row — it does not, on its own, establish user/credential-level uniqueness or multi-tenant isolation guarantees, which this new domain must still design explicitly, e.g. via `credential_id`/`user_id` scoping and appropriate constraints) |
| `closePosition` qty/price sourcing | **Order Lifecycle Domain으로 새로 정의** |
| Client-facing Cancel / Order Status for quick-trade orders | **Order Lifecycle Domain으로 새로 정의** |
| `OrderStateMachine`, `OrderFillPoller`, `PositionTracker`, `PositionReconciler`, `IdempotencyStore`, `KillSwitch`, `EmergencyFlattenManager` | **Execution Layer** (unchanged — continue serving only the strategy-automated-execution domain, not extended to quick-trade) |
| Quick Trade's crypto spot/swap UI concepts (leverage, market-order selector, spot/swap toggle) | **UI에서 제거/재설계** (unchanged from the prior audit — per `CLAUDE.md`'s own Stage 7 roadmap) |
| Order Detail / Order Status for **strategy-run** orders (the `backend/execution/*`-side gap, distinct from quick-trade) | **Execution Layer** (unchanged — this was never a quick-trade question; the prior audit's DB-vs-broker authority decision for strategy orders stands as-is) |

**Explicit reconciliation with `docs/P5_ORDER_LIFECYCLE_AUDIT.md`:** that document assigned `closePosition`, `Cancel`, `Order Status`, and `Fill Persistence` to **`Execution Layer`** as final owner (its §10). **This document revises those four assignments to `Order Lifecycle Domain`** (a new, quick-trade-scoped domain — not `backend/execution/*`), based on the credential-model and active-production-risk evidence gathered in this pass, which the prior audit did not have. `Order Detail` (for strategy-run orders) and the `UI` assignment for Quick Trade's conceptual mismatch are **unchanged** and remain consistent between both documents.

---

## 10. 다음 단일 작업 (Next Task)

**`P0-02 — Quick Trade Order Lifecycle Domain Design`**

Design (not implement) the new, quick-trade-scoped lightweight domain identified in §9: a `RiskManager`-gated, idempotency-checked, `Trade`-table-persisted order path for `api/routers/quick_trade.py`, explicitly independent of `backend/execution/*`'s schema and process model. This design pass should include the architectural fix for the `os.environ` credential-mutation pattern found in §1 (e.g. a per-request credential-scoped broker client instead of process-env mutation) — but that pattern is **already an active production concurrency risk today, not a problem this design creates or first discovers**, so it warrants urgent, near-term mitigation on its own timeline rather than waiting on this task's full design-and-implementation schedule.

---

## Final Review Summary

- **Quick Trade 실제 런타임 경로**: 완전히 상태를 갖지 않는(stateless) 요청 단위 어댑터 — 브로커 호출 1회, 응답 즉시 반환, 어떤 상태/체결/포지션도 추적·영속화하지 않음. `_load_kis`가 요청마다 프로세스 전역 `os.environ`을 변경하는 것은 "잠재적" 위험이 아니라 **현재 코드에서 실제로 도달 가능한 동시성 버그**임을 신규 확인 — SEPARATE 결정과 무관하게 긴급 대응이 필요하며, P0-02는 이를 포함한 아키텍처 차원의 근본 해결책임.
- **Execution Layer 실제 런타임 연결 상태**: `OrderStateMachine`/`OrderFillPoller`/`PositionTracker`/`PositionReconciler`/`IdempotencyStore`는 **실제로 활성화되어 운영 중**(기존 운영 문서 2건이 독립적으로 확인). `RiskManager`/`KillSwitch`는 클래스는 존재하나 **양쪽 스택 어디에도 배선되어 있지 않음**(완전히 dormant).
- **최종 선택**: `SEPARATE`
- **핵심 결정 근거**: `backend/execution/*`의 데이터 모델은 credential/user 개념이 전혀 없는 단일 계정 구조이며, 이는 현재 실제 운영 중인 시스템임. Quick Trade를 통합하려면 어댑터 작업이 아니라 실행 엔진의 근본적인 멀티테넌시 재설계가 필요하며, 이는 이미 가동 중인 자동 전략 실행 경로에 실질적 위험을 초래함.
- **다음 단일 작업**: `P0-02 — Quick Trade Order Lifecycle Domain Design`
