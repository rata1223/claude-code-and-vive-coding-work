# Trading Platform — Operational Philosophy & Survivability Constraints

> This document establishes the foundational constraints that govern all architecture and implementation decisions.
> Every design choice must be defensible against the axioms stated here.
> Read this before touching any execution, state, or recovery code.

---

## 1. Axiomatic Failure Model

These are not edge cases. They are the normal operating environment.

| Failure Class | Assumed Frequency |
|---|---|
| KIS API timeout / 5xx | Daily |
| WebSocket duplicate events | Every session |
| Fills arriving after order-assumed-failed | Weekly |
| Redis process restart | Weekly (deploy/OOM) |
| Worker process crash mid-order-cycle | Weekly |
| DB write succeeds but response lost | Monthly |
| Partial DB commit (power loss / OOM kill) | Low but non-zero |
| Stale market data served as current | Daily (off-hours) |
| Broker position diverges from local DB | After every crash |
| Clock skew between containers | Always |

Design every component assuming all of these happen simultaneously, independently, and without warning.

---

## 2. State Ownership Semantics

There are exactly three state authorities. Each owns a non-overlapping domain.

### 2.1 The Broker Is The Execution Oracle

The broker (KIS API) is the **sole authority** for:
- What orders are actually open
- What fills actually occurred
- What positions are actually held
- What cash balance is actually available

No local state contradicts the broker. If local DB says "position closed" and broker says "position open", **broker is correct**. Local DB is wrong and must be corrected, never the reverse.

### 2.2 The Database Is The Intent Ledger

The database is the **sole authority** for:
- What the system intended to do (order intent)
- What the system believes happened (fill records)
- Risk state at last known checkpoint
- Strategy configuration and run history

The DB records what we *tried* and what we *observed*. It is an append-only audit trail, not a live mirror of broker state. DB records are **never deleted**, only superseded by newer records.

### 2.3 Redis Is Ephemeral Infrastructure

Redis owns **nothing that cannot be reconstructed**:
- Token cache (reconstructed by re-authenticating)
- Rate-limit counters (reset on restart is safe; worst case is a slower first window)
- Pub/Sub message bus (messages in flight at crash time are lost and must be re-derived)

If Redis is empty, the system must function correctly. Any code path that fails because Redis is empty is a bug.

### 2.4 In-Memory State Is A Derived View

In-process variables, order books, position caches — these are always derived from DB + broker reconciliation. They are rebuilt on startup. They are never persisted directly. They are never the source of truth for any decision.

---

## 3. Execution Safety Principles

### 3.1 The Pre-Submission Fence

Before submitting any order to the broker, the system must:

1. **Write order intent to DB** (status = PENDING) — this is the idempotency anchor
2. **Check broker for an existing open order** with the same client-order-id
3. **Only submit if no such order exists at broker**
4. **Update DB to SUBMITTED** after broker acknowledgment
5. **Never retry a submission** without first querying broker state

A crash between steps 1 and 3 is recoverable. A crash between 3 and 4 requires broker reconciliation on restart. Both cases are safe because step 2 prevents duplicate submission.

### 3.2 Client Order ID Is The Idempotency Key

Every order must carry a deterministic `client_order_id`:
```
{strategy_run_id}:{symbol}:{direction}:{date}:{sequence}
```

The same logical order attempt always produces the same `client_order_id`. This makes deduplication stateless — the broker will reject a true duplicate. The DB will reject an insert collision. Both behaviors are correct and safe.

### 3.3 The Only Safe Order States

```
PENDING → SUBMITTED → PARTIAL_FILLED → FILLED
                    → CANCELED
                    → REJECTED
PENDING → CANCELED   (pre-submission cancel)
```

State transitions are append-only events in the DB. The current "state" of an order is always derived by reading its event log, never stored as a mutable field. Mutable order state is a concurrency bug waiting to happen.

### 3.4 Never Assume A Silent Order Succeeded

If the broker returns an error, timeout, or no response:
- The order may or may not exist at the broker
- The correct action is: **query broker for the order by client_order_id**
- Never place a second order on the assumption the first failed
- Never cancel an order on the assumption it doesn't exist

The difference between "order not placed" and "order placed but response lost" is determined only by the broker. This query is mandatory before any retry.

### 3.5 Fills Are Immutable Facts

A fill event is an immutable financial fact. Once written:
- It is never updated
- It is never deleted
- Corrections are new offsetting records, never edits

The fill ledger is the ground truth for P&L, position, and risk calculations.

---

## 4. Reconciliation Philosophy

### 4.1 Reconciliation Is Not Optional

Every startup sequence and every recovery sequence **must begin with broker reconciliation** before the system takes any trading action. Trading before reconciliation is forbidden.

Reconciliation sequence on start:
1. Fetch all open orders from broker
2. Fetch all positions from broker
3. Fetch cash balance from broker
4. Compare against DB last-known state
5. Write divergence records for every mismatch
6. Update local state to match broker
7. Only then: allow strategy execution

