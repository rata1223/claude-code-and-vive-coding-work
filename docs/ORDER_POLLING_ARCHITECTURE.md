# Order Polling Architecture

## 1. Purpose and Scope

The order polling engine is the mechanism that bridges broker execution events to the
in-memory order state machine and DB persistence layer. Because KIS (and Kiwoom) do not
push fill notifications, the system must periodically call `broker.get_order_status()` for
every open order and route the result through a single, idempotent fill pipeline.

**Broker is ground truth for order status and fill quantities.**  
**DB is ground truth for order intent (what this system submitted).**

This document specifies:

- The 7-state order lifecycle and its state machine
- The 7 components of the polling system
- Timeout, duplicate-detection, and restart-recovery policies
- Integration contracts with `BrokerSemanticMapper`, `OrderStateMachine`, and `PositionReconciler`

---

## 2. Order Lifecycle States

Every order polled by this system passes through the following 7 canonical states. They map
directly to the existing `OrderStatus` enum — no enum rename is required.

| Lifecycle State | `OrderStatus` Value | Entered When |
|---|---|---|
| **NEW** | `PENDING` | Order object created locally; not yet submitted to broker |
| **ACKNOWLEDGED** | `SUBMITTED` | Broker returned a `broker_order_id`; order accepted into exchange queue |
| **PARTIAL_FILL** | `PARTIAL_FILLED` | `filled_qty > 0` and `filled_qty < qty` |
| **FILLED** | `FILLED` | `filled_qty >= qty` — terminal |
| **CANCELED** | `CANCELED` | Canceled by this system or by the exchange — terminal |
| **REJECTED** | `REJECTED` | Broker refused to accept the order — terminal |
| **EXPIRED** | `EXPIRED` | Order timed out at the exchange (day-order past close, etc.) — terminal |

`UNKNOWN` is a transient diagnostic state used when the broker returns an unrecognizable
status string. It is never written as a permanent DB status. An order in `UNKNOWN` is
retained in the poller and retried on the next cycle.

---

## 3. State Machine Diagram

```
                   ┌──────────┐
                   │   NEW    │  (PENDING)
                   └────┬─────┘
                        │ broker_order_id returned
                        ▼
          ┌─────────────────────────┐
          │      ACKNOWLEDGED       │  (SUBMITTED)
          └──┬────────┬────────┬────┘
             │        │        │
   incremental│  complete│  exchange
     fill    │    fill  │  refuses
             ▼        │        ▼
     ┌──────────────┐ │  ┌──────────┐
     │ PARTIAL_FILL │ │  │ REJECTED │ ──── terminal
     └──────┬───────┘ │  └──────────┘
            │complete │
            │  fill   │
            └────┬────┘
                 │
                 ▼
           ┌──────────┐
           │  FILLED  │ ──── terminal
           └──────────┘

     ACKNOWLEDGED / PARTIAL_FILL  ──►  CANCELED  (terminal)
     ACKNOWLEDGED / PARTIAL_FILL  ──►  EXPIRED   (terminal)

     Any non-terminal  ──►  UNKNOWN  ──►  (any non-terminal or terminal)
                             (transient; never stored to DB)
```

---

## 4. Valid and Invalid State Transitions

### 4.1 Valid Transitions

```
NEW          → ACKNOWLEDGED, REJECTED, CANCELED
ACKNOWLEDGED → PARTIAL_FILL, FILLED, CANCELED, REJECTED, EXPIRED
PARTIAL_FILL → FILLED, CANCELED, EXPIRED
UNKNOWN      → ACKNOWLEDGED, PARTIAL_FILL, FILLED, CANCELED, REJECTED, EXPIRED
FILLED       → (none — terminal)
CANCELED     → (none — terminal)
REJECTED     → (none — terminal)
EXPIRED      → (none — terminal)
```

Any non-terminal state may transition to `UNKNOWN` transiently during a broker outage.
`UNKNOWN` is never written to DB; the in-memory order retains its previous status.

### 4.2 Invalid Transitions — StateTransitionError raised

```
FILLED       → any state          (immutable terminal)
CANCELED     → any state          (immutable terminal)
REJECTED     → any state          (immutable terminal)
EXPIRED      → any state          (immutable terminal)
PARTIAL_FILL → ACKNOWLEDGED       (status regression)
PARTIAL_FILL → REJECTED           (broker cannot reject after partial execution)
NEW          → FILLED             (must pass through ACKNOWLEDGED)
NEW          → PARTIAL_FILL       (same)
NEW          → EXPIRED            (same)
```

