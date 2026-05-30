# Architectural Audit & Operational Fragility Report

> Principal quantitative trading platform audit — May 2026.
> All findings are pre-implementation: nothing is fixed here, only documented.
> Fix priority: CRITICAL > HIGH > MEDIUM > LOW.

---

## 1. Architectural Audit Report

### 1.1 System Topology

The platform is not one system. It is three partially-overlapping systems sharing the same KIS account:

```
A.  Legacy Bot (bot/main.py)
      BlockingScheduler → TradingEngine → KISOrders → KIS API
      Risk: strategy/risk.py (Redis only)
      Notifications: bot/notifier.py

B.  Backend Worker (backend/worker/runner.py)        ← ACTIVE
      BackgroundScheduler → Redis PubSub → StrategyWorker
      → IndicatorStrategy → KISBroker → KIS API
      Risk: backend/quant/risk/engine.py (Redis + DB)
      Reconciliation: backend/execution/reconciler.py

C.  API Layer (api/main.py, FastAPI, port 8000)      ← ACTIVE
      QuantDinger-derived REST API
      Own DB models (api/models.py), own auth, own encryption
      Entirely separate from the worker's data path
```

System A is commented out in docker-compose.yml. Systems B and C are both active. Systems B and C share the same Postgres database but use entirely different ORM models and connection pools — they see the same physical tables but know nothing of each other's schemas.

### 1.2 Dual API Surface

| Service | Port | Framework | Auth | DB Models | Order Path |
|---|---|---|---|---|---|
| `api` | 8000 | FastAPI | JWT | `api/models.py` | None (read-only for credentials) |
| `kis-api` | 5001 | Flask | API Key | `backend/database/models.py` | Indirect via Redis |
| `kis-ws` | 5002 | (WebSocket) | Secret Key | None | Publish only |

The mobile app targets port 8000 (FastAPI). Strategy execution and order state happen in `kis-worker`. There is no direct API path from the mobile app to order placement — commands flow as: `POST /api/strategies → kis-api → Redis PubSub → kis-worker`. This is correct, but the kis-api bridge layer is thin and the exact API surface of `backend/api/server.py` was not audited in detail.

### 1.3 Broker Abstraction Quality

`BrokerAdapter` ABC in `backend/brokers/base.py` is clean. `KISBroker` wraps `kis_adapter/` correctly.

Defects found:

**D-1**: `KISBroker.place_order()` returns `Order(status=SUBMITTED)` on success without going through the `OrderStateMachine`. The state machine's `PENDING → SUBMITTED` transition is bypassed. Orders placed via `StrategyBase.buy()` → `broker.place_order()` are never registered in the state machine at all unless the strategy explicitly calls `machine.register()`.

**D-2**: `KISBroker.place_order()` catches all exceptions and returns `Order(status=REJECTED)`. A network timeout (order may have landed at broker) is indistinguishable from a true rejection. The caller has no way to distinguish between "broker rejected" and "we don't know."

**D-3**: `KISClient.post()` retries 3 times on any exception, including timeout. For order submission this creates duplicate orders. `get()` retries are safe; `post()` retries for order endpoints are not.

**D-4**: KR/US symbol detection heuristic `len(symbol) == 6 and symbol.isdigit()` appears in 4 separate locations: `KISBroker.place_order()`, `KISBroker.get_order_status()`, `KISBroker.get_price()`, and `runner.py:on_filled()`. This heuristic would misclassify a 6-digit US OTC symbol (edge case but possible).

**D-5**: `cancel_order()` in `KISBroker` only handles KR cancellation (uses domestic TR_ID). US order cancellation uses `KISOrders.cancel_us()` (in `kis_adapter/orders.py`) which has a different method signature. The `BrokerAdapter.cancel_order()` contract cannot uniformly cancel both.

### 1.4 Order State Machine

`OrderStateMachine` (`backend/execution/order_machine.py`) has sound transition validation.

Defects:

**D-6**: `submit()` mutates `order.id` in-place when `broker_order_id` is provided:
```python
order.id = broker_order_id
self._orders[broker_order_id] = order
```
The original key (local pending ID) is now orphaned in `_orders`. The same Order object lives under both the old and new key until the old one is garbage-collected by Python (it's removed from `_orders` by `transition()` calling `_get(order_id)` only on the new key).

**D-7**: `on_state_change` callback is called OUTSIDE the lock in `transition()` and `process_fill()`. If the callback is slow (DB write), another thread can interleave another transition on the same order. The callback sees a consistent order state but the state may advance before the callback completes.

**D-8**: `process_fill()` computes `avg_fill_price` incorrectly when the first fill arrives:
```python
prev_val = order.avg_fill_price * (order.filled_qty - event.filled_qty)
```
Before the first fill, `avg_fill_price = 0.0` and `filled_qty = 0`. When `event.filled_qty = 5`, `filled_qty` is incremented to 5 BEFORE computing prev_val: `prev_val = 0.0 * (5 - 5) = 0`. This is correct by coincidence. But if a second fill arrives: `prev_val = last_avg * (total_qty - new_fill_qty)`. This is correct. So the logic works, but is not obviously correct.

### 1.5 Fill Pipeline

The fill lifecycle in `runner.py:_make_fill_callback()` has 6 sequential steps. Steps 1–2 are in-memory only and wrapped in `try/except` that swallows exceptions silently. Steps 3–6 write to external state.

**D-9**: If step 2 (`tracker.on_fill`) throws and is swallowed, the in-memory position tracker is inconsistent with the DB fill record written in step 4. The position is now tracked as zero shares while the DB knows it was filled. On restart, reconciliation and DB restore will recover the position, but within the current session the strategy has no position and `can_place_order()` will return True again.

**D-10**: Steps 4 and 5 (fill persist + position upsert) each open independent DB sessions. If step 4 succeeds and step 5 fails, the fill is recorded but the position row is not updated. Reconciliation will later correct the position row, but the fill record and position row are momentarily inconsistent.

### 1.6 Position Tracker Initialization

In `worker/runner.py:main()`:
```
1. StrategyWorker.__init__()     — creates poller, risk tracker, reconciler
2. StartupRecovery.run()         — 8-step reconcile, fixes DB positions
3. StrategyWorker.run()
   → _restore_active()
   → _build_strategy()
   → _restore_positions(tracker)  ← reads DB positions AFTER step 2
```
This ordering is correct: tracker is populated from a reconciled DB. However, `StrategyWorker.__init__` creates its own `PersistentLossTracker` with a broker balance call, while `StartupRecovery._step_risk()` also creates an ephemeral `PersistentLossTracker`. Two separate instances read the same DB row on startup — one is discarded immediately. This is wasteful but safe.

### 1.7 Risk System Divergence

Two independent risk systems coexist:

| | `strategy/risk.py (RiskManager)` | `backend/quant/risk/engine.py (PersistentLossTracker)` |
|---|---|---|
| Kill-switch storage | Redis key only | Redis + DB (DailyRiskState) |
| Recovery after restart | Only if Redis survived | DB always survives |
| Daily loss tracking | Redis key with 24h TTL | Redis + DB dual-write |
| MDD tracking | Redis + file | Redis + DB |
| Used by | Legacy bot (disabled) | New backend worker |

The legacy system's kill-switch is a Redis key `risk:trading_halted` with 24h TTL. If Redis is cleared, the kill-switch is lost. The new system's kill-switch is in Postgres and survives all restarts. These two systems have no knowledge of each other's state.

### 1.8 Database Schema Issues

**D-11**: `fills.order_id` is typed `Integer` with `index=True` but no `ForeignKey("orders.id")` constraint. Orphaned fill records are possible and the DB will not prevent them.

**D-12**: `trades.strategy_run_id` has no FK constraint to `strategy_runs.id`.

**D-13**: `positions` table has no `UniqueConstraint("symbol", "broker")`. Multiple rows for the same symbol+broker can be inserted. `_upsert_position_db()` uses `.first()` which silently picks one row if duplicates exist, and leaves the others permanently stale.

**D-14**: No Alembic (or any migration framework). `Base.metadata.create_all()` creates tables but will not add new columns to existing tables. Schema evolution requires manual ALTER TABLE.

**D-15**: `orders.idempotency_key` is `nullable=True`. The `UniqueConstraint` on this column means NULLs are allowed and multiple NULL rows are permitted (SQL NULL ≠ NULL for unique constraint purposes). The idempotency key is never populated in `_persist_order()` in runner.py.

### 1.9 Scheduler Architecture

Two schedulers exist:
- `bot/scheduler.py`: `BlockingScheduler` — directly calls `TradingEngine` methods
- `backend/worker/scheduler.py`: `BackgroundScheduler` — publishes Redis events

Both define identical cron schedules (09:05 KST Korean, 22:35 KST US, 00:01 risk reset, 23:50 summary). If both run simultaneously (e.g., if `kis-bot` is uncommented), both fire. The new system's scheduler correctly routes through Redis PubSub to decouple the clock from execution. The legacy scheduler hard-couples them.

### 1.10 Mobile / Frontend Coupling

`mobile/` and `frontend/` are structurally identical (same component tree, same views, same stores). This is copy-paste duplication, not inheritance. Any change to the trading UI must be made twice. The mobile app's `capacitor.config.json` still references the default QuantDinger app ID — it has not been reconfigured for KIS Trading.

---

## 2. Operational Risk Analysis

### CRITICAL

**R-01: POST retry creates duplicate orders**
`KISClient.post()` retries 3 times on any exception. A timeout after the broker processed the order results in a second identical order with no deduplication mechanism (no client_order_id field in KIS order body). Probability: occurs on network flaps or broker API slowness. Impact: doubled position, excess capital deployed.

**R-02: Pre-submission fence not enforced**
`StrategyBase.buy()` calls `broker.place_order()` without first writing a PENDING record to the DB. Per PHILOSOPHY.md §3.1, the intent ledger must precede submission. A crash between `place_order()` succeeding at the broker and any DB write creates an untracked position discoverable only via reconciliation — which only runs on restart and at market open.

**R-03: EmergencyFlattenManager defaults to dry_run=True and is never called in production**
When `PersistentLossTracker._fire_kill_switch_alert()` fires (MDD breach), it:
1. Disables SAFE_MODE (blocks new orders)
2. Sends Telegram alert
3. Sends WebSocket alert
4. Does NOT call `EmergencyFlattenManager.flatten_all()`

`EmergencyFlattenManager` is defined but no production code path initializes it with `dry_run=False`. MDD breach = trading halted + alert, but existing positions are NOT automatically liquidated. If the operator does not manually intervene, positions remain open indefinitely after a 15% drawdown. Whether this is intentional (per PHILOSOPHY.md §5 "Do NOT automatically liquidate from API error") needs explicit documentation.

**R-04: KIS account accessed from two processes**
`kis-api` (port 5001) and `kis-worker` run simultaneously by default. If `kis-api` exposes any order placement endpoint, two processes may place orders against the same account. The rate limiter in `KISClient` is per-process (`RateLimiter` uses a local lock). Two processes independently manage rate limits, and combined call rate could exceed KIS limits (15/s real, 5/s paper).

### HIGH

**R-05: SAFE_MODE is a process-level in-memory singleton**
`SAFE_MODE = SafeModeState()` in `backend/worker/recovery.py` is a module-level object. Any process that imports this module gets its own independent SAFE_MODE instance. Changes to SAFE_MODE in the `kis-worker` process do not propagate to `kis-api`. If `kis-api` makes direct broker calls, it is not gated by the worker's SAFE_MODE.

**R-06: PersistentLossTracker is not thread-safe**
`LossTracker.record_pnl()` reads and writes `daily_pnl`, `weekly_pnl`, `peak_equity`, and `kill_switch` without any lock. In the worker, `_make_fill_callback()` calls `self._loss_tracker.record_pnl()` from the poller's background thread. If two fills arrive close together for different symbols, concurrent `record_pnl()` calls race on the same object. The result is a torn read: one call may see a partially-updated state and under-report losses.

**R-07: Daily PnL in legacy bot is unrealized-only, market-only**
`bot/main.py:send_daily_summary()` computes:
```python
daily_pnl = sum(float(p.get("evlu_pfls_amt", 0)) for p in kr_positions)
```
This is only Korean unrealized P&L. US positions are excluded. Realized P&L from sells is excluded. The value passed to `RiskManager.record_daily_loss()` (in the legacy system) is systematically wrong. This would cause the daily loss limit to trigger late or not at all.

**R-08: US order status query uses fallback `output[0]`**
`KISBroker._get_us_order_status()` fetches a list of recent US orders and attempts to find the specific order by `odno`. If the order is not found (e.g., pagination issue), it falls back to `row = output[0]`, returning status from a completely different order. This can mark a pending order as filled based on an unrelated order's status.

**R-09: `asyncio.run()` in synchronous Telegram notifier**
`bot/notifier.py:send_alert()` calls `asyncio.run(bot.send_message(...))`. This creates a new event loop per call. Inside any running async context (FastAPI, aiohttp), this raises `RuntimeError: This event loop is already running`. Telegram alerts are thus silently dropped when called from the async API layer.

### MEDIUM

**R-10: OrderStateMachine is in-memory only for active session**
On restart, the state machine is empty. The `_restore_active()` path restores positions into the tracker (from DB) but does not re-register orders into the state machine. Any order that was in flight at crash time is handled exclusively by `_step_pending_orders` in the recovery sequence (registered into the poller). The poller's `on_filled` callback for recovered orders writes to DB but does not call `machine.process_fill()` — because there is no machine entry for that order. So recovered fills do not go through state machine validation.

**R-11: StaleDataWatchdog is defined but not wired**
`backend/worker/emergency.py:StaleDataWatchdog` is a complete implementation for detecting stale OHLCV data. However, it is not instantiated or checked in `IndicatorStrategy` or anywhere in the execution path. Stale market data can flow into signal computation undetected.

**R-12: `_pending_symbols` lock TTL resets on restart**
`PositionTracker._pending_symbols` uses `time.monotonic()` as the lock timestamp. On process restart, `monotonic()` resets to near-zero. All pending locks from the previous session are automatically expired. This is safe (locks clear, no stuck orders), but means a symbol could receive a second buy order in the new session before the pending order from the previous session is confirmed filled or canceled.

**R-13: `hts_id` returned unmasked in credential API**
`credentials.py:_credential_to_dict()` returns:
```python
"hts_id": decrypt(cred.hts_id_enc) if cred.hts_id_enc else None,
```
`app_key` and `account_no` are masked with `"****"`, but `hts_id` is returned in plain text. HTS ID is a KIS authentication credential that should be masked.

**R-14: CORS is `allow_origins=["*"]`**
The FastAPI app allows all origins, all methods, all headers, with `allow_credentials=True`. This combination enables CSRF-style attacks from any domain.

---

## 3. Hidden Coupling Report

### C-01: `backend/quant/risk/engine.py` → `backend/worker/recovery.SAFE_MODE`
The quant engine's `LossTracker._fire_kill_switch_alert()` imports and mutates `SAFE_MODE` from the worker recovery module:
```python
from backend.worker.recovery import SAFE_MODE
SAFE_MODE.disable(f"킬스위치: {reason}")
```
This creates a downward dependency from the quant layer into the worker infrastructure layer. If the quant engine is used in a backtest or test context outside the worker, this import either fails or mutates a process-level singleton that doesn't represent a real trading gate.

### C-02: `backend/quant/risk/engine.py` → `bot/notifier.py`
The quant engine also imports from the legacy bot notifier:
```python
from bot.notifier import alert_emergency
```
This creates a dependency from `backend/` into the legacy `bot/` module. If `bot/` is removed (as per the roadmap), this breaks.

### C-03: `backend/worker/scheduler.py` → `bot/notifier.py`
Same coupling: the backend scheduler imports `alert_daily_summary` from the legacy bot notifier.

### C-04: `strategy/optimizer.py` → `MultiTimeframeSignals._base`
```python
df = signal_module._base.fetch_ohlcv(symbol)
```
`_base` is a private attribute. The optimizer accesses the internals of a signals class it does not own. Any refactor of `MultiTimeframeSignals` that renames `_base` breaks the optimizer silently.

### C-05: Duplicate `EXCD_MAP` definitions
`EXCD_MAP` (symbol → exchange code) is defined in:
- `strategy/signals.py` (legacy, used by bot)
- `backend/quant/data/universe.py` (new, imported by KISBroker)

They must be kept in sync manually. A symbol added to one but not the other will be routed incorrectly at the broker layer.

### C-06: Duplicate symbol universe definitions
`US_ETF`, `US_LARGE`, `KR_ETF`, `UNIVERSE` are defined in `strategy/signals.py` and referenced in the legacy bot. The backend presumably has its own universe definitions in `backend/quant/data/universe.py`. The canonical list lives in two places.

### C-07: KR/US detection heuristic in 4 places
The expression `len(symbol) == 6 and symbol.isdigit()` for detecting Korean market symbols appears in:
- `backend/brokers/kis.py:place_order()`
- `backend/brokers/kis.py:get_order_status()`
- `backend/brokers/kis.py:get_price()`
- `backend/worker/runner.py:on_filled()`

This is not a utility function but an inlined magic expression repeated across modules.

### C-08: Dual DB model layers sharing one Postgres instance
`api/models.py` (User, Credential, Strategy, etc.) and `backend/database/models.py` (Order, Fill, Position, etc.) target the same Postgres database without explicit schema separation. Both call `Base.metadata.create_all()` on startup. They share no SQLAlchemy metadata object, so one layer is unaware of the other's tables. Currently there are no table name collisions, but adding a table to one layer named the same as an existing table in the other would silently overwrite it.

### C-09: `StrategyWorker._poller` passed as private attribute to `StartupRecovery`
```python
recovery = StartupRecovery(..., poller=worker._poller)
```
`_poller` is a private attribute. StartupRecovery accesses it by convention, not contract. Any rename or refactor of the poller breaks recovery silently.

### C-10: `EmergencyFlattenManager` bypasses `StrategyBase.buy()/sell()` safety gates
`EmergencyFlattenManager.flatten_all()` calls `broker.place_order()` directly, bypassing SAFE_MODE and ENABLE_LIVE_TRADING checks in `StrategyBase`. This is intentional (emergency flatten must work even when SAFE_MODE is active), but it means emergency liquidation will always place real orders regardless of the shadow mode flag.

---

## 4. Failure-Mode Analysis

### FM-01: Network timeout on order POST → duplicate order
**Sequence**: KISClient.post() → broker receives order → network times out before response → KISClient retries (attempt 2) → second order submitted → broker now has two identical orders.
**No deduplication**: KIS order body has no client-controlled idempotency field.
**Detection**: Reconciliation at next market open discovers position 2x expected size.
**Recovery**: Manual cancel of one order.
**Probability**: Low-medium (network flaps during busy market hours).
**Capital impact**: Up to 5% position doubling.

### FM-02: Fill pipeline step 2 silent failure → position tracker desync
**Sequence**: Order fills → `tracker.on_fill()` throws (e.g., Position dataclass invariant) → exception caught silently → fill persisted to DB → tracker still shows zero qty.
**In-session**: `can_place_order("AAPL")` returns True → strategy places second buy.
**Post-restart**: Recovery restores from DB (correct) → position known.
**Probability**: Low.
**Capital impact**: Doubled position within session.

### FM-03: DB position table has no unique constraint → duplicate rows accumulate
**Sequence**: `_upsert_position_db()` fails (step 5) repeatedly, but preceding `_persist_fill()` (step 4) succeeds. Next call inserts a new row instead of updating because the first `.first()` returns the stale row from step 4. Over time, two rows exist for same symbol+broker.
**Symptom**: Reconciler reads stale row, shows incorrect qty.
**Detection**: Only visible via manual DB inspection.
**Probability**: Low.

### FM-04: Startup recovery `_step_balance` fails → SAFE_MODE stays disabled, but trading blocked
**Sequence**: KIS API is down when worker starts → `_step_balance` returns False → `recovery.run()` returns False → SAFE_MODE remains in initial state `can_trade=False`.
**Effect**: Worker starts, `_restore_active()` starts strategies, but all `buy()/sell()` calls are blocked by SAFE_MODE gate. System runs but cannot trade.
**Recovery**: When KIS API comes back up, the process must restart (recovery does not re-run automatically).
**Probability**: Medium (KIS API has scheduled maintenance windows).

### FM-05: Redis down at kill-switch moment → kill-switch state only in DB
**Sequence**: Daily loss limit exceeded → `PersistentLossTracker.record_pnl()` → `_persist()` → `_write_redis()` fails silently → `_write_db()` writes kill_switch=True to DB.
**On restart**: `_step_risk()` reads DB → kill_switch restored correctly. Safe.
**Within session**: `_fire_kill_switch_alert()` disables SAFE_MODE directly (not via Redis). Trading halted immediately. Safe.
**Legacy bot concern only**: `RiskManager.is_trading_halted()` checks Redis key only. If Redis is down and kill-switch fired, Redis key was never written → legacy bot would continue trading. This only affects the disabled legacy bot.

### FM-06: Multiple restarts accumulate position rows
**Sequence**: Process restarts with `_write_position_to_db()` in recovery (from `StartupRecovery._step_reconcile()`). This uses `db.merge()`. If the primary key is auto-increment and there is no unique constraint on `(symbol, broker)`, `merge()` may insert a new row rather than update.
**`db.merge()` behavior**: Without a matching PK, SQLAlchemy `merge()` does an insert (not an upsert by symbol). Since `positions.id` is auto-increment and the recovery code does not set `id`, every call to `_write_position_to_db()` inserts a new row.
**Result**: After N restarts, there are N position rows for the same symbol. All reconciliation queries using `.first()` return an arbitrary one.

### FM-07: `session:kr_open` Redis message fires after 5-minute dedup window expires
**Sequence**: Scheduler fires `kr_open` at 09:05. Network hiccup causes double-publish. Both arrive within 5 minutes: second is deduped. Correct.
**Edge case**: First message arrives at 09:04:59, second arrives at 09:10:01 (more than 5 minutes later). Second triggers `on_market_open()` again and a second reconciliation run. Two reconciliation threads run concurrently, both writing to DB. No lock on reconciliation runs.
**Probability**: Very low in practice.

### FM-08: `OrderFillPoller` fallback to `output[0]` in US order query
**Sequence**: Order for NVDA submitted. Poller queries broker. Response contains 20 orders (list). NVDA order is on page 2 (not fetched). Poller falls back to `output[0]` which is SPY order from earlier. SPY shows FILLED → `on_filled` called with SPY data for NVDA order entry.
**Effect**: Position tracker credits a NVDA fill at SPY's price. P&L calculation is wrong.
**Probability**: Low (US orders list is usually short for a retail account), but increases with order frequency.

### FM-09: MDD breach during market hours with no automatic liquidation
**Sequence**: Equity drops 15% → `_evaluate()` fires kill_switch → SAFE_MODE disabled → Telegram alert sent → strategies blocked. Existing positions remain open. Market continues to fall. No automatic liquidation occurs.
**Operators see Telegram alert but are unavailable** → positions continue losing.
**Current behavior**: This is a documented design choice (manual intervention required). However, there is no time limit after which the system escalates or takes conservative default action.

---

## 5. Deployment Blockers

### DB-01: No database migration tooling
Both `api/database.py` and `backend/database/models.py` use `create_all()`. Deploying schema changes to a running system requires manual `ALTER TABLE`. A schema mismatch between code and DB (e.g., missing column) causes silent failures or exceptions at runtime. **Blocker for any schema-touching feature release.**

### DB-02: `KIS_CREDENTIAL_KEY` defaults to empty string
`docker-compose.yml`:
```yaml
KIS_CREDENTIAL_KEY: ${KIS_CREDENTIAL_KEY:-}
```
`api/crypto.py` uses this key for credential encryption. An empty key means encryption is either noop or deterministic with a fixed key — both are insecure. KIS credentials stored in the DB are effectively plaintext if this env var is not set. **Blocker for production deployment with real credentials.**

### DB-03: `is_active=True` rows in `strategy_runs` from crashed sessions
If the worker crashes mid-session without calling `_mark_stopped()`, `strategy_runs.is_active` stays True. On the next restart, `_restore_active()` attempts to restart these strategies. If the strategy config references state that no longer exists (e.g., a position that was liquidated), the restart may produce unexpected orders. **Soft blocker — no hard enforcement that restored strategies are safe to restart.**

### DB-04: `kis-bot` (legacy) and `kis-worker` (new) can both be enabled
No system-level interlock prevents both from running against the same KIS account simultaneously. The `# DISABLED` comment in docker-compose is a human convention. A careless `docker compose up -d` after uncommenting would activate the legacy engine. **Blocker for safe ops — needs an enforced interlock.**

### DB-05: Mobile app `capacitor.config.json` uses QuantDinger branding
`mobile/capacitor.config.json` still has the original QuantDinger `appId` and `appName` (as per CLAUDE.md TODO). The mobile app cannot be published to the Play Store or App Store under this identity. **Blocker for mobile deployment.**

### DB-06: Frontend targets `http://api:8000` (Docker internal DNS)
`frontend/docker-compose.yml`:
```yaml
VITE_API_TARGET: http://api:8000
```
This is the internal Docker service name, not accessible from outside the container network. The mobile Capacitor app on a real device cannot reach `http://api:8000`. **Blocker for mobile connectivity.**

### DB-07: `kill_switch` manual reset has no API endpoint
When `DailyRiskState.kill_switch = True` (from MDD or weekly loss), the system requires "manual operator reset." No documented API endpoint or admin command exists for this reset. The only mechanism is direct DB manipulation: `UPDATE daily_risk_states SET kill_switch=false`. **Blocker for operator tooling.**

---

## 6. Implementation Constraints

### IC-01: No client_order_id on KIS API
KIS order submission endpoints accept no client-controlled idempotency field. Pre-submission deduplication must rely on: (1) checking pending symbols in PositionTracker before submission, and (2) reconciliation after the fact. The broker cannot deduplicate for us. This is a hard API constraint, not a design choice.

### IC-02: KIS order list queries are paginated and return lists, not single records
`_get_us_order_status()` must query a list and search by `odno`. The KIS API does not support single-order lookup by ID for US stocks. The list has a maximum page size (currently not handled). Orders older than the page window cannot be queried via this endpoint. Long-lived pending orders (> 1 day) may become invisible to the poller.

### IC-03: `asyncio.run()` cannot be called from within a running event loop
Telegram alerting via `asyncio.run()` is incompatible with FastAPI and any other async framework. All alert calls from async contexts will fail silently. The entire notifier must be rewritten async (preferred) or moved to a background thread with its own event loop.

### IC-04: yfinance has no SLA and may return stale data without signaling staleness
Signal computation depends entirely on yfinance. yfinance does not guarantee data freshness, rate limits aggressively under load, and silently returns cached or partial data. No circuit breaker exists for yfinance failures other than the `except: logger.warning` pattern throughout. The `StaleDataWatchdog` exists but is not wired.

### IC-05: `SAFE_MODE` singleton does not survive process restarts
`SAFE_MODE` is reconstructed from DB on every restart (via `_step_risk()` + `_step_enable_trading()`). The reconstruction logic reads `DailyRiskState.kill_switch` from DB. This is correct. But any other SAFE_MODE state (e.g., degraded due to market data staleness) is ephemeral and resets to "enabled" on every restart, which may be inappropriate.

### IC-06: Reconciler `_QTY_TOLERANCE = 1` is too coarse for high-priced stocks
A 1-share tolerance means a position in AVGO ($150+), MSFT ($400+), or SPY ($500+) can diverge by one share (= up to $500) before triggering a repair. For a 2M KRW (~$1,500) portfolio, a $500 silent error is 33% of capital. This tolerance was designed for small-cap / KR ETFs and should be symbol-specific or value-based.

### IC-07: `DailyRiskState` uses `trade_date` as primary key
`trade_date = Column(Date, primary_key=True)`. Only one row per calendar date. If the daily reset job fires twice (e.g., scheduler bug), the second upsert silently resets the daily PnL to 0. No history of multiple resets within a day.

### IC-08: `Position` table `qty` column is `Integer`, not `Decimal`
Fractional share support (if ever needed) is impossible without a schema change. US ETFs at KIS currently require whole shares, so this is acceptable today but constrains future flexibility.

### IC-09: Worker shutdown is not graceful
There is no signal handler for SIGTERM in `backend/worker/runner.py`. Docker sends SIGTERM before SIGKILL. Without a handler, `StrategyWorker` has 10 seconds (Docker default) before hard kill, with no opportunity to cancel open orders, flush state, or mark strategy_runs as inactive.

### IC-10: `LivePromotionGuard._check_paper_run()` checks `started_at <= cutoff` not `stopped_at` or continuous run
The check passes if any `StrategyRun` was created more than 28 days ago, regardless of whether it actually ran for 28 consecutive days. A strategy started 29 days ago and immediately stopped satisfies the check. This is a regulatory/process constraint, not a system bug, but it means the 4-week paper run gate is not enforced meaningfully.

---

## Summary: Priority Matrix

| ID | Severity | Category | Description |
|---|---|---|---|
| D-3 / R-01 | CRITICAL | Execution | POST retry creates duplicate orders |
| R-02 | CRITICAL | Execution | No pre-submission DB write (PHILOSOPHY §3.1 violated) |
| D-13 | CRITICAL | Schema | Position table has no unique constraint → duplicate rows |
| R-03 | HIGH | Risk | EmergencyFlattenManager is dry_run=True in production |
| R-05 | HIGH | State | SAFE_MODE is per-process, not shared across services |
| R-06 | HIGH | Concurrency | PersistentLossTracker not thread-safe |
| FM-08 / R-08 | HIGH | Broker | US order status query fallback to wrong order |
| D-1 | HIGH | Architecture | place_order() bypasses state machine PENDING→SUBMITTED |
| DB-02 | HIGH | Security | Credential encryption key defaults to empty |
| R-09 | HIGH | Alerting | asyncio.run() fails inside async contexts |
| IC-09 | HIGH | Ops | No SIGTERM handler → unclean shutdown |
| C-01 | MEDIUM | Coupling | quant engine → worker SAFE_MODE circular coupling |
| C-02/C-03 | MEDIUM | Coupling | backend imports legacy bot notifier |
| D-11/D-12 | MEDIUM | Schema | Missing FK constraints on fills, trades |
| D-15 | MEDIUM | Schema | idempotency_key never populated |
| FM-06 | MEDIUM | Recovery | db.merge() inserts new position row instead of updating |
| R-13 | MEDIUM | Security | hts_id returned unmasked in credential API |
| R-14 | MEDIUM | Security | CORS allow_origins=* with allow_credentials=True |
| IC-06 | MEDIUM | Risk | QTY_TOLERANCE=1 is too coarse for high-priced stocks |
| DB-01 | MEDIUM | Ops | No migration framework |
| IC-04 | LOW | Data | yfinance has no SLA; StaleDataWatchdog not wired |
| IC-10 | LOW | Process | Paper run gate check is trivially bypassable |
| DB-07 | LOW | Ops | No API endpoint to manually reset kill_switch |
| C-07 | LOW | Maintainability | KR/US detection heuristic duplicated in 4 places |
| C-04 | LOW | Coupling | optimizer accesses `_base` private attribute |

---

*This document is read-only analysis. No code has been changed. Implementation order should follow PHILOSOPHY.md §10 north-star rules: correctness first, recovery guaranteed, broker-authoritative.*
