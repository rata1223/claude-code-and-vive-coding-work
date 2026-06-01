# Reconciliation Engine — Design Specification

## 1. Purpose and Scope

The reconciliation engine is the single authoritative mechanism that aligns DB
state with broker state. It is the circuit-breaker between "what we think we
hold" and "what the broker actually holds". Its output is an append-only audit
log; its policy governs when state may be mutated automatically and when a human
must intervene.

**Broker is always ground truth for position quantities.**
**DB is always ground truth for trade intent (orders placed by this system).**

---

## 2. Architecture Overview

```
                     ┌─────────────────────────┐
                     │    ReconciliationEngine  │
                     │                         │
  Broker API ───────►│  BrokerSnapshot         │
  DB ───────────────►│  DBSnapshot             │
                     │          │              │
                     │    MismatchDetector      │
                     │          │              │
                     │    SeverityClassifier    │
                     │          │              │
                     │    RecoveryPolicyEngine  │
                     │          │              │
                     │    AuditWriter           │
                     └─────────────────────────┘
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
         AutoSync      ManualReview    EmergencyStop
         (DB write)    (alert only)    (halt trading)
```

Reconciliation runs are **idempotent** and **serialized per broker** (one
concurrent run per broker via non-reentrant lock). A run that cannot acquire the
lock within its timeout window emits a `LOCK_CONTENTION` audit event and returns
without mutating state.

---

## 3. Data Structures

### 3.1 BrokerSnapshot

Fetched at the start of every reconciliation run. All fields carry a
`fetched_at` timestamp. Stale snapshots (age > configurable threshold) abort the
run with a `BROKER_DATA_STALE` error rather than applying potentially-wrong
repairs.

```
BrokerSnapshot
  broker_name: str
  fetched_at: datetime (UTC)
  positions: list[BrokerPosition]
  open_orders: list[BrokerOrder]
  balance: BrokerBalance

BrokerPosition
  symbol: str
  qty: int
  avg_price: float
  market: str          # "KR" | "US"
  current_price: float | None

BrokerOrder
  broker_order_id: str
  symbol: str
  side: str            # "buy" | "sell"
  qty: int
  filled_qty: int
  remaining_qty: int   # qty - filled_qty
  status: OrderStatus
  avg_fill_price: float | None

BrokerBalance
  cash_krw: float
  cash_usd: float
  total_eval_krw: float
  buying_power_krw: float | None
```

### 3.2 DBSnapshot

Extracted from DB at the start of the same run, within the same logical
"reconciliation window". Extracted as plain dicts to avoid SQLAlchemy lazy-load
bugs after session close.

```
DBSnapshot
  broker_name: str
  snapped_at: datetime (UTC)
  positions: dict[symbol → DBPositionRow]
  open_orders: dict[broker_order_id → DBOrderRow]
  latest_equity: DBEquityRow | None

DBPositionRow
  id: int
  symbol: str
  qty: int
  avg_price: float
  updated_at: datetime | None

DBOrderRow
  id: int
  broker_order_id: str
  symbol: str
  side: str
  qty: int
  filled_qty: int
  status: str          # raw enum value string
  created_at: datetime

DBEquityRow
  total_krw: float
  cash_krw: float
  cash_usd: float
  snapped_at: datetime
```

### 3.3 ReconciliationGap

Every detected mismatch produces exactly one `ReconciliationGap`. Gaps are
never auto-merged or coalesced; the audit log sees every individual gap.

```
ReconciliationGap
  gap_id: str          # UUID, stable across retry of same mismatch
  domain: GapDomain    # POSITION | ORDER | PORTFOLIO
  severity: Severity   # CRITICAL | HIGH | MEDIUM | LOW
  symbol: str | None
  broker_order_id: str | None
  kind: str            # machine-readable subtype (see §4–§6)
  broker_value: Any    # broker's version of the field
  db_value: Any        # our version of the field
  detail: str          # human-readable description
  detected_at: datetime
  auto_repairable: bool
  repair_action: RecoveryAction | None
```