---

## 5. Component Designs

### 5.1 Order Polling Engine

**Responsibility:** Background daemon thread that wakes every 5 seconds, identifies orders
whose next-poll time has arrived, calls `broker.get_order_status()`, and dispatches results
to the Fill Event Processor.

**Polling schedule:** exponential backoff — `[10, 30, 60, 120, 300]` seconds, capped at 300.

**Entry state per registered order (`_PollEntry`):**

```python
@dataclass
class _PollEntry:
    order: Order
    on_filled: Callable[[Order], None]
    on_timeout: Callable[[Order], None]
    registered_at: datetime          # UTC; timeout clock starts here
    poll_index: int = 0              # index into _POLL_INTERVALS
    next_poll_at: float = 0.0        # monotonic; 0 = poll immediately
    last_reported_qty: int = 0       # dedup L1: cumulative qty already delivered
    consecutive_errors: int = 0      # network error counter; resets on success
    fill_event_ids: set[str] = field(default_factory=set)
                                     # dedup L2: broker fill sequence IDs (e.g. ODNO)
    outage_seconds: float = 0.0      # time spent in broker-unhealthy state (excluded from timeout)
    outage_started_at: float = 0.0   # monotonic; set when broker goes unhealthy
```

**Broker health tracking:**

- `_broker_healthy: bool = True` — process-level flag, shared across all entries.
- When `broker.get_order_status()` raises a network/timeout exception: increment
  `entry.consecutive_errors`. When `consecutive_errors >= broker_error_threshold` (default 3),
  set `_broker_healthy = False` and freeze `next_poll_at` (do not advance poll schedule).
- When a successful response is received after `_broker_healthy == False`: set
  `_broker_healthy = True`, reset `next_poll_at = 0` for **all** registered entries
  (immediate re-poll after reconnect), write `poll_broker_reconnected` audit event.

**Public API additions vs current implementation:**

```python
def is_registered(self, order_id: str) -> bool:
    """Reconciler calls this before inserting a fill to yield to the poller."""
    with self._lock:
        return order_id in self._entries
```

Registration is only permitted for orders that have a non-empty `broker_order_id`.
NEW-state orders (not yet submitted) must not be registered.

### 5.2 Fill Event Processor

**Responsibility:** Convert broker poll responses into `FillEvent` objects and route them
through the fill pipeline in strict order. There is exactly one implementation of this
pipeline; no separate recovery variant.

**Fill pipeline steps (executed in order for each fill event):**

```
1. StateTransitionValidator.validate(current_status → new_status)
      Raises StateTransitionError if invalid; aborts pipeline.

2. DuplicateEventProtector.is_duplicate(order_id, fill_sequence_id)
      Returns True → skip entire pipeline (audit poll_duplicate_skipped).

3. OrderStateMachine.process_fill(FillEvent(order_id, incremental_qty, fill_price))
      Raises ValueError if overfill or invalid qty.
      Updates in-memory filled_qty and avg_fill_price.
      Auto-transitions status: PARTIAL_FILL or FILLED.

4. PositionTracker.on_fill(fill)
      Updates in-memory position qty and avg_price.

5. DB transaction (single commit):
      a. INSERT fills(order_id, qty, price)  — guarded by dedup L3 check first
      b. UPDATE orders SET status=..., filled_qty=..., avg_fill_price=...
      c. UPSERT positions(symbol, qty, avg_price, ...)

6. AuditLog INSERT (separate DB session, committed before step 5):
      event_type = "poll_fill" or "poll_partial_fill"
      Failure does NOT block step 5.

7. WebSocket push (fire-and-forget).
```

If step 3 or 4 raises, steps 5–7 do not execute. The in-memory state has been updated but
not persisted; this divergence is corrected by the next reconciliation pass.

**Incremental fill delivery:** For PARTIAL_FILL events, only the incremental qty
`(filled_qty - last_reported_qty)` is passed downstream:

```python
partial = dataclasses.replace(updated, filled_qty=incremental_qty)
entry.on_filled(partial)
```

The callback receives an Order copy with `filled_qty = incremental`, not cumulative.

### 5.3 State Transition Validator

**Responsibility:** Enforce the valid/invalid transition table in §4. Standalone, stateless,
broker-agnostic.

