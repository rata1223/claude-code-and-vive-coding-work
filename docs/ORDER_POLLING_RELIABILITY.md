# Order Polling Reliability — Design Specification

**Status:** Design only — no code changes in this document.  
**Supersedes:** `docs/ORDER_POLLING_ARCHITECTURE.md` (v1, shipped).  
**Audit basis:** TASK 2-2A second-pass audit findings EX-01 through EX-19.

---

## 1. Purpose and Scope

This document specifies the hardened order polling system for the KIS/Kiwoom trading platform. It addresses 19 risk items identified in the TASK 2-2A second-pass audit, the most severe of which can cause:

- **Silent fill loss** (EX-02): callback throws after entry is already popped → fill permanently lost
- **Double position** (EX-06): recovery and runner both register the same order → two callbacks → two fills
- **Duplicate DB rows** (EX-04): no UNIQUE constraint on `fills` table → concurrent inserts succeed
- **Symbol lock-out** (EX-08): `unmark_pending()` never called if `on_filled` raises → symbol permanently blocked from new orders
- **Kiwoom orders stuck** (EX-13): `KiwoomDomesticMapper.map_status()` always returns `UNKNOWN` → no state progression

The design covers 6 polling states, 7 components, and all policies needed to implement a reliable, broker-agnostic polling engine.

---

## 2. Polling States

The poller operates on 6 states. These map to the existing `OrderStatus` enum; no rename is required.

| Polling State | `OrderStatus` Value | Meaning | Terminal? |
|---|---|---|---|
| **NEW** | `SUBMITTED` | Registered with poller; awaiting first fill | No |
| **PARTIAL_FILL** | `PARTIAL_FILLED` | `0 < filled_qty < qty` | No |
| **FILLED** | `FILLED` | `filled_qty >= qty` | Yes |
| **CANCELED** | `CANCELED` | Canceled by system or user | Yes |
| **REJECTED** | `REJECTED` | Broker refused the order | Yes |
| **EXPIRED** | `EXPIRED` | Order timed out at broker | Yes |

`UNKNOWN` is a transient error state entered when the broker returns an unrecognizable status. It is never written to the DB and never stored as a final polling state. It causes a retry on the next poll cycle.

`PENDING` (pre-submission) is not a polling state. Orders are only registered with the poller after `broker_order_id` is assigned by the broker acknowledgement.

---

## 3. State Machine

### 3.1 ASCII Diagram

```
              ┌──────────────────────────────────────────────────────────┐
              │                    POLLING STATES                        │
              │                                                          │
              │  ┌─────┐                                                 │
 register()   │  │ NEW │──────────────────────────────┐                 │
 (SUBMITTED)  │  └─────┘                              │                 │
      │       │     │                                 │                 │
      └──────►│     ├──────────►┌─────────────┐       │                 │
              │     │           │ PARTIAL_FILL │       │                 │
              │     │           └─────────────┘       │                 │
              │     │                 │               │                 │
              │     │                 ├──────────────►│                 │
              │     │                 │               ▼                 │
              │     │                 │          ┌────────┐             │
              │     └─────────────────┴─────────►│ FILLED │ (terminal) │
              │                                  └────────┘             │
              │                                                          │
              │  NEW / PARTIAL_FILL ──► CANCELED  (terminal)            │
              │  NEW                ──► REJECTED  (terminal)            │
              │  NEW / PARTIAL_FILL ──► EXPIRED   (terminal)            │
              │                                                          │
              │  Any non-terminal   ──► UNKNOWN ──► (retry)             │
              └──────────────────────────────────────────────────────────┘
```

### 3.2 Valid Transitions

```
NEW          → PARTIAL_FILL, FILLED, CANCELED, REJECTED, EXPIRED, UNKNOWN
PARTIAL_FILL → FILLED, CANCELED, EXPIRED, UNKNOWN
UNKNOWN      → NEW, PARTIAL_FILL, FILLED, CANCELED, REJECTED, EXPIRED
FILLED       → (none)
CANCELED     → (none)
REJECTED     → (none)
EXPIRED      → (none)
```

### 3.3 Invalid Transitions (StateTransitionError raised)

| From | To | Reason |
|---|---|---|
| Any terminal | Any state | Terminals are immutable |
| `PARTIAL_FILL` | `NEW` | Fill quantity regression |
| `PARTIAL_FILL` | `REJECTED` | Broker cannot reject after partial fill |
| `UNKNOWN` | `UNKNOWN` | Must resolve to a named state |

