# Idempotent Execution — Design Specification

**Status:** Design only — no code changes in this document.  
**Audit basis:** TASK 2-3A findings DO-01 through DO-12.

---

## 1. Purpose and Scope

This document specifies the idempotent execution layer for the KIS/Kiwoom trading platform. It addresses four root causes identified in the duplicate-order audit:

| Root Cause | Finding | Symptom |
|---|---|---|
| RC-1 | DO-01, DO-05 | HTTP client retries non-idempotent POST → ghost orders at broker |
| RC-2 | DO-02, DO-07 | In-memory session dedup resets on restart → duplicate market_open execution |
| RC-3 | DO-03, DO-04 | `idempotency_key` nullable, TOCTOU in `_persist_order()` → duplicate DB rows |
| RC-4 | DO-06 | Two competing registration paths for recovered orders → duplicate callbacks |

**Design goal:** An execution fingerprint-based gate that sits between the strategy signal and the broker call, and survives process restarts, network retries, and broker reconnects — without ever silently dropping a state change or blocking on unavailable infrastructure.

**Scope:**
- `backend/execution/idempotency.py` — all components
- `docs/IDEMPOTENT_EXECUTION.md` — this document
- 9 integration touch-points in existing files (§14)

---

## 2. Architecture Overview

```
Strategy Signal
    │
    ▼
StrategyBase.buy() / sell()
    │  self._run_id available (injected by runner)
    ▼
IdempotencyBrokerAdapter.place_order()
    │
    ├── ExecutionFingerprint.compute(run_id, symbol, side, qty, price, time_bucket)
    │           SHA256 → 64-char hex
    │
    ├── DuplicateDetector.check_and_gate(fingerprint)
    │     ├── IdempotencyStore.check(fingerprint)
    │     ├── If committed → return cached Order  (no broker call)
    │     ├── If in_flight < 90s → wait-and-poll
    │     ├── If in_flight ≥ 90s → RecoveryLogic
    │     └── If not found → INSERT in_flight + DistributedLock.acquire()
    │
    ├── BrokerAdapter.place_order()  (inner — no retry on POST)
    │
    └── IdempotencyStore.mark_committed(fingerprint, broker_order_id)
            OR
        IdempotencyStore.mark_failed(fingerprint, error)
```

---

## 3. Execution Fingerprint

### 3.1 Inputs

| Field | Type | Source | Detail |
|---|---|---|---|
| `strategy_id` | `int` | `_run_id` on `StrategyBase` (injected by runner) | `0` for API/emergency orders |
| `symbol` | `str` | Order symbol | Exact string match |
| `side` | `str` | `"buy"` or `"sell"` | |
| `quantity` | `int` | Order quantity | |
| `price` | `int` | `round(price * 100)` — **integer cents** | Avoids float hash instability across platforms |
| `time_bucket` | `int` | `floor(utcnow().timestamp() / 300) * 300` | Unix epoch of 5-min bucket start |

### 3.2 Algorithm

SHA256 over the canonical pipe-delimited string. Returns the full 64-character hex digest.

```
fingerprint = SHA256(
    f"{strategy_id}:{symbol}:{side}:{quantity}:{round(price * 100)}:{time_bucket}"
)
```

**Examples:**

| Inputs | Fingerprint (truncated) |
|---|---|
| `(42, "SPY", "buy", 100, 15000, 1717000000)` | `a3f1c9...` |
| `(42, "SPY", "buy", 100, 15000, 1717000000)` — identical | `a3f1c9...` — same |
| `(42, "SPY", "buy", 100, 15000, 1717000300)` — next bucket | `7b82e4...` — different |
| `(42, "SPY", "sell", 100, 15000, 1717000000)` — side changed | `9d4a11...` — different |

### 3.3 Time Bucket Rationale

**5-minute windows** (`TIME_BUCKET_SECONDS = 300`) because:
- Longer than the maximum KIS HTTP retry window: 3 attempts × 10s timeout = 30s max exposure
- Shorter than a trading session: multiple distinct buy signals can fire in the same session
- Matches the existing `SignalDeduplicator.window_minutes` in `safeguards.py`

A strategy generating buy signals for the same (symbol, qty, price) within 5 minutes → identical fingerprints → second execution blocked. Two signals separated by >5 minutes → different fingerprints → both allowed.

### 3.4 Price Representation