---

## 4. Position Reconciliation

For every symbol held either in broker or DB:

### 4.1 Comparison Fields

| Field | Tolerance | Gap Kind |
|---|---|---|
| qty | ±1 share (configurable) | `POSITION_QTY_MISMATCH` |
| avg_price | >0.01 absolute | `POSITION_PRICE_DRIFT` |
| symbol present in broker only | — | `POSITION_MISSING_IN_DB` |
| symbol present in DB only | — | `POSITION_STALE_IN_DB` |

Unrealized P&L and realized P&L are **derived fields** computed from qty ×
(current_price − avg_price). They are not reconciled independently; reconciling
qty and avg_price reconciles P&L implicitly.

### 4.2 Severity Rules — Positions

| Condition | Severity | Rationale |
|---|---|---|
| qty diff > 5 shares OR qty diff > 5% of position | CRITICAL | Large unknown exposure |
| qty diff ≤ tolerance AND open order exists | LOW | Expected in-flight partial |
| qty diff > tolerance AND open order exists | MEDIUM | Gap larger than order size — abnormal |
| qty diff ≤ tolerance AND no open order | MEDIUM | Small rounding drift |
| symbol in broker only (unknown position) | HIGH | Untracked exposure |
| symbol in DB only, age < 1h | LOW | Recently opened, poller may not have synced |
| symbol in DB only, age ≥ 1h, no open order | HIGH | Likely manual close not recorded |
| avg_price drift only (qty matches) | LOW | Rounding, always auto-fixable |

### 4.3 Auto-Repair Conditions — Positions

Auto-repair is allowed ONLY when ALL of the following hold:

1. `dry_run = False`
2. Severity is LOW or MEDIUM
3. No open order for the symbol with status PENDING or SUBMITTED
   (partial_filled orders are accepted — fill gap is separate from qty gap)
4. Broker snapshot age ≤ 2 minutes (freshness guard)
5. The repair has not been applied for this symbol in the last 10 minutes
   (prevents thrash loop if broker data is noisy)

Auto-repair is **prohibited** (manual review required) when:

- Severity is CRITICAL or HIGH
- An open PENDING or SUBMITTED order exists for the symbol
- The position change represents > 5% of total portfolio equity
- The reconciler has already auto-repaired this symbol twice in 30 minutes
  (detected via AuditLog query — prevents infinite correction loops)
- `KR_BROKER=kiwoom` and broker is KIS, or vice versa (wrong broker is
  attempting to fix a position it does not own)

---

## 5. Order Reconciliation

For every order in DB with status ∈ {pending, submitted, partial_filled, unknown}:

### 5.1 Comparison Fields

| Field | Gap Kind |
|---|---|
| DB status ≠ broker status | `ORDER_STATUS_MISMATCH` |
| DB filled_qty ≠ broker filled_qty | `ORDER_FILL_DIVERGENCE` |
| broker_order_id missing in DB | `ORDER_NO_BROKER_ID` |
| broker returns None for a known order_id | `ORDER_LOST_AT_BROKER` |
| remaining_qty = 0 but status not FILLED | `ORDER_FILL_NOT_RECORDED` |

### 5.2 Severity Rules — Orders

| Condition | Severity |
|---|---|
| DB=submitted, broker=filled (fill never recorded) | CRITICAL |
| DB=submitted, broker=canceled (never surfaced to system) | HIGH |
| DB=partial_filled, broker=filled (fill gap) | HIGH |
| DB=submitted, broker=rejected | HIGH |
| DB=submitted, broker=None, age > 1h | HIGH |
| DB=submitted, broker=None, age < 1h | MEDIUM |
| DB filled_qty < broker filled_qty | MEDIUM |
| DB filled_qty > broker filled_qty | CRITICAL (impossible — indicates state machine bug) |
| broker_order_id missing, age < 30s | LOW (order not yet acknowledged) |
| broker_order_id missing, age > 30s | HIGH (placement may have failed silently) |