---

## 4. Component Architecture

```
OrderFillPoller
    │
    ├── BrokerPollingAdapter (injected)
    │       ├── KISDomesticPollingAdapter
    │       ├── KISOverseasPollingAdapter
    │       └── KiwoomPollingAdapter
    │
    ├── StateTransitionValidator
    │
    ├── LostOrderDetector
    │
    ├── RetryLogic  (per-entry backoff)
    │
    ├── PollingHealthMonitor  (broker-level health)
    │
    └── AuditTrail
```

---

## 5. Component Designs

### 5.1 Order Polling Engine

**Responsibility:** Background daemon thread. Runs the main poll loop, owns the `_entries` dict, dispatches results through the fill pipeline, and enforces the pop-after-callback contract.

#### 5.1.1 Internal State Per Entry (`_PollEntry`)

```
_PollEntry:
  order: Order
  on_filled: Callable[[Order], None]
  on_timeout: Callable[[Order], None]
  registered_at: datetime
  poll_index: int                    # index into _POLL_INTERVALS
  next_poll_at: float                # monotonic timestamp
  last_reported_qty: int             # L1 dedup: cumulative qty delivered to callback
  consecutive_errors: int            # network error counter; feeds HealthMonitor
  fill_event_ids: set[str]           # L2 dedup: broker fill sequence IDs (ODNO etc.)
  outage_seconds: float              # accumulated broker-outage time; pauses timeout clock
```

#### 5.1.2 Pop-After-Callback Contract

**Current bug (EX-02):** Entry is popped BEFORE `on_filled` runs. If `on_filled` throws, the fill is permanently lost and will never be retried.

**Required behavior:**
- For FILLED: call `on_filled`, then pop on success. On exception: log `poll_callback_failed`, advance poll schedule, retry next cycle. Entry is NOT popped.
- For CANCELED/REJECTED/EXPIRED: same protocol — pop only after terminal-state callback completes without exception. (Terminal callbacks rarely throw, but the contract must hold.)

```
# Required pattern for FILLED
try:
    entry.on_filled(updated_order)
    with self._lock:
        self._entries.pop(order_id, None)   # pop AFTER success
except Exception as e:
    logger.error("on_filled 콜백 오류: %s — 다음 사이클에 재시도", e)
    self._audit.write("poll_callback_failed", order_id=order_id, detail=str(e))
    entry.advance()    # retry
```

#### 5.1.3 Thread Safety

**Current bug (EX-15):** `entry.order = updated` is written outside `self._lock`. The loop thread reads `entry.order` concurrently.

**Required:** All writes to `entry.order` must happen inside `with self._lock`.

#### 5.1.4 Dedup Registration

**Current bug (EX-06):** `recovery.py` and `runner.py` both call `poller.register()` for the same recovered order.

**Required:** `register()` checks `is_registered(order.id)` first. If the entry already exists, log `poll_double_registration` audit event and return — no duplicate entry created.

```python
def register(self, order, on_filled, on_timeout=None):
    if not order.id:
        logger.warning("broker_order_id 없음 — 폴링 스킵")
        return
    with self._lock:
        if order.id in self._entries:
            self._audit.write("poll_double_registration", order_id=order.id)
            return
        self._entries[order.id] = _PollEntry(...)
```

#### 5.1.5 Public API

```python
class OrderFillPoller:
    def start(self) -> None
    def stop(self) -> None
    def register(self, order: Order, on_filled: Callable, on_timeout: Callable = None) -> None
    def unregister(self, order_id: str) -> None
    def is_registered(self, order_id: str) -> bool      # NEW — reconciler uses this
    def pending_count(self) -> int
```

---

### 5.2 Broker Polling Adapter

**Responsibility:** Abstract the broker-specific polling call behind a single interface. The Polling Engine calls only this interface — never raw `BrokerAdapter.get_order_status()` directly from inside `_poll_one()`.

#### 5.2.1 Interface

```python
class BrokerPollingAdapter(ABC):
    @abstractmethod
    def poll_order(self, order_id: str, symbol: str) -> Optional[Order]:
        """
        Return current order state from broker, or None if broker has no record.
        Raises on network/auth error (caller handles retry logic).
        Never returns an Order with status=UNKNOWN permanently.
        """

    @abstractmethod
    def extract_fill_sequence_id(self, order: Order) -> str:
        """
        Return broker fill sequence ID for L2 dedup.
        Return '' if broker does not provide one.
        For KIS domestic: raw.get('output', {}).get('ODNO', '')
        """
```

