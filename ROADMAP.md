# Deployment-Hardening Roadmap

**Version**: 1.0  
**Date**: 2026-05-29  
**Status**: PLANNING — do not begin implementation until P0 is fully reviewed  
**Depends on**: [`PHILOSOPHY.md`](PHILOSOPHY.md), [`AUDIT.md`](AUDIT.md), [`BROKER_SEMANTICS.md`](BROKER_SEMANTICS.md)

> This roadmap translates the audit findings in `AUDIT.md` into a phased, dependency-respecting action plan.
> Every task references the originating defect, risk, or constraint code from the audit.
> Capital context: ₩2,000,000 (~$1,500 USD). A single duplicate order or unrecovered crash is material.

---

## Section 1 — Prioritized Roadmap

### Phase P0 — Deployment Blockers

These tasks MUST be complete before the system touches real capital.
Any single incomplete P0 item is sufficient to block the paper→real transition.

---

#### P0-01 — Fix `KISClient.post()` retry logic on order endpoints

| Field | Value |
|---|---|
| **Purpose** | Current retry loop retries ALL exceptions including successful-but-double-submitted orders. A `500` response after an order was already accepted causes a duplicate order submission. |
| **Risk Level** | CRITICAL |
| **Implementation Complexity** | Low — change exception filter from `except Exception` to `except (requests.ConnectionError, requests.Timeout)` on order paths only |
| **Dependencies** | None |
| **Operational Impact** | Eliminates the most dangerous single bug in the system — undetected duplicate order creation |
| **Affected Files** | `kis_adapter/client.py` (retry loop), `backend/brokers/kis.py` (`place_order`) |
| **Deployment Priority** | 1 of 15 |
| **Audit Reference** | AUDIT.md R-01, D-3 |

---

#### P0-02 — Pre-submission fence: write PENDING before every `place_order()` call

| Field | Value |
|---|---|
| **Purpose** | Currently `buy()`/`sell()` call `broker.place_order()` directly without writing intent to DB first. If the process crashes between submission and response, the order is orphaned — unknown to our system. |
| **Risk Level** | CRITICAL |
| **Implementation Complexity** | Medium — add DB write step in `StrategyBase.buy()/sell()` and `worker/runner.py`; requires transaction wrapping |
| **Dependencies** | P0-07 (idempotency key must exist before PENDING row is written) |
| **Operational Impact** | Enables crash recovery; eliminates orphaned orders; satisfies PHILOSOPHY.md §3 "Pre-Submission Fence" rule |
| **Affected Files** | `backend/strategy/base.py`, `backend/worker/runner.py` |
| **Deployment Priority** | 2 of 15 |
| **Audit Reference** | AUDIT.md R-02; PHILOSOPHY.md §3 |

---

#### P0-03 — Fix `EmergencyFlattenManager`: change `dry_run` default to `False`, wire into production path

| Field | Value |
|---|---|
| **Purpose** | `EmergencyFlattenManager` is instantiated with `dry_run=True` and never overridden. The kill switch fires alerts but does NOT flatten positions. The emergency stop is a no-op in production. |
| **Risk Level** | CRITICAL |
| **Implementation Complexity** | Low — change default; add integration test that verifies paper flatten executes at least one sell order |
| **Dependencies** | P0-04 (per-broker SAFE_MODE must exist before flatten is wired) |
| **Operational Impact** | Activates the only automated capital-protection mechanism. Without this, MDD breach triggers alerts only — not position reduction. |
| **Affected Files** | `backend/worker/emergency.py`, `backend/worker/runner.py` |
| **Deployment Priority** | 3 of 15 |
| **Audit Reference** | AUDIT.md R-03 |

---

#### P0-04 — Per-broker `SAFE_MODE`: replace global singleton with per-broker instance map

| Field | Value |
|---|---|
| **Purpose** | `SAFE_MODE = SafeModeState()` is a single process-level object. A KIS failure activates SAFE_MODE for Kiwoom too, blocking domestic orders unnecessarily. Conversely, if both share state, a Kiwoom WebSocket drop can pause US trading. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — refactor `SafeModeState` to be keyed by broker ID; inject into each broker adapter; update all call sites |
| **Dependencies** | None |
| **Operational Impact** | Broker failures are now isolated. One broker outage no longer freezes the entire system. |
| **Affected Files** | `backend/worker/recovery.py`, `backend/quant/risk/engine.py`, `backend/brokers/kis.py`, `backend/brokers/kiwoom.py` |
| **Deployment Priority** | 4 of 15 |
| **Audit Reference** | AUDIT.md R-05 |

---

#### P0-05 — Thread-safe `PersistentLossTracker`: add `RLock` around all read-modify-write operations

| Field | Value |
|---|---|
| **Purpose** | `record_pnl()` reads `_daily_loss`, modifies, then writes back without a lock. Under concurrent scheduler ticks, loss can be under-counted, allowing trading past the daily loss limit. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Low — wrap `_daily_loss` and `_peak_equity` mutations in `threading.RLock` |
| **Dependencies** | None |
| **Operational Impact** | Ensures daily loss limits are enforced correctly under concurrent fills |
| **Affected Files** | `backend/quant/risk/engine.py` |
| **Deployment Priority** | 5 of 15 |
| **Audit Reference** | AUDIT.md R-06 |

---

#### P0-06 — Fix US order status lookup: remove `output[0]` fallback

| Field | Value |
|---|---|
| **Purpose** | When a specific order is not found in the KIS response, the poller falls back to `output[0]` — the first order in the list. This returns the wrong order's status, silently marking unrelated orders as FILLED. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Low — raise `OrderNotFound` exception if specific order absent; let caller handle reconciliation |
| **Dependencies** | None |
| **Operational Impact** | Eliminates false FILLED status; triggers proper reconciliation path when order status is genuinely unknown |
| **Affected Files** | `backend/execution/order_poller.py` |
| **Deployment Priority** | 6 of 15 |
| **Audit Reference** | AUDIT.md R-08, FM-08 |

---

#### P0-07 — Populate `idempotency_key` in `_persist_order()` using deterministic schema

