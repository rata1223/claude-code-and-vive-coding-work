# P3-04 — Live Trading Readiness Gate Resolution

> Resolves the three operational gates that P3-03B
> (`docs/PAPER_TRADING_CERTIFICATION.md` §4–5) left open before promotion from
> paper trading to real trading. **Validation-first**: failing tests first,
> minimal backward-compatible fixes only where a test exposes a real defect, no
> architecture redesign, no scope expansion.

**Prerequisite:** P3-03B merged (certification report is the authoritative gate
list). This task addresses **only** the three documented blockers below.

---

## 1. Remaining certification gates

Carried verbatim from the P3-03B certification (NOT READY — CONDITIONAL, three
gates before a real-money GO):

| Gate | Certification wording | Scenario |
|---|---|---|
| **G1 — CA dividend runtime ruling** | Live `CorporateActionRuntime` classifies *quantity jumps*; a cash dividend produces no qty jump, so it never flows through the live reconcile/gate/restart chain (only isolated `CorporateActionService` math tested). Confirm defect vs by-design; add a runtime dividend test or a signed waiver. | 12 ⚠️ |
| **G2 — Emergency-flatten fill verification** | Real `EmergencyFlattenManager` submits sells; closure verified only on the broker book, not through the poller/tracker/reconciliation path. Certify closure, idempotency, no duplicate liquidation, no fail-open. | 16 ⚠️ |
| **G3 — DB/Redis restart-recovery** | Outage *tolerance* + per-write rollback covered; reconnect-and-recover after a real infra bounce is not. Validate reconnect, pending-order recovery, post-restart reconciliation. | 19 ⚠️ / 20 ❌ |

---

## 2. Root-cause analysis

### G1 — Cash dividend runtime handling → **correct by design**
- **Ownership contract** (`backend/data/corporate_action_runtime.py:8-14`,
  `docs/CORPORATE_ACTION_RUNTIME_INTEGRATION.md`): the **broker** is the sole
  authority for qty/avg **and cash**; the **reconciler** is the sole writer of
  positions; the `CorporateActionRuntime` only detects/classifies/records/gates —
  it **never** mutates qty/avg.
- A cash dividend changes **only broker cash** (reflected by
  `broker.get_balance()`) and leaves qty/avg unchanged, so it produces **no
  quantity jump**. The reconciler's classifier only fires on a qty jump beyond
  `_QTY_TOLERANCE` (`backend/execution/reconciler.py:220-234`), so a dividend is
  correctly a **position no-op** — never mis-classified as a split/UNKNOWN, never
  a spurious fail-closed block.
- The `cash_delta` computed by `PositionAdjuster.adjust()`
  (`backend/data/corporate_actions.py:231`) is backtest/audit math. At runtime it
  is written **only** to the append-only `corporate_action_history` row
  (`corporate_action_runtime.py::_persist_applied`), never posted to a cash/position
  ledger. Live cash is broker-authoritative.
- **Ruling: by design. No cash/position drift is possible by construction.** No
  code change required; the behavior is now validated by runtime tests (§3).

### G2 — Emergency-flatten fill path → **converges via reconciliation (existing architecture)**
- `EmergencyFlattenManager.flatten_all` submits fire-and-forget market-intent
  sells and returns; it does not register orders with the poller or persist them
  (documented R5, `docs/EMERGENCY_FLATTEN_VALIDATION.md`). Re-routing flatten
  through the execution pipeline is explicitly **architectural / out of scope**.
- The **existing** position-update mechanism for flatten is
  `PositionReconciler.reconcile()` with the broker as ground truth: once the sells
  fill and the broker book is flat, reconcile removes the now-stale DB positions
  (`delete_position`). This is the documented convergence path
  (EMERGENCY_FLATTEN_VALIDATION §5).
- **Idempotency / duplicate-liquidation** is enforced by the module-level
  `_FLATTEN_LOCK` (`backend/worker/emergency.py:23`): a concurrent second flatten
  is rejected with `status="already_in_progress"` and touches no broker; a
  sequential re-flatten against an already-flat book submits zero orders.
- **Fail-open review:** the only fail-open is deliberate and safe for an emergency
  liquidation — a `get_price()` failure falls back to `pos.avg_price` and still
  submits **one** sell per position; audit-sink failure is swallowed so a DB
  outage cannot block liquidation. Neither causes a duplicate or oversell.
