# Stage 2 — Engine Stabilization Audit & Remediation

Goal: eliminate remaining execution-path instability (fail-open paths, silent
risk-control gaps, wrong-order data) before Postgres CI and EmergencyFlattenManager
validation. Audited the full runtime path: Scheduler → Worker → Strategy →
SignalFusion → Execution → Broker → Polling → Reconciliation → Risk.

Scope discipline: fixed only issues that affect **order execution, position
consistency, recovery after restart, or risk controls**, with minimal changes and
a regression test per fix. No architecture changes.

---

## 1. Findings

### Fixed

| # | Severity | File | Category | Affects |
|---|---|---|---|---|
| F1 | CRITICAL | `backend/brokers/kis.py` `_get_kr_order_status` | wrong-order fallback (fail-open) | execution, position, recovery |
| F2 | HIGH | `backend/worker/recovery.py` `_step_risk` | fail-open on exception | risk, recovery |
| F3 | CRITICAL | `backend/worker/recovery.py` `_step_reconcile` | fail-open on reconcile errors | recovery, risk, position |
| F4 | CRITICAL | `backend/worker/runner.py` fill callback | silent risk-control skip | risk, position |
| F5 | HIGH | `backend/worker/runner.py` startup bootstrap | silent MDD skip (uninit fallback) | risk, recovery |
| F6 | HIGH | `backend/worker/scheduler.py` session gates | fail-open on calendar error | execution |

### Audited and refuted (no change — already safe)

- **Poller "backward transition" double-count** (`order_poller.py:271`): refuted.
  `last_reported_qty` only advances when `incremental > 0` and the entry is popped
  on any terminal FILLED, so a regressed broker status cannot double-count or emit a
  negative fill. The unblocked transition is cosmetic.
- **`PositionTracker.restore_positions` race** (`position_tracker.py:118`): refuted.
  The clear+reload runs entirely under `self._lock`, and `on_fill` takes the same
  lock — the swap is already atomic w.r.t. fills.
- **`OrderStateMachine.submit` transition outside lock** (`order_machine.py`):
  refuted. `_orders` is left consistent inside the lock; `transition()` re-acquires
  the lock and looks up the (now-stable) broker id.
- **Recovery duplicate-fill early return** (`recovery.py:256`): refuted. The dedup
  check precedes any mutation, and a matching fill row implies the prior processing
  already updated order+position — returning is correct/idempotent.

---

## 2. Root causes

- **F1**: the KR order-status parser took `output[0]` while the US parser correctly
  matched `odno`. The KIS inquiry endpoint can return multiple orders, so `output[0]`
  could be an unrelated order — marking a pending order FILLED from a different
  order's row, corrupting position/risk state.
- **F2**: `_step_risk` swallowed any exception and returned `True` without marking the
  kill-switch, so a failed risk-state restore (Redis/DB down) enabled trading on
  unknown risk state.
- **F3**: `_step_reconcile` returned `True` regardless of `result.errors` (and on
  exception), enabling trading even when broker↔DB positions could not be verified.
  `ReconciliationResult.ok` already means "no errors" (gaps are normal/repaired), so
  it is the correct gate.
- **F4**: the realized-P&L block was gated on `entry_price is not None`. A sell whose
  position was missing (restart lag / external sell / desync) skipped the *entire*
  block — so the equity-based MDD/kill-switch evaluation never ran and the loss was
  invisible to risk.
- **F5**: `_last_known_equity` was only set on a successful *fill-time* balance fetch.
  If the first post-restart fill's balance fetch failed, the fallback was `None` and
  MDD evaluation was silently skipped. Peak-equity was deliberately not used as the
  fallback (it would mask drawdown), so the real fix is to seed `_last_known_equity`
  from a real balance fetch at startup.
- **F6**: the calendar gate logged and *continued* on exception ("계속 진행"), so a
  calendar-service failure broadcast session-open signals on a holiday → strategies
  attempt orders on a closed market.

---

## 3. Modified files

- `backend/brokers/kis.py` — `_get_kr_order_status` now matches `odno` (mirrors the
  US path); returns `None` when the requested order is absent.
- `backend/worker/recovery.py` — `_step_risk` fail-closed (sets `_kill_switch_active`
  on error); `_step_reconcile` returns `result.ok` and `False` on exception.
- `backend/worker/runner.py` — fill callback runs MDD/kill-switch evaluation for every
  sell (realized_pnl `0.0` + `sell_without_entry_price` audit when entry unknown);
  startup seeds `_last_known_equity` from a real balance fetch (cold and warm restart).
- `backend/worker/scheduler.py` — KR/US session gates fail-closed (skip the signal on
  any calendar error).

All fixes preserve existing recovery behavior: a failed recovery step still leaves the
worker **alive in SafeMode** (no trading) per `main()` (`runner.py:794-795`) — it does
not crash the process.

## 4. Added tests

- `backend/worker/tests/test_engine_stabilization.py` (10): risk-restore fail-closed
  (error + success); reconcile fail-closed (errors / clean / exception); fill-callback
  risk eval (sell w/o position → MDD+audit; balance-fail uses seeded equity;
  no-equity → MDD skipped + audit); scheduler KR/US fail-closed on calendar error.
- `backend/brokers/tests/test_kr_order_status.py` (3): matches the requested order
  (not the first row); returns `None` when no row matches; returns `None` on empty
  output.

Full sweep: **750 passed, 14 skipped** (pre-existing env-dep skips), new tests included.

## 5. Remaining risks (not fixed this round — out of minimal scope)

- **Cross-process kill-switch latency** (`heartbeat.py` writes DB `kill_switch`, but the
  running worker never re-reads it). A watchdog-set halt is honored only at restart.
  HIGH for risk; needs a small runtime DB poll — see next task.
- **`PersistentLossTracker._persist()` runs outside the `record_pnl` lock** — a narrow
  torn-read window for concurrent fills (low concurrency today: single poller thread).
- **Post-recovery reconcile** runs on a daemon thread whose errors are only logged; a
  failed post-sync does not re-arm SafeMode (best-effort).
- **F1 follow-up**: KR/US order-status both return `None` on no-match; callers treat
  `None` as "no update" and keep polling until timeout — acceptable, but a persistent
  mismatch only surfaces via the timeout path.

## 6. Recommended next task

**Runtime cross-process kill-switch propagation**: add a lightweight periodic read of
`DailyRiskState.kill_switch` in the worker loop (and/or at the fill choke point) so a
watchdog- or operator-set halt takes effect without a restart. This is the highest
remaining risk-control gap and is the natural precursor to EmergencyFlattenManager
validation (which depends on the halt actually stopping the live worker).
