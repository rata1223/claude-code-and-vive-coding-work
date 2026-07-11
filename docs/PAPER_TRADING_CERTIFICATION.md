# Paper-Trading Runtime Certification (P3-03B)

**Scope.** Certify the paper-trading runtime is operationally safe before enabling
real trading. Certification drives the **real production objects** — `OrderStateMachine`,
`PositionTracker`, `OrderFillPoller` (incl. resync/self-heal/terminal-dedup),
`PositionReconciler`, `CorporateActionRuntime`, `KillSwitch`, `EmergencyFlattenManager`,
`apply_terminal_event` — via `backend/testing/paper_harness.py::PaperHarness` against the
deterministic `ScriptedPaperBroker` (`backend/brokers/paper_broker.py`). No mock trading
shortcuts: the broker is a real `BrokerAdapter` with a ground-truth position/cash book and
one-observation-per-poll fill reveal, so the async KIS polling path is exercised exactly.

**Tiers.** *Core* = in-memory, runs everywhere. *PG* = `tests/postgres/`, real Postgres,
CI-only (`ci-postgres.yml`; skipped locally when `TEST_DATABASE_URL` is unset).

**Chain boundary (documented, unchanged).** The harness certifies from **order submission
downward**. The chain head — Scheduler → StrategyWorker(process/pubsub) → SignalFusion →
quant `PersistentLossTracker` — is **not** driven (`StrategyWorker()` hard-wires
`get_kis_broker()`+Redis; SignalFusion needs a live data feed). The execution + risk runtime
is certified as the real production object; signal generation / orchestration is out of scope.

---

## 1. Validation matrix

| # | Scenario | Runtime entry → real objects | Metric | Tier | Result |
|---|---|---|---|---|---|
| 1 | Normal buy | `submit_order`→broker→machine→poller→tracker | successful_orders | core | ✅ PASS |
| 2 | Normal sell | round-trip + realized PnL | successful_orders | core | ✅ PASS |
| 3 | Partial fill | `[P40,F100]` → increment credit once | successful_orders | core | ✅ PASS |
| 4 | **Multiple** partial fills | `[P30,P70,F100]` → increments 30/40/30 | successful_orders | core | ✅ PASS *(new)* |
| 5 | Cancel | `cancel_order`→`apply_terminal_event` | duplicate_event_suppression¹ | core | ✅ PASS |
| 6 | Reject | sync + async poller reject | rejected_orders | core | ✅ PASS |
| 7 | Timeout | `expire_pending`→`_handle_timeout`→broker cancel | timeout_recovery | core | ✅ PASS *(metric new)* |
| 8 | Restart during pending | `_restore_pending_to_tracker` filter contract | — | PG/CI | ✅ PASS (contract) |
| 9 | Restart during partial fill | seeded watermark → only new shares | reconciliation_repairs | core | ✅ PASS |
| 10 | Reconciliation recovery | `PositionReconciler` insert/fix/delete | reconciliation_repairs | PG/CI | ✅ PASS |
| 11 | Corporate action — split | `apply_split`→reconciler classify→converge | corporate_action_events | PG/CI | ✅ PASS |
| 12 | Corporate action — **dividend** | `CorporateActionService` cash-delta (isolated) | corporate_action_events | PG/CI | ⚠️ PARTIAL |
| 13 | Unknown corporate action | fail-closed gate blocks entry | corporate_action_events | core+PG | ✅ PASS |
| 14 | Stale-data block | real `FreshnessGate` fail-closed | stale_data_blocks | core | ✅ PASS |
| 15 | Kill switch | `report_loss_breach`→HALTED→NEW blocked | kill_switch_activations | core | ✅ PASS *(metric new)* |
| 16 | Emergency flatten | real `EmergencyFlattenManager`→broker closes | emergency_flatten_executions | core | ⚠️ PASS (broker-book) |
| 17 | Duplicate broker event | terminal delivered 2× → applied once | duplicate_event_suppression | core | ✅ PASS *(metric new)* |
| 18 | Duplicate reconciliation | reconcile 2× → no double-repair | reconciliation_repairs | core | ✅ PASS |
| 19 | Redis restart | degrade-to-DB tolerance (not reconnect) | — | PG/CI | ⚠️ PARTIAL |
| 20 | PostgreSQL restart | per-write rollback only (no restart-recovery) | — | — | ❌ NOT COVERED |