```python
class StateTransitionValidator:
    VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
        OrderStatus.PENDING:        {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELED},
        OrderStatus.SUBMITTED:      {OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED,
                                     OrderStatus.CANCELED, OrderStatus.REJECTED,
                                     OrderStatus.EXPIRED, OrderStatus.UNKNOWN},
        OrderStatus.PARTIAL_FILLED: {OrderStatus.FILLED, OrderStatus.CANCELED,
                                     OrderStatus.EXPIRED, OrderStatus.UNKNOWN},
        OrderStatus.UNKNOWN:        {OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED,
                                     OrderStatus.FILLED, OrderStatus.CANCELED,
                                     OrderStatus.REJECTED, OrderStatus.EXPIRED},
        OrderStatus.FILLED:         set(),
        OrderStatus.CANCELED:       set(),
        OrderStatus.REJECTED:       set(),
        OrderStatus.EXPIRED:        set(),
    }

    def validate(self, from_status: OrderStatus, to_status: OrderStatus) -> None:
        """Raise StateTransitionError if transition is invalid."""

    def is_terminal(self, status: OrderStatus) -> bool:
        """Return True for FILLED, CANCELED, REJECTED, EXPIRED."""

    def allows_fill(self, status: OrderStatus) -> bool:
        """Return True only for SUBMITTED and PARTIAL_FILLED."""
```

`OrderStateMachine._assert_valid()` delegates to `StateTransitionValidator.validate()` so
there is one canonical transition table in the codebase.

### 5.4 Duplicate Event Protection

**Responsibility:** Prevent the same fill event from triggering the pipeline more than once.
Three independent layers provide defense-in-depth:

| Layer | Name | Mechanism | Where Checked |
|---|---|---|---|
| **L1** | Incremental qty tracking | `filled_qty - last_reported_qty`; skip if ≤ 0 | `_poll_one()` in poller |
| **L2** | Broker fill sequence ID | `fill_event_ids` set per entry; skip if ID seen before | `_poll_one()` in poller |
| **L3** | DB existence check | `DBFill` query on `(order_id, qty, price)` before INSERT | `_persist_fill()` and `_sync_order_status()` |

**L2 details:** `BrokerSemanticMapper.extract_fill_sequence_id(raw)` returns the broker's
per-fill identifier (KIS domestic: `ODNO` field; KIS overseas: `ODNO` field; Kiwoom: `""`
until verified). If the returned ID is non-empty and already in `fill_event_ids`, the event
is a duplicate and the pipeline is skipped. IDs are cleared when the order reaches a
terminal state. The set is bounded in size (one ID per fill; orders have bounded fill count).

For brokers without sequence IDs, L1 + L3 are sufficient. L2 adds protection specifically
for identical partial fills at the same qty and price (the F2 scenario).

### 5.5 Recovery Polling

**Responsibility:** On process restart, re-register open orders with the poller using the
same fill pipeline as live trading.

**The `_make_recovery_fill_cb()` pattern is eliminated.** Recovery uses
`WorkerSession._build_on_filled()` directly — the same callback factory used at order
placement time.

**Recovery registration flow:**

For each DB order with `status IN (pending, submitted, partial_filled)` AND
`broker_order_id IS NOT NULL`:

1. Call `broker.get_order_status(broker_order_id, symbol)` immediately (synchronous check
   before delegating to async poller).

2. Based on broker response:

   | Broker Status | Action |
   |---|---|
   | `FILLED` | Apply fill pipeline directly with `filled_qty = qty - already_persisted_fills`; do NOT register with poller |
   | `CANCELED` / `REJECTED` / `EXPIRED` | Update DB status only; do NOT register with poller |
   | `PARTIAL_FILLED` | Apply incremental fill for `broker.filled_qty - db.filled_qty`; register remainder with poller |
   | `SUBMITTED` / `PENDING` | Register with poller normally |
   | `None` AND `order_age > lost_order_min_age_minutes` | Mark LOST: attempt broker cancel, set DB status = CANCELED |
   | `None` AND `order_age ≤ lost_order_min_age_minutes` | Register with poller for continued monitoring |

3. `poller.register(order, on_filled=session._build_on_filled(...), on_timeout=...)` —
   same callback factory as live path.

### 5.6 Timeout Detector

**Responsibility:** Detect orders that exceed the maximum polling window without reaching a
terminal state and invoke the configured timeout action.

**Timeout thresholds:**

| Order Status at Timeout | Default Threshold | Configurable Via |
|---|---|---|
| ACKNOWLEDGED (no fill ever) | 30 minutes | `acknowledged_timeout_minutes` |
| PARTIAL_FILL (no fill progress) | 60 minutes from last fill event | `partial_fill_timeout_minutes` |