| Field | Value |
|---|---|
| **Purpose** | `_persist_order()` never sets `idempotency_key` despite the column existing. Duplicate submissions cannot be detected at the DB level. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Low — generate key as `{broker}:{market}:{run_id}:{symbol}:{side}:{date}:{seq}` before DB write |
| **Dependencies** | None |
| **Operational Impact** | Enables idempotent order creation; DB-level duplicate prevention |
| **Affected Files** | `backend/worker/runner.py`, `backend/database/models.py` (ensure `idempotency_key` has `UNIQUE NOT NULL`) |
| **Deployment Priority** | 7 of 15 |
| **Audit Reference** | AUDIT.md D-15; BROKER_SEMANTICS.md §5 |

---

#### P0-08 — Add `UniqueConstraint("symbol", "broker")` to `positions` table

| Field | Value |
|---|---|
| **Purpose** | Without this constraint, reconnect/recovery cycles can insert duplicate position rows. `db.merge()` then operates on ambiguous rows, producing incorrect aggregated quantities. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Low — add constraint to SQLAlchemy model; create Alembic migration |
| **Dependencies** | P0-14 (Alembic must be initialized first) |
| **Operational Impact** | Prevents ghost positions; makes upsert semantics deterministic |
| **Affected Files** | `backend/database/models.py`, `alembic/versions/` (new migration) |
| **Deployment Priority** | 8 of 15 |
| **Audit Reference** | AUDIT.md D-13 |

---

#### P0-09 — Fix `db.merge()` → explicit upsert using `ON CONFLICT DO UPDATE`

| Field | Value |
|---|---|
| **Purpose** | `db.merge()` on `Position` does a SELECT then UPDATE/INSERT without holding the row lock. Concurrent writers race to upsert, producing double entries when the unique constraint does not yet exist. With P0-08, this becomes a correctness fix rather than a safety patch. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — replace `db.merge(pos)` with `INSERT ... ON CONFLICT (symbol, broker) DO UPDATE SET qty=...` via SQLAlchemy Core |
| **Dependencies** | P0-08 (unique constraint required for ON CONFLICT target) |
| **Operational Impact** | Atomic position updates; no race window for concurrent fills |
| **Affected Files** | `backend/execution/position_tracker.py` |
| **Deployment Priority** | 9 of 15 |
| **Audit Reference** | AUDIT.md FM-06 |

---

#### P0-10 — SIGTERM handler for graceful shutdown

| Field | Value |
|---|---|
| **Purpose** | Without a SIGTERM handler, Docker stop/restart sends SIGTERM then SIGKILL after 10 seconds. Active order submissions are interrupted mid-flight, leaving orders in UNKNOWN state with no recovery record. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — register `signal.signal(SIGTERM, ...)` handler; drain active polls, checkpoint equity to DB, close sessions |
| **Dependencies** | None |
| **Operational Impact** | Clean shutdown path; no orphaned in-flight orders on restart |
| **Affected Files** | `backend/worker/runner.py` |
| **Deployment Priority** | 10 of 15 |
| **Audit Reference** | AUDIT.md IC-09 |

---

#### P0-11 — Enforce `KIS_CREDENTIAL_KEY` non-empty at startup

| Field | Value |
|---|---|
| **Purpose** | `docker-compose.yml` sets `KIS_CREDENTIAL_KEY` to an empty string default. Credentials are stored encrypted with this key. An empty key means all credential data is either unencrypted or silently corrupt. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Low — add startup assertion: `if not os.getenv("KIS_CREDENTIAL_KEY"): raise EnvironmentError(...)` |
| **Dependencies** | None |
| **Operational Impact** | Prevents silent credential exposure; fails fast on misconfigured deployments |
| **Affected Files** | `backend/worker/runner.py`, `backend/api/routers/credentials.py`, `docker-compose.yml` |
| **Deployment Priority** | 11 of 15 |
| **Audit Reference** | AUDIT.md DB-02 |

---

#### P0-12 — Kill-switch reset API endpoint

| Field | Value |
|---|---|
| **Purpose** | Once `SAFE_MODE` activates, there is no programmatic way to clear it without restarting the process. Operators must SSH in and restart the worker, which creates a window of uncontrolled state. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low — add `POST /admin/safe-mode/reset` with admin token auth; call `safe_mode_map[broker].clear()` |
| **Dependencies** | P0-04 (per-broker SAFE_MODE map) |
| **Operational Impact** | Controlled recovery path without process restart; audit log of who reset which broker |
| **Affected Files** | `backend/api/routers/` (new `admin.py`), `backend/worker/recovery.py` |
| **Deployment Priority** | 12 of 15 |
| **Audit Reference** | AUDIT.md DB-07 |

---

#### P0-13 — Add FK constraints: `fills.order_id → orders.id`, `trades.order_id → orders.id`

| Field | Value |
|---|---|
| **Purpose** | Without FK constraints, fills can reference deleted or non-existent orders. Orphaned fills are counted in PnL calculations, producing phantom gains/losses. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low — add `ForeignKeyConstraint` in SQLAlchemy models; Alembic migration |
| **Dependencies** | P0-14 (Alembic framework) |
| **Operational Impact** | DB-level fill integrity; prevents phantom PnL from orphaned records |
| **Affected Files** | `backend/database/models.py`, `alembic/versions/` |
| **Deployment Priority** | 13 of 15 |
| **Audit Reference** | AUDIT.md D-11, D-12 |

---

#### P0-14 — Alembic migration framework

| Field | Value |
|---|---|
| **Purpose** | No migration tooling exists. Schema changes are applied manually or via `create_all()` on startup — which silently skips existing tables and never applies column changes. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low — `alembic init alembic`; configure `env.py` to point at `backend/database/models.py`; create baseline revision from current schema |
| **Dependencies** | None (but blocks P0-08, P0-13, P2-01) |
| **Operational Impact** | All schema changes are versioned, reversible, and auditable. Prerequisite for all future DB changes. |
| **Affected Files** | `alembic/`, `alembic.ini` (new), `backend/database/models.py` |
| **Deployment Priority** | 14 of 15 (must complete first despite number) |
| **Audit Reference** | AUDIT.md DB-01 |

---

#### P0-15 — Fix CORS: replace `allow_origins=["*"]` with env-var allowlist

| Field | Value |
|---|---|
| **Purpose** | `allow_origins=["*"]` with `allow_credentials=True` is a browser security violation (browsers reject it) and allows any origin to make credentialed requests if the client is permissive. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low — read `CORS_ALLOWED_ORIGINS` from env; parse comma-delimited list; pass to `CORSMiddleware` |
| **Dependencies** | None |
| **Operational Impact** | Closes credential theft vector via malicious cross-origin requests |
| **Affected Files** | `api/main.py` |
| **Deployment Priority** | 15 of 15 |
| **Audit Reference** | AUDIT.md R-14 |

