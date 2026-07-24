# P0-06: Quick Trade Scope Verification & Execution Guide — Scope Audit

**Date:** 2026-07-24
**Type:** Read-only audit (documentation only — zero code changes)
**Baseline:** `main` @ `edc921c` (merge of PR #140)
**Preconditions:** P0-02 (PR #136), P0-03 (PR #137), P0-04 (PR #138), P0-05 (PR #139), QT reconciliation (PR #140) — all merged.

---

## 1. P0-06 actual definition

**Classification: documentation / scope-definition gap.**

The string "P0-06" appears in exactly one committed file in this repository, and it belongs to a **different, older numbering scheme**:

| Scheme | Source | P0-06 meaning |
|---|---|---|
| **A — Deployment-hardening track** (2026-05-29) | `ROADMAP.md:98` | "Fix US order status lookup: remove `output[0]` fallback" in `backend/execution/order_poller.py` (AUDIT.md R-08/FM-08). Targets the single-account execution daemon, **not** Quick Trade. |
| **B — Quick Trade track** (2026-07-21~) | `docs/P0_QUICK_TRADE_EXECUTION_DECISION.md`, `docs/P0_QUICK_TRADE_DOMAIN_ISOLATION.md` | **Undefined.** The track's migration plan (`P0_QUICK_TRADE_DOMAIN_ISOLATION.md` §10) enumerates P0-03, P0-04, P0-05 and stops. No doc, commit message, or code comment defines a Quick-Trade P0-06. |

Additional findings:

- `COMMANDS-QUICK-REF.md` (named as a mandatory review source in the task brief) **does not exist** anywhere in the repository. This was already noted verbatim in `docs/P0_QUICK_TRADE_EXECUTION_DECISION.md:3`.
- No commit in history mentions "P0-06". PR #140's reconciliation work (`efc1941`, `a61216e`) was intentionally not labeled with a P0 ordinal.

**Conclusion:** In the Quick Trade track, "P0-06" can only mean what this task brief says it means — *scope verification and execution guide*. This document **is** the P0-06 deliverable. It closes the track's definition gap; it does not require, and must not include, code changes.

---

## 2. Current runtime path (verified end-to-end)

All components below were verified by direct code reading against `main` @ `edc921c`.

```
Frontend                mobile/src/views/quick-trade/index.vue
                        → quickTradeApi (mobile/src/api/index.js:537-572)
        ↓ POST /api/quick-trade/place-order
CompatMiddleware        api/compat.py (_ORDERS_PATH_CONFIG:392, wired api/main.py:75)
                        DTO-shape translation only: amount→qty, market_type→market,
                        response reshaping (balance/position/history aliases)
        ↓
Quick Trade API         api/routers/quick_trade.py:153 place_order
                        auth → _get_cred (credential ownership check, :68)
        ↓
Durable reservation +   api/services/quick_trade_service.py:118 reserve_and_submit
idempotency (P0-04)     INSERT quick_trade_orders(status=RESERVED) → db.commit()
                        BEFORE any broker call.
                        UniqueConstraint(user_id, idempotency_key) (api/models.py:181);
                        key = Idempotency-Key header or server-derived 10s
                        double-click bucket; param mismatch → IdempotencyConflict.
        ↓
RiskManager pre-submit  get_risk_gate (quick_trade.py:29-41), called after the
gate (P0-05)            reservation commit, before submit. Halted → QT_BLOCKED;
                        any gate error → QT_BLOCKED (fail-closed). Broker never called.
        ↓
KIS broker submit       broker_submit closure (quick_trade.py:188-198) — the ONLY
(P0-03 credentials)     broker call site. _load_kis (quick_trade.py:46) builds a
                        request-scoped KISClient from the decrypted per-user
                        Credential; no os.environ mutation.
                        Success → QT_SUBMITTED; broker rt_cd≠0 → QT_REJECTED;
                        network/timeout → stays QT_RESERVED (recoverable, no retry).
        ↓
Reserved-order          api/services/quick_trade_service.py:228 reconcile_reserved,
reconciliation (#140)   wired via recover_on_startup (quick_trade_recovery.py:197,
                        scheduled api/main.py:104-106 in a startup executor).
                        Sweeps RESERVED rows older than QT_RECOVERY_GRACE_SECONDS
                        (60s) with SELECT…FOR UPDATE SKIP LOCKED, queries KIS
                        read-only (date-bounded, a61216e):
                        MATCH → QT_SUBMITTED / ABSENT → QT_FAILED / ambiguous → skip.
```

**Caveat (accurate wording matters):** reconciliation runs as a **one-shot startup sweep only**. There is no periodic/scheduled reconciliation job. An order left indeterminate while the process keeps running is not reconciled until the next restart. This matches what PR #140 shipped; it is a scope note, not a defect claim.

---

## 3. Existing implemented pieces

| Piece | Status | Evidence |
|---|---|---|
| Durable reservation (reserve-before-submit) | **Implemented** | `quick_trade_service.py:118` — RESERVED row committed before `broker_submit`; `quick_trade_orders` table `api/models.py:164` |
| Idempotency (per-user, DB-enforced) | **Implemented** | `UniqueConstraint(user_id, idempotency_key)` `api/models.py:181`; `derive_idempotency_key` / `request_fingerprint` `quick_trade_service.py:68-116`; duplicate → existing row returned, broker not called |
| RiskManager pre-submit gate | **Implemented** (narrow scope — see §5-f) | `quick_trade.py:29-41,159`; enforced in `reserve_and_submit` (`quick_trade_service.py:185-202`), fail-closed to `QT_BLOCKED` |
| Reserved-order reconciliation | **Implemented** (startup-only) | `reconcile_reserved` `quick_trade_service.py:228`; `recover_on_startup` `quick_trade_recovery.py:197`; wired `api/main.py:104-106`; env-gated `QT_RECOVERY_ON_STARTUP` |
| Request-scoped KIS credentials | **Implemented** | `_load_kis` `quick_trade.py:46`; `KISClient(credentials=…)` `kis_adapter/client.py:28`; no `os.environ` mutation on the QT path |
| Quick Trade domain isolation | **Implemented** | No imports from `backend/execution/*` or `backend/database/models.py` on the QT path; allowed coupling limited to `kis_adapter`, `backend/brokers/semantic_mapper`, `strategy/risk.RiskManager`, api identity models — exactly the DOMAIN_ISOLATION.md allowed set |
| Order lifecycle states + guarded transitions | **Implemented** | `QT_RESERVED/SUBMITTED/REJECTED/FAILED/BLOCKED`, `qt_transition()` `api/models.py:9-40` |
| CompatMiddleware QT coverage | **Implemented** | `_ORDERS_PATH_CONFIG` `api/compat.py:392-406`: balance, position, place-order, history. `close-position` deliberately excluded (`compat.py:387,417`) |
| Test coverage | **Implemented** | 84 tests: `test_quick_trade_persistence.py` (19), `test_quick_trade_risk_gate.py` (21), `test_quick_trade_recovery.py` (13), `test_quick_trade_credential_scope.py` (2), `test_compat_orders.py` (29) |

Not implemented on the QT path (by prior explicit decision or documented DEFER — see §4):

| Piece | Status |
|---|---|
| `close_position` hardening | **Planned / not implemented** — direct broker call, bypasses reservation/idempotency/risk gate/persistence (`quick_trade.py:233-266`) |
| Cancel order | **Planned / not implemented** — no route, no frontend call |
| Order detail / status endpoint | **Planned / not implemented** — submission outcome returned inline only |
| Fill persistence / async fill confirmation | **Planned / not implemented** (documented DEFER) — `submitted` never means filled |
| QT orders surfaced in `/history` | **Planned / not implemented** — `get_history` (`quick_trade.py:271-302`) reads `Trade⋈Strategy` only; `quick_trade_orders` rows are never shown to users (flagged in DOMAIN_ISOLATION.md §8, still open) |
| Periodic reconciliation | **Planned / not implemented** — startup sweep only |

---

## 4. Explicit exclusions (out of scope for P0-06)

Per the task brief, all of the following are **Out of scope for P0-06** and were not touched:

1. **Pagination changes** (P0-04 / PR #140 territory) — backend `get_history` paginates (`page`/`page_size`); the frontend calls `getHistory()` with no params and client-side slices `history.slice(0, 12)` (`index.vue:139`). Location noted; not analyzed or modified.
2. **New business logic** — none added.
3. **`closePosition`** — not modified (see §5-b for the recorded contract conflict).
4. **Cancel / Order Detail / Order Status** — not added.
5. **Fill persistence redesign** — not touched (existing DEFER stands).
6. **CompatMiddleware expansion** — not touched; the deliberate exclusion of `close-position` from `_ORDERS_PATH_CONFIG` is left as-is.

---

## 5. Conflicts and ambiguities

- **(a) P0-06 numbering collision — Conflicting / needs clarification.** `ROADMAP.md` P0-06 (US order-status `output[0]` fix, `backend/execution/order_poller.py`) and the Quick Trade track's implicit P0-06 (this audit) are unrelated tasks sharing an ordinal. Future task briefs should say which track they mean. The ROADMAP P0-06 remains open in its own track and is **not** addressed here.
- **(b) `close-position` frontend↔backend contract is broken — Conflicting / needs clarification.** Frontend sends `{credential_id, symbol, market_type, position_side, source}` (`index.vue:430-436`); backend `ClosePositionRequest` requires `qty` and `price` and reads `market` (`api/schemas.py:196-202`); compat deliberately does not translate this route. Result: the Close button returns a 422 validation error every time — the endpoint is effectively unreachable from the shipped UI. Separately, the handler itself bypasses all P0-04/P0-05 safety. Both facts are recorded; neither is fixed here (excluded by §4).
- **(c) QT orders persisted but invisible — Conflicting (docs §8 vs runtime).** `quick_trade_orders` rows are written on every order but `/history` never reads them. Users cannot see reserved/blocked/failed QT orders. Known follow-up from DOMAIN_ISOLATION.md §8, still open.
- **(d) "Recovery sweep" wording vs runtime — ambiguity resolved.** Commit `efc1941` says "recovery sweep"; the runtime reality is a one-shot startup executor (`main.py:104-106`), not a recurring job. Documented in §2.
- **(e) Table provisioning deviation — minor.** DOMAIN_ISOLATION.md §10 planned an Alembic migration for `quick_trade_orders`; the implementation provisions it via `create_all` (noted in the `api/models.py` docstring as intentional). Functional, but deviates from the migration-first convention established by P1-05B.
- **(f) Risk gate narrower than design — Conflicting (design §6 vs code).** The gate checks only `RiskManager.is_trading_halted()` (global halt flag). DOMAIN_ISOLATION.md §6's per-order validation boundary (qty>0, price tick/precision, exposure cap, symbol scope) is not implemented. The fail-closed behavior is correct for what it covers.
- **(g) Frontend semantic mismatch — known, pre-existing.** The UI is a crypto spot/swap/leverage form over a us/kr equities backend; `amount` (a currency figure) is compat-aliased to `qty` and cast to integer shares; `order_type`/`leverage`/`source` are silently dropped. First flagged in `docs/P5_ORDER_LIFECYCLE_AUDIT.md`; unchanged.
- **UNVERIFIED items:** live runtime behavior on the deployed AWS stack (this audit is code-level only; no deployment probing was performed), and actual KIS API behavior of the date-bounded order inquiry under real pagination (asserted by tests, not by a live call). The 84 QT tests were identified and read, **not re-executed** in this session — CI on the PR is the verification vehicle.

---

## 6. Recommended next action

**P0-06 closes with this document.** The core hardened path (P0-03 + P0-04 + P0-05 + PR #140 reconciliation) is implemented, wired, and tested; no implementation gap exists inside P0-06's scope.

The smallest safe next implementation step, proposed as a **separate task (suggested: P0-07 — Close Position hardening)**, in order of increasing scope:

1. **Minimal (recommended):** route `close_position` through the existing `reserve_and_submit` service — reuse the exact P0-04/P0-05 machinery verbatim (reservation, idempotency, risk gate, persistence, reconciliation eligibility) with a `sell` request. This is a wiring change inside one handler; no new business logic, no schema change, no compat change. The frontend contract fix (supplying `qty`/`price`) is a prerequisite decision: either the frontend sends them, or the backend derives them from the live position — the latter is new business logic and should be scoped deliberately.
2. **Smaller stopgap, if (1) is deferred:** feature-flag or hide the Close button in the frontend until the contract is fixed, since the current button can only ever produce a 422.

Explicitly **not** recommended as next steps: pagination work (excluded), periodic reconciliation (valuable but larger — needs a scheduler decision), history surfacing (needs a UI contract decision).

---

## 7. Is implementation needed?

**No — not within P0-06.**

| Question | Answer |
|---|---|
| Is P0-06 an implementation gap? | No — the runtime path it verifies is **Implemented** |
| Is P0-06 a documentation gap? | **Yes — this document closes it** |
| Is P0-06 a runtime wiring gap? | No — reservation, gate, submit, and startup reconciliation are wired (`main.py:75,104-106,127`) |
| Is P0-06 a certification gap? | No formal certification artifact was ever defined for this track |
| Is P0-06 a safety-gate gap? | No new gate required; the §5-f gate-scope narrowing is recorded for a future task |
| Final classification | **Already complete (P0-03..P0-05 core path) + documentation gap (closed by this audit)** |

**Verdict: Read-only audit only. Zero code changed. Next implementation work belongs to a new task (proposed P0-07), not to this one.**