Price is stored as integer cents (`round(price * 100)`) to ensure bit-identical hashing across different float representations of the same value. The tolerance for broker verification (§6) is applied separately.

---

## 4. Idempotency Store

### 4.1 New DB Table: `execution_fingerprints`

```sql
CREATE TABLE execution_fingerprints (
    fingerprint      VARCHAR(64)  NOT NULL PRIMARY KEY,
    strategy_run_id  INTEGER      REFERENCES strategy_runs(id),
    symbol           VARCHAR(20)  NOT NULL,
    side             VARCHAR(4)   NOT NULL,
    qty              INTEGER      NOT NULL,
    price_cents      INTEGER      NOT NULL,
    time_bucket      INTEGER      NOT NULL,
    status           VARCHAR(20)  NOT NULL DEFAULT 'in_flight',
        -- 'in_flight' | 'committed' | 'failed' | 'abandoned'
    broker_order_id  VARCHAR(50),
    reject_reason    TEXT,
    error            TEXT,
    created_at       DATETIME     NOT NULL DEFAULT (CURRENT_TIMESTAMP),
    committed_at     DATETIME
);

CREATE INDEX idx_fp_status_created ON execution_fingerprints (status, created_at);
CREATE INDEX idx_fp_run_id          ON execution_fingerprints (strategy_run_id);
```

### 4.2 Status Lifecycle

```
             ┌─────────────┐
  INSERT ──► │  in_flight  │
             └─────────────┘
                    │
        ┌───────────┼───────────────┐
        ▼           ▼               ▼
  ┌──────────┐ ┌────────┐    ┌──────────────────────┐
  │committed │ │ failed │    │ in_flight (stale, 90s)│
  └──────────┘ └────────┘    └──────────────────────┘
        │           │                  │
  (terminal)  → in_flight         RecoveryLogic
              (on retry)               │
                                  committed / failed
                                       │
                              age > 24h → abandoned
```

### 4.3 Interface

```python
class IdempotencyStore:
    def check(fingerprint: str) -> Optional[FingerprintRow]
    def insert_in_flight(fingerprint, strategy_run_id, symbol, side, qty,
                         price_cents, time_bucket) -> None
        # Raises IntegrityError if fingerprint already exists (handled by caller)
    def mark_committed(fingerprint, broker_order_id: str,
                       reject_reason: str = "") -> None
    def mark_failed(fingerprint, error: str) -> None
    def mark_in_flight_retry(fingerprint) -> None
        # failed → in_flight (for retry after failure)
    def mark_abandoned(fingerprint) -> None
    def scan_stale(cutoff_seconds: int = 90) -> list[FingerprintRow]
        # Returns in_flight rows older than cutoff_seconds
```

---

## 5. Duplicate Detector

### 5.1 Decision Tree

Executed on every `IdempotencyBrokerAdapter.place_order()` call before touching the broker:

```
1. fingerprint = ExecutionFingerprint.compute(run_id, symbol, side, qty, price, time_bucket)

2. row = IdempotencyStore.check(fingerprint)

3. row is None:
   → IdempotencyStore.insert_in_flight(fingerprint, ...)
        On IntegrityError: another thread raced → sleep 2s → go to step 2
   → DistributedLock.acquire(fingerprint, ttl=90)
   → Proceed to broker call

4. row.status == "committed" AND row.broker_order_id non-empty:
   → Return Order(id=row.broker_order_id, status=SUBMITTED)  [cached success]

5. row.status == "committed" AND row.broker_order_id empty:
   → Return Order(id="", status=REJECTED, raw={"reject_reason": row.reject_reason})

6. row.status == "in_flight" AND age < 90s:
   → Wait: poll IdempotencyStore every 5s, up to 30s
   → After 30s still in_flight: raise IdempotencyTimeoutError
     (caller receives REJECTED-equivalent; may retry after current time_bucket expires)

7. row.status == "in_flight" AND age ≥ 90s (stale):
   → RecoveryLogic.recover(row)
   → Re-evaluate from step 2

8. row.status == "failed":
   → IdempotencyStore.mark_in_flight_retry(fingerprint)
   → DistributedLock.acquire(fingerprint, ttl=90)
   → Proceed to broker call (retry)

9. row.status == "abandoned":
   → Raise ExecutionAbandonedError (operator review required)
```

### 5.2 Concurrent INSERT Race