---

### Phase P1 — Operational Hardening

Tasks that make the running system observable and resilient to common failure modes.

---

#### P1-01 — Fix Kiwoom base URL

| Field | Value |
|---|---|
| **Purpose** | `kiwoom_adapter/client.py` uses `openapi.koreainvestment.com:9443` — the KIS endpoint. Every Kiwoom API call silently routes to KIS and fails with auth errors. The entire Kiwoom adapter is non-functional. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Trivial — change `KIWOOM_BASE` constant to `https://openapi.kiwoom.com:10000` |
| **Dependencies** | None |
| **Operational Impact** | Makes domestic Korean trading possible for the first time |
| **Affected Files** | `kiwoom_adapter/client.py` |
| **Audit Reference** | BROKER_SEMANTICS.md §3 |

---

#### P1-02 — `OrderStateMachine` callback outside lock

| Field | Value |
|---|---|
| **Purpose** | `self._callbacks[event]()` is called after the state lock is released. A concurrent transition can fire a second callback for the same event before the first completes, causing double-processing of fill events. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Low — move callback invocation inside the `with self._lock:` block; ensure callbacks are re-entrant safe |
| **Dependencies** | None |
| **Operational Impact** | Eliminates double-fill processing race condition |
| **Affected Files** | `backend/execution/order_machine.py` |
| **Audit Reference** | AUDIT.md D-7 |

---

#### P1-03 — Fix order ID mutation: generate client order ID before submission

| Field | Value |
|---|---|
| **Purpose** | `submit()` mutates `order.id` with the broker-assigned ID after the response arrives. If the process crashes between submission and response capture, `order.id` is never set and the PENDING row cannot be matched to the broker order. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Low — generate a deterministic client order ID before submission; pass it to broker; store both client ID and broker ID |
| **Dependencies** | P0-07 (idempotency key schema applies here) |
| **Operational Impact** | Enables crash recovery by matching orphaned orders via client ID |
| **Affected Files** | `backend/execution/order_machine.py` |
| **Audit Reference** | AUDIT.md D-6 |

---

#### P1-04 — `StaleDataWatchdog`: TTL-based staleness rejection on all market data reads

| Field | Value |
|---|---|
| **Purpose** | No freshness check exists on price data. A stalled yfinance fetch or Redis cache hit from hours ago is used as-is for signal generation. Strategy acts on prices that may be arbitrarily stale. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — implement `StaleDataWatchdog` class; wrap all `get_price()` calls; reject data older than configurable TTL (default: 5 min for US, 10 sec for KR live) |
| **Dependencies** | None |
| **Operational Impact** | Prevents trading on stale prices; degrades to NO_DATA state rather than acting on phantom signals |
| **Affected Files** | New: `backend/execution/watchdog.py`; `kis_adapter/market_data.py`, `kiwoom_adapter/market_data.py` |
| **Audit Reference** | AUDIT.md IC-04, R-11 |

---

#### P1-05 — Redis reconnect resilience

| Field | Value |
|---|---|
| **Purpose** | Redis client uses default connection settings. A transient Redis restart drops the connection and the next operation raises `ConnectionError` with no retry, blocking token refresh and rate limiting. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — add `retry_on_timeout=True`, `retry_on_error=[ConnectionError, TimeoutError]`, health-check ping on each operation with backoff |
| **Dependencies** | None |
| **Operational Impact** | Redis restarts no longer crash the worker; graceful degradation to in-memory token cache for short outages |
| **Affected Files** | `kis_adapter/auth.py`, `backend/worker/runner.py` |
| **Audit Reference** | AUDIT.md R-12 |

---

#### P1-06 — Worker heartbeat: periodic liveness signal to Redis

| Field | Value |
|---|---|
| **Purpose** | No heartbeat exists. A hung worker (deadlocked scheduler, blocked DB query) appears alive to Docker health checks but is not processing orders. Silent hangs are indistinguishable from normal operation. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — write `worker:heartbeat:{pid}` key to Redis every 30s with 90s TTL; add `/health/worker` API endpoint that checks key existence |
| **Dependencies** | P1-05 (Redis must be resilient before heartbeat is meaningful) |
| **Operational Impact** | Enables automated watchdog restarts; exposes hung workers to monitoring |
| **Affected Files** | `backend/worker/runner.py`, `backend/api/routers/health.py` |
| **Audit Reference** | AUDIT.md IC-09 (shutdown symmetry with heartbeat) |

---

#### P1-07 — SQLAlchemy session safety: per-operation sessions, no long-lived session reuse

| Field | Value |
|---|---|
| **Purpose** | Long-lived SQLAlchemy sessions accumulate stale state, hold open transactions, and can deadlock under concurrent scheduler ticks. Each DB operation should acquire and release its own session. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — audit all DB call sites; wrap in `with SessionLocal() as db:` context manager; remove any module-level or instance-level `db` session fields |
| **Dependencies** | None |
| **Operational Impact** | Eliminates deadlock risk; each operation gets a fresh consistent view of DB state |
| **Affected Files** | `backend/worker/runner.py`, `backend/execution/position_tracker.py`, `backend/execution/order_machine.py`, `backend/quant/risk/engine.py` |
| **Audit Reference** | AUDIT.md D-10 |

---

#### P1-08 — Decommission legacy bot: remove `kis-bot` from docker-compose

| Field | Value |
|---|---|
| **Purpose** | `kis-bot` (legacy `bot/main.py` scheduler) is commented out in docker-compose but its code still exists and its scheduler definitions duplicate the new worker's schedules. Uncommenting it by accident creates two concurrent trading processes sharing one KIS account. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Low — delete `kis-bot` service block from docker-compose; archive `bot/` directory to `_archive/bot/` |
| **Dependencies** | P1-06 (worker heartbeat must be in place so we have visibility before removing the safety net) |
| **Operational Impact** | Eliminates dual-engine account conflict risk; removes 10+ coupling points |
| **Affected Files** | `docker-compose.yml`, `bot/` (archive or delete) |
| **Audit Reference** | AUDIT.md C-01, C-02, C-03, C-04 |

---

#### P1-09 — Single scheduler: unify duplicate APScheduler instances

