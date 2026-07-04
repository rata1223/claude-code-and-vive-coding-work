# P3-02C — Runtime Reconciliation Synchronization

> Phase M4. Implements the runtime-sync fixes from the P3-02C-A audit.
> Scope: keep the in-memory runtime (PositionTracker + OrderStateMachine +
> pending locks) consistent with the broker by repairing **missed callbacks
> without a worker restart** — routing every reconciliation-discovered fill
> through the **single** existing `OrderFillPoller` processing pipeline.
> No architecture redesign; no second fill processor.

---

## 1. Runtime flow

The broker is ground truth. Two paths turn a broker state into a runtime effect,
and after this change they share **one** processing core:

```
                       ┌──────────────── Broker (ground truth) ───────────────┐
                       │ get_order_status(order_id, symbol)                    │
                       └───────┬───────────────────────────────────┬──────────┘
                               │                                    │
              (A) OrderFillPoller._loop (daemon)      (B) PositionReconciler.reconcile()
                  per-order backoff poll                  periodic / startup / manual
                               │                                    │
                    _poll_one(entry)                        _reconcile_pending_orders()
                       fetch + None/err handling               broker status != DB status?
                               │                                    │ _sync_order_status()
                               ▼                                    ▼ poller.resync(broker_order)
                    ┌───────────────────────────────────────────────────────┐
                    │  OrderFillPoller._apply_update(entry, updated)         │  ← SINGLE
                    │  FILLED / PARTIAL / CANCELED / REJECTED / EXPIRED       │    processing
                    │  increment = updated.filled_qty − entry.watermark      │    core
                    └───────────────┬───────────────────────────────────────┘
                                    │ entry.on_filled(increment_copy)   (fills)
                                    │ entry.on_canceled/rejected/expired (terminal, no fill)
                                    ▼
                    _make_fill_callback.on_filled  (runner.py) — the ONE fill pipeline
                       1 machine.process_fill      (state transition)
                       2 tracker.on_fill           (position + pending-lock release)
                       3 realized PnL → loss_tracker.record_pnl  (kill-switch eval)
                       4 _persist_fill             (DBFill + DBOrder.filled_qty, dedup)
                       5 _upsert_position_db
                       6 websocket push
```

`resync()` re-drives an order through **exactly the same** `_apply_update` →
`on_filled` path as live polling. The reconciler never applies a fill itself.

### Single-authority guarantees

| Responsibility | Sole authority |
|---|---|
| Fill processing | `OrderFillPoller._apply_update` → the strategy `on_filled` pipeline |
| PnL calculation | `runner._make_fill_callback` (`realized_pnl = (fill.price − entry_price) · qty`) |
| KillSwitch evaluation | `PersistentLossTracker.record_pnl` → `_evaluate` (unchanged; the standalone `KillSwitch` class stays unwired — see §4) |
| Audit entry (per fill) | pipeline `_persist_fill` writes `fill`; poller writes `poller_filled`; the reconciler writes **no** fill row when it routes via the poller |
| State transition | `OrderStateMachine` (fills via `process_fill`; terminal non-fills via `_make_terminal_callback`) |

---

## 2. Synchronization lifecycle

**Persistent watermark.** Each `_PollEntry.last_reported_qty` is the high-water
mark of processed fill quantity. It is now **seeded from the order's known
`filled_qty` at registration** (`OrderFillPoller.register`), so:

- live orders start at 0;
- orders re-registered after a restart start at the DB-persisted `filled_qty`
  (`runner._register_recovered_order` carries `filled_qty` into the recovered
  `Order`). This closes the pre-restart replay gap (audit CL-1).

**Increment gate.** For FILLED/PARTIAL, `increment = updated.filled_qty −
last_reported_qty`. The callback fires and the watermark advances **only when
`increment > 0`**. A repeat delivery yields `increment = 0` → no-op.

**Lost-callback self-heal.** On FILLED/PARTIAL the watermark advances and the
entry is popped **only after `on_filled` returns successfully**. If the callback
raises, the entry stays registered and the watermark is unchanged, so the next
poll (or a `resync`) re-drives the same increment. A fill is never silently
dropped (audit CL-4). Bound: a callback that fails indefinitely is popped by the
30-minute poll timeout.