- **Ruling: no defect.** The full chain is certified end-to-end through existing
  objects (§3). No code change required.

### G3 — DB/Redis restart recovery → **reconnect already implemented; coverage gap only**
- **PostgreSQL:** the sole production session factory `init_db_factory`
  (`backend/database/models.py:196-200`, used by api/server, worker/runner,
  scheduler, heartbeat) sets **`pool_pre_ping=True`**, so SQLAlchemy validates and
  transparently recycles a stale connection after a DB bounce. The non-pre-ping
  `init_db` helper has **zero production callers**.
- **Redis:** clients are `redis.from_url(...)`; redis-py reconnects on the next
  command. `StartupRecovery._step_redis` (`backend/worker/recovery.py:139-148`)
  treats Redis as a cache and **degrades non-fatally** (DB is authoritative).
- `StartupRecovery.run()` is the real reconnect-and-recover sequence (DB `SELECT 1`
  → Redis ping → risk restore → broker balance/positions → reconcile → pending-order
  re-register → validate → enable). The gap was **test coverage**, not code.
- **Ruling: no defect.** Reconnect + pending-order recovery + post-restart
  reconciliation are now certified (§3). No code change required.

---

## 3. Runtime validation results

All tests use **real production objects** (`EmergencyFlattenManager`,
`PositionReconciler`, `CorporateActionRuntime`, `StartupRecovery`,
`OrderFillPoller`, `ScriptedPaperBroker`) — no mocks of the runtime under test.

**Core tier (SQLite, runs everywhere) — 12 passed:**

| Gate | Test | Proves |
|---|---|---|
| G1 | `test_cash_dividend_produces_no_qty_jump_no_gap_no_drift` | dividend → live reconcile is a position no-op; no CA gap; no drift |
| G1 | `test_dividend_gate_blocks_then_restore_then_apply_preserves_book` | gate blocks → restart-restore re-blocks → apply clears gate; qty/avg preserved; cash_delta in history only |
| G1 | `test_dividend_apply_never_mutates_position_table` | applying a dividend never touches the positions table |
| G2 | `test_flatten_full_chain_to_reconcile_and_audit` | trigger→order→settle→reconcile `delete_position`→DB flat; audit `start`/`order`×2/`complete` |
| G2 | `test_concurrent_flatten_rejected_no_duplicate_orders` | concurrent flatten → `already_in_progress`, 0 broker orders |
| G2 | `test_sequential_reflatten_after_settle_is_noop` | re-flatten on a flat book submits nothing (no duplicate liquidation) |
| G2 | `test_audit_failure_does_not_block_or_duplicate_liquidation` | broken audit sink → liquidation still completes, exactly once |
| G2 | `test_get_price_failure_falls_back_and_liquidates_once` | price feed down → avg-price fallback → one sell per position |
| G3 | `test_startup_recovery_reconnects_and_recovers_pending_order` | `run()`→True, `SAFE_MODE` enabled, pending order re-registered with shared poller |
| G3 | `test_step_redis_tolerates_down_and_healthy_clients` | Redis step non-fatal for none/down/healthy clients |
| G3 | `test_step_db_reconnect_via_fresh_factory` | fresh factory executes the DB reconnect probe |
| G3 | `test_init_db_factory_engine_keeps_pool_pre_ping` | production factory keeps `pool_pre_ping` (reconnect contract regression guard) |

**Postgres tier (real Postgres, CI-only via `ci-postgres.yml`; auto-skips locally) — 5 tests:**
`test_pg_dividend_no_qty_jump_no_gap_no_drift`,
`test_pg_dividend_apply_preserves_book`,
`test_pg_flatten_settle_reconcile_converges_and_audits`,
`test_restart_recovery_reconnects_and_recovers_pending_order`,
`test_restart_restores_ca_gate_and_halts_on_kill_switch`.

**Regression:** full suite (excl. CI-only `tests/postgres`) **1094 passed, 6
skipped — 0 regressions**; certification sweep (`backend/testing` +
`test_emergency_flatten` + `backend/execution` + `backend/data`) **555 passed**.

---

## 4. Minimal fixes applied