#### 5.2.2 Implementations

| Class | File | `poll_order` delegates to | `extract_fill_sequence_id` |
|---|---|---|---|
| `KISDomesticPollingAdapter` | `brokers/polling_adapter.py` | `KISBroker.get_order_status()` | `order.raw["output"]["ODNO"]` |
| `KISOverseasPollingAdapter` | `brokers/polling_adapter.py` | `KISBroker.get_order_status()` (US path) | `order.raw["output"]["ODNO"]` |
| `KiwoomPollingAdapter` | `brokers/polling_adapter.py` | `KiwoomBroker.get_order_status()` | `""` (not yet available) |

#### 5.2.3 EX-13 Fix: Kiwoom UNKNOWN

`KiwoomDomesticMapper.map_status()` currently returns `OrderStatus.UNKNOWN` for all inputs, causing Kiwoom orders to never advance past NEW state.

**Required change to `KiwoomDomesticMapper`:**
- Map actual Kiwoom order status field values to the correct `OrderStatus` enum values.
- The `KiwoomPollingAdapter` must validate that `poll_order()` never returns a permanent `UNKNOWN` status (i.e. one that appears on two consecutive polls without change).
- Until `KiwoomDomesticMapper` is corrected, `KiwoomPollingAdapter.poll_order()` raises `NotImplementedError` rather than silently returning an `UNKNOWN` `Order` object.

**New `KiwoomDomesticMapper` status mapping** (field name `ord_stat_cd` — verify against actual Kiwoom API response before implementation):

| Kiwoom `ord_stat_cd` | `OrderStatus` |
|---|---|
| `"00"` (접수) | `SUBMITTED` |
| `"01"` (부분체결) | `PARTIAL_FILLED` |
| `"02"` (체결완료) | `FILLED` |
| `"03"` (취소) | `CANCELED` |
| `"04"` (거부) | `REJECTED` |
| `"05"` (만료) | `EXPIRED` |
| other | `UNKNOWN` (transient only) |

> **Note:** Field names and codes must be verified against the live Kiwoom API before implementation. The table above is a placeholder.

---

### 5.3 State Transition Validator

**Responsibility:** Enforce the valid/invalid transition table in §3. Stateless; no broker dependency. Shared between Polling Engine and OrderStateMachine.

```python
class StateTransitionValidator:
    # Terminal states — no outgoing transitions
    TERMINAL = frozenset({
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    })

    # Valid transitions per source state
    VALID: dict[OrderStatus, frozenset[OrderStatus]] = {
        OrderStatus.SUBMITTED:      frozenset({PARTIAL_FILLED, FILLED, CANCELED, REJECTED, EXPIRED, UNKNOWN}),
        OrderStatus.PARTIAL_FILLED: frozenset({FILLED, CANCELED, EXPIRED, UNKNOWN}),
        OrderStatus.UNKNOWN:        frozenset({SUBMITTED, PARTIAL_FILLED, FILLED, CANCELED, REJECTED, EXPIRED}),
        OrderStatus.FILLED:         frozenset(),
        OrderStatus.CANCELED:       frozenset(),
        OrderStatus.REJECTED:       frozenset(),
        OrderStatus.EXPIRED:        frozenset(),
    }

    def validate(self, from_status: OrderStatus, to_status: OrderStatus) -> None:
        """Raises StateTransitionError if transition is not in VALID."""

    def is_terminal(self, status: OrderStatus) -> bool:
        return status in self.TERMINAL

    def allows_fill(self, status: OrderStatus) -> bool:
        """Returns True only for SUBMITTED and PARTIAL_FILLED."""
        return status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED)
```

`OrderStateMachine._assert_valid()` delegates to `StateTransitionValidator.validate()`. No duplicate validation logic.

---

### 5.4 Lost Order Detector

**Responsibility:** Detect orders that the broker no longer recognizes. Distinct from the timeout detector — a lost order has a broker response of `None`, not just elapsed time.

#### 5.4.1 Lost Order Criteria (all must hold)

1. `BrokerPollingAdapter.poll_order()` returns `None`
2. Order age ≥ `lost_order_min_age_minutes` (default: 60 min)
3. Order is not already in a terminal state

#### 5.4.2 Transient None (age < threshold)

Log warning, advance poll schedule, retry next cycle. Do not declare lost.

#### 5.4.3 Action on Lost Detection