### 5.3 Auto-Repair Conditions — Orders

**Safe to auto-repair (no Fill record created):**
- Status drift from submitted → canceled or rejected: update DB status, close order
- Status drift from submitted → partial_filled: update DB status, do not insert Fill

**Safe to auto-repair with Fill creation:**
- DB status = submitted, broker status = filled, NO existing Fill row for this
  order_id — insert exactly one Fill record, update order status
- The Fill insert MUST be guarded by a DB-level unique constraint on
  `(order_id)` for terminal fills (see §12.1)
- The reconciler MUST check `poller.is_registered(broker_order_id)` before
  creating a Fill — if the poller owns the order, let the poller win

**PROHIBITED — always manual review:**
- DB filled_qty > broker filled_qty (data corruption; never auto-correct)
- Any order whose position would change by > 5% of portfolio equity
- Any order that is also currently registered in the OrderFillPoller
  (let the poller win; reconciler defers)
- Any order where a Fill record already exists but status mismatch remains
  (indicates state machine divergence; requires manual investigation)

### 5.4 Lost Order Policy

An order is declared lost when:
- `broker.get_order_status()` returns None
- Order age > 1 hour
- Order is not found in any broker order history

Lost order recovery (auto):

1. Attempt broker cancel (idempotent — broker may already have it gone)
2. Set DB status = CANCELED
3. Write `LOST_ORDER` AuditLog event with full order snapshot
4. Do NOT create a Fill record for lost orders

---

## 6. Portfolio Reconciliation

Runs after position and order reconciliation. Uses the EquitySnapshot table as
DB ground truth.

### 6.1 Comparison Fields

| Field | Tolerance | Gap Kind |
|---|---|---|
| Total equity (KRW) | 1% of equity | `PORTFOLIO_EQUITY_DRIFT` |
| Cash KRW | ₩1,000 | `PORTFOLIO_CASH_DRIFT` |
| Cash USD | $1.00 | `PORTFOLIO_CASH_USD_DRIFT` |
| Buying power | 5% | `PORTFOLIO_BUYING_POWER_DRIFT` |

### 6.2 Severity Rules — Portfolio

| Condition | Severity |
|---|---|
| Equity drift > 5% | CRITICAL |
| Equity drift 1–5% | HIGH |
| Equity drift < 1% | LOW |
| Cash drift > ₩100,000 | HIGH |
| Broker balance fetch failed | HIGH (use last known snapshot, log gap) |
| Last equity snapshot > 24h old | MEDIUM |

### 6.3 Auto-Repair Conditions — Portfolio

Portfolio reconciliation is **observability-only**. No auto-repair is performed
for portfolio values. All portfolio gaps produce an AuditLog entry and optional
alert. The drift values inform DailyRiskState calculations but are never
auto-corrected in the DB without explicit operator action.

Rationale: Portfolio drift can reflect legitimate in-flight settlement (T+2
cash), dividend payments, FX movements, or fee deductions not captured in
fill records. Auto-correcting equity or cash can mask real losses and invalidate
the kill-switch evaluation.

---

## 7. Severity Levels

### CRITICAL

**Definition:** State divergence that could cause significant financial loss or
incorrect risk calculations if left unresolved for > 5 minutes.

**Examples:**
- DB=SUBMITTED, broker=FILLED — fill never recorded, position wrong
- DB filled_qty > broker filled_qty — state machine corruption
- Position qty diff > 5 shares with no open order
- Equity drift > 5%

**Response:** Immediately emit alert (Telegram + WebSocket). Block new orders
for the affected symbol — or all symbols if portfolio-level. Require manual
sign-off before any auto-repair. Log to AuditLog with
`event_type = reconcile_critical`.

### HIGH

**Definition:** State divergence that will cause wrong behavior within the
current trading session if not addressed, but does not require immediate halt.

**Examples:**
- DB=submitted, broker=canceled (order silently gone)
- Unknown position at broker (untracked exposure)
- Stale DB position with age ≥ 1h and no pending order
- broker_order_id missing for order > 30s old