**Reconciler routing.** In `_sync_order_status`, when a poller is present it calls
`poller.resync(broker_order)`:

- **owned + FILLED** → the pipeline persisted the fill; the reconciler records a
  `sync_order_runtime` repair and writes nothing more (no duplicate fill/audit).
- **owned + non-fill terminal** → runtime synced by the terminal callback; the
  reconciler still writes the DB status row.
- **not owned** (no live entry, e.g. an externally-placed order) → falls back to
  the prior DB-only sync, guarded by the existing `existing_fill` check.

**Terminal non-fill states.** `runner._make_terminal_callback` transitions the
machine and releases the pending lock for CANCELED/REJECTED/EXPIRED, wired as
`on_canceled/on_rejected/on_expired` on both live (`IndicatorStrategy`) and
recovered orders. Idempotent: a machine already terminal is skipped;
`unmark_pending` is a no-op if the lock is gone.

---

## 3. Idempotency guarantees

| Scenario | Mechanism | Result |
|---|---|---|
| Duplicate fill (poll + reconcile) | watermark `increment > 0` gate + FILLED entry pop | applied once |
| Duplicate reconciliation | first `resync` drives + pops the entry → DB becomes FILLED → next reconcile doesn't re-query the order | applied once |
| Repeated reconciliation (N×) | same as above; steady state `increment = 0` | converges, stays converged |
| Restart before callback | recovered entry watermark = DB `filled_qty` (0) → `resync` applies the missed fill | runtime repaired |
| Restart after reconciliation | recovered entry watermark = DB `filled_qty` (full) → poll `increment = 0` | not re-applied |
| Lost callback | entry retained on exception; watermark unadvanced | retried, applied once on success |
| Duplicate terminal (cancel/reject/expire) | machine already-terminal skip + idempotent `unmark_pending` | transitioned/released once |
| DB fill row | `_persist_fill` dedup on `(order_id, qty, price)` | one row |

Tests: `tests/integration/test_runtime_reconciliation.py` — one class per required
scenario (callback lost, restart before callback, reconciliation repairs runtime,
duplicate reconciliation, duplicate fill, repeated reconciliation, restart after
reconciliation) plus terminal-state sync.

---

## 4. Remaining risks (deliberately out of scope)

- **Non-atomic fill pipeline (audit D-5).** Steps 1–5 run in separate DB
  sessions. A crash between `tracker.on_fill` (step 2) and `_persist_fill`
  (step 4) can leave the runtime ahead of the DB watermark; the next `resync`,
  keyed on the DB watermark, would then re-drive that increment into the tracker.
  This narrow window is bounded by the next broker-truth reconcile, which repairs
  the DB position. Making runtime+DB atomic is a broad execution rewrite and was
  explicitly excluded.
- **Two kill-switch mechanisms (audit D-2).** The wired
  `PersistentLossTracker.kill_switch` remains the only runtime evaluator; the
  standalone `KillSwitch` class is still unwired. Choosing a single authority is
  an architecture decision deferred to a later task, so this change does not wire
  a reconcile→halt bridge.
- **Cross-process reconcile race (audit D-6).** `PositionReconciler`'s lock is
  per-process; the API-triggered manual reconcile and the worker periodic
  reconcile are not mutually excluded. Unchanged here.
- **`tracker.on_fill` still has no internal dedup.** Idempotency depends on the
  upstream watermark. A caller bypassing the poller/reconciler (there is none in
  production today) would not be protected.

---

## Files changed

- `backend/execution/order_poller.py` — watermark seeding in `register`;
  `_poll_one` split into fetch + `_apply_update`; lost-callback self-heal;
  new `resync()`.
- `backend/execution/reconciler.py` — `_sync_order_status` routes fills through
  `poller.resync()`; DB-only fallback preserved.
- `backend/worker/runner.py` — recovered orders carry DB `filled_qty`;
  `_make_terminal_callback`; terminal callbacks wired into recovery.
- `backend/strategy/indicator/strategy.py` — accepts `on_terminal_cb`, registers
  terminal callbacks on the poller.
- `tests/integration/test_runtime_reconciliation.py` — new scenario suite.