| Field | Value |
|---|---|
| **Purpose** | `bot/scheduler.py` and `backend/worker/scheduler.py` define identical cron schedules. If both run, every market-open event fires twice — two independent strategy evaluations, potentially two sets of orders. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Low — after P1-08, only `backend/worker/scheduler.py` survives; verify no duplicate job IDs |
| **Dependencies** | P1-08 |
| **Operational Impact** | Each market event fires exactly once |
| **Affected Files** | `backend/worker/scheduler.py` |
| **Audit Reference** | AUDIT.md (topology §1) |

---

#### P1-10 — `on_filled` exception propagation: remove bare except in fill callback

| Field | Value |
|---|---|
| **Purpose** | `on_filled` callback in `runner.py` wraps the handler in `try/except Exception: pass`. Fill processing failures (DB write errors, position update failures) are silently swallowed. The order is marked FILLED but position/PnL state is not updated. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Low — remove bare except; let exceptions surface to worker loop error handler; log and enter SAFE_MODE on fill processing failure |
| **Dependencies** | None |
| **Operational Impact** | Fill failures are visible and trigger SAFE_MODE rather than corrupting state silently |
| **Affected Files** | `backend/worker/runner.py` |
| **Audit Reference** | AUDIT.md D-9 |

---

#### P1-11 — Mask `hts_id` in API credential response

| Field | Value |
|---|---|
| **Purpose** | `GET /credentials` returns `hts_id` in plaintext. HTS ID combined with app credentials enables full account access. This field should never be returned after initial save. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Trivial — exclude `hts_id` from response schema; return `"hts_id": "***"` sentinel |
| **Dependencies** | None |
| **Operational Impact** | Reduces credential exposure surface |
| **Affected Files** | `backend/api/routers/credentials.py` |
| **Audit Reference** | AUDIT.md R-13 |

---

#### P1-12 — `_QTY_TOLERANCE` fractional: replace hardcoded `1` share with dynamic calculation

| Field | Value |
|---|---|
| **Purpose** | Reconciliation tolerance of 1 share is too coarse for high-priced stocks (NVDA at $900 = $900 tolerance). For fractional share brokers, it is too strict. Tolerance should be a percentage of position size. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low — replace `_QTY_TOLERANCE = 1` with `max(1, round(position_qty * 0.005))` (0.5% of position) |
| **Dependencies** | None |
| **Operational Impact** | More accurate reconciliation; fewer false divergence alerts on large positions |
| **Affected Files** | `backend/execution/reconciler.py` |
| **Audit Reference** | AUDIT.md IC-06 |

---

### Phase P2 — Execution Validation

Makes the execution layer correct-by-construction rather than correct-by-convention.

---

#### P2-01 — Append-only `order_events` table: replace mutable status with event log

| Field | Value |
|---|---|
| **Purpose** | Current schema mutates `orders.status` in-place. A crashed update leaves status in an intermediate state with no audit trail. Append-only events mean current status is always derivable by replaying history — crash safety is built in. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | High — new `order_events` table; state machine emits events rather than mutations; query layer derives current state from `MAX(sequence)` |
| **Dependencies** | P0-14 (Alembic), P0-06 (status polling fix), P1-07 (session safety) |
| **Operational Impact** | Full execution audit trail; crash-safe state recovery; enables post-hoc debugging of any order |
| **Affected Files** | `backend/database/models.py`, `backend/execution/order_machine.py`, `alembic/versions/`, new: `backend/execution/event_store.py` |
| **Audit Reference** | PHILOSOPHY.md §8; BROKER_SEMANTICS.md §4 |

---

#### P2-02 — `StartupRecovery` 8-gate validation sequence

| Field | Value |
|---|---|
| **Purpose** | Current startup runs `restore_positions()` but skips most consistency checks. Gates must be: (1) DB connection, (2) load open orders, (3) query broker, (4) reconcile positions, (5) load risk state, (6) validate kill-switch, (7) check stale data watchdog, (8) arm scheduler. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | High — refactor `backend/worker/recovery.py`; each gate is a function returning pass/fail; any gate failure blocks startup |
| **Dependencies** | P0-09 (position upsert), P1-04 (watchdog), P1-05 (Redis reconnect), P2-04 (reconciler) |
| **Operational Impact** | System never starts in a known-inconsistent state; startup failures are explicit rather than silent |
| **Affected Files** | `backend/worker/recovery.py` |
| **Audit Reference** | BROKER_SEMANTICS.md §4 |

---

#### P2-03 — Fill idempotency: deduplicate on `(order_id, seq_no)` before inserting

| Field | Value |
|---|---|
| **Purpose** | KIS WebSocket and polling can both deliver the same fill event. Without deduplication, fills are double-counted in PnL and position calculations. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — add `UNIQUE(order_id, fill_seq_no)` to `fills` table; wrap insert in `INSERT ... ON CONFLICT DO NOTHING` |
| **Dependencies** | P0-14 (Alembic), P0-02 (pre-submission fence ensures `order_id` exists) |
| **Operational Impact** | PnL and positions are correct regardless of fill delivery duplication |
| **Affected Files** | `backend/database/models.py`, `backend/execution/order_machine.py`, `alembic/versions/` |
| **Audit Reference** | PHILOSOPHY.md §3 |

---

#### P2-04 — `PositionReconciler`: broker-wins reconciliation with divergence logging

| Field | Value |
|---|---|
| **Purpose** | Current reconciler compares quantities but does not enforce broker-wins rule. On divergence it logs a warning. It should apply the broker's position as authoritative and record the divergence in `reconciliation_events` for audit. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | High — implement `reconcile_positions()`: fetch broker state, diff against DB, apply broker value to DB, insert reconciliation event row |
| **Dependencies** | P0-09 (atomic upsert), P0-14 (Alembic) |
| **Operational Impact** | DB positions always converge to broker truth within one reconciliation cycle |
| **Affected Files** | `backend/execution/reconciler.py`, `backend/database/models.py` (new `reconciliation_events` table) |
| **Audit Reference** | PHILOSOPHY.md §4; BROKER_SEMANTICS.md §3 |

---

#### P2-05 — `BrokerCapabilities` dataclass + `BrokerSemanticMapper` ABC

