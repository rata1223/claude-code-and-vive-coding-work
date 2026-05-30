# BROKER_OWNERSHIP_MATRIX

> **Phase:** Broker Boundary Audit  
> **Status:** Pre-refactoring gate document — no broker refactoring is permitted without resolving all Critical violations first.  
> **Generated:** 2026-05-30  
> **Scope:** Full repository trace — `kis_adapter/`, `backend/brokers/`, `backend/execution/`, `backend/worker/`, `backend/strategy/`, `backend/quant/`, `backend/api/`, `api/routers/`, `backend/websocket/`

---

## Architecture Overview

The platform has two distinct API surfaces that must be kept separate:

| Surface | Path | Purpose |
|---------|------|---------|
| **Legacy Flask API** | `api/` | Mobile client REST layer (FastAPI-based auth, dashboard, quick-trade) |
| **Backend API** | `backend/api/server.py` | Internal worker control plane (strategy management, admin ops) |

Both surfaces must route all broker I/O through `backend/brokers/` — never through `kis_adapter/` directly.

### Layer Stack (bottom → top)

```
kis_adapter/            ← Raw KIS HTTP: auth tokens, hashkeys, TR_IDs, rate limiter
backend/brokers/        ← BrokerAdapter ABC + KISBroker singleton + models
backend/execution/      ← OrderStateMachine, OrderFillPoller, PositionTracker, Reconciler
backend/worker/         ← Strategy runtime, emergency flatten, scheduler, recovery
backend/strategy/       ← StrategyBase, IndicatorStrategy, ScriptStrategy
backend/quant/          ← Signal generation, LivePipeline, risk engine (BrokerAdapter consumer)
backend/api/            ← Internal control-plane API (get_kis_broker() only)
api/routers/            ← Mobile REST API (MUST use backend/brokers/ — currently violated)
backend/websocket/      ← Redis Pub/Sub bridge (no broker access — correct)
```

---

## Ownership Matrix

| # | Responsibility | Current Owner (file) | Expected Owner | Status | Risk |
|---|---------------|----------------------|---------------|--------|------|
| 1 | **Order submission** | `backend/brokers/kis.py:105` via `kis_adapter/orders.py` | `backend/brokers/kis.py` | ⚠️ SPLIT | Critical |
| 2 | **Order cancellation** | `backend/brokers/kis.py:133` + `api/routers/quick_trade.py:178` | `backend/brokers/kis.py` | ⚠️ SPLIT | Critical |
| 3 | **Order status tracking** | `backend/execution/order_poller.py` + `backend/execution/order_machine.py` | `backend/execution/` | ✅ CORRECT | — |
| 4 | **Fill processing** | `backend/worker/runner.py:_make_fill_callback()` → `backend/execution/position_tracker.py` | `backend/worker/` + `backend/execution/` | ✅ CORRECT | — |
| 5 | **Balance retrieval** | `backend/brokers/kis.py:50` + `api/routers/quick_trade.py:59` + `api/routers/dashboard.py:69` | `backend/brokers/kis.py` | ⚠️ SPLIT | Critical |
| 6 | **Portfolio retrieval** | `backend/brokers/kis.py:70` + `api/routers/quick_trade.py:102` + `api/routers/dashboard.py:69` | `backend/brokers/kis.py` | ⚠️ SPLIT | Critical |
| 7 | **Position tracking** | `backend/execution/position_tracker.py` | `backend/execution/position_tracker.py` | ✅ CORRECT | — |
| 8 | **Account synchronization** | `backend/execution/reconciler.py` (triggered from worker, scheduler, API) | `backend/execution/reconciler.py` | ✅ CORRECT | — |
| 9 | **Market data / live price** | `backend/brokers/kis.py:289` + `api/routers/dashboard.py:164` (KISMarketData direct) | `backend/brokers/kis.py` | ⚠️ PARTIAL | High |
| 10 | **WebSocket events** | `backend/websocket/server.py` (publish_* helpers) | `backend/websocket/server.py` | ✅ CORRECT | — |
| 11 | **Reconciliation** | `backend/execution/reconciler.py` | `backend/execution/reconciler.py` | ✅ CORRECT | — |
| 12 | **Authentication (KIS tokens)** | `kis_adapter/auth.py` ← called only via `kis_adapter/client.py` ← `KISBroker` | `kis_adapter/auth.py` via `KISBroker` | ⚠️ VIOLATED | Critical |
| 13 | **Rate-limit handling** | `kis_adapter/client.py:RateLimiter` + uncoordinated instances in `api/routers/` | `kis_adapter/client.py` (single shared instance via KISBroker singleton) | ⚠️ VIOLATED | High |
| 14 | **Retry handling** | `kis_adapter/client.py:get()/post()` | `kis_adapter/client.py` | ✅ CORRECT | — |
| 15 | **Error handling / circuit breaker** | `backend/execution/circuit_breaker.py` used in `backend/brokers/kis.py:46` | `backend/brokers/kis.py` (gate before KIS calls) | ✅ CORRECT | — |