### 4.2 Broker Wins On Divergence

When local state and broker state disagree:

| Scenario | Resolution |
|---|---|
| DB has open order, broker does not | Mark order CANCELED in DB, log divergence |
| Broker has fill, DB does not | Write fill to DB, update position, log divergence |
| DB has position X shares, broker has Y | Trust broker Y, write correction event, log divergence |
| DB has cash A, broker has cash B | Trust broker B, log divergence for investigation |

Divergence is **never silently ignored**. Every divergence is logged as a reconciliation event with timestamp, source, expected, actual. This creates an audit trail for debugging and for detecting systemic issues.

### 4.3 Convergence Is Eventual, Not Guaranteed

The system converges toward consistency through reconciliation, not through distributed locking. Accepting eventual consistency means:
- There is always a window where local state lags broker state
- The system must tolerate operating on slightly stale local state
- All risk calculations use **conservative** estimates when uncertainty exists (assume worst case: position is larger, cash is smaller)
- No hard assertions that local == broker at all times

### 4.4 Staleness Has Explicit Expiry

Every piece of state has an explicit freshness TTL:

| State | Freshness TTL | Action on expiry |
|---|---|---|
| Market price | 60 seconds | Block order, fetch fresh |
| Account balance | 5 minutes | Refetch before placing order |
| Open orders list | 2 minutes | Refetch before reconcile |
| Token | 23 hours | Renew proactively |
| Position snapshot | 15 minutes | Refetch on next cycle |

Stale state is **rejected**, not used with a warning. Using stale market data to size an order is a risk violation, not a minor inefficiency.

---

## 5. Recovery Guarantees

### 5.1 Restart Recovery Contract

After any crash and restart, the system guarantees:
1. No duplicate orders will be placed for already-submitted intents
2. No fills will be double-counted in P&L
3. No risk limits will be bypassed due to stale risk state
4. Positions will reflect broker reality within one reconciliation cycle
5. Strategy execution resumes only after full reconciliation

What is NOT guaranteed after restart:
- Intra-session P&L continuity (session P&L resets on restart)
- Exact fill timing reconstruction (fills may arrive in different order on replay)

### 5.2 Recovery Is Idempotent

Running the recovery procedure N times produces the same result as running it once. This means:
- Reconciliation queries are read-only at broker
- Divergence writes use `INSERT OR IGNORE` / `ON CONFLICT DO NOTHING` semantics
- Position corrections are expressed as absolute values, not deltas

### 5.3 Partial Transaction Handling

If a DB transaction partially commits (process killed between writes):
- Orders with status PENDING but no SUBMITTED event: re-check broker, then decide
- Positions with no corresponding fill events: query broker for recent fills
- Any state without a corresponding broker confirmation: treat as UNKNOWN

UNKNOWN state suspends all strategy execution for the affected symbol until reconciliation resolves it. UNKNOWN does not mean "assume success." UNKNOWN means "stop and ask the broker."

### 5.4 No Recovery Requires Human Intervention

The system must be able to recover to a safe, trading-ready state after any single-point failure without human intervention. Human intervention is required only for:
- Risk limit breaches (MDD 15%, daily loss 3%) — intentionally require manual reset
- Broker credential failures
- Hardware / infrastructure failures

Everything else must self-heal.

---

## 6. Operational Constraints

### 6.1 Capital Context

The platform operates with 200만원 (~$1,500 USD). This has direct design implications:
- A single duplicate order is material — no "it's just a test" tolerance for production
- Commission drag is significant — unnecessary orders are financial losses, not just log noise
- There is no capital buffer to absorb bugs — correctness is a financial requirement
- Paper trading 4 weeks minimum before any real capital touches the system

### 6.2 Simplicity Over Sophistication

When two designs solve the same problem, choose the simpler one. This is not about laziness — it is about survivability. Complex systems have more failure modes, require more context to debug at 3am, and degrade less gracefully. Specifically:
- No distributed transactions across services
- No optimistic locking in hot paths — use database-level SERIALIZABLE where consistency matters
- No eventual-consistency tricks for risk state — risk state is always consistent or the system halts
- No micro-services — monolith worker with clear internal module boundaries

### 6.3 The Strategy Cannot Override Safety

Safety checks are outside the strategy execution path. A strategy cannot:
- Bypass the pre-submission fence
- Override position size limits
- Suppress reconciliation
- Disable risk checks

The execution layer enforces safety unconditionally. A strategy that attempts to bypass safety is rejected at the execution layer, not debugged.

### 6.4 Market Hours Are Hard Boundaries

Orders are only submitted during defined market windows:
- KR: 09:05–15:25 KST (buffer from open/close volatility)
- US: 22:35–05:30 KST

Outside these windows, no orders are placed regardless of signal strength. Stale open orders are canceled at market close. This eliminates entire classes of overnight risk and API edge cases.

### 6.5 One Worker, Serialized Execution

The strategy execution worker is a single process executing serially:
- No concurrent order submission
- No parallel strategy execution for the same symbol
- One in-flight order per symbol at any time