| Field | Value |
|---|---|
| **Purpose** | Market routing is currently a heuristic (`len(symbol)==6 and symbol.isdigit()`). A domestic symbol passing through KIS or a US symbol through Kiwoom is silently routed to the wrong broker and fails. Explicit capability enforcement prevents misrouting at submission time. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — define `BrokerCapabilities(supports_domestic_kr, supports_overseas_us, ...)` dataclass; `BrokerSemanticMapper.route(symbol) -> BrokerAdapter`; raise `MarketMismatchError` on wrong routing |
| **Dependencies** | P1-01 (Kiwoom URL fix — mapper must route to a working broker) |
| **Operational Impact** | Wrong-market orders fail fast with a clear error rather than silently misfiring |
| **Affected Files** | New: `backend/brokers/capabilities.py`, `backend/brokers/mapper.py`; `backend/brokers/kis.py`, `backend/brokers/kiwoom.py` |
| **Audit Reference** | BROKER_SEMANTICS.md §6 |

---

#### P2-06 — KIS polling loop: structured `OrderFillPoller` with exponential backoff and circuit breaker

| Field | Value |
|---|---|
| **Purpose** | KIS provides no push notification for US overseas fills. The current polling implementation has no circuit breaker — if KIS API is degraded, the poller hammers the API every tick, consuming rate limit budget and potentially triggering IP bans. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — implement `OrderFillPoller` with configurable poll interval, exponential backoff on errors, circuit breaker (open after 5 consecutive failures), and metrics emission |
| **Dependencies** | P0-06 (correct status lookup), P1-05 (Redis for circuit breaker state) |
| **Operational Impact** | Graceful degradation on KIS API degradation; protects rate limit budget |
| **Affected Files** | `backend/execution/order_poller.py` |
| **Audit Reference** | AUDIT.md IC-01, IC-02 |

---

### Phase P3 — Mobile Optimization

---

#### P3-01 — Replace crypto exchanges with KIS + Kiwoom in `exchanges.js`

| Field | Value |
|---|---|
| **Purpose** | Mobile app shows 11 cryptocurrency exchanges. This platform trades Korean equities only. Wrong exchange list creates confusion and dead code paths. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Trivial |
| **Dependencies** | None |
| **Affected Files** | `mobile/src/constants/exchanges.js` |

---

#### P3-02 — `CredentialForm.vue`: KIS + Kiwoom fields, paper/real toggle

| Field | Value |
|---|---|
| **Purpose** | Current form likely mirrors QuantDinger's crypto credential schema. Need KIS-specific fields (app key, secret, account no, HTS ID, paper/real toggle) and Kiwoom fields. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Medium |
| **Dependencies** | P3-01 |
| **Affected Files** | `mobile/src/views/profile/CredentialForm.vue` |

---

#### P3-03 — Remove dead routes: `profile/referral`, `profile/credits`, `market/*`

| Field | Value |
|---|---|
| **Purpose** | QuantDinger-inherited routes serve features that do not exist in this platform. Dead routes produce 404s and confuse operators. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Trivial |
| **Dependencies** | None |
| **Affected Files** | `mobile/src/router/` |

---

#### P3-04 — Pinia store split: auth, broker, strategy, market, websocket

| Field | Value |
|---|---|
| **Purpose** | All state likely in a single monolithic store inherited from QuantDinger. Split enables independent state updates and reduces re-render scope. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Medium |
| **Dependencies** | P3-02 |
| **Affected Files** | `mobile/src/stores/` |

---

#### P3-05 — Fix `DEFAULT_SERVER_URL`: set to empty string, configure from build-time env

| Field | Value |
|---|---|
| **Purpose** | Hardcoded server URL in mobile app means APK/IPA must be rebuilt to change server address. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low |
| **Dependencies** | None |
| **Affected Files** | `mobile/src/config/index.js` |
| **Audit Reference** | AUDIT.md DB-06 |

---

### Phase P4 — Quant Intelligence Expansion

---

#### P4-01 — Deduplicate `EXCD_MAP`: single canonical source in `universe.py`

| Field | Value |
|---|---|
| **Purpose** | Exchange code mapping defined in both `backend/quant/data/universe.py` and `backend/brokers/kis.py`. Divergence causes wrong exchange codes on orders. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low — keep `universe.py` as canonical; import from there in `kis.py` |
| **Dependencies** | None |
| **Affected Files** | `backend/quant/data/universe.py`, `backend/brokers/kis.py` |
| **Audit Reference** | AUDIT.md C-05 |

---

#### P4-02 — `SimulatedBroker`: same `BrokerAdapter` interface for backtesting and live

| Field | Value |
|---|---|
| **Purpose** | Strategy code that works in backtest must work in live without modification. `SimulatedBroker` provides a paper-trading stub that satisfies `BrokerAdapter` with realistic fills (0.015% KIS commission, slippage model). |
| **Risk Level** | LOW |
| **Implementation Complexity** | Medium |
| **Dependencies** | P2-05 (BrokerCapabilities defines the interface) |
| **Affected Files** | New: `backend/strategy/runtime/simulator.py` |

---

#### P4-03 — `IndicatorStrategy` backtest endpoint

| Field | Value |
|---|---|
| **Purpose** | Mobile strategy builder needs server-side backtest execution. Wraps `backtesting.py` with JSON condition input and returns `{sharpe, mdd, win_rate, cagr, equity_curve, trades}`. |
| **Risk Level** | LOW |
| **Implementation Complexity** | High |
| **Dependencies** | P4-02 (SimulatedBroker) |
| **Affected Files** | New: `backend/strategy/indicator/backtest.py`, `backend/api/routers/backtest.py` |

---

#### P4-04 — Wire `TradingEngine` real FX rate with graceful degradation

| Field | Value |
|---|---|
| **Purpose** | If `get_usdkrw()` fails, the engine may silently use a stale or zero rate, distorting all KRW-denominated PnL calculations. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low — add explicit staleness check; on failure, halt new US orders until rate refreshes |
| **Dependencies** | P1-04 (StaleDataWatchdog) |
| **Affected Files** | `backend/worker/runner.py` (FX rate fetch path) |
| **Audit Reference** | AUDIT.md R-11 |

---

### Phase P5 — Strategy Consolidation

---

#### P5-01 — `StrategyBase` event methods: `on_start`, `on_bar`, `on_fill`, `on_market_open/close`, `on_stop`

| Field | Value |
|---|---|
| **Purpose** | Current `StrategyBase` lacks lifecycle hooks. Strategy code directly calls broker methods rather than declaring intent via events. Event methods enable simulation, replay, and safe interception. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Medium |
| **Dependencies** | P2-05 (broker routing must be correct before strategies run) |
| **Affected Files** | `backend/strategy/base.py` |

---

#### P5-02 — `ScriptStrategy` sandbox: RestrictedPython + AST whitelist + timeout