---

## Ownership Violations

### V-01 — Direct KIS Adapter Instantiation in Quick-Trade Router

**Classification:** 🔴 Critical

**Location:** `api/routers/quick_trade.py:21–34`

```python
def _load_kis(cred: Credential):
    from kis_adapter import KISClient, KISOrders, KISPortfolio   # ← bypasses broker layer
    os.environ["KIS_APP_KEY"]    = decrypt(cred.app_key_enc) or ""   # ← process-wide mutation
    os.environ["KIS_APP_SECRET"] = decrypt(cred.app_secret_enc) or ""
    os.environ["KIS_ACCOUNT_NO"] = decrypt(cred.account_no_enc) or ""
    os.environ["KIS_HTS_ID"]     = decrypt(cred.hts_id_enc) or ""
    os.environ["KIS_ENV"]        = cred.env
    client    = KISClient()         # ← new, unshared rate-limiter instance
    orders    = KISOrders(client)
    portfolio = KISPortfolio(client)
    return client, orders, portfolio
```

Called at: lines 59 (balance), 102 (positions), 133 (place order), 178 (close position).

**Reason:** `_load_kis()` directly instantiates `kis_adapter` objects, bypassing the `KISBroker` singleton which owns the shared `RateLimiter` and `ConsecutiveFailureBreaker`. Additionally it mutates `os.environ` process-wide.

**Risk:**
- **Race condition**: Concurrent API requests with different user credentials overwrite each other's `KIS_APP_KEY` / `KIS_APP_SECRET` in `os.environ`. A strategy order in the worker thread reads the swapped credentials during the window.
- **Rate limit breach**: Each `_load_kis()` call creates a fresh `KISClient` with its own `RateLimiter(5)`. Multiple concurrent API requests each believe they are within limit while collectively hammering KIS at N×5 req/s.
- **Phantom orders**: Orders placed via `orders.buy_kr()` / `orders.sell_us()` are never registered in `OrderStateMachine`, never polled by `OrderFillPoller`, and never persisted in the `orders` DB table. Fills are invisible to `PositionTracker` and `PositionReconciler`.
- **Circuit breaker bypass**: `ConsecutiveFailureBreaker` in `KISBroker` never trips on quick-trade failures.

**Affected systems:** Order tracking, fill pipeline, position reconciliation, AuditLog, rate limiting, authentication.

---

### V-02 — Direct KIS Adapter Instantiation in Dashboard Router

**Classification:** 🔴 Critical

**Location:** `api/routers/dashboard.py:29–49`

```python
def _build_kis_client_from_cred(cred: Credential):
    from kis_adapter import KISClient, KISPortfolio   # ← bypasses broker layer
    os.environ["KIS_APP_KEY"]    = app_key            # ← process-wide mutation
    os.environ["KIS_APP_SECRET"] = app_secret
    os.environ["KIS_ACCOUNT_NO"] = account_no
    os.environ["KIS_HTS_ID"]     = hts_id
    os.environ["KIS_ENV"]        = cred.env
    client    = KISClient()                           # ← unshared rate-limiter
    portfolio = KISPortfolio(client)
    return client, portfolio
```

Called at: line 68 (`get_summary`), line 162 (`get_pending_orders`).

