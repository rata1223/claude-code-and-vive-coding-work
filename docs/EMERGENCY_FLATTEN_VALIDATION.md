# EmergencyFlattenManager — Runtime Validation Audit

> Validation-first audit of the emergency liquidation path. Scope: verify that
> `EmergencyFlattenManager` safely liquidates all positions under catastrophic
> conditions. **No architecture redesign, no new features.** Minimal safety/
> observability fixes applied; deeper items recorded as remaining risks.

Audited modules: `backend/worker/emergency.py`, `backend/quant/risk/engine.py`
(kill switch), `backend/execution/position_tracker.py`,
`backend/brokers/{base,kis,capabilities}.py`, `backend/execution/order_poller.py`,
`backend/worker/recovery.py`, `backend/api/server.py` (`/api/admin/flatten`),
`backend/database/models.py` (`AuditLog`).

---

## 1. Execution flow (trigger → audit log)

```text
trigger ─┬─ MDD / daily / weekly loss breach   (engine.py LossTracker._evaluate)
         ├─ stale market data                  (freshness_gate → kill switch HALTED)
         ├─ reconciliation failure (startup)   (recovery._step_reconcile → SafeMode)
         ├─ manual API  POST /api/admin/flatten (server.py:318)
         └─ operator Telegram / stop command   (runner strategy:stop — session only)
                                │
                                ▼
            EmergencyFlattenManager.flatten_all(reason)        (emergency.py:49)
                                │  ── in-process duplicate guard (_FLATTEN_LOCK)
                                ▼
            broker.get_positions()                             (KIS get_positions)
                                │
                  for each position:
                    price = broker.get_price()  (P0-07 G2: live quote is the ONLY
                      source; missing/raising/non-finite/non-positive → NO order,
                      reported in `failed` + audited. Never falls back to avg_price.)
                    broker.place_order(sym, "sell", qty, price)   ← order_type defaults "limit"
                                │
                                ▼
            OrderFillPoller   ⚠️ NOT wired for flatten orders (fire-and-forget)
                                │
                                ▼
            PositionTracker / PnL pipeline  ⚠️ bypassed for flatten orders
                                │
                                ▼
            AuditLog: emergency_flatten_start / _order / _failed /
                      _positions_error / _complete / _rejected /
                      _price_rejected  (P0-07 G2: priced-out, nothing submitted)
```

The flatten path **submits sell orders and returns** — it does not poll for fills,
does not register orders with `OrderFillPoller`, and does not persist them to the
`orders` table. Fill confirmation and position-closure are therefore **not
verified** by the manager itself (see findings 1 & 5).

---

## 2. Trigger matrix

| Trigger | Detected at | Current action | Auto-liquidate? | Tested? |
|---|---|---|---|---|
| **MDD breach** (−15%) | `quant/risk/engine.py` `LossTracker._evaluate` (~L268) | kill switch + `SAFE_MODE.disable` + alert | ❌ halt only | ✅ `test_robustness`, `test_kill_switch` |
| **Daily loss** (−3%) | `engine.py` `_evaluate` (~L251) | kill switch + halt | ❌ halt only | ✅ |
| **Weekly loss** (−6%) | `engine.py` `_evaluate` (~L259) | kill switch + halt | ❌ halt only | ❌ |
| **Critical stale data** | `data/freshness_gate.py` `_finalize` → `make_kill_switch_halt_callback` | kill switch HALTED + `StaleFeedError` blocks orders | ❌ halt only | ✅ `test_freshness_gate` |
| **Reconciliation failure** (startup) | `worker/recovery.py` `_step_reconcile` | stays in SafeMode (no trading) | ❌ halt only | ✅ (partial) |
| **Manual API flatten** | `api/server.py:318` `POST /api/admin/flatten` | `flatten_all()` — submits market-intent sells | ✅ (operator) | ✅ `test_emergency_flatten` (manager); ❌ endpoint |
| **Manual stop** | `worker/runner.py` `strategy:stop` (redis/DB) | stops one strategy session | ❌ no liquidation | ✅ (implicit) |
| **Catastrophic exception** | `worker/runner.py` `WorkerSession._run` try/except | marks session stopped | ❌ no halt/flatten | ❌ |
| **Watchdog (dead worker)** | `worker/watchdog.py` (not wired into worker) | planned `strategy:stop`; "never flattens" by design | ❌ | ✅ unit (undeployed) |

**Key takeaway:** every automatic catastrophic trigger **halts** (blocks new orders
via kill switch / SafeMode); none **auto-liquidates**. Liquidation is operator-manual
through `/api/admin/flatten` (finding 7).

---

## 3. Findings & risks

### Fixed in this change (minimal, non-architectural)

**F2 — `success` conflated submission with liquidation, and counted dry-runs.**
`results["success"]` was incremented both for dry-run logs and submitted orders, so
an operator could not distinguish "did nothing (dry)" / "order placed, unfilled" /
"closed". **Fix:** the result dict now carries `dry_run` (bool) and `submitted`
(real orders sent; `0` in dry-run); `success` is retained for back-compat but
documented as *processed*, not *confirmed filled*.

**F3 — no duplicate-flatten guard.** `flatten_all` reads `broker.get_positions()`
and places sells directly, bypassing `PositionTracker.try_mark_pending`. Two calls
inside the 3-per-5-min rate window (or an auto + manual call) would double-submit
sells → oversell / unintended short. **Fix:** a module-level `_FLATTEN_LOCK`
acquired non-blocking at entry; a concurrent call audits `emergency_flatten_rejected`
and returns `{"status": "already_in_progress", ...}` without touching the broker.
*In-process only — cross-process duplication remains a risk (R3).*

