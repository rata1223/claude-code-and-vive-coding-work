# Broker Semantics, Reconciliation Rules & State-Consistency Architecture

> Execution reliability engineering reference.
> This document defines the authoritative rules for broker integration.
> Any implementation that diverges from these rules is incorrect.

---

## Critical Separation Constraint

```
KIS   → US OVERSEAS TRADING ONLY
Kiwoom → DOMESTIC KOREAN TRADING ONLY
```

These are not interchangeable. Their APIs, order lifecycles, settlement rules, fill
notification mechanisms, and timing semantics are fundamentally different. Any code
that allows semantic cross-contamination is a bug, not a configuration option.

The current codebase violates this. `KISOrders` has `buy_kr` / `sell_kr` methods for
domestic Korean stocks, and `KISBroker` routes domestic symbols through KIS. This must
be removed. `kiwoom_adapter/client.py` currently points to the KIS API endpoint
(`openapi.koreainvestment.com:9443`) — it is a mis-labeled stub, not a Kiwoom client.

---

## 1. Broker Semantic Audit

### 1.1 KIS — US Overseas Execution Only

**Endpoints**
```
paper:  https://openapivts.koreainvestment.com:9443
real:   https://openapi.koreainvestment.com:9443
```

**Authentication**
- OAuth2 client_credentials → `access_token`, 24h TTL
- POST orders require `Hashkey` header (hash of request body via `/uapi/hashkey`)
- Token stored in Redis with 1h pre-expiry renewal
- `app_key` + `app_secret` per environment (paper ≠ real — separate credentials)

**Rate Limits**
```
paper: 5 requests/second
real:  15 requests/second (per KIS documentation)
```
These limits are per-account, per-session. No burst headroom documented. Treat
as hard walls.

**US Overseas Order TR_IDs**
```
Overseas buy:    TTTT1002U (real)  / JTTT1002U (paper)
Overseas sell:   TTTT1006U (real)  / JTTT1006U (paper)
Overseas cancel: TTTT1004U (real)  / JTTT1004U (paper)
Overseas balance:TTTS3012R (real)  / VTTS3012R (paper)
Order list (US): TTTS3035R (real)  / VTTS3035R (paper)
```

**FORBIDDEN KIS TR_IDs (domestic — must not be used)**
```
Korean buy:    TTTC0802U / VTTC0802U  — Kiwoom responsibility
Korean sell:   TTTC0801U / VTTC0801U  — Kiwoom responsibility
Korean balance:TTTC8434R / VTTC8434R  — Kiwoom responsibility
Korean cancel: TTTC0803U / VTTC0803U  — Kiwoom responsibility
```
These TR_IDs must not appear in the KIS adapter after the separation refactor. Any
domestic Korean execution attempted via KIS is a semantic violation.

**Fill Notification: POLLING ONLY**
KIS provides no push/WebSocket fill notification for overseas orders in the REST API.
Fill status must be obtained by polling `TTTS3035R` / `VTTS3035R` (order list query).
This is a hard constraint: no event-driven fill path exists.

**Order Status Query Semantics**
- Returns a paginated list of recent orders (not a single-order lookup by ID)
- Match by `odno` field to find a specific order
- If order is not in the first page (pagination): order is either old or on a subsequent page
- Fallback to `output[0]` is FORBIDDEN — it returns status from a different order
- Pagination must be implemented; absence of an order on page 1 is not absence of the order

**Order Lifecycle (US overseas)**
```
PENDING (local intent written to DB)
  ↓ POST /uapi/overseas-stock/v1/trading/order
SUBMITTED (odno returned from broker)
  ↓ polling TTTS3035R every 10→30→60→120→300s
PARTIAL_FILLED (ft_ccld_qty > 0 but < ft_ord_qty)
  ↓ continue polling
FILLED (ft_ccld_qty == ft_ord_qty)
CANCELED (ord_stts_name contains "취소" or "cancel")
REJECTED (ord_stts_name contains "거부" or "reject")
```

**Cancel Semantics (US)**
- Requires original `odno` (broker order number)
- Cancel endpoint: `/uapi/overseas-stock/v1/trading/order-rvsecncl`
- TR: `TTTT1004U` (real) / `JTTT1004U` (paper)
- Body requires: `ORGN_ODNO`, `PDNO`, `OVRS_EXCG_CD`, qty
- A cancel request does not guarantee cancellation. Must poll to confirm status.
- Cancel of a fully-filled order returns an error code; this is not a failure — it means the order filled before cancel landed.

**Exchange Code (EXCD) Requirement**
Every US order and query requires `OVRS_EXCG_CD`: `NASD`, `NYSE`, `AMEX`, `SNAS` (NASDAQ), etc.
This field is mandatory and must be correct for the symbol. An incorrect EXCD causes order rejection.

**Price Format (US)**
- Decimal string to 2 decimal places: `"123.45"`
- Integer prices are rejected by KIS overseas endpoints
- Do NOT cast to `int()` before submission

**Settlement (US)**
- T+2 business days (US market convention)
- Cash is not available for reinvestment until T+2
- KIS balance query reflects cash including unsettled sells as "settl_amt" separate from available cash