| Field | Value |
|---|---|
| **Purpose** | User-submitted Python strategy scripts could contain `import os; os.remove("/")` or network calls. Sandbox must block dangerous imports and enforce execution timeout. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | High — RestrictedPython + AST node visitor whitelist; `RESTRICTED_BUILTINS`; 30-second execution timeout via `concurrent.futures` |
| **Dependencies** | P5-01 (StrategyBase) |
| **Affected Files** | New: `backend/strategy/script/sandbox.py` |

---

#### P5-03 — Dual risk system unification: route all state through `backend/quant/risk/engine.py` + DB

| Field | Value |
|---|---|
| **Purpose** | `strategy/risk.py` writes `peak_equity` to a local file. `backend/quant/risk/engine.py` writes to Redis + DB. Two systems can disagree on peak equity, causing incorrect MDD calculations. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — migrate file-based peak equity to DB; remove `strategy/risk.py` file reader after one-sprint shadow period |
| **Dependencies** | P0-05 (thread-safe loss tracker must be correct before unification) |
| **Affected Files** | `strategy/risk.py`, `backend/quant/risk/engine.py` |
| **Audit Reference** | AUDIT.md C-07 |

---

#### P5-04 — API/Worker process separation via Redis PubSub

| Field | Value |
|---|---|
| **Purpose** | API and worker currently share process or use direct function calls. Strategy start/stop commands from the API should publish to Redis PubSub; worker subscribes and acts. This enables independent restarts and horizontal scaling. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | High |
| **Dependencies** | P1-05 (Redis resilience), P5-01 (StrategyBase) |
| **Affected Files** | `backend/api/server.py`, `backend/worker/runner.py` |

---

### Phase P6 — Maintainability / Documentation

---

#### P6-01 — Unit tests: `OrderStateMachine` all valid/invalid transitions + duplicate fill rejection

| Field | Value |
|---|---|
| **Purpose** | Core execution state machine has no tests. Any regression in transition logic is invisible until a live order misbehaves. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Medium |
| **Dependencies** | P2-01 (append-only events) |
| **Affected Files** | New: `tests/execution/test_order_machine.py` |

---

#### P6-02 — Integration test: paper trade dry-run must pass before any deploy

| Field | Value |
|---|---|
| **Purpose** | `scripts/test_paper_trade.py` should be a mandatory CI gate. Currently it is advisory only. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Low — add to GitHub Actions workflow as required check |
| **Dependencies** | P0-01 through P0-03 (safe order path required) |
| **Affected Files** | `scripts/test_paper_trade.py`, `.github/workflows/` |

---

#### P6-03 — Docker Compose health checks for all services

| Field | Value |
|---|---|
| **Purpose** | No `healthcheck:` directives in docker-compose. Services report "Up" even when internally broken. Container orchestration cannot restart unhealthy services automatically. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Low |
| **Dependencies** | P1-06 (worker heartbeat endpoint) |
| **Affected Files** | `docker-compose.yml` |

---

#### P6-04 — Gate `quantdinger` dependency behind env flag

| Field | Value |
|---|---|
| **Purpose** | `docker-compose.yml` requires `./quantdinger/` to exist (external git clone). Deployments fail if this directory is absent. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low — add `profiles: ["quantdinger"]` to the quantdinger service; or use `ENABLE_QUANTDINGER=true` env gate |
| **Dependencies** | None |
| **Affected Files** | `docker-compose.yml` |
| **Audit Reference** | AUDIT.md DB-03 |

---

#### P6-05 — Update `CLAUDE.md`: mark completed stages, advance next-work pointers

| Field | Value |
|---|---|
| **Purpose** | `CLAUDE.md` is the handoff document for new sessions. Stale stage status causes duplicated work or skipped dependencies. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Trivial — update after each sprint completion |
| **Dependencies** | None |
| **Affected Files** | `CLAUDE.md` |

---

## Section 2 — Sprint Structure

All sprints are 2 weeks. Exit criteria are binary: either all listed tasks pass their acceptance test, or the sprint is extended. No partial credit.

### Sprint 0 — Foundation (Weeks 1–2)

**Goal**: Eliminate all CRITICAL bugs. System must not be able to create duplicate orders.

| Task | Acceptance Test |
|------|----------------|
| P0-14 Alembic init | `alembic upgrade head` runs clean on fresh DB |
| P0-01 Retry fix | POST to `/trading/order` with simulated 500 response does NOT create second order |
| P0-07 Idempotency key | Every `orders` row has non-null `idempotency_key` after insert |
| P0-11 Credential key check | Worker refuses to start if `KIS_CREDENTIAL_KEY` is empty |
| P0-05 LossTracker lock | Concurrent `record_pnl()` calls do not under-count loss (verified by stress test) |
| P0-06 US status fix | Poller raises `OrderNotFound` instead of returning wrong order status |
| P0-15 CORS fix | Browser rejects credentialed cross-origin request from non-allowlisted origin |

**Sprint 0 Exit Gate**: All acceptance tests pass. `scripts/test_connection.py` passes.

---

### Sprint 1 — Kill Switch + Position Safety (Weeks 3–4)

**Goal**: Emergency flatten is functional. Positions table is correct.

| Task | Acceptance Test |
|------|----------------|
| P0-03 Emergency flatten | Paper mode: trigger kill switch → at least one sell order placed |
| P0-04 Per-broker SAFE_MODE | KIS SAFE_MODE does not block Kiwoom orders and vice versa |
| P0-08 Positions UniqueConstraint | INSERT duplicate `(symbol, broker)` raises `IntegrityError` |
| P0-09 Atomic upsert | Concurrent position updates produce correct final quantity |
| P0-02 Pre-submission fence | DB contains PENDING row before any `place_order()` is called |
| P0-10 SIGTERM handler | `docker stop` → worker logs "graceful shutdown" within 10s |
| P0-12 Kill-switch reset API | `POST /admin/safe-mode/reset` clears SAFE_MODE and logs event |
| P0-13 FK constraints | `INSERT fill with non-existent order_id` raises FK violation |

**Sprint 1 Exit Gate**: Paper trading runs for 48 hours with no DB integrity errors.

---

### Sprint 2 — Observability + Legacy Removal (Weeks 5–6)

**Goal**: System is fully observable. Legacy bot is gone. Single scheduler.