**Reason:** Same pattern as V-01: `os.environ` mutation + unshared `KISClient`. Dashboard is read-only (no orders), so the phantom-order risk does not apply, but authentication races and rate-limit breaches are identical.

**Risk:**
- Dashboard portfolio queries compete with live strategy orders at the raw KIS HTTP level.
- `os.environ` race identical to V-01 (shared process).
- `KISMarketData` also instantiated at line 164–166 outside broker layer.

**Affected systems:** Rate limiting, authentication, circuit breaker isolation.

---

### V-03 — Order Placement Without State Machine Registration

**Classification:** 🔴 Critical

**Location:** `api/routers/quick_trade.py:133–146` and `api/routers/quick_trade.py:178–185`

```python
_, orders, _ = _load_kis(cred)
result = orders.buy_kr(body.symbol, qty, int(body.price))   # raw KIS call
# OrderStateMachine.register() never called
# OrderFillPoller never sees this order ID
# DBOrder row never created
```

**Reason:** The `kis_adapter.KISOrders` call submits an order to KIS and returns a raw JSON dict. The order ID from `result["output"]["ODNO"]` is returned to the client but is never:
- Registered with `OrderStateMachine` (no state tracking)
- Added to `OrderFillPoller` (no fill detection)
- Written to the `orders` DB table (no audit trail)
- Communicated to `PositionTracker` (no position update on fill)
- Written to `AuditLog`

**Risk:** If the order fills, the fill is invisible to the system. `PositionReconciler` will later detect the position drift and attempt to repair it, but will have no fill history. The system's equity tracking, PnL, and stop-loss calculations will be wrong until the next reconciliation cycle.

**Affected systems:** Fill pipeline, position tracking, PnL accounting, stop-loss engine, AuditLog.

---

### V-04 — os.environ Credential Mutation (Authentication Race)

**Classification:** 🔴 Critical

**Location:**
- `api/routers/quick_trade.py:25–29`
- `api/routers/dashboard.py:41–45`

**Reason:** `KISClient.__init__()` reads `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO`, `KIS_ENV` from `os.environ` at instantiation time. Both routers overwrite these environment variables per-request in a multi-threaded Flask/gunicorn process. There is no lock. Thread A (user 1, paper account) can overwrite the variables mid-way through Thread B's (user 2, real account) `KISClient()` instantiation. The result is a `KISClient` with mixed credentials.

**Risk:** Orders or balance queries can be submitted under the wrong user's credentials. In a multi-user scenario this is a financial data leak and potential unauthorized trading.

**Affected systems:** Authentication, all KIS API calls in these routers.

---

### V-05 — KISMarketData Instantiated Outside Broker Layer

**Classification:** 🟠 High

**Location:** `api/routers/dashboard.py:164–166`

```python
_client, _ = _build_kis_client_from_cred(cred)
from kis_adapter import KISMarketData
md = KISMarketData(_client)
pending = md.get_pending_us(account_no)
```

**Reason:** `KISMarketData` is a `kis_adapter` class. Its instantiation belongs in `backend/brokers/kis.py` (already done: `self._market = KISMarketData(self._client)` at line 41). The dashboard constructs a second instance on a separate unshared `KISClient`.

**Risk:** `get_pending_us()` is fetching order status via a non-singleton client — same rate-limit and credential-race risks as V-01/V-02. This also represents undocumented duplication of broker capability.

**Affected systems:** Rate limiting, pending-order data consistency.

---

### V-06 — Duplicate Symbol→Market Routing Logic

**Classification:** 🟡 Medium

**Location:**
- `backend/brokers/kis.py:35–36` (`_is_kr()` static method)
- `backend/brokers/semantic_mapper.py:60–65` (`BrokerSemanticMapper.market_for_symbol()`)

```python
# kis.py
@staticmethod
def _is_kr(symbol: str) -> bool:
    return symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit())

# semantic_mapper.py
def market_for_symbol(self, symbol: str) -> Market:
    if symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit()):
        return Market.KR
    return Market.US
```

**Reason:** Two identical routing heuristics exist independently. If the KR symbol detection rule changes (e.g. KOSDAQ codes, ETF universe expansion), both must be updated. `_is_kr()` is used in 4 call sites inside `KISBroker` while `market_for_symbol()` is used by the strategy layer.