**Market Hours (US sessions)**
```
US regular session: 09:30–16:00 ET
KST equivalent:    22:30–05:00 KST (next day)
Pre/after market:  NOT supported via KIS standard overseas API
```
Do not submit orders outside the regular session window. KIS will reject them.

**Paper vs Real Environment**
- Paper and real credentials are separate KIS accounts. The paper account does NOT share state with the real account.
- Any position or order query against the wrong environment returns the wrong account's state.
- Environment is selected by `KIS_ENV=paper|real` which controls both the endpoint URL and the TR_ID prefix.
- Mixing environments (e.g., placing a real order while checking paper balance) is a critical configuration error.

---

### 1.2 Kiwoom — Domestic Korean Execution Only

**API Type**
Kiwoom offers two API surfaces:
1. **Open API+ (COM-based, Windows only)**: The traditional Kiwoom API using COM automation (HTS integration). Not suitable for Linux/Docker deployment.
2. **Open API REST** (`https://openapi.kiwoom.com:10000`): REST interface, suitable for server deployment.

The `kiwoom_adapter/` currently targets the REST endpoint incorrectly. It reuses KIS endpoint paths (`/uapi/domestic-stock/v1/trading/order-cash`) which do not exist on `openapi.kiwoom.com`. The adapter is non-functional and must be rewritten from Kiwoom's actual REST API documentation.

**Rate Limits (Kiwoom REST)**
- 5 requests/second (approximate; confirm from Kiwoom developer portal)
- Different from KIS limits; do NOT share a rate limiter between brokers

**Authentication (Kiwoom REST)**
- OAuth2 client_credentials → `access_token`
- No Hashkey requirement (unlike KIS)
- Token stored per-process; not shared with KIS token cache

**Domestic Order TR_IDs (Kiwoom-specific)**
Kiwoom's REST API uses its own endpoint paths and field names — NOT the same as KIS domestic.
Kiwoom documentation must be consulted for actual paths. Do not assume KIS field name compatibility.

**Fill Notification: WEBSOCKET AVAILABLE**
Unlike KIS, Kiwoom provides real-time fill push via WebSocket for domestic stocks.
This is a fundamental architectural difference from KIS. The execution path for Kiwoom
must use WebSocket callbacks, not polling. Using polling for Kiwoom domestic is:
- Slower (unnecessary latency on fill acknowledgment)
- Higher API load
- Misses the architectural advantage Kiwoom provides

The `OrderFillPoller` class used for KIS is NOT the right mechanism for Kiwoom.
Kiwoom requires a separate `DomesticFillReceiver` that subscribes to the WebSocket channel.

**Order Lifecycle (Domestic Korean)**
```
PENDING (local intent written to DB)
  ↓ POST to Kiwoom order endpoint
SUBMITTED (odno returned from broker)
  ↓ WebSocket push (체결 통보)
PARTIAL_FILLED (체결수량 < 주문수량)
  ↓ continue WebSocket subscription
FILLED (체결수량 == 주문수량)
CANCELED (취소 확인)
REJECTED (거부)
```

**Cancel Semantics (Domestic)**
- Cancel requires original order number (`ORGN_ODNO`)
- Kiwoom domestic cancel: same concept, different endpoint and field names than KIS
- A partial fill cannot be fully canceled — only the unfilled remainder can be canceled
- Must query broker after cancel attempt to confirm terminal state

**Price Format (Domestic Korean)**
- Integer KRW prices only: `"10000"`, NOT `"10000.00"`
- Decimal prices are rejected
- Do NOT use float for KRW prices; use `int(price)` always

**Market Hours (Domestic Korean)**
```
Regular session: 09:00–15:30 KST
Pre-market:      08:30–09:00 KST (limited order types)
After-market:    15:30–16:00 KST (single price auction)
```
The execution engine should target the regular session only (09:05–15:25 KST with buffers).

**Settlement (Domestic Korean)**
- T+2 KRX business days
- Settlement calendar follows KRX (Korean Exchange) holidays, not US holidays
- Cash released on T+2 KRX business day

**Symbol Format (Domestic)**
- 6-digit zero-padded numeric string: `"069500"`, `"005930"`, `"360750"`
- Never interpreted as a number (leading zeros are significant)
- Do NOT cast to `int()` — `int("069500")` = `69500` which is a different or nonexistent symbol

**Account Format (Kiwoom)**
- Format may differ from KIS. KIS uses CANO(8) + ACNT_PRDT_CD(2).
- Kiwoom account format must be confirmed from Kiwoom documentation.
- Do NOT assume KIS account splitting logic applies.

---

### 1.3 Critical Semantic Differences Summary

| Dimension | KIS (overseas) | Kiwoom (domestic) |
|---|---|---|
| Market | US stocks (NYSE, NASDAQ, AMEX) | KRX domestic stocks/ETFs |
| Fill notification | Polling only | WebSocket push |
| Price type | Decimal USD string | Integer KRW string |
| Exchange routing | EXCD required per order | Not required |
| Settlement calendar | US business days | KRX business days |
| Settlement days | T+2 US | T+2 KRX |
| Currency | USD | KRW |
| Hashkey on POST | Required | Not required |
| Token cache | Redis (shared w/ rate limiter) | Separate |
| Rate limit | 5/s paper, 15/s real | 5/s (confirm) |
| Cancel mechanism | ORGN_ODNO + OVRS_EXCG_CD | ORGN_ODNO (Kiwoom fields) |
| Order status query | List-paginated, match by odno | Real-time via WebSocket |
| Retry on POST | UNSAFE (no client_order_id) | UNSAFE (no client_order_id) |
| Session hours (KST) | 22:30–05:00 (+1 day) | 09:05–15:25 |
| Price for KR symbols | N/A — not permitted | Integer KRW |
| Paper env available | Yes (separate endpoint) | Confirm availability |