**Timeout clock is paused during broker outage:** The `outage_seconds` field on `_PollEntry`
accumulates time spent with `_broker_healthy == False`. The effective timeout window is:

```
elapsed = (now_utc - registered_at).total_seconds() - entry.outage_seconds
timed_out = elapsed > threshold
```

**Timeout callback contract:**
- Default handler: `logger.error("수동 취소 필요: ...")` — no automatic cancel, human required.
- Custom handler (injected via `on_timeout` parameter): may call `broker.cancel_order()`;
  must not raise; must be idempotent; errors are caught and logged.
- After `on_timeout()` executes, the order is unregistered from the poller. Its status in
  the `OrderStateMachine` is not changed — the caller's timeout handler is responsible for
  calling `machine.cancel()` if desired.

**PARTIAL_FILL timeout restart:** Each time a new partial fill is delivered (L1 incremental
> 0), the partial-fill timeout clock resets (`last_fill_at = now`). An order with steady
partial fills never times out.

### 5.7 Audit Trail

**Responsibility:** Append-only log of every significant polling event to `audit_logs`.
Audit writes are non-blocking: a failure to write the audit log is logged as a warning and
does not halt the polling operation.

**Pre-action semantics for fill events:** The audit INSERT for `poll_fill` happens in a
separate DB session that commits **before** the fill DB transaction in step 5 of the fill
pipeline. If the fill transaction fails, a `poll_fill_failed` event is written in a
second attempt. The audit log always reflects intent, not success.

**Events:**

| `event_type` | When | Key `detail` fields |
|---|---|---|
| `poll_registered` | Order registered with poller | `order_id, symbol, side, qty, trigger` |
| `poll_fill` | Full fill delivered to pipeline | `order_id, fill_qty, fill_price, cumulative_qty, incremental_qty, status_before, status_after` |
| `poll_partial_fill` | Partial fill delivered | same + `remaining_qty` |
| `poll_timeout` | Timeout threshold reached | `order_id, symbol, elapsed_minutes, last_status` |
| `poll_terminal` | CANCELED / REJECTED / EXPIRED detected | `order_id, symbol, final_status` |
| `poll_duplicate_skipped` | Duplicate event dropped | `order_id, fill_sequence_id, reason` |
| `poll_broker_error` | `get_order_status()` raised exception | `order_id, error, consecutive_errors` |
| `poll_broker_unhealthy` | Error threshold reached; polling frozen | `consecutive_errors, threshold` |
| `poll_broker_reconnected` | First success after outage | `outage_duration_seconds` |
| `poll_lost_order` | Order declared lost | `order_id, symbol, age_minutes` |
| `poll_fill_failed` | Fill DB transaction failed | `order_id, error` |

**`actor` field:** always `"poller:{broker_name}"` (e.g. `"poller:kis"`).

---

## 6. Timeout Policy

```
PollingConfig:
  # Poll schedule
  poll_intervals: list[int] = [10, 30, 60, 120, 300]   # seconds; capped at last value
  loop_sleep_secs: int = 5                              # main loop wake interval

  # Timeout thresholds
  acknowledged_timeout_minutes: int = 30               # ACKNOWLEDGED → on_timeout
  partial_fill_timeout_minutes: int = 60               # no fill progress → on_timeout

  # Broker health
  broker_error_threshold: int = 3                      # consecutive errors → unhealthy
  reconnect_immediate_poll: bool = True                # reset next_poll_at=0 on reconnect

  # Lost order detection (recovery phase)
  lost_order_min_age_minutes: int = 60                 # broker returns None AND age > this

  # Fill sequence ID dedup
  fill_sequence_id_enabled: bool = True                # set False if broker has no ODNO
```

---

## 7. Recovery Policy

### 7.1 On Process Restart

See §9 (Restart Recovery Flow) for the full sequence.

Key rules:
- Recovery is synchronous and blocking. No strategy session starts until recovery completes.
- The fill pipeline used during recovery is identical to the live pipeline.
- `_make_recovery_fill_cb()` must not exist as a separate implementation.
- PositionTracker is loaded from DB **after** recovery fills are applied (Phase 7 in §9).

### 7.2 On Broker Reconnect (mid-session)

1. Set `_broker_healthy = True`.
2. Accumulate `outage_seconds` for all entries from `outage_started_at`.
3. Reset `next_poll_at = 0` for all registered entries (immediate re-poll).
4. Write `poll_broker_reconnected` audit event with `outage_duration_seconds`.
5. Do NOT trigger `PositionReconciler` on reconnect — the poller provides faster feedback.
   The reconciler's existing schedule (every 30 minutes + on market open) is sufficient.