If two threads simultaneously reach step 3 (both see `row is None`), the second `INSERT` raises `IntegrityError` because `fingerprint` is the PRIMARY KEY. The losing thread:
1. Catches `IntegrityError`
2. Sleeps 2 seconds
3. Re-evaluates from step 2
4. Finds the winner's "in_flight" row → enters step 6 (wait-and-poll)

---

## 6. Distributed Lock

### 6.1 Mechanism

Redis `SET NX EX`:

```
Key:   idempotency:lock:{fingerprint}
Value: "{hostname}:{pid}:{monotonic_ts}"
TTL:   90 seconds
```

### 6.2 Acquisition

- Called immediately after successful `INSERT` of "in_flight" row
- Single attempt only (`SET NX` — no blocking wait)
- If `SET NX` returns `None` (key already exists): a concurrent process holds the lock
  → Fall through to Duplicate Detector step 6 (wait-and-poll on IdempotencyStore)

The Redis lock is defense-in-depth. The `INSERT` uniqueness constraint is the primary gate.

### 6.3 Release

- **Explicit:** `DEL` the key on `mark_committed()` or `mark_failed()`
- **Implicit:** TTL=90s automatic expiry if process crashes

### 6.4 Redis Unavailability

If `redis.set(key, value, nx=True, ex=ttl)` raises `redis.ConnectionError`:
- Log warning
- Continue without lock (DB-only dedup via `execution_fingerprints` PRIMARY KEY)
- Never raise to caller; never block execution for Redis unavailability

---

## 7. Recovery Logic

### 7.1 Trigger Conditions

1. **Live:** `DuplicateDetector` encounters "in_flight" row with `age ≥ 90s`
2. **Startup:** `StartupRecovery` Phase 6 scans all stale "in_flight" rows

### 7.2 Recovery Algorithm

```python
def recover(row: FingerprintRow, broker: BrokerAdapter) -> None:
    if age(row) > 24 * 3600:
        store.mark_abandoned(row.fingerprint)
        audit("idempotency_abandoned", ...)
        return

    # Layer 1: DB check
    db_order = (
        DBOrder.query
        .filter_by(symbol=row.symbol, side=row.side, qty=row.qty,
                   trade_date=date.today())
        .filter(DBOrder.price.between(price * 0.999, price * 1.001))
        .first()
    )
    if db_order and db_order.broker_order_id:
        store.mark_committed(row.fingerprint, db_order.broker_order_id)
        audit("idempotency_recovered_from_db", ...)
        return

    # Layer 2: Broker check
    broker_orders = broker.list_today_orders(row.symbol)
    match = _find_matching(broker_orders, row.side, row.qty, row.price_cents / 100)
    if match:
        store.mark_committed(row.fingerprint, match.broker_order_id)
        audit("idempotency_recovered_from_broker", ...)
        return

    # Not found anywhere — original submission did not reach broker
    store.mark_failed(row.fingerprint, error="stale_in_flight:not_found")
    audit("idempotency_recovery_failed", ...)
```

### 7.3 Startup Scan

Runs in `StartupRecovery` Phase 6 (after broker connectivity confirmed, before enabling trading):

```python
def run_startup_scan(broker: BrokerAdapter) -> None:
    stale_rows = store.scan_stale(cutoff_seconds=90)
    for row in stale_rows:
        recover(row, broker)
    # After scan: no "in_flight" rows remain
```

---

## 8. Broker Verification

Three-layer deterministic check. Used by `RecoveryLogic` and available as a standalone API.

### 8.1 Layer 1 — Idempotency Store

```python
row = store.check(fingerprint)
if row and row.status == "committed":
    return row.broker_order_id  # already resolved
```

### 8.2 Layer 2 — DB (DBOrder)

```python
db_order = DBOrder.query.filter(
    symbol=symbol, side=side, qty=qty,
    trade_date=date.today(),
    price BETWEEN price * 0.999 AND price * 1.001
).first()
if db_order and db_order.broker_order_id:
    store.mark_committed(fingerprint, db_order.broker_order_id)
    return db_order.broker_order_id
```

### 8.3 Layer 3 — Broker API

```python
broker_orders = broker.list_today_orders(symbol)
match = find_matching_order(broker_orders, side, qty, price, tolerance_pct=0.1)
if match:
    store.mark_committed(fingerprint, match.broker_order_id)
    return match.broker_order_id
return None  # not found — safe to retry
```

### 8.4 Price Tolerance