---

## 2. Reconciliation Philosophy

### 2.1 Authority Hierarchy (by state domain)

```
┌─────────────────────────────────────────────────────────────────────┐
│  BROKER (KIS or Kiwoom)                                             │
│  Authoritative for: actual fills, current open orders,              │
│  current positions, available cash                                  │
│  Must query on: startup, market-open, any ambiguous order state     │
│  Trust level: HIGHEST — never override with local state             │
├─────────────────────────────────────────────────────────────────────┤
│  DATABASE (PostgreSQL)                                              │
│  Authoritative for: order intent, idempotency keys, fill history,   │
│  risk state (kill_switch, peak_equity), strategy runs,              │
│  reconciliation log (what divergences were found and when)          │
│  Trust level: HIGH — authoritative for our intent, not broker state │
├─────────────────────────────────────────────────────────────────────┤
│  REDIS                                                              │
│  Authoritative for: NOTHING                                         │
│  Caches: rate limits, tokens, daily PnL (with DB backup)            │
│  Trust level: EPHEMERAL — reconstruction from broker+DB must work   │
│  if Redis is empty                                                  │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Reconciliation Triggers

```
1. STARTUP        — mandatory before any order placement
2. MARKET_OPEN    — before each session (KR 09:05, US 22:35 KST)
3. MARKET_CLOSE   — after each session (KR 15:30, US 05:00 KST)
4. PERIODIC       — every 30 minutes while a session is active
5. FILL_RECEIVED  — immediately after a fill event (Kiwoom WebSocket)
6. MANUAL         — operator-triggered via API
```

Each reconciliation run is **scoped by broker and market**. KIS overseas reconciliation
and Kiwoom domestic reconciliation run **independently** and never share state.

### 2.3 Reconciliation Conflict Resolution Rules

These rules apply identically to both brokers. "Broker" means the relevant
broker for the market being reconciled.

**Rule 1: Broker position ≠ DB position**
```
Resolution: Broker wins. DB position is updated to match broker exactly.
Action: Write ReconciliationEvent(type="position_correction", source="broker_wins")
Log: WARNING — includes broker qty, DB qty, symbol, timestamp
```

**Rule 2: DB has order in SUBMITTED/PENDING, broker has no record**
```
If order age < 1 hour:
    Resolution: Treat as UNKNOWN. Do NOT cancel or mark filled.
    Action: Queue for re-query after 5 minutes.
    Do NOT place a replacement order.

If order age >= 1 hour:
    Resolution: Mark as LOST in DB (status = "lost").
    Action: Write ReconciliationEvent(type="order_lost")
    Do NOT place a replacement order.
    Alert operator via Telegram.
```

**Rule 3: Broker has fill, DB has no fill record**
```
Resolution: Broker wins. Write fill record to DB.
Action: Write FillEvent to DB. Update position.
Log: WARNING — missing fill detected, reconstructed from broker
```

**Rule 4: DB has position, broker has no position**
```
Resolution: Broker wins. Delete DB position row (mark as closed).
Action: Write ReconciliationEvent(type="ghost_position_removed")
Log: WARNING — ghost position; verify no open orders for this symbol
```

**Rule 5: Broker fill price ≠ DB fill price (same qty)**
```
Resolution: Broker wins. Update DB fill price.
Action: Write ReconciliationEvent(type="fill_price_correction")
Log: INFO — price drift corrected
Impact: Recalculate P&L from corrected fill price
```

**Rule 6: DB position qty differs from broker by ≤ tolerance**
```
Tolerance (value-based, not share-based):
  Domestic (KRW): difference in position value <= 50,000 KRW
  Overseas (USD): difference in position value <= $10 USD