1. Attempt `broker.cancel_order(order_id)` — idempotent (broker may already have it gone)
2. Update DB: `order.status = CANCELED`, add `detail = "LOST: broker returned None"`
3. Write `poll_lost_order` audit event with `{order_id, symbol, age_minutes}`
4. Unregister from poller
5. Call `on_timeout(order)` — NOT `on_filled`

#### 5.4.4 Configuration

```
lost_order_min_age_minutes: int = 60
```

---

### 5.5 Retry Logic

**Responsibility:** Per-entry backoff on poll failure; distinguish transient from permanent errors.

#### 5.5.1 Backoff Schedule

```
_POLL_INTERVALS = [10, 30, 60, 120, 300]  # seconds
```

After the last interval, schedule stays at 300 seconds.

**During broker outage** (`_broker_healthy == False`): backoff does NOT advance. `next_poll_at` is frozen. On reconnect, all entries are immediately re-queued (`next_poll_at = 0`).

#### 5.5.2 Error Classification (EX-14 Fix)

**Current bug (EX-14):** Any exception from `poll_order()` is treated as a generic warning and advances schedule once. An API timeout that clears immediately still burns one backoff cycle.

| Error Type | HTTP Status / Exception | Action |
|---|---|---|
| Network timeout | `requests.Timeout`, `ConnectionError` | Advance schedule; increment `entry.consecutive_errors` |
| Server error | 5xx | Same as network timeout |
| Auth failure | 401, 403 | Log critical; write `poll_auth_failed` audit; unregister order; halt polling for this broker |
| Rate limit | 429 | Freeze `next_poll_at` for 60s; do NOT increment `consecutive_errors` |
| Not found | 404 | Treat as `None` response; feeds Lost Order Detector |
| Client error | 4xx (other) | Log error; advance schedule once; do not increment `consecutive_errors` |

`entry.consecutive_errors` is reset to 0 on any successful `poll_order()` call.

---

### 5.6 Polling Health Monitor

**Responsibility:** Track broker health at the poller level. Gate all polling operations on `_broker_healthy`.

#### 5.6.1 State

```python
_broker_healthy: bool = True
_consecutive_broker_errors: int = 0     # incremented on every entry's network error
_unhealthy_since: Optional[float] = None  # monotonic; set on transition to unhealthy
```

#### 5.6.2 Health Transitions

**Healthy → Unhealthy:**
- When any `entry.consecutive_errors >= broker_error_threshold` (default: 3)
- Set `_broker_healthy = False`
- Freeze ALL entries: set `entry.next_poll_at = float('inf')` so no polls fire
- Record `_unhealthy_since = time.monotonic()`
- Write `poll_broker_unhealthy` audit event: `{consecutive_errors, threshold}`

**Unhealthy → Healthy:**
- On first successful `poll_order()` call after outage
- Set `_broker_healthy = True`
- Reset ALL entries: `entry.next_poll_at = 0` (immediate re-poll)
- Compute `outage_duration_seconds = time.monotonic() - _unhealthy_since`
- Add `outage_duration_seconds` to every `entry.outage_seconds` (pauses timeout clock)
- Write `poll_broker_reconnected` audit event: `{outage_duration_seconds}`
- Reset `_consecutive_broker_errors = 0`

#### 5.6.3 Timeout Clock Adjustment

`_PollEntry.is_timed_out` uses adjusted active time:

```python
@property
def is_timed_out(self) -> bool:
    elapsed = (datetime.now(timezone.utc) - self.registered_at).total_seconds()
    active_seconds = elapsed - self.outage_seconds
    return active_seconds > self._timeout_threshold_seconds
```

Timeout thresholds by state:
- NEW (SUBMITTED, no fills yet): 30 minutes
- PARTIAL_FILL: 60 minutes from last incremental fill

#### 5.6.4 Thread Crash Recovery (EX-10 Fix)

**Current bug (EX-10):** If the poller thread crashes, no restart mechanism exists.

`StrategyWorker` heartbeat (runs every 30 seconds) must check `poller._thread.is_alive()`. If the thread is dead:
1. Log critical error
2. Restart: `poller._thread = threading.Thread(target=poller._loop, daemon=True); poller._thread.start()`
3. All `_entries` are preserved in memory — the dict survives thread death
4. Write `poll_thread_restarted` audit event

---

### 5.7 Audit Trail

**Responsibility:** Append-only logging of every significant polling event. Write failures do NOT block polling.