**Risk:** Silent divergence — one rule updated, the other not. Misrouted orders (KR order sent as US or vice versa) result in KIS API rejections.

**Affected systems:** Order routing for KR/US symbols.

---

### V-07 — LivePipeline as Second Order Submission Path

**Classification:** 🟡 Medium

**Location:** `backend/quant/live/pipeline.py:195, 302`

```python
order = self.broker.place_order(pos.symbol, "sell", pos.qty, price)  # line 195
order = self.broker.place_order(symbol, "buy", qty, price)            # line 302
```

**Reason:** `LivePipeline.broker.place_order()` is a valid `BrokerAdapter` call, so the broker layer boundary is not broken. However, this creates a second order submission code path that runs independently from `StrategyBase.buy()/sell()`. The `StrategyBase` path registers orders with `PositionTracker` at lines 107–125; the `LivePipeline` path does not call `StrategyBase` at all.

**Risk:** Orders placed by `LivePipeline` are not tracked through the same `PositionTracker` instance used by `IndicatorStrategy`. Position state can diverge between `LivePipeline` and `StrategyBase`. Partially mitigated by `PositionReconciler` but creates latent inconsistency window.

**Affected systems:** Position tracking consistency between LivePipeline and StrategyBase.

---

### V-08 — No DB Persistence for Quick-Trade Orders

**Classification:** 🟠 High

**Location:** `api/routers/quick_trade.py:122–162`

**Reason:** The endpoint returns `{"status": "submitted"}` with the KIS order number but writes no row to the `orders` table. There is a comment in the code acknowledging this: `"We store it under a special 'manual' strategy if needed – skip for simplicity."` This is a known gap that has not been closed.

**Risk:** Quick-trade fills are invisible to the reconciler's order-aging logic (`_reconcile_pending_orders`). Repeated quick-trades accumulate ghost positions that reconciler will attempt to delete as stale DB positions (if the position is later zeroed at the broker). AuditLog has no record.

**Affected systems:** AuditLog, reconciler, position tracker, PnL accounting.

---

## Broker Boundary Rules

These rules define the strict architectural constraints for each layer. All code must comply before any broker refactoring is permitted.

### `kis_adapter/` — KIS HTTP Transport Layer

**Permitted:**
- KIS-specific HTTP calls (`GET`, `POST`) with TR_ID routing
- Token acquisition and renewal (`KISAuth`)
- Hashkey generation
- Rate limiting (`RateLimiter`) — single instance per process, owned by `KISClient`
- Retry logic (HTTP 5xx, connection errors)

**Forbidden:**
- Instantiation anywhere outside `backend/brokers/kis.py`
- Direct calls from `api/`, `backend/worker/`, `backend/strategy/`, `backend/quant/`
- Business logic (order state, position accounting, risk checks)

### `backend/brokers/` — Broker Abstraction Layer

**Permitted:**
- `BrokerAdapter` ABC definition (`base.py`)
- `KISBroker` singleton — wraps `kis_adapter`, owns the single `KISClient` instance
- `KiwoomBroker` stub
- Data models (`Order`, `Position`, `Balance`, `OrderStatus`, `BrokerCapabilities`)
- `BrokerSemanticMapper` (symbol→market routing, cost/tax normalization)
- FX rate caching (`_get_fx()`) — external data, acceptable here
- Process-level singleton (`get_kis_broker()`) — enforces single rate-limiter

**Forbidden:**
- Direct DB access (SQLAlchemy queries)
- Strategy signals or decision logic
- Risk engine calls
- WebSocket publishing
- os.environ mutation after process startup

### `backend/execution/` — Order & Position State Layer

**Permitted:**
- `OrderStateMachine`: state transitions, PENDING→SUBMITTED→FILLED/CANCELED/REJECTED
- `OrderFillPoller`: calls `broker.get_order_status()` only
- `PositionTracker`: in-memory position accounting, atomic `try_mark_pending()`
- `PositionReconciler`: calls `broker.get_positions()`, `broker.get_order_status()`, `broker.cancel_order()`
- `CircuitBreaker`: consecutive-failure tracking (consumed by `KISBroker`)