Resolution: If within tolerance, log INFO and do not repair.
If outside tolerance, apply Rule 1.
```
Note: `_QTY_TOLERANCE = 1` (share count) is rejected. Tolerance must be
value-based. 1 share of SPY = $500+ USD — not a tolerable silent error.

**Rule 7: Duplicate fill records detected**
```
Condition: Two fill records with same broker_order_id and same qty+price
Resolution: Deduplicate — keep the earlier record, delete the newer.
Action: Write ReconciliationEvent(type="duplicate_fill_removed")
This cannot occur via normal operations; investigate root cause immediately.
```

**Rule 8: Broker position exists with no matching order or fill in DB**
```
Resolution: Accept broker position as ground truth. Insert position row.
Action: Write ReconciliationEvent(type="untracked_position_accepted")
Alert operator: "Untracked position found — manual review required"
Do NOT auto-generate backfill orders.
```

### 2.4 What Reconciliation Must Never Do

- Place new orders (reconciliation is read-adjust, never execute)
- Cancel orders without confirmed broker state (confirm first, then cancel if appropriate)
- Modify fill prices retroactively without broker confirmation
- Proceed with trading after reconciliation errors (halt until clean pass)
- Run both KIS and Kiwoom reconciliations on the same object/connection pool

---

## 3. State-Consistency Architecture

### 3.1 Append-Only Execution History

The current `orders` table has a mutable `status` column. This is wrong. The execution
truth must be an event log. The `status` column becomes a derived materialized view.

**Canonical event log tables (one pair per market):**

```
order_events_overseas   — KIS US events
order_events_domestic   — Kiwoom KR events
fill_events_overseas    — KIS US fills
fill_events_domestic    — Kiwoom KR fills
```

Each event is append-only:

```sql
CREATE TABLE order_events_overseas (
    id              BIGSERIAL PRIMARY KEY,
    idempotency_key VARCHAR(100) NOT NULL,
    broker_order_id VARCHAR(50),
    symbol          VARCHAR(20) NOT NULL,
    side            VARCHAR(4) NOT NULL,
    qty             INTEGER NOT NULL,
    price           NUMERIC(12, 4) NOT NULL,
    event_type      VARCHAR(30) NOT NULL,  -- PENDING/SUBMITTED/PARTIAL_FILLED/FILLED/CANCELED/REJECTED/LOST
    event_source    VARCHAR(20) NOT NULL,  -- strategy/poller/reconciler/operator
    strategy_run_id INTEGER,
    raw_payload     JSONB,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ON order_events_overseas (idempotency_key);
CREATE INDEX ON order_events_overseas (broker_order_id) WHERE broker_order_id IS NOT NULL;
CREATE INDEX ON order_events_overseas (occurred_at);
```

The "current order status" is always: `SELECT event_type FROM order_events_overseas WHERE idempotency_key = $1 ORDER BY occurred_at DESC LIMIT 1`.

A materialized view or denormalized cache column is acceptable as a performance
optimization, but it is always derived from the event log, never the source of truth.

### 3.2 Idempotency Key Construction

Since neither KIS nor Kiwoom accepts a client-controlled order reference, idempotency
is enforced exclusively on our side.

**Idempotency key schema:**
```
{broker}:{market}:{strategy_run_id}:{symbol}:{side}:{date_yyyymmdd}:{sequence}
```

Examples:
```
kis:us:42:NVDA:buy:20260528:001
kiwoom:kr:17:069500:sell:20260528:001
```

Properties:
- Deterministic for a given logical order attempt
- Unique within a trading day and strategy run
- Stored in DB before any broker call
- If the same key already exists in DB with a non-PENDING/LOST terminal status:
  the order is a duplicate — reject without calling broker
- Sequence increments only after the previous order for this symbol+side reaches
  a terminal state (FILLED / CANCELED / REJECTED / LOST)

**Pre-submission fence protocol:**
```
1. Generate idempotency_key
2. INSERT INTO order_events (event_type='PENDING', idempotency_key=key, ...)
   ON CONFLICT (idempotency_key) DO NOTHING
3. Check: if INSERT affected 0 rows → duplicate, abort
4. Check: if existing row has terminal status → duplicate, abort
5. Query broker: does an open order for this symbol+side already exist?
   If yes and matches intent → attach to it (update broker_order_id), skip POST
6. Submit to broker
7. INSERT INTO order_events (event_type='SUBMITTED', broker_order_id=returned_id, ...)
8. Register with fill poller (KIS) or WebSocket subscription (Kiwoom)
```

Steps 1–3 are atomic (DB unique constraint). A crash between step 6 and step 7
leaves a PENDING event with no SUBMITTED follow-up. Recovery handles this via step 5
on the next reconciliation cycle.

### 3.3 Broker State Cache Freshness (Staleness TTL)

All locally-held broker state has a maximum freshness TTL. After expiry, the state
must be considered stale and re-fetched before use. Do not make trading decisions
on stale state.

```
State                   | TTL      | On expiry
------------------------|----------|------------------------------------------
Market price (US)       | 60s      | Block order. Fetch fresh price.
Market price (KR)       | 30s      | Block order. Fetch fresh price.
Account balance (US)    | 5 min    | Refetch before position sizing.
Account balance (KR)    | 5 min    | Refetch before position sizing.
Open orders list (US)   | 2 min    | Refetch before reconciliation.
Open orders list (KR)   | 1 min    | Refetch (WebSocket gives real-time, but confirm)
Position snapshot       | 15 min   | Trigger reconciliation.
FX rate (USD/KRW)       | 1 hour   | Use cached value; refetch in background.
Auth token              | 23 hours | Renew proactively at 23h.
```

Using price data older than its TTL for order sizing is a risk violation, not a
minor inefficiency.

### 3.4 BrokerCapabilities Layer

Each broker exposes a `BrokerCapabilities` object that the execution engine consults
before performing any operation. This prevents the execution engine from attempting
operations the broker cannot safely support.

```python
@dataclass(frozen=True)
class BrokerCapabilities:
    broker_id: str              # "kis" | "kiwoom"
    market: str                 # "overseas" | "domestic"
    currency: str               # "USD" | "KRW"
    fill_mechanism: str         # "polling" | "websocket"
    price_type: str             # "decimal" | "integer"
    requires_exchange_code: bool
    requires_hashkey: bool
    rate_limit_per_second: int
    settlement_days: int
    session_open_kst: time      # market open (KST)
    session_close_kst: time     # market close (KST)
    supports_market_orders: bool
    supports_stop_orders: bool
    retry_safe_on_submit: bool  # False for both KIS and Kiwoom
    max_single_order_qty: int
    cancel_requires_exchange_code: bool
    order_id_format: str        # "numeric_string" | "broker_specific"
```

**KIS capabilities:**
```python
KIS_OVERSEAS = BrokerCapabilities(
    broker_id="kis",
    market="overseas",
    currency="USD",
    fill_mechanism="polling",
    price_type="decimal",
    requires_exchange_code=True,
    requires_hashkey=True,
    rate_limit_per_second=15,   # real; 5 for paper
    settlement_days=2,
    session_open_kst=time(22, 30),
    session_close_kst=time(5, 0),
    supports_market_orders=True,
    supports_stop_orders=False,
    retry_safe_on_submit=False,
    max_single_order_qty=99999,
    cancel_requires_exchange_code=True,
    order_id_format="numeric_string",
)
```

**Kiwoom capabilities:**
```python
KIWOOM_DOMESTIC = BrokerCapabilities(
    broker_id="kiwoom",
    market="domestic",
    currency="KRW",
    fill_mechanism="websocket",
    price_type="integer",
    requires_exchange_code=False,
    requires_hashkey=False,
    rate_limit_per_second=5,
    settlement_days=2,
    session_open_kst=time(9, 5),
    session_close_kst=time(15, 25),
    supports_market_orders=True,
    supports_stop_orders=False,
    retry_safe_on_submit=False,
    max_single_order_qty=99999,
    cancel_requires_exchange_code=False,
    order_id_format="broker_specific",
)
```

### 3.5 BrokerSemanticMapper Layer

Each broker has a mapper that translates the abstract `Order` model to and from
broker-specific wire format. The execution engine only speaks `Order`; it never
constructs broker-specific JSON payloads directly.

```python
class BrokerSemanticMapper(ABC):

    @abstractmethod
    def to_submit_payload(self, order: Order, account: AccountConfig) -> dict:
        """Translate Order to broker POST body."""

    @abstractmethod
    def from_status_response(self, raw: dict, original_order: Order) -> Order:
        """Translate broker status response to Order."""

    @abstractmethod
    def from_fill_event(self, raw: dict) -> FillEvent:
        """Translate broker fill notification to FillEvent."""

    @abstractmethod
    def to_cancel_payload(self, order: Order, account: AccountConfig) -> dict:
        """Translate Order to broker cancel body."""

    @abstractmethod
    def extract_broker_order_id(self, submit_response: dict) -> str:
        """Extract broker-assigned order ID from submit response."""

    @abstractmethod
    def is_terminal(self, raw_status: dict) -> bool:
        """True if broker response indicates a terminal order state."""
```

`KISSemanticMapper` and `KiwoomSemanticMapper` are the two concrete implementations.
They encapsulate all field-name knowledge, price formatting, status code interpretation,
and exchange code lookup. No code outside these mappers should know the name `OVRS_EXCG_CD`
or `ORD_DVSN`.

---

## 4. Recovery Semantics

### 4.1 Startup Recovery Sequence

Trading is forbidden until the full recovery sequence completes for each broker.
Recovery is per-broker and per-market. KIS overseas recovery and Kiwoom domestic
recovery run independently and in parallel (they touch different tables and different
broker APIs).

```
For each broker in [KIS_OVERSEAS, KIWOOM_DOMESTIC]:

  Step 1: DB connectivity check
    - Execute SELECT 1 on the relevant event log table
    - Failure = fatal; halt startup for this broker

  Step 2: Broker API connectivity check
    - Fetch auth token (KIS) or verify existing token (Kiwoom)
    - Failure = non-fatal; broker enters DEGRADED state
    - Trading blocked for this broker until connectivity restored

  Step 3: Risk state restoration
    - Load DailyRiskState for today from DB
    - Restore kill_switch, peak_equity, daily_pnl
    - If kill_switch=True: this broker remains in SAFE_MODE

  Step 4: Broker balance fetch
    - Fetch current balance from broker API
    - Record as baseline for session risk calculations
    - Failure = non-fatal; retry up to 3x with 5s backoff before declaring DEGRADED

  Step 5: Broker position fetch
    - Fetch all open positions from broker
    - Store as broker_snapshot for comparison in step 6

  Step 6: DB ↔ broker reconciliation
    - Apply conflict rules from Section 2.3
    - Any reconciliation error with positions → halt trading for this broker

  Step 7: Open order recovery
    - Query DB for orders in [PENDING, SUBMITTED, PARTIAL_FILLED] state
    - For each: query broker for current status
    - Apply any status updates as new events in the event log
    - Register still-open orders with the fill mechanism (poller/WebSocket)

  Step 8: Enable trading for this broker
    - Set SAFE_MODE[broker] = True
    - Log: "Recovery complete — {broker} trading enabled"
```

### 4.2 Recovery Guarantees

After any single-point failure and successful restart:

**Guaranteed:**
- No duplicate orders for orders already submitted in the previous session
- No double-counting of fills that were already recorded
- Risk limits (kill_switch, peak_equity) reflect the state at crash time
- Position tracker reflects broker-authoritative positions
- All in-flight orders (SUBMITTED, PARTIAL_FILLED) are re-registered with fill mechanism

**Not guaranteed:**
- Intra-session P&L continuity (daily P&L is restored from DB pessimistically)
- Exact order of fill event processing if multiple fills arrived simultaneously at crash
- Real-time continuity of WebSocket subscriptions (Kiwoom must re-subscribe after restart)

### 4.3 Idempotent Recovery

The recovery procedure may be run multiple times (e.g., if startup is retried).
Every step must be idempotent:

- DB writes use `INSERT ... ON CONFLICT DO NOTHING` or `ON CONFLICT DO UPDATE`
- Broker queries are read-only (no side effects)
- Position corrections use absolute values (`qty = broker_qty`), not deltas
- ReconciliationEvent inserts have a `(broker, symbol, started_at)` uniqueness constraint
  to prevent duplicate log entries from multiple recovery runs in the same startup cycle

### 4.4 SAFE_MODE Per Broker

`SAFE_MODE` must be broker-scoped, not process-scoped. The current single global
`SAFE_MODE = SafeModeState()` does not distinguish between KIS and Kiwoom being
individually available.

```python
@dataclass
class BrokerSafeMode:
    broker_id: str
    _can_trade: bool = False
    _reason: str = "initializing"

    def enable(self): ...
    def disable(self, reason: str): ...

SAFE_MODE: dict[str, BrokerSafeMode] = {
    "kis":    BrokerSafeMode("kis"),
    "kiwoom": BrokerSafeMode("kiwoom"),
}
```

Kill-switch or connectivity failure for KIS overseas should not block Kiwoom domestic
trading (and vice versa). These are separate accounts, separate API sessions,
separate risk envelopes.

### 4.5 WebSocket Reconnection (Kiwoom)

Kiwoom's real-time fill channel requires an active WebSocket connection.
On disconnect, fills are lost during the gap. Recovery protocol:

```
1. Detect disconnect (heartbeat timeout or explicit close event)
2. Record disconnect_at timestamp
3. Reconnect with exponential backoff: 2s, 4s, 8s, 16s, 32s (max)
4. On reconnect: re-subscribe to fill channel for all open orders
5. Immediately query broker for order status of all SUBMITTED/PARTIAL_FILLED orders
   (to catch fills that arrived during the disconnect window)
6. Apply any newly-discovered fills as RecoveredFillEvents in the event log
7. Resume normal WebSocket fill processing
```

Step 5 is mandatory. Any fill that arrived during the disconnect window will be
silently missed otherwise.

---

## 5. Execution-Safety Constraints

### 5.1 Unsafe Retry Rule

**POST requests to order endpoints MUST NOT be retried on timeout or network error.**

This applies to both KIS and Kiwoom. Neither provides a client_order_id field.
The only safe behavior on a POST timeout for an order submission:

```
1. Do NOT retry the POST.
2. Wait for the order's idempotency_key entry in DB (written in pre-submission fence step 2).
3. Query broker for any open order matching this symbol+side.
4. If found with matching qty/price: attach the existing broker order to this idempotency_key.
5. If not found after 60 seconds: mark as LOST, write event, alert.
```

Retry is safe for:
- GET requests (all query endpoints)
- Authentication/token endpoints
- Balance/position query endpoints

Retry is UNSAFE for:
- Any POST to an order placement endpoint
- Any POST to a cancel endpoint (canceling twice cancels an already-filled order)

### 5.2 Order Submission Gate (per order attempt)

Before calling any broker's order submission endpoint:

```
Gate 1: SAFE_MODE[broker].can_trade == True
Gate 2: Within market session hours for this broker's market
Gate 3: idempotency_key does NOT already exist in DB with non-PENDING status
Gate 4: No open order for this symbol (PositionTracker.can_place_order())
Gate 5: ENABLE_LIVE_TRADING == "true" (shadow mode gate)
Gate 6: Kill-switch not active for this broker's risk envelope
Gate 7: Position size within limits (max 5% of portfolio)
Gate 8: Market data freshness ≤ TTL for this broker
```

All gates must pass. Any gate failure → order rejected, logged, alerted.
Gates are evaluated in order; first failure stops evaluation.

### 5.3 Duplicate Order Prevention (defense in depth)

Three independent layers, all must be satisfied:

```
Layer 1 (DB uniqueness):
  The idempotency_key unique constraint on order_events prevents two PENDING
  events for the same logical order. One of them will fail at INSERT.

Layer 2 (in-memory lock):
  PositionTracker._pending_symbols blocks a second order for a symbol
  that already has an outstanding order.

Layer 3 (broker query):
  Pre-submission broker query checks for an existing open order for this
  symbol+side before submitting. If one exists, attach rather than submit.
```

Layer 3 queries the broker on every submission. This costs one API call per order
but prevents duplicates that slipped past layers 1 and 2 (e.g., crash between
fence write and layer 2 lock).

### 5.4 Cancel Safety

```
Before calling cancel:
  1. Query broker for current order status.
  2. If already FILLED: do not cancel. Log and return.
  3. If already CANCELED: idempotent, no action needed. Log and return.
  4. If SUBMITTED or PARTIAL_FILLED: proceed with cancel.

After calling cancel:
  1. Do NOT mark as CANCELED in DB immediately.
  2. Wait for broker confirmation via polling/WebSocket.
  3. Only write CANCELED event after broker confirms terminal state.
```

Premature CANCELED marking (before broker confirmation) is a state desynchronization
bug. If the cancel fails silently, the DB says CANCELED while the broker still has
an open order.

### 5.5 Market Hours Enforcement

Order submission is only permitted within the broker's session window.

```python
def is_within_session(broker_caps: BrokerCapabilities, now_kst: datetime) -> bool:
    """Returns True only during the confirmed regular trading session."""
    t = now_kst.time()
    open_t = broker_caps.session_open_kst
    close_t = broker_caps.session_close_kst

    if open_t < close_t:
        return open_t <= t <= close_t
    else:
        # Overnight session (e.g., US: 22:30 to 05:00 next day)
        return t >= open_t or t <= close_t
```

If `is_within_session()` returns False: reject the order, log a WARNING.
Do NOT silently drop orders — log them so the operator knows a signal was discarded.

### 5.6 Stale Market Data Gate

A price fetch that exceeds its TTL must not be used for order sizing.
The `StaleDataWatchdog` must be called in the execution path, not defined and ignored.

```python
def price_for_order(symbol: str, broker: BrokerAdapter) -> float:
    cap = broker.capabilities
    price = broker.get_price(symbol)
    # Record fetch timestamp
    if not is_fresh(symbol, cap.market):
        raise StaleMarketDataError(f"{symbol} price is stale — order blocked")
    return price
```

`StaleMarketDataError` halts the order attempt for that symbol. It does not halt
all trading — other symbols with fresh data may proceed.

---

## 6. Broker Separation Strategy

### 6.1 Target Directory Structure

```
backend/
├── brokers/
│   ├── base.py                  # BrokerAdapter ABC, BrokerCapabilities, BrokerSemanticMapper ABC
│   ├── models.py                # Order, Fill, Position, Balance (broker-agnostic)
│   │
│   ├── kis/                     # KIS — US overseas ONLY
│   │   ├── __init__.py
│   │   ├── auth.py              # KIS token, Hashkey
│   │   ├── client.py            # KISClient with rate limiter
│   │   ├── mapper.py            # KISSemanticMapper
│   │   ├── broker.py            # KISBroker(BrokerAdapter) — overseas methods only
│   │   ├── poller.py            # OrderFillPoller — KIS-specific polling
│   │   └── capabilities.py     # KIS_OVERSEAS BrokerCapabilities constant
│   │
│   └── kiwoom/                  # Kiwoom — domestic Korean ONLY
│       ├── __init__.py
│       ├── auth.py              # Kiwoom token
│       ├── client.py            # KiwoomClient with rate limiter
│       ├── mapper.py            # KiwoomSemanticMapper
│       ├── broker.py            # KiwoomBroker(BrokerAdapter) — domestic methods only
│       ├── websocket.py         # DomesticFillReceiver — Kiwoom WebSocket
│       └── capabilities.py     # KIWOOM_DOMESTIC BrokerCapabilities constant
│
├── execution/
│   ├── base.py                  # ExecutionEngine ABC
│   ├── gate.py                  # Submission gate (all 8 gates)
│   ├── order_machine.py         # OrderStateMachine (broker-agnostic)
│   ├── position_tracker.py      # PositionTracker (broker-agnostic)
│   ├── circuit_breaker.py       # ConsecutiveFailureBreaker
│   │
│   ├── overseas/                # KIS execution path
│   │   ├── engine.py            # OverseasExecutionEngine
│   │   ├── poller.py            # Fill polling loop
│   │   └── reconciler.py       # OverseasReconciler
│   │
│   └── domestic/                # Kiwoom execution path
│       ├── engine.py            # DomesticExecutionEngine
│       ├── ws_receiver.py       # WebSocket fill receiver
│       └── reconciler.py       # DomesticReconciler
│
└── database/
    └── models.py                # Includes separate event tables per market

execution/
├── order_events_overseas        — KIS append-only event log
├── order_events_domestic        — Kiwoom append-only event log
├── fill_events_overseas         — KIS fills (immutable)
├── fill_events_domestic         — Kiwoom fills (immutable)
├── positions_overseas           — KIS positions (from fills + reconciliation)
├── positions_domestic           — Kiwoom positions (from fills + reconciliation)
├── reconciliation_logs          — one entry per reconciliation run, per broker
└── daily_risk_states            — per broker (kis_overseas, kiwoom_domestic)
```

### 6.2 What Must Be Removed or Quarantined

**Remove from KIS adapter:**
- `KISOrders.buy_kr()` — domestic, not KIS responsibility
- `KISOrders.sell_kr()` — domestic, not KIS responsibility
- `KISBroker.cancel_order()` current implementation — only handles KR cancel; US cancel has different TR_ID and body
- All domestic TR_IDs from `kis_adapter/orders.py` (`TTTC0802U`, `VTTC0802U`, etc.)

**Remove from routing logic:**
- Symbol heuristic `len(symbol) == 6 and symbol.isdigit()` for market routing — replace with explicit `market` parameter on all execution methods
- `KISBroker.place_order()` routing switch — this method cannot exist as a single unified endpoint; replace with `place_overseas_order()` and disallow domestic symbols

**Rewrite kiwoom_adapter:**
- Current `kiwoom_adapter/client.py` uses KIS API URL and endpoints — this is entirely wrong
- `kiwoom_adapter/orders.py` uses KIS endpoint paths — also wrong
- The stub must be rewritten from actual Kiwoom REST API documentation

**Remove from UNIVERSE:**
- `KR_ETF` must not appear in the KIS trading universe
- `US_ETF` and `US_LARGE` must not appear in the Kiwoom trading universe
- `UNIVERSE = US_ETF + US_LARGE + KR_ETF` is a shared list that enables accidental cross-routing

```python
# Replace with:
KIS_OVERSEAS_UNIVERSE = US_ETF + US_LARGE
KIWOOM_DOMESTIC_UNIVERSE = KR_ETF
```

### 6.3 Mobile Separation

The mobile UI must present separate views and controls for domestic and overseas brokers.
Credential storage, position display, and order entry must be market-scoped.

```
mobile/src/
├── brokers/
│   ├── overseas/              # KIS context
│   │   ├── credentials.js     # KIS-specific credential fields
│   │   ├── portfolio.js       # US position display
│   │   └── orders.js          # US order entry (decimal prices, USD)
│   └── domestic/              # Kiwoom context
│       ├── credentials.js     # Kiwoom-specific credential fields
│       ├── portfolio.js       # KR position display
│       └── orders.js          # KR order entry (integer prices, KRW)
```

A UI screen must never display KIS positions and Kiwoom positions in the same
portfolio aggregation view without explicit currency conversion and clear market labeling.
Commingling KIS USD positions and Kiwoom KRW positions as a single "total" is a
display liability (incorrect FX, incorrect P&L attribution).

### 6.4 Risk Envelope Separation

Each broker has its own independent risk envelope:

```python
@dataclass
class BrokerRiskState:
    broker_id: str
    market: str
    daily_pnl_krw: float          # always in KRW (convert USD at snapshot time)
    peak_equity_krw: float
    kill_switch: bool
    kill_reason: str
    trade_date: date

# Two independent risk states, never merged:
KIS_RISK    = BrokerRiskState(broker_id="kis",    market="overseas", ...)
KIWOOM_RISK = BrokerRiskState(broker_id="kiwoom", market="domestic", ...)
```

A kill-switch on KIS overseas does not halt Kiwoom domestic trading.
A MDD breach on Kiwoom domestic does not liquidate KIS overseas positions.
Cross-broker portfolio P&L is calculated at display/summary time with explicit FX conversion,
not shared in the execution risk state.

### 6.5 Shared Infrastructure (explicit boundaries)

These components are legitimately shared between brokers:

| Component | Shared? | Notes |
|---|---|---|
| `OrderStateMachine` | Yes | Broker-agnostic state transitions |
| `PositionTracker` | Yes (separate instance per broker) | Each broker gets its own instance |
| `BrokerCapabilities` | No | One per broker, not mixed |
| `SafeModeState` | Yes (separate instance per broker) | Indexed by broker_id |
| `PersistentLossTracker` | Yes (separate instance per broker) | Separate DB rows |
| Postgres connection pool | Shared | Separate tables per market |
| Redis | Shared | Keys namespaced by broker: `kis:*`, `kiwoom:*` |
| Telegram notifier | Shared | Single notification channel |
| Scheduler | Shared (BackgroundScheduler) | Separate job IDs per broker |

---

## Appendix A: Forbidden Patterns

The following patterns are symptoms of semantic leakage and must be rejected in code review:

```python
# FORBIDDEN: symbol routing by heuristic
if len(symbol) == 6 and symbol.isdigit():
    use_kiwoom()
else:
    use_kis()

# FORBIDDEN: shared order table for both markets
db.query(Order).filter(Order.symbol == symbol)  # which market?

# FORBIDDEN: unified place_order on both brokers
broker.place_order(symbol, ...)  # without market parameter

# FORBIDDEN: KIS domestic TR_IDs in any new code
tr_id = "TTTC0802U"  # this is KIS domestic — must not exist

# FORBIDDEN: KIS adapter retry on order POST
for attempt in range(MAX_RETRIES):
    resp = requests.post(order_url, ...)  # retrying order POST

# FORBIDDEN: tolerance by share count
QTY_TOLERANCE = 1  # 1 share of AVGO = $150+

# FORBIDDEN: KR_ETF in KIS universe
KIS_UNIVERSE = US_ETF + US_LARGE + KR_ETF  # cross-contamination

# FORBIDDEN: db.merge() without unique constraint
db.merge(Position(symbol=sym, ...))  # inserts new row if no PK match
```

## Appendix B: Settlement Calendar Notes

**US (KIS overseas):**
- T+2 US business days
- US holiday calendar (NYSE closures apply)
- Proceeds from sell orders available T+2 in USD
- Wire transfers to KRW account: additional banking days

**Domestic Korean (Kiwoom):**
- T+2 KRX business days
- KRX holiday calendar (Korean national holidays, substitute holidays)
- US and KRX holidays do not overlap — a day closed in the US may be open in KR and vice versa
- Do not use the same calendar for both markets

---

*This document is the specification. Any implementation that diverges from it is a bug.*
*Broker semantics are facts, not design preferences.*