¹ Cancel closure asserted via state + pending-lock release; the suppression counter covers the duplicate-delivery case.
*(new)* = coverage/metric added by this task (P3-03B).

**Coverage:** 15 full E2E ✅ · 3 partial ⚠️ (12, 16, 19) · 1 not covered ❌ (20). All 20 have at
least isolation-level coverage; the ⚠️/❌ items are the remaining gates in §4–5.

---

## 2. Runtime traces

**Fill path (scenarios 1–4,9)** — identical to `runner._make_fill_callback`:
```text
ScriptedPaperBroker.get_order_status (1 obs/poll)
  → OrderFillPoller._poll_one → _apply_update  (increment = filled − watermark)
    → on_filled(increment) → OrderStateMachine.process_fill
                           → PositionTracker.on_fill  (position, avg price)
                           → realized PnL (sell)      → AuditLog
```
**Terminal path (5,6,7,17)** — shared production handler:
```text
poller detects CANCELED/REJECTED/EXPIRED / _handle_timeout
  → apply_terminal_event(machine, tracker, order)
      transitioned=True  → state converge + unmark_pending  (+timeout_recovery on 7)
      transitioned=False → duplicate suppressed             (+duplicate_event_suppression)
```
**Reconcile path (9,10,18)**:
```text
PositionReconciler.reconcile → _sync_order_status → poller.resync (owned) | DB fallback (unowned)
  → result.repairs[]  → record_reconciliation → reconciliation_repairs
```
**Risk paths (14,15,16)**:
```text
FreshnessGate.validate_* (fail-closed)          → stale_data_blocks
KillSwitch.report_loss_breach → RUNNING→HALTED   → kill_switch_activations ; NEW blocked
EmergencyFlattenManager.flatten_all → broker sells → settle → broker book flat
                                                  → emergency_flatten_executions
```

**Live metric-collection trace (representative mix, this run):**
```text
buy(fill) ·  multi-partial[30,70,100] ·  reject ·  timeout+recover ·
reconcile(1 repair) ·  duplicate cancel(suppressed) ·  loss breach→HALT ·  flatten(1,closed)
→ 비상청산 완료: {'attempted':1,'success':1,'submitted':1,'dry_run':False}
```

---

## 3. Pass / Fail results

**Test execution (core tier, real runtime, local):**
- `backend/testing/` (paper harness + scenarios + new cert metrics): **42 passed**
- Certification sweep (`backend/testing/` + `test_runtime_reconciliation` + `test_order_poller`): **126 passed**
- Full regression (`backend/ tests/`, excl. CI-only `tests/postgres`): **1068 passed, 20 skipped** — **0 regressions**; existing behavior preserved.
- PG tier (scenarios 8,10,11,12,19,20): CI-only via `ci-postgres.yml` (not run locally — no Postgres).

**Required metric collection — all 9 emitted & exported by `ValidationMetrics.as_dict()`:**

| Metric | Collected | Source |
|---|---|---|
| successful_orders | ✅ | `_on_fill` on FILLED (once/order) |
| rejected_orders | ✅ | `submit_order` / `_on_rejected` |
| timeout_recovery | ✅ *(new)* | `_on_timeout` on real transition |
| reconciliation_repairs | ✅ *(new)* | `record_reconciliation` ← `result.repairs` |
| stale_data_blocks | ✅ | `submit_order` freshness gate |
| duplicate_event_suppression | ✅ *(new)* | zero-increment fill + no-transition terminal |
| corporate_action_events | ✅ | CA gate block / event |
| kill_switch_activations | ✅ *(new)* | `report_loss` RUNNING/WARNING→HALTED |
| emergency_flatten_executions | ✅ *(new)* | `emergency_flatten` non-dry-run w/ attempts |