This eliminates race conditions, double-submission bugs, and position accounting errors. The throughput cost is irrelevant for a small-capital daily-bar strategy.

---

## 7. Safe Degradation Hierarchy

When components fail, the system degrades in this order. Each level is safe to run at indefinitely.

```
Level 0: FULL OPERATION
  All components healthy. Normal trading.

Level 1: MARKET DATA DEGRADED
  Stale price data detected.
  → Cancel all pending orders.
  → No new order submissions.
  → Continue position monitoring.
  → Alert via Telegram.
  → Auto-recover when fresh data available.

Level 2: BROKER API DEGRADED
  KIS API returning errors / timeouts.
  → Halt all order submission.
  → Continue reconciliation attempts with backoff.
  → Maintain current positions (no emergency liquidation from API error).
  → Alert via Telegram.
  → Auto-recover when API healthy.

Level 3: DATABASE DEGRADED
  DB write failures.
  → Halt all order submission immediately.
  → Cannot write execution truth = cannot trade safely.
  → Alert via Telegram.
  → Require manual restart after DB recovery.

Level 4: REDIS DEGRADED
  Redis unavailable.
  → Fall back to in-process rate limiting (conservative 3 req/s).
  → Fall back to direct API token refresh.
  → Continue trading at reduced throughput.
  → Log degradation, do not alert unless > 10 minutes.

Level 5: RISK LIMIT BREACH
  Daily loss ≥ 3% OR MDD ≥ 15%.
  → Halt all new orders immediately.
  → Do NOT automatically liquidate (liquidation under duress compounds losses).
  → Alert via Telegram with details.
  → Require explicit manual reset to resume.
```

The key property: **the system never does more when in doubt. It does less.**

---

## 8. Append-Only Execution Truth

The execution database is an event log, not a state store.

### 8.1 What This Means In Practice

No row is ever updated or deleted in these tables:
- `order_events` — every status transition is a new row
- `fill_events` — every fill is a new row
- `reconciliation_events` — every divergence detected is a new row
- `risk_snapshots` — every risk state capture is a new row

Current state is always derived by reading the event log, not stored as mutable state. Example:
- Current order status = last `order_events` row for that order_id
- Current position = sum of all `fill_events` for that symbol
- Current session P&L = sum of all realized P&L from today's `fill_events`

### 8.2 Why This Matters For Recovery

An append-only log means:
- A crash never corrupts existing data (partial writes are new rows, not half-overwritten rows)
- Replay from any point in history is possible
- Divergence detection compares event log against broker snapshot
- Debugging is post-hoc analysis of immutable facts, not reconstruction of mutable state transitions

### 8.3 The One Exception: Snapshots

Periodic equity snapshots (`equity_snapshots` table) are write-new-row on each snapshot cycle. They are a performance optimization for startup (avoid replaying entire fill history to compute current equity). They are never used as the authoritative state — only as a checkpoint from which to forward-apply recent events.

---

## 9. Deployment Safety

### 9.1 Deployment Is A Risk Event

Every deployment carries risk:
- In-flight orders during process restart
- Schema migrations against live data
- Configuration changes taking effect mid-session

Mitigation:
- Deploy only outside market hours (automated enforcement)
- Cancel all open orders before initiating deploy (deploy pre-hook)
- Run reconciliation as the first action post-deploy
- Schema migrations are forward-only, never destructive, applied before code deployment

### 9.2 The Paper-to-Real Gate

`KIS_ENV=real` is a one-way gate. Moving from paper to real requires:
- 4 weeks consecutive paper operation without a risk limit breach
- Manual review of all divergence events from paper period
- No code changes for 48 hours prior to switching

The system does not enforce this automatically. It is a process constraint documented here so it cannot be "forgotten."

### 9.3 Environment Parity

Paper and real environments run **identical code paths**. The only difference is the `TR_ID` prefix and the API endpoint. Any code that branches on `KIS_ENV` beyond these two values is a design smell and must be refactored.

---

## 10. Summary: The North Star Rules

When in doubt, apply these in order:

1. **If it would place an unintended order, don't.** Stop and reconcile.
2. **If broker state is unknown, ask the broker.** Never assume.
3. **If state is stale, reject it.** Freshness is a correctness property.
4. **If a component fails, degrade gracefully.** Never trade in an uncertain state.
5. **If recovery is ambiguous, stop trading.** Resume only after full reconciliation.
6. **Write the event, then act.** Intent precedes execution in the DB, always.
7. **The broker is always right.** Local state that contradicts the broker is wrong.
8. **Simpler is safer.** Complexity is technical debt denominated in capital.

These rules exist because this platform trades real capital with no human in the loop during execution. The cost of a wrong order is real money. The cost of a missed order is a missed opportunity. The asymmetry is obvious: correctness first, performance never at the expense of correctness.

---

*This document is not versioned separately from the codebase. When these principles are violated in implementation, the implementation is wrong — update the code, not this document.*
