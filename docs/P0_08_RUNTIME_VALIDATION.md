# P0-08: Quick Trade Runtime Hardening Validation

**Date:** 2026-07-31
**Baseline:** `claude/p0-06-scope-audit-b3p3ps` @ `35a2b21` (P0-07C close-position hardening), base `main` @ `74c6b4a`
**Contract documents:** `docs/P0_06_SCOPE_AUDIT.md`, `docs/P0_07_CLOSE_POSITION_AUDIT.md`, `docs/P0_07_CLOSE_POSITION_PLAN.md`, `docs/P0_07_IMPLEMENTATION.md`
**Method:** code-level invariant verification (file:line evidence) + executed regression suites. One runtime-correctness defect was found and fixed under strict TDD; nothing else was modified.

---

## 1. Validation Matrix (18 scenarios × 9 invariants)

Legend — **✓** holds, verified; **✗** violated; **N/A** structurally inapplicable to the Quick Trade domain; **—** not exercised by this scenario. Invariants: I1 durable reservation · I2 idempotency key · I3 RiskManager exactly-once · I4 reservation precedes submit · I5 reconciliation converges · I6 runtime==broker==DB · I7 duplicate/retry tolerance · I8 callback-loss repair · I9 no orphan reservation.

| # | Scenario | I1 | I2 | I3 | I4 | I5 | I6 | I7 | I8 | I9 | Evidence (test or code) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Normal Buy | ✓ | ✓ | ✓ | ✓ | — | ✓ | — | N/A | ✓ | `test_quick_trade_persistence.py::test_first_request_reserves_and_submits`, `::test_broker_submit_only_after_commit` |
| 2 | Normal Sell | ✓ | ✓ | ✓ | ✓ | — | ✓ | — | N/A | ✓ | **new** `test_p0_08_runtime_validation.py::test_normal_sell_us_reserves_and_submits`, `::test_normal_sell_kr_reserves_and_submits` |
| 3 | closePosition | ✓ | ✓ | ✓ | ✓ | — | ✓ | — | N/A | ✓ | `test_quick_trade_close_position.py` (19 tests / 24 cases) |
| 4 | Duplicate Buy | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ | N/A | ✓ | `persistence::test_duplicate_key_returns_existing_without_recalling_broker`, `::test_http_double_submit_same_params_dedupes_at_router` |
| 5 | Duplicate Sell | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ | N/A | ✓ | **new** `::test_duplicate_sell_collapses_to_one_reservation_and_one_broker_call` |
| 6 | Duplicate closePosition | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ | N/A | ✓ | `close_position::test_duplicate_close_request_submits_to_broker_once`, `::test_explicit_idempotency_key_is_honoured` |
| 7 | Reservation Retry | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | N/A | ✓ | **new** `::test_retry_while_reservation_still_reserved_does_not_resubmit`; `persistence::test_reconcile_reserved_*` |
| 8 | Broker Reject | ✓ | ✓ | ✓ | ✓ | — | ✓ | — | N/A | ✓ | `persistence::test_broker_rejection_persists_rejected`; `close_position::test_broker_rejection_is_reported_not_masked` |
| 9 | Broker Timeout | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✓ | N/A | ✗ | `persistence::test_broker_timeout_keeps_reserved` — stays RESERVED; convergence depends on a **startup-only** sweep (§5 R-1) |
| 10 | Lost Callback | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | No poller/callback exists in `api/` (§3, I8). Owned by `backend/execution/order_poller.py`; covered by `tests/integration/test_runtime_reconciliation.py::TestCallbackLost` |
| 11 | Reconciliation Repair | ✓ | ✓ | — | ✓ | ✗ | ✗ | ✓ | N/A | ✗ | `test_quick_trade_recovery.py` (13 tests): MATCH→SUBMITTED, ABSENT→FAILED converge; **SKIP is a permanent no-op** (§5 R-1) |
| 12 | Worker Restart | ✓ | ✓ | — | ✓ | ✓ | ✓ | ✓ | N/A | ✓ | `persistence::test_reservation_durable_across_new_session`; `recovery::test_recover_on_startup_*`. QT has an API-startup sweep only; the worker itself is `backend/worker` (separate domain) |
| 13 | Redis Restart | ✓ | ✓ | ✓ | ✓ | — | ✓ | ✓ | N/A | ✓ | Redis-down fails **closed**: `risk_gate::test_risk_error_fails_closed`, `::test_http_risk_manager_raises_fails_closed`, `::test_risk_manager_redis_client_has_socket_timeout`. Recovery-after-restart untested in QT (§7 RR-3) |
| 14 | Database Restart | ✓ | ✓ | ✓ | ✓ | — | ✗ | ✓ | N/A | ✗ | `persistence::test_boundary_a_crash_before_commit_no_broker_call`; `recovery::test_recover_on_startup_swallows_errors`. A commit failure *after* a successful broker call leaves RESERVED (§7 RR-4) |
| 15 | Pending Reservation Recovery | ✓ | ✓ | — | ✓ | ✗ | ✗ | ✓ | N/A | ✗ | `recovery::test_match_adopts_submitted`, `::test_absent_marks_failed`, `::test_inquiry_error_skips_leaves_reserved`, `::test_ambiguous_multi_match_skips`, `::test_grace_window_skips_recent_reservation` |
| 16 | Partial Fill | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | QT has no fill state (§3). `backend/execution/tests/test_order_poller.py::TestPartialFill` |
| 17 | Multiple Partial Fill | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | `backend/execution/tests/test_order_poller.py::TestMultiplePartialFills` |
| 18 | Cancel after Partial Fill | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | No cancel endpoint in `api/`. `backend/execution/tests/test_order_poller.py::TestCancelAfterPartialFill` |