`±0.1%` applied at Layers 2 and 3 only. Fingerprint computation uses exact integer cents (no tolerance). The tolerance handles rounding differences between the application's float price and the broker's recorded price.

---

## 9. IdempotencyBrokerAdapter

New class in `backend/execution/idempotency.py`. Implements `BrokerAdapter`; wraps any inner `BrokerAdapter`.

### 9.1 Responsibilities

- Computes fingerprint for every `place_order()` call
- Gates the call through `DuplicateDetector`
- Commits/fails the fingerprint based on broker response
- All other `BrokerAdapter` methods pass through to `inner` unchanged

### 9.2 Interface

```python
class IdempotencyBrokerAdapter(BrokerAdapter):
    def __init__(
        self,
        inner: BrokerAdapter,
        store: IdempotencyStore,
        lock: DistributedLock,
        strategy_run_id: int = 0,
    ) -> None

    def place_order(
        self, symbol: str, side: str, qty: int, price: float, order_type: str = "limit"
    ) -> Order:
        """
        1. Compute fingerprint
        2. DuplicateDetector.check_and_gate(fingerprint)
           → returns cached Order if duplicate
        3. inner.place_order(symbol, side, qty, price, order_type)
        4. On success: store.mark_committed(fingerprint, order.id)
        5. On REJECTED: store.mark_committed(fingerprint, "", reject_reason=...)
        6. On exception: store.mark_failed(fingerprint, str(e)); lock.release; re-raise
        7. Return Order
        """

    # Pass-through methods — all delegate to self._inner:
    def get_balance(self) -> Balance
    def get_positions(self) -> list[Position]
    def cancel_order(self, order_id: str) -> bool
    def get_order_status(self, order_id: str, symbol: str) -> Optional[Order]
    def get_price(self, symbol: str) -> float
```

### 9.3 Wiring in runner.py

```python
# In runner.py._build_strategy(), after broker is obtained:
idempotency_broker = IdempotencyBrokerAdapter(
    inner=raw_broker,
    store=idempotency_store,   # shared instance on StrategyWorker
    lock=distributed_lock,     # shared instance on StrategyWorker
    strategy_run_id=run_id,
)
strategy = IndicatorStrategy(broker=idempotency_broker, ...)
strategy._run_id = run_id
```

---

## 10. HTTP Client Fix — Disable POST Retry

### 10.1 Root Cause (DO-01)

`KISClient.post()` retries on ANY exception, including `requests.Timeout`. Order placement (buy/sell) POSTs are non-idempotent — retrying after a timeout can create a second broker order if the first was accepted before the connection dropped.

### 10.2 Fix: `post_safe()`

Add a non-retrying variant to `KISClient`:

```python
def post_safe(self, path: str, tr_id: str, body: dict) -> dict:
    """
    Single-attempt POST for non-idempotent operations (order placement).
    Raises immediately on any error — no retry.
    The idempotency layer above this call handles retry logic.
    """
    hashkey = self.auth.get_hashkey(body)
    headers = self.auth.get_headers(tr_id)
    headers["hashkey"] = hashkey
    url = f"{self.base_url}{path}"
    self._limiter.wait()
    resp = requests.post(url, headers=headers, json=body, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("rt_cd") != "0":
        raise RuntimeError(f"KIS API error: {data.get('msg1')}")
    return data
```

`KISOrders.buy_kr()`, `sell_kr()`, `buy_us()`, `sell_us()` call `post_safe()` instead of `post()`.

The existing `post()` (with retry) remains for idempotent operations: order status queries, balance checks, price lookups.

---

## 11. Session Market-Open Dedup (DO-02)

### 11.1 Problem

`StrategyWorker._last_market_open` is in-memory and resets to `{}` on every process restart. Any worker restart during a scheduled market session bypasses the 300s dedup gate and fires `on_market_open()` again for all restored strategies.

### 11.2 Fix: DB Session Key

**New column on `strategy_runs`:** `last_session_key VARCHAR(30)` (e.g., `"KR:2026-06-06"` or `"US:2026-06-06"`).

```sql
ALTER TABLE strategy_runs ADD COLUMN last_session_key VARCHAR(30);
```

**In `runner.py._handle_market_open()`** (inside the existing lock scope, after session snapshot):