#### 5.7.1 Events

| `event_type` | When | Key `detail` fields |
|---|---|---|
| `poll_registered` | `register()` called | `{order_id, symbol, side, qty, trigger}` |
| `poll_fill` | Full fill callback succeeded | `{order_id, symbol, fill_qty, fill_price, cumulative_qty, incremental_qty}` |
| `poll_partial_fill` | Partial fill callback succeeded | same + `{remaining_qty}` |
| `poll_terminal` | CANCELED/REJECTED/EXPIRED detected | `{order_id, symbol, final_status}` |
| `poll_timeout` | Timeout threshold exceeded | `{order_id, symbol, elapsed_minutes, last_status}` |
| `poll_lost_order` | `None` from broker AND age > threshold | `{order_id, symbol, age_minutes}` |
| `poll_duplicate_skipped` | L1 or L2 dedup triggered | `{order_id, reason, fill_event_id}` |
| `poll_broker_error` | `poll_order()` raised exception | `{order_id, error_type, error_msg, consecutive_errors}` |
| `poll_broker_unhealthy` | Error threshold crossed | `{consecutive_errors, threshold}` |
| `poll_broker_reconnected` | First success after outage | `{outage_duration_seconds}` |
| `poll_callback_failed` | `on_filled` threw exception | `{order_id, error_msg}` (fill NOT lost — will retry) |
| `poll_double_registration` | `register()` for already-registered ID | `{order_id}` |
| `poll_thread_restarted` | Poller thread was dead, restarted | `{}` |
| `poll_auth_failed` | 401/403 from broker | `{order_id, status_code}` |

#### 5.7.2 Write Contract

- Audit INSERT runs in a **separate DB session** from the fill transaction.
- Write order: audit `poll_fill` event is inserted BEFORE the fill DB transaction begins.
- If audit write fails: log warning, proceed with fill anyway (observability failure must not cause fill loss).
- If fill DB transaction fails after successful audit: write `poll_fill_db_failed` event in a separate attempt.
- `actor` field: always `"poller:kis"` or `"poller:kiwoom"` — never `"worker"`.

#### 5.7.3 Required DB Schema Changes (EX-17 Fix)

The `audit_logs` table currently lacks `run_id` and `severity` columns. Add:

```sql
ALTER TABLE audit_logs ADD COLUMN run_id VARCHAR(36);
ALTER TABLE audit_logs ADD COLUMN severity VARCHAR(10);  -- CRITICAL | HIGH | MEDIUM | LOW | INFO
CREATE INDEX IF NOT EXISTS idx_audit_logs_run_id ON audit_logs (run_id);
```

---

## 6. Duplicate Detection Policy

Three-layer hierarchy. All three layers apply independently; each is a backstop for the layers before it.

### Layer 1 — Incremental Qty Tracking (in-memory, zero-cost)

Always applied. Catches the common case of repeated poll with the same cumulative filled qty.

```python
incremental = updated.filled_qty - entry.last_reported_qty
if incremental <= 0:
    self._audit.write("poll_duplicate_skipped", reason="L1_no_increment")
    entry.advance()
    return
entry.last_reported_qty = updated.filled_qty
```

### Layer 2 — Fill Sequence ID (in-memory, per-order set)

Applied when `BrokerPollingAdapter.extract_fill_sequence_id()` returns a non-empty string.

```python
seq_id = self._adapter.extract_fill_sequence_id(updated)
if seq_id and seq_id in entry.fill_event_ids:
    self._audit.write("poll_duplicate_skipped", reason="L2_seen_seq_id", fill_event_id=seq_id)
    entry.advance()
    return
if seq_id:
    entry.fill_event_ids.add(seq_id)
```

`fill_event_ids` is cleared when the order reaches a terminal state. TTL guard: entries older than `fill_sequence_id_ttl_seconds` (default 3600) are pruned on each poll cycle to prevent unbounded growth.

### Layer 3 — DB Check Before Insert (transactional)

Applied in `_persist_fill()` before every `DBFill` INSERT:

```python
existing = db.query(DBFill).filter(
    DBFill.order_id == db_order_pk,
    DBFill.qty == fill_qty,
    DBFill.price == fill_price,
).first()
if existing:
    return  # duplicate — skip insert
```

### DB Backstop (EX-04 Fix)

Add a UNIQUE constraint to the `fills` table as the final defense against concurrent inserts that both pass the application-level L3 check:

```sql
-- Migration
ALTER TABLE fills ADD COLUMN filled_at_date DATE
    GENERATED ALWAYS AS (DATE(filled_at)) STORED;

CREATE UNIQUE INDEX IF NOT EXISTS uq_fills_order_qty_price_date
    ON fills (order_id, qty, price, filled_at_date);
```

This makes duplicate inserts fail at the DB level rather than silently succeeding.

---

## 7. Timeout Policy

### Configuration

```
PollingConfig:
  poll_intervals: list[int] = [10, 30, 60, 120, 300]   # seconds
  loop_sleep_secs: int = 5

  # Timeout thresholds (outage time excluded from clock)
  acknowledged_timeout_minutes: int = 30    # NEW → no fill
  partial_fill_timeout_minutes: int = 60    # PARTIAL_FILL → no further fill

  # Broker health
  broker_error_threshold: int = 3
  reconnect_immediate_poll: bool = True

  # Lost order detection
  lost_order_min_age_minutes: int = 60

  # L2 dedup
  fill_sequence_id_ttl_seconds: int = 3600
```

### Timeout Action

1. Log `poll_timeout` audit event: `{order_id, symbol, elapsed_minutes, last_status}`
2. Call `on_timeout(order)` callback
3. Unregister from poller

**Default `on_timeout` handler:** `logger.error("수동 취소 필요: ...")`  — no automatic cancel.

**Custom `on_timeout` handler** (injected at registration): may call `broker.cancel_order()`; must not raise; must be idempotent.

---

## 8. Recovery Policy

### 8.1 On Process Restart

**EX-06 Fix — Single Registration Point:**

Currently both `recovery.py` (line 290) and `runner.py` (line 651) call `poller.register()` for recovered orders, creating duplicate entries. The fix: only `recovery.py._step_pending_orders()` registers with the poller. `runner.py._register_recovered_order()` is deleted.

Recovery flow per pending order:

```
broker.poll_order() immediately
    │
    ├── FILLED       → apply fill pipeline via WorkerSession._build_on_filled()
    │                  do NOT register with poller
    │
    ├── CANCELED /
    │   REJECTED /
    │   EXPIRED      → update DB status only; do NOT register
    │
    ├── PARTIAL_FILL → apply incremental fill (broker cumulative − DB filled_qty)
    │                  poller.register() for remainder
    │
    ├── SUBMITTED    → poller.register()
    │
    ├── None AND
    │   age > 60m    → declare LOST; mark CANCELED in DB
    │
    └── None AND
        age ≤ 60m    → poller.register() for monitoring
```

**Single Fill Pipeline Rule:** `_make_recovery_fill_cb()` in `recovery.py` must be eliminated. Recovery uses `WorkerSession._build_on_filled()` — the same callback factory used at order placement time. One fill pipeline, one set of guards.

### 8.2 On Broker Reconnect

1. `_broker_healthy = True`
2. `next_poll_at = 0` for all registered entries (immediate re-poll)
3. Write `poll_broker_reconnected` audit event with `outage_duration_seconds`
4. Do NOT trigger `PositionReconciler` — the poller itself polls first; reconciler runs on its independent schedule

---

## 9. Reconciliation Compatibility

The poller and reconciler share ownership of open orders. The `is_registered()` method is the coordination primitive.

| Situation | Who Acts |
|---|---|
| `poller.is_registered(broker_order_id) == True` | Poller owns — reconciler observes only, inserts no Fill |
| Not in poller AND status mismatch found | Reconciler may apply `_sync_order_status()` |
| Poller reaches terminal state | Poller unregisters → reconciler takes over on next scheduled cycle |

**EX-11 Fix** — wire `is_registered()` guard into `PositionReconciler._sync_order_status()` before the fill insert:

```python
def _sync_order_status(self, db, broker_order_id, ...):
    if self._poller and self._poller.is_registered(broker_order_id):
        return   # yield to poller; it will handle the fill
    # ... existing fill logic
```

`PositionReconciler` already accepts `poller` as a constructor arg (`reconciler.py:106`). The guard just needs to be wired into the body of `_sync_order_status()`.

---

## 10. Restart Recovery Flow