**Response:** Emit alert. Auto-repair only if all auto-repair conditions are
met. Otherwise queue for manual review. Log to AuditLog.

### MEDIUM

**Definition:** State divergence that does not affect live trading decisions
today but will compound into a CRITICAL gap if left unaddressed.

**Examples:**
- Position qty diff within tolerance with open order
- DB partial_filled qty lower than broker
- Missing broker_order_id for order < 30s old

**Response:** Log to AuditLog. Auto-repair if conditions allow. No alert unless
three or more MEDIUM gaps for the same symbol appear within one session.

### LOW

**Definition:** Known-harmless drift or expected transient states.

**Examples:**
- avg_price rounding drift (< 0.01)
- Position too young to be considered stale (< 1h)
- Partial fill within tolerance

**Response:** Log to AuditLog at DEBUG level. Auto-repair silently. No alert.

---

## 8. Recovery Actions

### 8.1 AutoSync

Applies a specific DB write to bring DB state into alignment with broker state.
Each AutoSync action is:
- **Atomic** — single DB transaction
- **Idempotent** — safe to run twice with same outcome
- **Pre-logged** — AuditLog intent written before execution
- **Reversible** — the log contains the before-state; manual rollback is possible

| Action | What it does | When permitted |
|---|---|---|
| `SYNC_POSITION_QTY` | Update Position.qty and avg_price to broker values | Severity LOW/MEDIUM, no open order, fresh snapshot |
| `SYNC_POSITION_PRICE` | Update Position.avg_price only | Severity LOW, qty matches |
| `INSERT_POSITION` | Create new DBPosition from broker | External buy detected; HIGH needs manual override |
| `DELETE_POSITION` | Remove stale DBPosition | Age ≥ stale threshold, no open order |
| `SYNC_ORDER_STATUS` | Update DBOrder.status from broker | Severity LOW/MEDIUM; FILLED requires Fill guard |
| `INSERT_FILL` | Create DBFill for order confirmed filled at broker | Guarded by unique constraint; poller must not own it |
| `MARK_ORDER_LOST` | Set DBOrder.status=CANCELED | Age > 1h, broker returns None |
| `INSERT_EQUITY_SNAPSHOT` | Persist broker balance as EquitySnapshot | Always; observability only |

### 8.2 ManualReview

Emits a gap record, writes an AuditLog event, and optionally sends an alert.
Does NOT mutate DB state. Places an advisory Redis lock
(`reconcile:manual_hold:{broker}:{symbol}`) so subsequent auto-reconcile runs
skip this symbol until the hold is cleared by an operator or by a manual repair
API call (`POST /api/admin/reconcile/clear-hold`).

ManualReview is triggered by:
- CRITICAL or HIGH severity gaps that fail auto-repair preconditions
- Any symbol that was auto-repaired twice in 30 minutes
- `dry_run=True` (all gaps become ManualReview regardless of severity)

### 8.3 EmergencyStop

Calls `SAFE_MODE.disable(reason)` to halt all new order placement. Fires the
Telegram emergency alert and WebSocket broadcast. Logs
`reconcile_emergency_stop` AuditLog event.

EmergencyStop is triggered by:
- CRITICAL gap AND broker snapshot is confirmed fresh (eliminating stale data
  as the cause)
- DB filled_qty > broker filled_qty (invariant violation)
- Three or more CRITICAL gaps in a single reconcile run
- Reconciler itself raises an unhandled exception (fail-safe: prefer halt over
  continuing in uncertain state)

EmergencyStop does **not** automatically re-enable trading. An operator must
call `POST /api/admin/resume` or restart the worker with `--clear-safe-mode`.

---

## 9. Append-Only Audit Behavior

### 9.1 AuditLog Contract

Every reconciliation action — gap detected, repair applied, review queued,
emergency stop triggered — produces exactly one AuditLog row. The AuditLog is:

- **Append-only**: no UPDATE or DELETE is ever issued against it
- **Pre-action**: intent is logged BEFORE the repair is applied; if the repair
  fails, the intent is still in the log
- **Before-state captured**: every repair log includes the DB value at time
  of detection, not after repair
- **Correlation ID**: every run produces a `run_id` (UUID); all events from
  the same run share this ID

Required AuditLog fields (additions to current schema):

```
AuditLog
  id: int (PK)
  event_type: str     # reconcile_gap | reconcile_repair | reconcile_review |
                      # reconcile_emergency_stop | reconcile_critical | fill
  symbol: str | None
  order_id: str | None        # broker_order_id
  actor: str                  # "reconciler:kis" | "worker" | "manual"
  detail: JSON                # full before/after snapshot
  severity: str | None        # CRITICAL | HIGH | MEDIUM | LOW
  run_id: str | None          # UUID — correlates all events in one run
  created_at: datetime        # UTC, indexed, never null
```

### 9.2 Audit Failure Policy

AuditLog writes run in a **separate DB transaction** from the repair itself.
Order of operations for every auto-repair:

1. Write `reconcile_gap` event (pre-action intent) → commit
2. Apply repair in separate transaction → commit
3. On success: write `reconcile_repair` event → commit
4. On failure: write `reconcile_repair_failed` event → commit

**If step 1 fails**, the repair is not applied. The reconciler logs a warning
and skips to the next gap. A repair without an audit trail is not performed.

---

## 10. Restart Recovery Flow

On process startup, reconciliation runs in the following phases in order:

### Phase 1 — DB Health Check
Ping the database. On failure: enter SAFE_MODE, emit alert, abort startup.

### Phase 2 — Broker Connectivity
Fetch broker balance with a 30-second timeout. On failure: retry once after
10 seconds. If second attempt fails: enter SAFE_MODE, abort startup.

### Phase 3 — Pending Order Recovery

For each DBOrder with status ∈ {pending, submitted, partial_filled}:

1. Call `broker.get_order_status(broker_order_id)` for each order
2. Compare broker status to DB status
3. **Broker = FILLED, DB ≠ FILLED:**
   - Apply `INSERT_FILL` via the single shared fill pipeline (see §12.2)
   - Apply `SYNC_ORDER_STATUS`
   - The fill pipeline updates PositionTracker in-memory
4. **Broker = CANCELED, DB ≠ CANCELED:**
   - Apply `SYNC_ORDER_STATUS` only; do not insert Fill
5. **Broker = None, age > 1h:** apply `MARK_ORDER_LOST`
6. **Broker = None, age ≤ 1h:** re-register with OrderFillPoller for live
   monitoring using the same fill callback as placement time
7. **Broker = still open:** re-register with OrderFillPoller

**Critical constraint:** The recovery path uses the identical fill persistence
function as the main execution path. There is no separate recovery fill
callback. See §12.2.

### Phase 4 — Position Reconciliation

Run full position reconciliation with `trigger="startup"`. Broker is ground
truth. Apply auto-repairs per policy in §4.3. Emit AuditLog for all gaps.

### Phase 5 — Restore PositionTracker

Load DBPosition rows for this broker into the in-memory PositionTracker. This
happens AFTER Phase 4 so the positions loaded reflect repairs just applied.

### Phase 6 — Portfolio Snapshot

Fetch broker balance and write an EquitySnapshot row. Observability only; no
auto-repair.

### Phase 7 — Enable Trading

If all phases passed without CRITICAL gaps or EmergencyStop:
call `SAFE_MODE.enable()`. Log `startup_reconcile_ok` audit event.

If any phase produced a CRITICAL gap: keep SAFE_MODE disabled, send operator
alert, log `startup_blocked` event.

---

## 11. Broker Reconnect Behavior

Broker reconnect (detected by heartbeat or by API errors clearing after an
outage) triggers a **partial reconciliation**:

1. Fetch fresh BrokerSnapshot (positions + open orders only; skip balance)
2. Run position reconciliation with `trigger="reconnect"`, `dry_run=False`
3. Run pending-order reconciliation for orders placed during the outage window
   (created_at ≥ disconnect_detected_at)
4. Skip portfolio reconciliation on reconnect — T+2 settlement state is
   ambiguous after a brief outage
5. Log `broker_reconnect_reconcile` audit event with gap/repair summary

**During a broker outage** (circuit breaker open):
- No reconcile runs attempt broker API calls
- Existing open orders remain in their last-known status in DB
- SAFE_MODE is not automatically disabled solely due to reconcile inability
- Heartbeat monitor is responsible for alerting; reconciler waits for the
  circuit breaker to close

---

## 12. Idempotency and Fill Deduplication

### 12.1 Fill Uniqueness

The `fills` table requires a database-level constraint to enforce exactly-once
fill recording. Application-level deduplication alone is insufficient due to
TOCTOU races between the reconciler and the OrderFillPoller callback.

**Recommended constraint:**

For terminal (complete) fills: `UNIQUE(order_id)` — an order_id has at most
one fill record when the order is fully filled.

For partial fills: the broker-provided fill sequence number (KIS provides `ODNO`
per fill event for domestic orders) must be included in the dedup key:
`UNIQUE(order_id, broker_fill_seq)`.

The current dedup key `(order_id, qty, price)` is inadequate because two
legitimate partial fills at identical size and price cannot be distinguished.

### 12.2 Single Fill Pipeline Rule

There is exactly **one** fill pipeline function in the entire codebase:

```python
persist_fill(order_id: str, qty: int, price: float,
             fill_source: str, broker_fill_seq: str | None = None) -> bool
```

This function is called by:
- OrderFillPoller callback (live fills during trading)
- Reconciler `_sync_order_status()` (catch-up fills discovered at reconcile time)
- Startup recovery (pending order fills detected at restart, Phase 3)

It is not acceptable to have separate fill persistence implementations in
`runner.py`, `recovery.py`, and `persistence.py`. One function, one set of
guards, one place to maintain idempotency logic.

### 12.3 OrderStateMachine Idempotency

`OrderStateMachine.process_fill(event)` must be idempotent for the same
FillEvent. Two mechanisms are viable:

**Option A:** Accept a `fill_id` parameter; maintain a per-order seen-set;
silently return if already seen.

**Option B:** Assert `filled_qty + event.filled_qty ≤ order.qty` before
applying; raise `ValueError` if the invariant would be violated.

Option B is preferred because it makes the double-apply visible as an error
rather than silently swallowing it.

---

## 13. When NOT to Mutate State Automatically

State mutation is prohibited when ANY of the following is true:

1. **`dry_run=True`** — always read-only regardless of severity
2. **Gap severity is CRITICAL or HIGH** and auto-repair preconditions are not
   fully met
3. **An open SUBMITTED or PENDING order exists for the symbol** and the gap is
   a position quantity mismatch (in-flight fills make broker position transient)
4. **Broker snapshot is stale** (age > 2 minutes for position quantity changes;
   > 5 minutes for avg_price-only changes)
5. **The symbol has been auto-repaired twice in 30 minutes** (thrash detection)
6. **A ManualReview hold is active for the symbol** (operator has taken ownership)
7. **The reconciler does not own the broker** — `broker_name` filter must be
   enforced on all DB queries; KIS reconciler must not touch Kiwoom positions
8. **DB filled_qty > broker filled_qty** — invariant violation indicating a
   state machine bug; mutating it would destroy evidence; trigger EmergencyStop
9. **The repair would create a Fill record for an order that already has one**
   (check for existing Fill before insert; enforce via DB constraint)
10. **EmergencyStop is active** — no auto-repairs while system is halted;
    all gaps become ManualReview

---

## 14. Configuration