```python
session_key = f"{market}:{date.today().isoformat()}"
to_broadcast = []
with db_session() as db:
    for session in sessions:
        run = db.get(StrategyRun, session.run_id)
        if run is None:
            continue
        if run.last_session_key == session_key:
            logger.info("Session already processed: run_id=%d key=%s — skip", 
                        session.run_id, session_key)
            continue
        run.last_session_key = session_key
        to_broadcast.append(session)
    db.commit()

for session in to_broadcast:
    threading.Thread(target=session.trigger_market_open, args=(market,), daemon=True).start()
```

The in-memory `_last_market_open` check remains as a fast first-pass within the same process lifetime. The DB check is the authoritative cross-restart guard.

---

## 12. API Layer Idempotency (DO-10)

### 12.1 Problem

`api/routers/quick_trade.py` has no dedup mechanism. Two concurrent API requests (double-click, frontend retry on timeout) both reach KIS independently.

### 12.2 Fix: X-Idempotency-Key Header

**Request:** Client provides `X-Idempotency-Key: <uuid>` header.

**Server behavior:**

```python
idem_key = request.headers.get("X-Idempotency-Key")
if idem_key:
    cache_key = f"idempotency:api:{idem_key}"
    cached = redis.get(cache_key)
    if cached:
        return JSONResponse(content=json.loads(cached), status_code=200)

# Process request normally
result = place_order_and_persist(...)

if idem_key:
    redis.setex(cache_key, 300, json.dumps(result))  # cache 5 min
return JSONResponse(content=result, status_code=200)
```

**Key:** `idempotency:api:{client_uuid}` — TTL 300s (5 minutes).

The `X-Idempotency-Key` header is optional. Manual API orders without the header remain unguarded (manual trading intentionally allows multiple orders).

For API orders, `strategy_run_id=0` and `ExecutionFingerprint` is not used — the API dedup operates at the HTTP response level only.

---

## 13. Scheduler Fix (DO-07)

Add `max_instances=1, coalesce=True` to all session trigger jobs in `backend/worker/scheduler.py`:

```python
scheduler.add_job(
    _trigger_kr_session,
    CronTrigger(day_of_week="mon-fri", hour=9, minute=5, timezone="Asia/Seoul"),
    id="kr_session",
    name="한국주식 매매",
    max_instances=1,   # prevent concurrent instances
    coalesce=True,     # collapse missed firings into one
)
scheduler.add_job(
    _trigger_us_session,
    CronTrigger(...),
    id="us_session",
    name="미국주식 매매",
    max_instances=1,
    coalesce=True,
)
```

---

## 14. Policies

### 14.1 Duplicate Detection Policy

| Scenario | Action |
|---|---|
| Fingerprint "committed", broker_order_id present | Return cached Order — no broker call |
| Fingerprint "committed", broker_order_id empty | Return cached REJECTED Order |
| Fingerprint "in_flight", age < 90s | Wait-and-poll (5s intervals, max 30s total) |
| Fingerprint "in_flight", age ≥ 90s | RecoveryLogic → re-evaluate |
| Fingerprint "failed" | Mark "in_flight" again → allow retry |
| Fingerprint "abandoned" | Raise `ExecutionAbandonedError` |
| No fingerprint found | INSERT "in_flight" → proceed |
| INSERT race (IntegrityError) | Sleep 2s → re-evaluate from step 2 |

### 14.2 Lock Acquisition Policy

| Step | Rule |
|---|---|
| When | After successful INSERT of "in_flight" row |
| Attempt | Single — `SET NX`; no blocking wait |
| Failure | Another process holds lock → wait-and-poll IdempotencyStore |
| Redis down | Skip lock; DB PRIMARY KEY dedup is sole gate; log warning |

### 14.3 Lock Expiry Policy

| Scenario | TTL / Action |
|---|---|
| Normal success | Explicit `DEL` after `mark_committed()` |
| Normal failure | Explicit `DEL` after `mark_failed()` |
| Process crash | TTL = 90s automatic expiry |
| Redis unavailable | No lock held; DB is authoritative |

### 14.4 Recovery Policy

| Condition | Action |
|---|---|
| "in_flight", age 90s–24h | RecoveryLogic: DB check → broker check → committed/failed |
| "in_flight", age > 24h | Mark "abandoned"; write audit event; no broker check |
| Recovery: broker found | Mark "committed" with broker's ODNO |
| Recovery: broker not found | Mark "failed" — original call did not reach broker |
| Recovery: DB found | Mark "committed" with DB's broker_order_id |

### 14.5 Stale Fingerprint Handling