```
Process starts
    │
    ▼
Phase 1: DB health check
    │  → fail: SAFE_MODE.disable("db_unavailable"), abort
    ▼
Phase 2: Broker connectivity (2 retries, 10s apart)
    │  → fail: SAFE_MODE.disable("broker_unavailable"), abort
    ▼
Phase 3: Restore risk state (PersistentLossTracker)
    │
    ▼
Phase 4: Fetch broker balance + positions (BrokerSnapshot)
    │
    ▼
Phase 5: Startup reconcile (PositionReconciler.reconcile("startup"))
    │  → CRITICAL gaps: block trading, alert operator
    ▼
Phase 6: Pending order recovery [SINGLE registration point — recovery.py only]
    For each DB order with status IN (submitted, partial_filled)
    AND broker_order_id IS NOT NULL:
    ├── FILLED       → apply WorkerSession._build_on_filled(); no poller.register()
    ├── CANCELED/
    │   REJECTED/
    │   EXPIRED      → update DB only
    ├── PARTIAL_FILL → apply incremental; poller.register() for remainder
    ├── SUBMITTED    → poller.register()
    ├── None+age>60m → mark LOST
    └── None+age≤60m → poller.register() for monitoring
    │
    ▼
Phase 7: Load PositionTracker from DB
    │  (after Phase 5 repairs any gaps)
    ▼
Phase 8: Enable trading (SAFE_MODE.enable())
    │  → if any CRITICAL gap from Phase 5: stay disabled
    ▼
Background (30s delay): PositionReconciler.reconcile("post_recovery")
```

---

## 11. EX-08 Fix — unmark_pending Guarantee

**Current bug (EX-08):** `_guarded_on_filled()` in `runner.py` has no `finally` block. If `on_filled_cb` raises an exception, `unmark_pending()` is never called. The symbol is permanently blocked from new orders for the rest of the session (or until the `_pending_symbols` TTL of 30 min expires).

**Required change to `runner.py`:**

```python
def _guarded_on_filled(self, order: Order, on_filled_cb: Callable) -> None:
    try:
        on_filled_cb(order)
    except Exception as e:
        logger.error("on_filled 콜백 오류: %s %s", order.id, e)
    finally:
        self._tracker.unmark_pending(order.symbol)   # always called
```

This ensures `unmark_pending()` runs regardless of callback outcome.

---

## 12. BrokerSemanticMapper Integration

The poller delegates all broker-response parsing to `BrokerSemanticMapper` via the `BrokerPollingAdapter`.

```
OrderFillPoller._poll_one(entry)
    │
    ▼
BrokerPollingAdapter.poll_order(order_id, symbol)
    │
    ▼
broker.get_order_status() → Order (with raw: dict)
    │
    ▼
mapper.map_status(raw, filled_qty, ord_qty)         → OrderStatus
mapper.extract_filled_qty(raw)                       → int
mapper.extract_avg_price(raw)                        → float
mapper.extract_fill_sequence_id(raw)                 → str   ← NEW
    │
    ▼
BrokerPollingAdapter returns Order to poller
    │
    ▼
StateTransitionValidator.validate(old_status, new_status)
    │
    ▼
L1/L2 dedup checks
    │
    ▼
on_filled(order) callback  →  fill pipeline
```

**New method required on `BrokerStatusMapper` ABC:**

```python
@abstractmethod
def extract_fill_sequence_id(self, raw: dict) -> str:
    """Return broker fill sequence ID, or '' if not available."""
```

**Implementations:**
- `KISDomesticMapper`: `return raw.get("output", {}).get("ODNO", "")`
- `KISOverseasMapper`: `return raw.get("output", {}).get("ODNO", "")`
- `KiwoomDomesticMapper`: `return ""` (placeholder until verified)

---

## 13. New Code Requirements

| File | Change Required |
|---|---|
| `backend/execution/order_poller.py` | Pop-after-callback; `is_registered()`; `_broker_healthy`; `fill_event_ids`; dedup `register()`; `entry.order = updated` inside lock; inject `BrokerPollingAdapter` |
| `backend/brokers/polling_adapter.py` | **NEW FILE** — `BrokerPollingAdapter` ABC + KIS/Kiwoom implementations |
| `backend/brokers/semantic_mapper.py` | Add `extract_fill_sequence_id()` to ABC and all 3 mappers; fix `KiwoomDomesticMapper.map_status()` stub |
| `backend/execution/reconciler.py` | Wire `is_registered()` guard in `_sync_order_status()` before fill insert |
| `backend/worker/recovery.py` | Delete `_make_recovery_fill_cb()`; use `WorkerSession._build_on_filled()` for recovery fills |
| `backend/worker/runner.py` | Add `finally: unmark_pending()` in `_guarded_on_filled()`; delete `_register_recovered_order()` |
| `backend/database/models.py` | Add `run_id`, `severity` to `audit_logs`; add UNIQUE constraint to `fills` table |