**None.** Every gate was already correct by design (G1, G2) or already implemented
in the production runtime (G3, `pool_pre_ping` + degrade-to-DB). No failing test
exposed a runtime defect, so — per the task's minimal-change mandate — no source
code was modified. The deliverables are the certification tests and this report.
No public interface, schema, or runtime behavior changed.

---

## 5. Test coverage

Added (tests only):
- `backend/execution/tests/test_dividend_runtime_p3_04.py` — G1 core (3)
- `backend/testing/tests/test_emergency_flatten_certification_p3_04.py` — G2 core (5)
- `backend/worker/tests/test_startup_recovery_p3_04.py` — G3 core (4)
- `tests/postgres/test_restart_recovery_p3_04.py` — G3 PG (2)
- `tests/postgres/test_live_readiness_db_p3_04.py` — G1+G2 PG (3)

Certification chain covered: trigger → flatten request → broker order → order
polling (`settle_all_open`) → position update (reconcile) → reconciliation →
audit log; dividend detect/gate/restore/apply; DB/Redis reconnect + pending-order
recovery + post-restart reconcile. **No duplicate orders / fills / PnL** asserted
via idempotency + single-submission tests.

---

## 6. Remaining risks

Out of scope for this validation pass (each a separate future task, unchanged from
P3-03B / EMERGENCY_FLATTEN_VALIDATION):
1. **R5 — flatten orders are fire-and-forget** (not poller-registered / not
   persisted). Convergence relies on the next reconcile. Routing flatten through
   the execution pipeline is architectural.
2. **R1 — limit-not-market flatten fill risk.** In a fast down-market a limit sell
   at last/avg price may not fill; there is no price-chase/re-submit. Strongest
   real-money hazard; a policy/execution change.
3. **R7 — kill switch halts but does not auto-liquidate.** Auto-flatten-on-breach
   is a config/policy decision, not wired.
4. **R3 — cross-process flatten duplication.** `_FLATTEN_LOCK` is in-process only;
   a DB/Redis-backed lock would close it.
5. **No live dividend feed.** Dividends are modeled and gate-capable but never
   auto-registered at runtime (by design — cash is broker-authoritative).
6. **Promotion-gate coupling.** `LivePromotionGuard` does not yet require the 9
   certification metrics or zero unresolved reconciliation gaps (P3-03B risk 6).
7. **Chain head not certified** (Scheduler/Worker-process/SignalFusion/quant
   `PersistentLossTracker`) — covered only by their own unit tests.
8. **P3-02C residuals** (D-2, D-5, D-6, I-1) — documented, bounded, non-regression.

---

## 7. Final recommendation

### G1 ✅ · G2 ✅ · G3 ✅ — three gates resolved (all by-design / already-implemented, now certified)

### READY FOR LIMITED LIVE — **CONDITIONAL**

The three P3-03B blockers are resolved: the CA dividend runtime path is ruled
**by design with no drift** and validated; the emergency-flatten fill path is
certified end-to-end (trigger→order→settle→reconcile→audit) with idempotency and
no duplicate liquidation; DB/Redis reconnect + pending-order recovery + post-
restart reconciliation are certified against the real `StartupRecovery` sequence.
Zero regressions (1094 passing), zero code changes required.

**Conditions for a *limited* live start** (small capital, close monitoring),
carried as operational guardrails rather than code blockers:
1. Emergency liquidation depends on limit-sell fills (**R1**) and manual invocation
   (**R7**) — an operator must monitor flatten fills and re-submit if a fast market
   prevents a fill. Wiring auto-flatten + price-chase (R1/R5/R7) is the recommended
   pre-**full**-live task.
2. Run single-process (or add the cross-process flatten lock, **R3**) to preserve
   the in-process duplicate-flatten guarantee.
3. Keep `KIS_ENV=paper` until the 4-week `LivePromotionGuard` window completes with
   the certification metrics collected (P3-03B), then couple the certification
   result into the promotion gate (**risk 6**) before flipping to `real`.

**Not a blanket GO to full autonomous real trading** until R1/R5/R7 (auto-flatten +
fill-verified execution) and promotion-gate coupling are closed. For a **limited,
supervised** live start with the guardrails above: **GO**.

---

*Method: real-runtime tests on production objects + `ScriptedPaperBroker`. Fixes:
none (validation-only). New tests: 17 across core + Postgres tiers. Regression:
1094 passed / 0 regressions.*