- **Stale threshold:** `LOCK_TTL_SECONDS = 90`
- **Stale detection:** `status == "in_flight" AND created_at < (now - 90s)`
- **Startup scan:** Runs before trading is enabled; zero stale rows after scan
- **Live detection:** `DuplicateDetector` handles stale row inline (step 7)
- **Cleanup:** Rows with `status = "abandoned"` are archived after 7 days (background job; never deleted — append-only audit requirement)

### 14.6 Failure and Timeout Handling

| Error | Fingerprint Result | Retry Allowed? |
|---|---|---|
| Network timeout (in broker call) | "failed" | Yes — next attempt uses same fingerprint if within 5-min bucket |
| HTTP 5xx from KIS | "failed" | Yes |
| Broker REJECTED order | "committed" (broker_order_id="") | No — rejection is terminal within bucket |
| KIS `rt_cd != "0"` error | "failed" | Yes |
| DB write failure after broker success | "in_flight" (stale) | RecoveryLogic on next attempt finds it at broker |
| Redis unavailable | No lock; DB-only dedup | Yes |
| `IdempotencyTimeoutError` (waited 30s) | "in_flight" (unchanged) | Yes — retry after current time_bucket window |
| `ExecutionAbandonedError` | "abandoned" | No — manual operator review |

### 14.7 Restart Behavior

```
StartupRecovery Phase 6 (before SAFE_MODE.enable()):
    1. IdempotencyStore.scan_stale(cutoff_seconds=90) → stale_rows
    2. For each row: RecoveryLogic.recover(row, broker)
    3. Log count: committed / failed / abandoned
    4. Verify: no "in_flight" rows remain
    5. Proceed to Phase 7 (load PositionTracker)
```

After startup scan, the IdempotencyStore is in a clean state. No "in_flight" rows exist when normal trading begins.

---

## 15. Append-Only Audit Behavior

All IdempotencyStore state changes write a corresponding `AuditLog` event in a **separate DB transaction** (same fire-and-forget pattern as existing `_audit()` in `runner.py`).

| `event_type` | When |
|---|---|
| `idempotency_in_flight` | Fingerprint inserted |
| `idempotency_committed` | Broker call succeeded; fingerprint committed |
| `idempotency_rejected` | Broker REJECTED order; fingerprint committed with empty id |
| `idempotency_failed` | Broker call failed; fingerprint marked failed |
| `idempotency_duplicate_blocked` | Duplicate detected; cached Order returned |
| `idempotency_recovery_started` | RecoveryLogic invoked for stale row |
| `idempotency_recovered_from_db` | DB check resolved stale row |
| `idempotency_recovered_from_broker` | Broker check resolved stale row |
| `idempotency_recovery_failed` | Not found at DB or broker; marked failed |
| `idempotency_abandoned` | Row > 24h old; marked abandoned |
| `idempotency_startup_scan` | Startup scan completed: `{resolved, abandoned}` counts |
| `session_key_dedup` | `_handle_market_open()` skipped already-processed strategy run |
| `api_idempotency_hit` | Quick-trade API returned cached response for repeated key |

Audit write failures do NOT block the idempotency gate. If the AuditLog write fails, a warning is logged and execution continues.

---

## 16. Restart Recovery Flow (Updated)

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
Phase 6: Idempotency scan + pending order recovery  ← NEW
    │  RecoveryLogic.run_startup_scan(broker)
    │    → Resolve all stale "in_flight" fingerprints
    │    → Rows > 24h → "abandoned"
    │  Then: StartupRecovery._step_pending_orders()
    │    → Re-register surviving open orders with poller (single path)
    ▼
Phase 7: Load PositionTracker from DB
    │  (after Phase 5 repairs and Phase 6 recovery)
    ▼
Phase 8: Enable trading (SAFE_MODE.enable())
    │  → if CRITICAL gap from Phase 5: stay disabled
    ▼