Scenarios 10 and 16–18 are **not deficiencies of this work**: the QT order model has exactly five states (`reserved/submitted/rejected/failed/blocked`, `api/models.py:11-27`) with `submitted` terminal and no `filled_qty` column. QT records *submission durability*, not order lifecycle — that is the P0-02 SEPARATE decision, and the lifecycle domain (`backend/execution/*`) has its own coverage.

## 2. Runtime Trace

```text
Frontend  mobile/src/views/quick-trade/index.vue
    ↓
Compat    api/compat.py:422 — place-order/balance/position/history remapped;
          close-position deliberately EXCLUDED (:387-391)
    ↓
QT API    api/routers/quick_trade.py
          place-order  :204  auth → _get_cred(:212) → [NEW P0-08 guard :221-231]
          close-pos    :284  auth → _get_cred(:300) → live holdings(:312) → live quote(:334)
    ↓
Reservation   quick_trade_service.py:146-162  INSERT status=RESERVED → db.commit()  ← DURABLE
    ↓
Idempotency   key = header or derive_idempotency_key (:234 / :356);
              DB UniqueConstraint(user_id, idempotency_key) api/models.py:180-182;
              IntegrityError → rollback → re-query → return existing (:163-178)
    ↓
RiskManager   quick_trade_service.py:186 — after commit, before submit;
              RiskDenied → QT_BLOCKED (:187-192); any other error → QT_BLOCKED (:193-202)
    ↓
Broker submit quick_trade_service.py:206 — the single call site;
              RuntimeError → QT_REJECTED (:207-212); network/timeout → stays RESERVED (:213-220)
    ↓
Polling       ✗ DOES NOT EXIST in the QT domain (see §3 I8)
    ↓
Reconciliation api/services/quick_trade_recovery.py — startup sweep only
              (api/main.py:101-108); MATCH→SUBMITTED, ABSENT→FAILED, ambiguous→SKIP
    ↓
Position update ✗ QT never mutates positions (backend/execution/position_tracker.py owns that)
    ↓
Audit/Persistence  quick_trade_orders row per request incl. blocked/rejected/failed outcomes
```

## 3. Invariant Verification