```python
@dataclass
class ReconcilerConfig:
    broker_name: str                          # "kis" | "kiwoom"
    qty_tolerance: int = 1                    # ±shares before qty gap flagged
    price_tolerance: float = 0.01             # absolute avg_price drift threshold
    stale_position_min_age_hours: float = 1.0
    broker_snapshot_max_age_seconds: int = 120
    auto_repair_cooldown_minutes: int = 10
    auto_repair_max_per_symbol_per_session: int = 2
    large_position_change_pct: float = 0.05   # 5% of portfolio → block auto-repair
    emergency_stop_on_critical: bool = True
    dry_run: bool = False
    alert_on_high: bool = True
    alert_on_critical: bool = True
    lost_order_age_hours: float = 1.0
```

---

## 15. Integration Points

| Component | Interface | Notes |
|---|---|---|
| OrderFillPoller | `poller.is_registered(order_id) → bool` | Reconciler defers if poller owns the order |
| OrderStateMachine | `machine.get(order_id) → Order?` | Check in-memory state before DB repair |
| PositionTracker | `tracker.restore_positions(positions)` | Called after startup Phase 4 |
| SAFE_MODE | `SAFE_MODE.disable(reason)` | Called on EmergencyStop |
| AuditLog | DB append, separate session | Never in same transaction as repair |
| Telegram | `alert_emergency(msg)` | On EmergencyStop and CRITICAL gaps |
| WebSocket | `publish_alert(msg, level)` | On HIGH+ gaps |
| Redis | Advisory locks `reconcile:lock:{broker}` | Prevent concurrent runs across processes |

---

## 16. Mismatch Scenarios Reference

| Scenario | Domain | Severity | Action |
|---|---|---|---|
| DB qty=10, broker qty=9, no open order | POSITION | MEDIUM | AutoSync if cooldown OK |
| DB qty=10, broker qty=9, open order exists | POSITION | LOW | Log only; wait for poller |
| DB=FILLED, broker=PARTIAL_FILLED | ORDER | CRITICAL | ManualReview; EmergencyStop if confirmed |
| DB=SUBMITTED, broker=CANCELED | ORDER | HIGH | AutoSync status; no Fill |
| DB=OPEN, broker returns None, age > 1h | ORDER | HIGH | MARK_ORDER_LOST |
| DB position exists, broker has none, age < 1h | POSITION | LOW | Log only |
| DB position exists, broker has none, age ≥ 1h | POSITION | HIGH | ManualReview or AutoSync |
| Broker has position, DB has none | POSITION | HIGH | INSERT_POSITION; alert |
| DB filled_qty > broker filled_qty | ORDER | CRITICAL | EmergencyStop; no auto-repair |
| Equity drift > 5% | PORTFOLIO | CRITICAL | Alert; no auto-repair |

---

## 17. Mapping to TASK 2-1A Audit Findings

Each of the 10 audit findings is addressed by a specific design element:

| Finding | Root Cause | Design Fix | Section |
|---|---|---|---|
| F1: `_apply_fill_to_position_db` undefined | Recovery has separate fill path | Single fill pipeline rule | §12.2 |
| F2: Fill dedup drops identical partials | Dedup key too narrow | Broker fill seq in unique key | §12.1 |
| F3: TOCTOU duplicate Fill | No DB unique constraint | `UNIQUE(order_id)` + poller check | §12.1, §5.3 |
| F4: `submit()` orphan key | Old key not deleted | State machine must delete old key | §12.3 |
| F5: `_handle_market_open()` race | No lock on dedup gate | Lock required around read-check-write | §13 |
| F6: `process_fill()` no idempotency | No seen-event guard | Invariant assertion before apply | §12.3 |
| F7: Recovery callback no dedup | Recovery has separate fill path | Single fill pipeline rule | §12.2 |
| F8: String literals in status query | Type-unsafe DB filter | Use `OrderStatus.X.value` in all queries | §5.3 |
| F9: Stale equity in kill-switch | Live balance fetch on fill path | Portfolio is observability-only; use snapshots | §6.3 |
| F10: `persistence.persist_fill` no guard | Duplicate implementation | Single fill pipeline; persistence helper retired | §12.2 |