| Task | Acceptance Test |
|------|----------------|
| P1-01 Kiwoom URL | Kiwoom client can reach `openapi.kiwoom.com:10000` without SSL error |
| P1-02 OSM callback lock | No double-fill events under concurrent fill delivery test |
| P1-03 Order ID before submit | Crash between submit and response → PENDING row has client ID set |
| P1-04 StaleDataWatchdog | Price older than TTL → `get_price()` raises `StaleDataError` |
| P1-05 Redis reconnect | Redis restart → worker recovers within 30s without process restart |
| P1-06 Worker heartbeat | `/health/worker` returns 503 when heartbeat key has expired |
| P1-07 SQLAlchemy sessions | No long-lived session objects in any worker code path |
| P1-08 Legacy bot removed | `docker-compose.yml` has no `kis-bot` service; `bot/` is archived |
| P1-09 Single scheduler | Market open fires exactly once per day (verified by log count) |
| P1-10 Fill exception surface | Fill DB failure enters SAFE_MODE and emits error alert |
| P1-11 Mask HTS ID | `GET /credentials` response contains `"hts_id": "***"` |
| P1-12 QTY tolerance | Tolerance is `max(1, round(qty * 0.005))` for all positions |

**Sprint 2 Exit Gate**: System passes 1-week paper run with no anomalies.

---

### Sprint 3 — Execution Correctness (Weeks 7–8)

**Goal**: Execution layer is correct-by-construction. Startup recovery is complete.

| Task | Acceptance Test |
|------|----------------|
| P2-01 Append-only events | `order_events` table grows; `orders.status` column removed |
| P2-02 Startup recovery 8 gates | Startup with inconsistent DB → worker refuses to proceed past failed gate |
| P2-03 Fill idempotency | Duplicate fill event delivered twice → single DB row inserted |
| P2-04 PositionReconciler | Inject position divergence → reconciler overwrites with broker value + logs event |
| P2-05 BrokerSemanticMapper | KR symbol routed to KIS → `MarketMismatchError` raised |
| P2-06 KIS polling circuit breaker | 5 consecutive poller failures → circuit opens; rate limit not exceeded |

**Sprint 3 Exit Gate**: All execution unit tests pass. 2-week paper run completed without reconciliation divergence.

---

### Sprint 4 — Mobile + Quant (Weeks 9–10)

P3 tasks + P4 tasks. Gate: mobile app connects to KIS/Kiwoom; paper backtest returns valid equity curve.

---

### Sprint 5 — Strategy + Operations (Weeks 11–12)

P5 + P6 tasks. Gate: sandbox rejects dangerous script; docker-compose healthchecks all green; all unit tests pass.

---

## Section 3 — Dependency Graph

Tasks that must complete before other tasks can begin:

```
P0-14 (Alembic init)
├── P0-08 (positions UniqueConstraint)
│   └── P0-09 (atomic upsert)
│       └── P2-04 (PositionReconciler)
├── P0-13 (FK constraints)
├── P2-01 (append-only events)
│   └── P6-01 (OrderStateMachine tests)
└── P2-03 (fill idempotency)

P0-01 (retry fix)
└── P0-02 (pre-submission fence)
    └── P0-07 (idempotency key)
        └── P1-03 (order ID before submit)
            └── P2-03 (fill idempotency)

P0-04 (per-broker SAFE_MODE)
├── P0-03 (emergency flatten)
└── P0-12 (kill-switch reset API)

P0-05 (LossTracker lock)
└── P5-03 (risk system unification)

P0-06 (US status fix)
└── P2-06 (polling circuit breaker)

P1-01 (Kiwoom URL)
└── P2-05 (BrokerSemanticMapper)
    └── P4-02 (SimulatedBroker)
        └── P4-03 (backtest endpoint)

P1-04 (StaleDataWatchdog)
├── P2-02 (startup recovery gate 7)
└── P4-04 (FX rate degradation)

P1-05 (Redis reconnect)
├── P1-06 (worker heartbeat)
│   └── P6-03 (docker healthchecks)
└── P2-06 (polling circuit breaker)

P1-08 (legacy bot removed)
└── P1-09 (single scheduler)
    └── P5-04 (API/Worker PubSub)

P2-02 (startup recovery)
    depends on: P0-09, P1-04, P1-05, P2-04

P5-01 (StrategyBase events)
├── P5-02 (ScriptStrategy sandbox)
└── P5-04 (API/Worker PubSub)

P3-01 (exchanges.js)
└── P3-02 (CredentialForm)
    └── P3-04 (Pinia stores)
```

---

## Section 4 — Deployment-Readiness Assessment

### Current State (pre-roadmap)

| Dimension | Status | Evidence |
|---|---|---|
| Duplicate order prevention | BROKEN | `KISClient.post()` retries on any exception |
| Pre-submission intent record | MISSING | `base.py` calls `place_order()` without DB write |
| Emergency flatten | NO-OP | `EmergencyFlattenManager(dry_run=True)` never overridden |
| Kill switch scope | GLOBAL | Single `SafeModeState()` affects all brokers |
| Loss tracking thread safety | BROKEN | No lock on `record_pnl()` |
| Kiwoom functionality | BROKEN | Wrong API URL — 0% of Kiwoom calls succeed |
| Position table integrity | AT RISK | No unique constraint; duplicates possible |
| Schema migrations | MISSING | Manual `create_all()` only |
| Execution audit trail | ABSENT | Mutable status column, no event log |
| **Overall verdict** | **NOT SAFE FOR REAL CAPITAL** | |

---

### Minimum Safe-to-Deploy State (after Sprint 0–1)

All P0 tasks complete. Verified by Sprint 1 exit gate (48-hour clean paper run).

| Dimension | Status After P0 |
|---|---|
| Duplicate order prevention | FIXED |
| Pre-submission intent record | ACTIVE |
| Emergency flatten | FUNCTIONAL |
| Kill switch scope | PER-BROKER |
| Loss tracking thread safety | FIXED |
| Position table integrity | ENFORCED |
| **Overall verdict** | **SAFE FOR PAPER TRADING ONLY** |

**Real capital gate**: `KIS_ENV=paper` → `KIS_ENV=real` requires:
1. Sprint 0–1 complete ✓
2. 4-week uninterrupted paper run with no anomalies ✓
3. Manual human sign-off ✓
4. Deploy only outside market hours ✓

**This transition is FORBIDDEN before all four conditions are met. It is NOT automated.**

---

### Production-Ready State (after Sprint 2–3)

All P0 + P1 + P2 complete. Verified by Sprint 3 exit gate (2-week clean paper run with reconciliation).