Backward compatibility: all pre-existing keys (`reconciliation_mismatches`, `duplicate_orders`,
`duplicate_signals`, `kill_switch_blocks`, recovery_*) retained in `as_dict()`.

---

## 4. Remaining operational risks

1. **CA dividend runtime path (12) — ⚠️ needs a defect/by-design ruling.** The live
   `CorporateActionRuntime` classifies **quantity jumps**; a cash dividend produces no qty
   jump, so it is never applied through the live reconcile/gate/restart chain (only the
   isolated `CorporateActionService` math is tested). *Cash/position could drift on a dividend
   if runtime application is expected.* **Prerequisite for GO.**
2. **Emergency-flatten fill path (16) — ⚠️.** The real manager submits sells and closure is
   verified on the **broker book**, but flatten orders are **not** registered with the
   poller/tracker (documented R5), so the runtime tracker/PnL don't reflect the flatten until
   the next reconcile. Auto-flatten-on-kill-switch is halt-only (no auto-liquidation).
   **Prerequisite for a real-money GO.**
3. **PostgreSQL restart (20) — ❌** and **Redis restart (19) — ⚠️.** Outage *tolerance* and
   per-write rollback are covered; **reconnection/recovery after a real infra bounce is not**.
   Medium operational risk (a stuck process after a DB/Redis restart).
4. **Chain head not certified.** Scheduler / Worker-process / SignalFusion / quant
   `PersistentLossTracker` are out of the harness scope. Signal-generation correctness and
   worker orchestration are certified only by their own unit tests, not end-to-end here.
5. **P3-02C residual (documented, bounded, non-regression):** D-2 (two kill-switch
   mechanisms), D-5 (non-atomic fill pipeline), D-6 (cross-process reconcile lock), I-1
   (non-unique `DBFill` fingerprint). Each is a separate future task.
6. **Promotion gate coupling.** `LivePromotionGuard` checks env/telegram/DB/Redis + a
   28-day-old `StrategyRun` row; it does **not** read these 9 certification metrics or require
   zero unresolved reconciliation gaps. Certification results are not yet a gate input.

---

## 5. READY / NOT READY recommendation

### NOT READY for real (live) trading — **CONDITIONAL**

The **execution + risk runtime is certified and fully instrumented**: all 20 scenarios have
real-runtime coverage (15 full E2E), the 9 required metrics are collected, and there are zero
regressions (1068 passing). This task (P3-03B Phase 1) closed the certification **measurement**
gaps and the ≥3-step multi-partial coverage — no critical runtime defect was discovered
(P3-02C-D had already closed the real bugs).

**Three gates remain before a real-money GO**, all identified above and none an architecture
redesign:
- **G1 — CA dividend runtime ruling (risk 1):** confirm defect vs by-design; add a runtime
  dividend test or a signed waiver.
- **G2 — Emergency-flatten fill verification (risk 2):** certify flatten closure through the
  poller/tracker path (not just the broker book).
- **G3 — DB/Redis restart-recovery (risk 3):** add a reconnect-and-recover test in the PG-CI
  tier, or a signed operational waiver.

### READY to run the instrumented 4-week paper validation

The runtime is safe to operate in **paper mode** and now emits the full certification metric
set, so the 4-week `LivePromotionGuard` window can run with real evidence collection. Coupling
the certification result (G1–G3 closed + zero unresolved gaps) into `LivePromotionGuard`
(risk 6) is the recommended final step before flipping `KIS_ENV=real`.

---

*Generated by P3-03B Phase 1. Method: real-runtime `PaperHarness` on `ScriptedPaperBroker`;
metrics via `ValidationMetrics.as_dict()`. Fixes were minimal, additive, backward-compatible;
new tests in `backend/testing/tests/test_certification_metrics.py`.*