**F4 — sparse audit trail.** Only `emergency_flatten_start` and per-successful-order
were audited. **Fix:** added `emergency_flatten_positions_error` (fetch failure),
`emergency_flatten_failed` (per-position place_order error),
`emergency_flatten_complete` (final summary with attempted/success/submitted/failed),
and `emergency_flatten_rejected` (duplicate). A partial/failed flatten is now
reconstructable from the audit log.

### Remaining risks (documented, NOT changed — would require redesign)

**R1 — limit-not-market fill risk (most severe).** `place_order` defaults to
`order_type="limit"` and KIS reports `supports_market_sell=False`
(`brokers/capabilities.py`). The flatten sell is priced at last/avg price. In a fast
down-market — precisely when a flatten fires — a limit at last price **may not fill**,
so positions are *not actually liquidated* even though the API reports
`success == attempted`. There is no fill check or price-chase/re-submit. This is the
central emergency-flatten hazard and the strongest argument for the next task.

**R5 — fire-and-forget orders.** Flatten orders are neither registered with
`OrderFillPoller` nor persisted to the `orders` table. Consequences: no fill/timeout
tracking, no auto-cancel-and-retry; on restart the reconciler treats them as external
broker activity; partial fills bypass the normal PnL/position pipeline. Closing this
means routing flatten through the execution pipeline (architectural).

**R6 — no flatten-in-progress persistence (restart hazard).** A crash mid-loop leaves
a partially-liquidated book. The kill switch persists (so **no new trades** — safe),
but the remaining positions are **not** auto-flattened on restart; an operator must
re-invoke `/api/admin/flatten`. Pending sells already placed *are* recovered by the
existing pending-order recovery, but un-submitted ones are not.

**R7 — kill switch never auto-invokes flatten.** `LossTracker._fire_kill_switch_alert`
disables `SAFE_MODE` and sends alerts; it does not call `flatten_all`. So under MDD/
daily/weekly breach the system halts but holds its positions until an operator acts.
Wiring auto-flatten is a **policy/feature decision** → recommended next task.

**R3 — cross-process duplicate flatten.** The new guard is in-process. Two worker/API
processes could still both flatten. A DB/redis-backed lock (or routing flatten through
`PositionTracker` pending locks) would close this.

---

## 4. Affected modules

| Module | Role in flatten | Changed here? |
|---|---|---|
| `backend/worker/emergency.py` | the manager (`flatten_all`) | ✅ audit + result + dup-guard |
| `backend/api/server.py` | `/api/admin/flatten` entry; `dry_run = ENABLE_LIVE_TRADING != "true"` | no (verified correct) |
| `backend/quant/risk/engine.py` | kill switch sets halt; does not flatten | no |
| `backend/brokers/{base,kis,capabilities}.py` | order placement; no true market sell | no |
| `backend/execution/order_poller.py` | fill polling — not used by flatten | no |
| `backend/execution/position_tracker.py` | position state; bypassed by flatten | no |
| `backend/worker/recovery.py` | restart recovery; recovers pending orders only | no |
| `backend/database/models.py` | `AuditLog` sink | no |

---

## 5. Recovery behavior

- **Kill switch** is persisted to `DailyRiskState` (DB) and restored on startup
  (`recovery._step_risk` / `_step_enable_trading`). If active, trading stays blocked
  after restart until **manual** reset (`LossTracker.manual_reset` /
  `risk/kill_switch.py` `RecoveryManager.resume` with a 5-minute cooldown). Fail-closed.
- **Pending flatten sells** already submitted are re-registered with `OrderFillPoller`
  by `recovery._step_pending_orders` and fills are reconciled (idempotent). Un-submitted
  positions are **not** auto-flattened (R6).
- **Reconciliation** treats the broker as ground truth; on restart it repairs the DB to
  match actual broker positions, so post-flatten state converges to reality even if the
  in-memory tracker was bypassed.

## 6. Rollback strategy

- **Undo an erroneous flatten:** there is no automatic re-buy (correct — re-entering a
  position is a strategy decision, not a rollback). To resume trading after a flatten or
  kill switch: clear the kill switch via `manual_reset` / `RecoveryManager.resume`
  (cooldown enforced), confirm positions via a manual `/api/admin/reconcile`, then
  re-enable SafeMode through the normal startup-recovery gate.
- **Revert this change:** code-only, additive (audit rows, two result keys, an
  in-process lock). Reverting the commit fully restores prior behavior; no schema or
  API contract change (the result dict only gains keys).

---

## 7. Remaining risks → recommended next task

**Recommended next task — auto-flatten wiring + fill verification (successor to Stage 2):**
1. Wire the kill switch to *optionally* invoke `flatten_all` on `LOSS_LIMIT_BREACH`
   (config-gated; default off) — closes R7.
2. Route flatten sells through the execution pipeline so they are poller-tracked and
   fill-verified, with a price-chase/re-submit on non-fill — closes R1 + R5.
3. Add a DB/redis-backed flatten lock + a persisted "flatten-in-progress" marker for
   cross-process safety and restart resume — closes R3 + R6.
4. Add an integration test: *MDD breach → (config on) → all positions liquidated and
   confirmed filled*, plus a `/api/admin/flatten` endpoint test.

Each of these changes order semantics or the execution pipeline and is therefore
**out of scope for this validation-first pass**.

---

## Test coverage added

`backend/worker/tests/test_emergency_flatten.py` (6):
dry-run reports `submitted=0`; live counts `submitted`; per-position failure audited
+ reported; positions-fetch failure audited (no false `complete`); empty book reports
`complete`; concurrent call rejected with `already_in_progress`.

Verified: `pytest backend/worker backend/quant/risk` → 92 passed; ruff clean; runtime
driver confirms dry-run vs live submission counts and the audit event set
(`start` / `order` / `complete`).