**Forbidden:**
- `kis_adapter` imports
- Direct KIS TR_ID or API call knowledge
- Strategy or signal logic
- Risk engine calls (use callbacks/events only)

### `backend/worker/` — Strategy Runtime Layer

**Permitted:**
- Obtaining the broker via `get_kis_broker()` (singleton only — never `KISBroker()` directly)
- Passing broker to strategy constructors and to `PositionReconciler`
- `broker.get_balance()`, `broker.get_positions()`, `broker.cancel_order()` for runtime operations
- `EmergencyFlattenManager`: `broker.get_positions()`, `broker.get_price()`, `broker.place_order()` for emergency liquidation only
- Publishing WebSocket events after order/fill events

**Forbidden:**
- `kis_adapter` imports
- `KISClient`, `KISOrders`, `KISPortfolio` instantiation
- os.environ mutation for credentials

### `backend/strategy/` — Strategy Logic Layer

**Permitted:**
- `StrategyBase.buy()/sell()/get_price()`: delegates to `self._broker` (BrokerAdapter) only
- `SimulatedBroker`: implements `BrokerAdapter` for backtesting
- Calling `self._tracker` (`PositionTracker`) to check position state before ordering

**Forbidden:**
- Direct `kis_adapter` imports
- KIS-specific TR_IDs, order routing, or market detection
- Risk engine state mutation (risk is observed, not mutated from strategy)

### `backend/quant/` — Signal & Pipeline Layer

**Permitted:**
- `LivePipeline`: accepts `BrokerAdapter` by injection — may call `broker.place_order()`, `broker.get_positions()`, `broker.get_price()`
- Signal generators: pure computation, no broker calls
- Risk engine: pure state machine, receives price events

**Forbidden:**
- `kis_adapter` imports
- KIS-specific logic
- `get_kis_broker()` calls (broker injected, never pulled from singleton inside quant)

### `api/routers/` (Legacy Flask API) — Mobile REST Layer

**Permitted:**
- Read operations via `backend/brokers/kis.py.get_kis_broker()`: balance, positions, price
- Order operations via `backend/brokers/kis.py.get_kis_broker()`: place_order, cancel_order
- Passing results of `DBOrder` queries for order history (DB read only)
- Triggering worker-control actions via Redis Pub/Sub commands

**Forbidden:**
- `kis_adapter` imports of any kind
- `KISClient`, `KISOrders`, `KISPortfolio`, `KISMarketData`, `KISAuth` instantiation
- `os.environ` mutation for KIS credentials
- Calling any `kis_adapter` function directly

### `backend/api/server.py` (Backend Control-Plane API)

**Permitted:**
- `get_kis_broker()` for read operations (balance, positions)
- Triggering `PositionReconciler` via dependency injection
- `EmergencyFlattenManager` instantiation

**Forbidden:**
- `kis_adapter` imports
- Bypassing broker singleton

---

## Recommended Ownership Map

### Target State: Single Broker Access Path

```
All broker I/O
     │
     ▼
backend/brokers/kis.py:KISBroker  ← singleton (get_kis_broker())
     │
     ├── kis_adapter/client.py:KISClient   ← 1 instance, 1 RateLimiter
     │         └── kis_adapter/auth.py:KISAuth   ← token cache
     ├── kis_adapter/orders.py:KISOrders
     ├── kis_adapter/portfolio.py:KISPortfolio
     └── kis_adapter/market_data.py:KISMarketData
```

**All callers** above the broker layer use only `BrokerAdapter` methods:

| Caller | Allowed Methods |
|--------|----------------|
| `backend/strategy/base.py` | `place_order`, `get_price` |
| `backend/quant/live/pipeline.py` | `place_order`, `cancel_order`, `get_positions`, `get_balance`, `get_price` |
| `backend/worker/runner.py` | `get_balance`, `get_positions`, `cancel_order` (+ injected broker to strategies) |
| `backend/worker/emergency.py` | `get_positions`, `get_price`, `place_order` |
| `backend/worker/scheduler.py` | `get_balance`, `get_positions` |
| `backend/worker/recovery.py` | `get_balance`, `get_positions` (thread pool) |
| `backend/execution/reconciler.py` | `get_positions`, `get_order_status`, `cancel_order` |
| `backend/execution/order_poller.py` | `get_order_status` |
| `backend/api/server.py` | `get_balance`, `get_positions` |
| `api/routers/quick_trade.py` | `place_order`, `cancel_order`, `get_balance`, `get_positions` |
| `api/routers/dashboard.py` | `get_balance`, `get_positions`, `get_order_status` |