Background (30s delay): PositionReconciler.reconcile("post_recovery")
```

---

## 17. New Code Requirements

| File | Change Required |
|---|---|
| `backend/execution/idempotency.py` | **NEW FILE** — `ExecutionFingerprint`, `IdempotencyStore`, `DuplicateDetector`, `DistributedLock`, `RecoveryLogic`, `BrokerVerification`, `IdempotencyBrokerAdapter` |
| `backend/database/models.py` | Add `execution_fingerprints` table; add `last_session_key` to `strategy_runs`; make `idempotency_key` NOT NULL with fallback default; add UNIQUE on `orders.broker_order_id` |
| `kis_adapter/client.py` | Add `post_safe()` (single-attempt, no retry) |
| `kis_adapter/orders.py` | `buy_kr/sell_kr/buy_us/sell_us` call `post_safe()` |
| `backend/strategy/base.py` | Add `_run_id: int = 0` to `__init__` |
| `backend/worker/runner.py` | Set `strategy._run_id = run_id`; wrap broker in `IdempotencyBrokerAdapter`; DB session-key check in `_handle_market_open()` |
| `backend/worker/recovery.py` | Call `RecoveryLogic.run_startup_scan()` in Phase 6 |
| `api/routers/quick_trade.py` | Add `X-Idempotency-Key` header handling (Redis cache, TTL=300s) |
| `backend/worker/scheduler.py` | Add `max_instances=1, coalesce=True` to session trigger jobs |

---

## 18. Audit Finding Coverage

| Finding | Severity | This Design Addresses It Via |
|---|---|---|
| DO-01 HTTP retry creates ghost orders | CRITICAL | `post_safe()` — no retry on POST; `IdempotencyBrokerAdapter` handles retry at application level |
| DO-02 Post-restart market_open replay | CRITICAL | DB `last_session_key` on `strategy_runs` — authoritative cross-restart dedup |
| DO-03 Nullable idempotency_key | HIGH | `execution_fingerprints` PRIMARY KEY is always non-null (fingerprint is always computable) |
| DO-04 SELECT-then-INSERT TOCTOU | HIGH | `execution_fingerprints` PK → INSERT race → IntegrityError → wait-and-poll |
| DO-05 No broker idempotency token | HIGH | Application-level fingerprint gate prevents second broker call |
| DO-06 Double-registration recovery | HIGH | `RecoveryLogic.run_startup_scan()` + single registration path in recovery |
| DO-07 Scheduler no coalesce | MEDIUM | `max_instances=1, coalesce=True` on session jobs |
| DO-08 Commands table race | MEDIUM | `last_session_key` DB guard supersedes command-based dedup for market_open |
| DO-09 Crash between lock and submission | MEDIUM | Stale "in_flight" fingerprint → RecoveryLogic on restart |
| DO-10 API double-click | MEDIUM | `X-Idempotency-Key` header → Redis cache |
| DO-11 Multi-instance scheduler | LOW | `max_instances=1` + DB session-key guard |
| DO-12 on_bar/on_market_open race | LOW | Already mitigated by `try_mark_pending()` — no change needed |

---

## 19. Verification

```bash
# Fingerprint determinism: same inputs → same hash
pytest tests/execution/test_idempotency.py -v -k fingerprint_deterministic

# Different time bucket: same params → different fingerprints
pytest tests/execution/test_idempotency.py -v -k different_bucket_allows_new

# Duplicate blocked: committed fingerprint → cached Order, broker not called
pytest tests/execution/test_idempotency.py -v -k duplicate_returns_cached

# Concurrent INSERT race: two threads race → one waits, gets cached result
pytest tests/execution/test_idempotency.py -v -k concurrent_insert_race

# Stale recovery — broker found: in_flight > 90s, broker has order → committed
pytest tests/execution/test_idempotency.py -v -k stale_recovery_broker_found

# Stale recovery — broker not found: in_flight > 90s, not at broker → failed → retry
pytest tests/execution/test_idempotency.py -v -k stale_recovery_broker_not_found

# Startup scan: stale rows resolved before trading enabled
pytest tests/worker/test_recovery.py -v -k idempotency_startup_scan

# post_safe no retry: timeout raises immediately, no second broker POST
pytest tests/brokers/test_kis_client.py -v -k post_safe_no_retry

# Session key dedup: on_market_open with same session_key skips already-processed runs
pytest tests/worker/test_runner.py -v -k session_key_dedup_restart

# API idempotency: repeated X-Idempotency-Key → same response, broker called once
pytest tests/api/test_quick_trade.py -v -k idempotency_key_header

# Redis down: lock acquisition fails gracefully, DB dedup still works
pytest tests/execution/test_idempotency.py -v -k redis_unavailable_fallback

# Full end-to-end: restart mid-order → recovery → no duplicate position
pytest tests/integration/test_order_restart.py -v -k no_duplicate_on_restart
```