| Dimension | Status After P2 |
|---|---|
| Execution audit trail | APPEND-ONLY |
| Startup recovery | 8-GATE VALIDATED |
| Fill deduplication | ENFORCED |
| Position reconciliation | BROKER-WINS |
| Broker routing | SEMANTIC ENFORCEMENT |
| KIS polling | CIRCUIT BREAKER PROTECTED |
| **Overall verdict** | **SAFE FOR REAL CAPITAL (post 4-week gate)** |

---

## Section 5 — Rollout Sequencing

```
┌─────────────────────────────────────────────────────────┐
│ STEP 1: Deploy Sprint 0 to paper environment            │
│   Gate: test_connection.py PASS                         │
│          test_paper_trade.py PASS                       │
│          Zero DB integrity errors in 24h                │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 2: 4-week paper operation (Sprint 0–1 deployed)    │
│   Gate: Zero duplicate orders                           │
│          Zero missed fills                              │
│          Daily reconciliation CLEAN                     │
│          Kill-switch test fires and flattens correctly  │
│          SIGTERM test completes within 10s              │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 3: Deploy Sprint 2–3 (still paper)                 │
│   Gate: Alembic migrations apply cleanly to paper DB    │
│          Startup recovery passes all 8 gates            │
│          Append-only event log populating correctly     │
│          Reconciler fires on injected divergence        │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 4: Paper→Real transition (HUMAN APPROVAL REQUIRED) │
│   Action: Set KIS_ENV=real in .env                      │
│   Preconditions (ALL must be true):                     │
│     □ 4-week paper gate passed                          │
│     □ Sprint 0–3 complete                               │
│     □ Deploy outside market hours                       │
│     □ Manual human sign-off                             │
│   FORBIDDEN: automated trigger of this step             │
│   FORBIDDEN: setting real before 4-week paper passes    │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 5: Week 1 of real trading at 50% allocation cap    │
│   Gate: No risk limit breaches                          │
│          Reconciler CLEAN                               │
│          PnL within ±2σ of paper period                 │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│ STEP 6: Full production (100% allocation, Sprint 4–5)   │
└─────────────────────────────────────────────────────────┘
```

---

## Section 6 — Rollback-Critical Tasks

Tasks where a failed deployment can leave DB or broker state inconsistent and requires an explicit rollback procedure.

---

### R-CRIT-01: P0-14 — Alembic init / baseline revision

**Risk**: Incorrect baseline revision marks unmigrated schema as already migrated. Subsequent `alembic upgrade` skips required changes silently.

**Rollback Procedure**:
1. `alembic stamp base` — clears version table
2. Manually verify all tables match `models.py` column-by-column
3. `alembic stamp head` — re-set to current if schema is correct
4. If schema diverged: `pg_dump` before any migration; restore from dump; re-init

---

### R-CRIT-02: P0-08 — `positions` UniqueConstraint migration

**Risk**: Migration adds constraint to table with existing duplicate rows → migration fails midway, leaving constraint in partial state.

**Rollback Procedure**:
1. Before running: `SELECT symbol, broker, COUNT(*) FROM positions GROUP BY symbol, broker HAVING COUNT(*) > 1` — must return empty
2. If duplicates exist: deduplicate manually before migrating
3. On migration failure: `alembic downgrade -1`
4. Restore from pre-migration DB snapshot (take snapshot before running)

---

### R-CRIT-03: P2-01 — Append-only `order_events` table

**Risk**: Schema change deployed while orders are in-flight silently drops status updates for in-progress orders. Orders stuck in PENDING with no status path forward.

**Rollback Procedure**:
1. Deploy ONLY outside market hours
2. Verify zero open orders before deploy: `SELECT COUNT(*) FROM orders WHERE status NOT IN ('filled','canceled','rejected')`
3. Keep old `orders.status` column as read-shadow for one sprint (do not drop until P6)
4. On failure: `alembic downgrade -1`; revert `order_machine.py` commit

---

### R-CRIT-04: P2-02 — Startup recovery 8-gate sequence

**Risk**: Bug in new recovery sequence causes worker to refuse all startups, blocking the entire platform.

**Rollback Procedure**:
1. Keep `recovery_legacy.py` alongside new `recovery.py` for one sprint
2. Add env flag: `RECOVERY_MODE=legacy` to fall back to old sequence
3. On failure: set `RECOVERY_MODE=legacy` in `.env`; restart worker
4. Fix gate bug; re-deploy; test with `RECOVERY_MODE=new`; remove legacy after 2 sprints

---

### R-CRIT-05: P1-08 — Legacy bot decommission

**Risk**: Legacy bot wrote state to DB in a format the new worker cannot parse. Removing bot without migrating that state creates data gaps.

**Rollback Procedure**:
1. Before removing: `SELECT * FROM orders WHERE source='legacy_bot'` — document all legacy-source rows
2. Keep `kis-bot` service commented (not deleted) in docker-compose for Sprint 2–3
3. Only delete `bot/` after Sprint 3 paper run confirms no missing state
4. Emergency rollback: uncomment `kis-bot` in docker-compose; restart — legacy bot resumes from its last state

---

### R-CRIT-06: P5-03 — Risk system unification (peak equity migration)

**Risk**: Deleting file-based `peak_equity` reader before DB persistence is verified causes MDD calculation to use zero as peak, triggering false MDD alerts.

**Rollback Procedure**:
1. On first deploy: read from BOTH file and DB; assert values match within 1% before deleting file reader
2. Log discrepancy as `WARN` for one sprint; do not act on it
3. Only delete file reader after 1 sprint of matching values
4. Emergency rollback: restore file reader code from git; manually copy DB peak value to file

---

### R-CRIT-07: P0-03 — `EmergencyFlattenManager` `dry_run=False`

**Risk**: First deployment with `dry_run=False` could trigger a spurious flatten if kill-switch signal fires unexpectedly on startup.

**Rollback Procedure**:
1. Integration test REQUIRED before deploy: inject kill-switch signal in paper mode; verify only sell orders are placed; verify no orders placed for unrelated symbols
2. Deploy during market hours only after integration test passes in paper mode
3. Emergency rollback: redeploy with `dry_run=True` override via env flag `EMERGENCY_FLATTEN_DRY_RUN=true`

---

*This roadmap is authoritative until superseded. Any task not listed here must be justified against PHILOSOPHY.md north-star rules before being added.*