### 7.3 On Poller Thread Death

`StrategyWorker` maintains a heartbeat check: if `self._poller._thread.is_alive()` returns
False, restart the thread with `self._poller._thread = threading.Thread(...)` and call
`self._poller.start()`. The `_entries` dict is preserved — thread restart is safe because the
dict is protected by `_lock` and the thread is stateless except for `_stop`.

### 7.4 On Whole Process Crash

`StartupRecovery._step_pending_orders()` handles full re-registration from DB state.
No additional recovery mechanism is needed.

---

## 8. Duplicate Detection Policy

Three-layer defense in depth (see §5.4 for full details):

**L1 — Incremental qty tracking (zero-cost, always active):**
- Catch: repeated broker poll returning the same cumulative `filled_qty`.
- Misses: identical partial fills at the same qty delivered as separate events.

**L2 — Broker fill sequence ID (low-cost, active when broker provides ODNO):**
- Catch: identical partial fills at the same qty and price (the F2 scenario).
- Requires: `BrokerSemanticMapper.extract_fill_sequence_id(raw)` returning a non-empty ID.
- Misses: brokers without fill sequence IDs (Kiwoom placeholder returns `""`).

**L3 — DB dedup before insert (transactional, always active):**
- Catch: any concurrent fill insert from reconciler or recovery running alongside poller.
- Dedup key: `(order_id, qty, price)` — sufficient for KIS where fill prices are deterministic.
- Future hardening: add `UNIQUE(order_id, qty, price, DATE(filled_at))` DB constraint to
  convert the application-level check into a DB-enforced invariant.

---

## 9. Restart Recovery Flow

```
Process starts
    │
    ▼
Phase 1 — DB health check
    │  → fail: SAFE_MODE.disable("db_unavailable"), abort startup
    ▼
Phase 2 — Broker connectivity
    │  → fail (after 2 retries, 10s apart): SAFE_MODE.disable("broker_unavailable"), abort
    ▼
Phase 3 — Restore risk state
    │  PersistentLossTracker loads peak_equity, daily_pnl, kill_switch from DB/file
    ▼
Phase 4 — Fetch broker balance + positions (BrokerSnapshot)
    │
    ▼
Phase 5 — Startup reconcile
    │  PositionReconciler.reconcile(trigger="startup")
    │  → CRITICAL gaps: block trading (stay in SAFE_MODE), send operator alert
    │  → HIGH / MEDIUM gaps: auto-repair if allowed by reconciler policy
    ▼
Phase 6 — Pending order recovery
    │  For each DB order in {pending, submitted, partial_filled}
    │  with broker_order_id IS NOT NULL:
    │    ├── broker.get_order_status() immediately
    │    ├── FILLED:    apply fill pipeline → skip poller
    │    ├── CANCELED/REJECTED/EXPIRED: update DB status → skip poller
    │    ├── PARTIAL_FILL: apply incremental fill → register remainder with poller
    │    ├── SUBMITTED:  register with poller
    │    └── None:
    │         age > 60m → mark LOST (cancel attempt + set CANCELED)
    │         age ≤ 60m → register with poller (may still fill)
    ▼
Phase 7 — Restore PositionTracker from DB
    │  (AFTER Phase 5 and 6 so the tracker sees repaired state)
    ▼
Phase 8 — Enable trading
    │  SAFE_MODE.enable() if no CRITICAL gaps
    │  → CRITICAL gap remains: keep SAFE_MODE disabled, log startup_blocked event
    ▼
Background (30s delay) — Post-recovery reconcile
    └── PositionReconciler.reconcile(trigger="post_recovery")
        Catches any fills that arrived between Phase 6 and full strategy startup
```

---

## 10. BrokerSemanticMapper Integration

The polling engine delegates all broker-response parsing to `BrokerSemanticMapper`. The
poller does not interpret raw API dicts directly.

**Call chain:**

```
OrderFillPoller._poll_one(entry)
    │
    ▼
broker.get_order_status(order_id, symbol) → Order (order.raw = raw_broker_dict)
    │
    ▼
mapper.map_status(raw, filled_qty, ord_qty)      → OrderStatus
mapper.extract_filled_qty(raw)                   → int
mapper.extract_avg_price(raw)                    → float
mapper.extract_fill_sequence_id(raw)             → str    ← NEW method
    │
    ▼
FillEventProcessor.process(
    order_id, incremental_qty, fill_price, fill_sequence_id, new_status
)
```