---

## 14. Full Audit Finding Coverage

| Finding | Severity | Fix Location | Component |
|---|---|---|---|
| EX-01 No atomicity tracker↔DB | HIGH | fill pipeline (runner.py) | §5.1 + §6 (single pipeline) |
| EX-02 Entry popped before callback | CRITICAL | `order_poller.py` | §5.1.2 |
| EX-03 Partial fill skips error logging | MEDIUM | `order_poller.py` | §5.1.2 |
| EX-04 No UNIQUE on fills table | HIGH | DB migration | §6 (DB Backstop) |
| EX-05 `restore_positions()` no runtime guard | MEDIUM | `position_tracker.py` | out-of-scope (separate fix) |
| EX-06 Double registration | CRITICAL | `recovery.py` + `runner.py` | §5.1.4, §8.1 |
| EX-07 Terminal state not persisted if on_filled throws | CRITICAL | `order_poller.py` | §5.1.2 (pop-after-callback) |
| EX-08 `unmark_pending` not called on exception | HIGH | `runner.py` | §11 |
| EX-09 Redis message loss not handled | MEDIUM | `runner.py` scheduler | out-of-scope (command polling fallback) |
| EX-10 No poller thread crash detection | HIGH | `runner.py` heartbeat | §5.6.4 |
| EX-11 Reconciler inserts fill when poller owns | HIGH | `reconciler.py` | §9 |
| EX-12 No fill_sequence_id | HIGH | `order_poller.py`, `polling_adapter.py` | §5.2, §6 (L2) |
| EX-13 KiwoomDomesticMapper returns UNKNOWN | CRITICAL | `semantic_mapper.py` | §5.2.3 |
| EX-14 No retry differentiation on API errors | MEDIUM | `order_poller.py` | §5.5.2 |
| EX-15 `entry.order` written outside lock | MEDIUM | `order_poller.py` | §5.1.3 |
| EX-16 `process_fill` no FillEvent dedup | HIGH | `order_machine.py` | §5.3 (validate before process_fill) |
| EX-17 AuditLog missing `run_id` | MEDIUM | DB migration | §5.7.3 |
| EX-18 `fill_event_ids` TTL / memory leak | LOW | `order_poller.py` | §6 (L2 TTL) |
| EX-19 `unregister()` on terminal before callback | CRITICAL | `order_poller.py` | §5.1.2 |

---

## 15. Verification

```bash
# State transition validation
pytest tests/execution/test_order_machine.py -v -k transition

# Pop-after-callback: callback failure does not lose fill; retried next cycle
pytest tests/execution/test_order_poller.py -v -k callback_failure_retries

# Partial fill pipeline: 3 partials each fire on_filled with incremental qty
pytest tests/execution/test_order_poller.py -v -k partial

# Dedup L1: same cumulative qty on two polls → 1 callback, not 2
pytest tests/execution/test_order_poller.py -v -k idempotent_l1

# Dedup L2: same ODNO sequence ID → callback skipped
pytest tests/execution/test_order_poller.py -v -k idempotent_l2

# Double-registration: second register() for same order_id is a no-op
pytest tests/execution/test_order_poller.py -v -k double_registration

# Reconnect: broker down → all entries frozen → up → all entries re-polled immediately
pytest tests/execution/test_order_poller.py -v -k reconnect

# Timeout clock: outage time excluded from elapsed threshold
pytest tests/execution/test_order_poller.py -v -k timeout_outage

# Thread restart: poller thread dead → heartbeat restarts it, entries preserved
pytest tests/execution/test_order_poller.py -v -k thread_restart

# Recovery: single registration path; fill detected at restart applied via live callback
pytest tests/worker/test_recovery.py -v -k pending_orders_single_registration

# Recovery dedup: no _make_recovery_fill_cb; recovery uses _build_on_filled
pytest tests/worker/test_recovery.py -v -k recovery_uses_live_pipeline

# Reconciler yields to poller: no fill insert when poller owns the order
pytest tests/execution/test_reconciler.py -v -k is_registered

# unmark_pending always called even if on_filled throws
pytest tests/worker/test_runner.py -v -k unmark_pending_finally

# DB UNIQUE constraint: concurrent inserts for same (order_id, qty, price, date) → second fails
pytest tests/database/test_fills_unique.py -v
```