| # | Invariant | Verdict | Evidence |
|---|---|---|---|
| 1 | Durable reservation exists | **HOLDS** | `quick_trade_service.py:162` commit precedes `:206` submit; no branch reaches `:206` bypassing `:162` (exhaustive check of returns at `:175,177,178,192,202,212,220`). Close-position performs read-only balance/quote GETs before reserving — no order is placed, so the submit invariant is intact. |
| 2 | Idempotency key exists | **HOLDS** | `:234`/`:356` `key = header or derive_idempotency_key(...)` (total function); `idempotency_key` NOT NULL + unique (`api/models.py:180-187`); required keyword arg (`:124`). Best-effort caveat: the derived 10s bucket (`:110`) can split two clicks across a boundary. |
| 3 | RiskManager exactly once | **HOLDS** | Single call site `:186`; required arg; both routers bind the real `RiskManager` (`:210`, `:290`). Correctly **not** re-run on the duplicate short-circuit (returns at `:178`, before `:186`) — that path also never submits, so there is nothing to gate. |
| 4 | Reservation precedes submit | **HOLDS** | Line order `:146→:160→:162 (commit) →:186 (risk) →:206 (submit)`; pinned by `persistence::test_broker_submit_only_after_commit`, which reads the RESERVED row from a *separate session* inside `broker_submit`. |
| 5 | Reconciliation converges | **VIOLATED (bounded)** | MATCH/ABSENT converge (`recovery:174-179`), but ambiguous/inquiry-error/credential-failure → `_SKIP` leaves the row RESERVED with **no retry budget or escalation** (`recovery:180-182`), and the only trigger is process startup (`api/main.py:106`). Each sweep is a bounded single pass — it does not spin — but a long-lived process never re-sweeps. |
| 6 | runtime == broker == DB | **VIOLATED (partial)** | Reconciliation writes back only `broker_order_id` + status, and only for RESERVED rows (`quick_trade_service.py:241-249`; the broker's own status string is destructured and discarded). A `SUBMITTED` row is never re-verified, so a later broker-side cancel/reject is never reflected. Also `:222-223` transitions to SUBMITTED with an **unvalidated, possibly empty** `broker_order_id`. |
| 7 | Duplicate / retry tolerance | **HOLDS** | commit → `IntegrityError` → `rollback()` → re-query → return existing, all before risk and broker (`:160-178`). Non-idempotency `IntegrityError` (e.g. FK) is correctly re-raised, not masked (`:174-175`). Hash mismatch → `IdempotencyConflict` (`:176-177`). Newly pinned for a still-RESERVED winner by `test_p0_08::test_retry_while_reservation_still_reserved_does_not_resubmit`. |
| 8 | Callback loss / repair | **NOT APPLICABLE** | Repo-wide grep over `api/` for poller/fill/callback/websocket/scheduler yields only docstrings disclaiming the coupling (`quick_trade_service.py:21,239`) and read-only `Trade.filled_at` renderings. Owned by `backend/execution/order_poller.py`, `order_events.py`, `reconciler.py`. |
| 9 | No orphan reservation | **VIOLATED (bounded)** | A row stays RESERVED indefinitely when: the sweep is disabled (`QT_RECOVERY_ON_STARTUP=false`), the process never restarts, classification is ambiguous (≥2 same side+qty broker rows), the broker inquiry keeps raising, the credential can no longer be loaded, or the row is beyond the 200-row sweep limit. Fail-safe (never guesses, never blind-retries) but not live. |

**No broker submission bypasses `reserve_and_submit`** — verified exhaustively: `KISOrders` is instantiated only at `api/routers/quick_trade.py:63`, and all six order calls (`:245,246,248,249,365,366`) live inside a `broker_submit` closure passed to the service. No cancel/modify endpoint exists.

## 4. Regression Results

| Suite | Before (35a2b21) | After |
|---|---|---|
| `api/tests` (full QT + compat + auth) | 204 passed, 3 skipped | **215 passed, 3 skipped** |
| `api/tests/test_p0_08_runtime_validation.py` (new) | — (6 failing red) | **11 passed** |
| `tests/` excluding `tests/postgres` (execution layer) | 132 passed | **132 passed** |
| `pytest backend tests` (CI-equivalent) | requires Postgres | skipped locally — see limitation below |

3 skips are the Postgres-only concurrency proofs (`persistence::test_concurrent_same_key_single_reservation`, `risk_gate::test_concurrent_duplicate_single_eval_and_broker`, `recovery::test_concurrent_sweeps_reconcile_once`). **Environment limitation:** this sandbox has no Postgres, so `TEST_DATABASE_URL` is unset and `tests/postgres/conftest.py:22-27` skips those paths; they execute in the CI Postgres job. `/code-review` and `/verify` are not available as tools here — the equivalent full regression above was run instead, and CodeQL/Codacy/CodeRabbit run on the pull request.

## 5. Discovered Defects

| ID | Severity | Defect | Evidence |
|---|---|---|---|
| **D-1** | **High — fixed** | `place_order` performed **no positivity validation**. `qty=0`/negative and `price=0`/negative were reserved and submitted to the broker with a success response, and the KR path's `int(body.price)` truncated a sub-1 KRW quote to a **price-0 limit order** — the exact rule the contract states as "NEVER submit price = 0", enforced on the close path (P0-07C) but absent on buy/direct-sell. Reproduced: a `qty=0` request returned `Resp(code=1, data={'qty': 0, 'status': 'submitted'})` after a real broker call. | `api/routers/quick_trade.py:217,245-246` (pre-fix); red test run 6/6 failing |
| **R-1** | Medium — reported | Reconciliation does not converge for ambiguous cases: `_SKIP` has no retry budget, escalation, or alert, and the only trigger is process startup. Root cause of I5 + I9. | `quick_trade_recovery.py:110-114,180-182`; `api/main.py:101-108` |
| **R-2** | Medium — reported | `SUBMITTED` is written with an unvalidated `broker_order_id`; `extract_broker_order_id` returns `""` on an unexpected response shape, so an order with no id is reported as success and can never be reconciled or cancelled. | `quick_trade_service.py:222-223`; `backend/brokers/semantic_mapper.py:177-178,232-233` |
| **R-3** | Medium — reported | `MarketClosedError` is not a `RuntimeError` (`backend/data/calendar.py:63-68`), so a **conclusive** market-closed rejection lands in the generic handler and is recorded as *indeterminate* RESERVED instead of terminal. Self-heals only at the next startup sweep. | `quick_trade_service.py:207,213-220` |
| **R-4** | Medium — reported | The recovery sweep matches broker orders on `side + qty` only — no price, timestamp, or ordering — so an unrelated pre-existing broker order can be adopted into a reservation, or a legitimate one made ambiguous. | `quick_trade_recovery.py:99,106-114` |
| **R-5** | Low — reported | `KISClient.post` retries up to 3× on network errors *below* the service layer, so one reservation can produce multiple real order POSTs if KIS received a request that timed out. The service's "one `broker_submit()` call" guarantee holds; "one order at the broker" does not. | `kis_adapter/client.py:26,67-77` |
| **R-6** | Low — reported | `DEFAULT_GRACE_SECONDS` is bound at import time, so setting `QT_RECOVERY_GRACE_SECONDS` after import has no effect; and the 60s grace has no formal margin proof against the ~32s worst-case broker retry window. | `quick_trade_recovery.py:61,136` |

Only **D-1** is a runtime-correctness defect in the sense the contract defines (wrong order reaching the exchange). R-1…R-6 are liveness/fidelity limitations whose fixes would require modifying the reservation service, recovery service, broker adapter, or adding a scheduler — all explicitly forbidden for this task, so they are recorded, not patched.

## 6. Applied Fixes

**One fix, TDD, six lines of guard, one file.**

`api/routers/quick_trade.py:221-231` — reject before reserving:

```python
if qty <= 0:
    return Resp.err(f"qty must be greater than 0 (got {body.qty})")
if body.price <= 0:
    return Resp.err(f"price must be greater than 0 (got {body.price})")
if market == "kr" and int(body.price) <= 0:
    return Resp.err(f"KR price would truncate to 0 (got {body.price})")
```

Placed before the `req` dict is built, so a rejected request creates **no reservation row** and makes **no broker call** — the same pre-reservation rejection shape close-position uses (`:327-342`). No new dependency, no schema change, no behavioral change for valid orders (`test_kr_valid_price_still_submits` pins that 9500.0 KRW still submits).

**Untouched, as mandated:** `reserve_and_submit()` / reservation service, `RiskManager`, execution layer, `OrderStateMachine`, `PositionTracker`, reconciliation & recovery, persistence model, DB schema/Alembic, broker adapters, compat layer, pagination, frontend, architecture.

## 7. Remaining Risks

| ID | Risk | Impact |
|---|---|---|
| RR-1 | **Reconciliation is startup-only with no escalation** (R-1). A reservation left ambiguous stays RESERVED until a restart, and permanently ambiguous ones never resolve. | Orphan reservations accumulate silently in a long-lived process; no alert exists. Fixing needs a periodic sweep + retry budget = architecture change, out of P0-08 scope. |
| RR-2 | **`SUBMITTED` is never re-verified** (R-2, R-3). DB can claim `submitted` for an order the broker later cancelled/rejected, or with an empty `broker_order_id`. | `runtime == broker == DB` cannot be asserted for terminal rows. Requires a post-submit verification pass. |
| RR-3 | **Dependency *recovery* is untested in QT.** Redis-down and DB-down-at-startup fail closed correctly, but recovery after the dependency returns is only tested in `backend/execution`. | Low — the QT gate is stateless per request, but sticky-failure behavior is unproven. |
| RR-4 | **DB failure between a successful broker call and the final commit** leaves a RESERVED row for an order the broker accepted. | The sweep adopts it later (correct terminal state), but the end-to-end path has no test. |
| RR-5 | **Concurrency proofs are Postgres-gated.** The three tests proving single-reservation/single-broker-call under real concurrency skip on SQLite. | If a CI lane runs SQLite-only, that guarantee is unasserted there. The dedicated Postgres job does run them. |
| RR-6 | **No fill/position truth in QT** (by design). `submitted` never means filled; a resting limit order is invisible to the QT domain. | Unchanged from P0-04; documented DEFER. |
| RR-7 | **Broker-level retry amplification** (R-5) and **`side+qty`-only reconciliation matching** (R-4) can each produce a duplicate or mis-adopted broker order despite a single reservation. | The application guarantee remains "one durable reservation per key", not "exactly-once at the exchange" — as the service docstring already states. |

## 8. Assessment

**READY — for the Quick Trade submission path, with the recorded liveness limits.**

Met:
- Every invariant that governs *what reaches the exchange* holds: durable reservation before submit (I1, I4), always-present idempotency key (I2), fail-closed RiskManager exactly once (I3), and duplicate/retry collapse to a single reservation and a single broker call (I7).
- **Duplicate reservations: 0. Duplicate submissions: 0.** No broker submission path bypasses `reserve_and_submit` (exhaustively verified).
- All 18 scenarios are accounted for: 14 verified against the QT domain, 4 (10, 16–18) structurally N/A and owned by `backend/execution/*`, which has its own passing coverage.
- Regression green: 215 passed / 3 skipped (api), 132 passed (execution layer), 0 failures, no test weakened or deleted.
- The one runtime-correctness defect found (D-1) is fixed with a minimal, tested guard.

Not met — and why this is *not* NOT-READY:
- **I5, I6, I9 are violated in the liveness sense only** (R-1…R-4): an uncertain reservation stays RESERVED rather than being resolved eagerly, and a terminal row is never re-verified. Every one of these fails **safe** — the system never guesses, never blind-retries, and never submits on uncertainty. No orphan reservation can cause an unintended order; it can only cause a stale record and a delayed truth.
- Closing them requires touching the reservation service, recovery service, or adding a scheduler — all explicitly forbidden here. They are the natural content of a follow-up task (suggested **P0-09: reconciliation liveness** — periodic sweep, `_SKIP` retry budget with escalation, post-submit `SUBMITTED` verification, and `broker_order_id` validation at transition time).

**Recommendation:** ship the D-1 fix; schedule P0-09 for RR-1/RR-2 before any real-money enablement, since both bear directly on the "runtime state == broker state == database" guarantee that live trading depends on.