### Remediation Priority

| Priority | Violation | Action |
|----------|-----------|--------|
| **P0** | V-01, V-04 (`quick_trade._load_kis`, env mutation) | Replace with `get_kis_broker()` calls; remove `os.environ` writes |
| **P0** | V-02, V-04 (`dashboard._build_kis_client_from_cred`, env mutation) | Replace with `get_kis_broker()` calls; remove `os.environ` writes |
| **P0** | V-03 (orders placed without OrderStateMachine) | Register every `place_order()` result with `OrderStateMachine.register()` and `DBOrder` insert |
| **P1** | V-05 (`KISMarketData` in dashboard) | Route through `KISBroker.get_order_status()` or add `get_pending_orders()` to `BrokerAdapter` |
| **P1** | V-08 (no DB persistence) | Insert `DBOrder` row before returning; add to `OrderFillPoller` |
| **P2** | V-06 (duplicate symbol routing) | Delete `_is_kr()` from `KISBroker`; use `BrokerSemanticMapper.market_for_symbol()` at all 4 call sites |
| **P3** | V-07 (LivePipeline dual path) | Evaluate whether `LivePipeline` should route through `StrategyBase.buy()/sell()` or maintain its own tracked-order path |

### Quick-Trade Remediation Sketch (P0)

```python
# api/routers/quick_trade.py — AFTER remediation (no code change authorized here)

# REMOVE: _load_kis(), os.environ writes, KISClient/KISOrders/KISPortfolio imports

# REPLACE with:
from backend.brokers.kis import get_kis_broker

@router.post("/place-order")
def place_order(body: PlaceOrderRequest, ...):
    broker = get_kis_broker()
    order  = broker.place_order(body.symbol, body.side, int(body.qty), body.price)
    # then: OrderStateMachine.register(order) + DBOrder insert + AuditLog
```

---

## Violation Summary

| ID | File | Lines | Classification | Risk Description |
|----|------|-------|---------------|-----------------|
| V-01 | `api/routers/quick_trade.py` | 21–34, 59, 102, 133, 178 | 🔴 Critical | Direct `kis_adapter` bypass; unshared rate limiter; broker singleton bypass |
| V-02 | `api/routers/dashboard.py` | 29–49, 68, 162 | 🔴 Critical | Direct `kis_adapter` bypass; unshared rate limiter; broker singleton bypass |
| V-03 | `api/routers/quick_trade.py` | 138–146, 182–185 | 🔴 Critical | Orders placed without OrderStateMachine / fill pipeline / DB persistence |
| V-04 | `api/routers/quick_trade.py`, `api/routers/dashboard.py` | 25–29, 41–45 | 🔴 Critical | `os.environ` credential mutation — process-wide race in multi-threaded API |
| V-05 | `api/routers/dashboard.py` | 164–166 | 🟠 High | `KISMarketData` instantiated outside broker layer |
| V-06 | `backend/brokers/kis.py`, `backend/brokers/semantic_mapper.py` | 35–36, 60–65 | 🟡 Medium | Duplicate symbol→market routing logic |
| V-07 | `backend/quant/live/pipeline.py` | 195, 302 | 🟡 Medium | Second order-submission path independent of StrategyBase tracker |
| V-08 | `api/routers/quick_trade.py` | 122–162 | 🟠 High | No DB row / AuditLog for quick-trade orders |

**Total:** 4 Critical · 2 High · 2 Medium · 0 Low

**Files compliant:** 24 of 26 broker-related files operate within correct boundaries.  
**Files with violations:** 2 (`api/routers/quick_trade.py`, `api/routers/dashboard.py`) plus 2 intra-layer duplications.

---

*No code was modified during this audit. All changes are blocked pending resolution of V-01 through V-04.*