### 10.1 New Method: `extract_fill_sequence_id`

Must be added to `BrokerStatusMapper` ABC and all three concrete mappers:

```python
@abstractmethod
def extract_fill_sequence_id(self, raw: dict) -> str:
    """Return a broker-assigned per-fill unique ID, or '' if not available."""
```

| Mapper | Implementation |
|---|---|
| `KISDomesticMapper` | `raw.get("output", {}).get("ODNO", "")` |
| `KISOverseasMapper` | `raw.get("output", {}).get("ODNO", "")` |
| `KiwoomDomesticMapper` | `""` (placeholder — ODNO equivalent TBD) |

The `BrokerAdapter.get_order_status()` contract already returns `Order(raw=...)` so the
raw dict is available to the mapper without changing the broker interface.

---

## 11. Reconciliation Compatibility

The poller and reconciler have overlapping responsibilities for open orders. The ownership
rule is simple and enforced at the reconciler boundary:

| Situation | Who Acts | Rule |
|---|---|---|
| Order is registered in poller | **Poller owns** | Reconciler observes only; does NOT insert Fill |
| Order not in poller AND status mismatch | **Reconciler** | May call `_sync_order_status()` |
| Order reaches terminal state in poller | Poller unregisters | Reconciler takes over on next cycle |
| Reconciler `_sync_order_status()` wants to insert Fill | **Check poller first** | `if poller.is_registered(broker_order_id): return` |

**Implementation:** `PositionReconciler` already accepts `poller` as a constructor argument
(stored as `self._poller`). The guard in `_sync_order_status()` must be:

```python
if self._poller is not None and self._poller.is_registered(row.broker_order_id):
    result.gap("poller_owns", row.symbol,
               f"주문 {row.broker_order_id}: 폴러가 관리 중 — fill 삽입 스킵")
    return
```

This prevents the TOCTOU race (F3) structurally — not just via application-level timing.

---

## 12. New Code Requirements

The following changes are required to implement this architecture. They are listed as
implementation guidance; none are implemented in this document.

| File | Change Required |
|---|---|
| `backend/execution/order_poller.py` | Add `is_registered()`, `_broker_healthy` flag + reconnect logic, `consecutive_errors` counter, `fill_event_ids` set on `_PollEntry`, `outage_seconds` tracking, `PollingConfig` dataclass |
| `backend/brokers/semantic_mapper.py` | Add `extract_fill_sequence_id()` to `BrokerStatusMapper` ABC and all three concrete mappers (`KISDomesticMapper`, `KISOverseasMapper`, `KiwoomDomesticMapper`) |
| `backend/execution/reconciler.py` | Wire `is_registered()` check in `_sync_order_status()` before fill insert; move `is_registered` guard before the `existing_fill` query |
| `backend/worker/recovery.py` | Replace `_make_recovery_fill_cb()` with reuse of `WorkerSession._build_on_filled()`; add immediate `broker.get_order_status()` call in recovery loop |
| `backend/execution/order_machine.py` | No changes required — existing `VALID_TRANSITIONS`, `process_fill()`, and rollback are correct |
| `backend/database/models.py` | Future: add `UNIQUE(order_id, qty, price, DATE(filled_at))` constraint on `fills` table to harden L3 dedup |

---

## 13. Verification

```bash
# State machine transition guard
pytest tests/execution/test_order_machine.py -v -k transition

# Poller idempotency: same fill event delivered twice → exactly 1 callback, 1 DBFill row
pytest tests/execution/test_order_poller.py -v -k idempotent

# Partial fill pipeline: 3 incremental partials → 3 on_filled calls with correct incremental qty
pytest tests/execution/test_order_poller.py -v -k partial

# Broker reconnect: outage → polling frozen → reconnect → all entries re-polled immediately
pytest tests/execution/test_order_poller.py -v -k reconnect

# Timeout: acknowledged_timeout fires on_timeout; outage time excluded from timeout window
pytest tests/execution/test_order_poller.py -v -k timeout

# Recovery: FILLED-during-restart applied via fill pipeline; no poller registration
pytest tests/worker/test_recovery.py -v -k pending_orders

# Reconciler compatibility: poller.is_registered() blocks fill insert in _sync_order_status
pytest tests/execution/test_reconciler.py -v -k is_registered

# Audit trail: every fill event produces a poll_fill AuditLog row before the fill transaction
pytest tests/execution/test_order_poller.py -v -k audit
```
