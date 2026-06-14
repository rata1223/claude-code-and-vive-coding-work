# Failure Scenario Audit — Platform Resilience Analysis

> **Read-only audit — no code changes in this task.** This document traces the full
> live trading pipeline (`Scheduler → Worker → Strategy → Execution → Broker → Polling →
> Reconciliation → Database`) under 10 infrastructure/process-level failure scenarios and
> answers: what detects the failure, how it's currently handled, how far the damage spreads,
> whether it can propagate silently, whether recovery is automatic, and what test coverage
> exists today. No `.py` files were created or modified to produce this report.

## Relationship to prior audits

This audit is **horizontal** (one pass across the whole pipeline, organized by *failure
scenario*) where the five prior documents below are **vertical** (one pass per *subsystem*,
organized by design concern). This document does not re-derive their findings — it
cross-references the ones that are still relevant to each of the 10 scenarios, and marks
explicitly which ones have since been **RESOLVED** by code changes that landed after those
audits were written.

| Prior doc | ID prefix | Subsystem focus |
|---|---|---|
| `docs/RECONCILIATION_ENGINE.md` | `F1`–`F10` | Reconciliation engine design + bug fixes |
| `docs/IDEMPOTENT_EXECUTION.md` | `DO-01`–`DO-12` | Idempotent order execution / ghost orders |
| `docs/ORDER_POLLING_RELIABILITY.md`, `docs/ORDER_POLLING_ARCHITECTURE.md` | `EX-01`–`EX-19` | Order-fill polling reliability |
| `docs/CORPORATE_ACTION_AUDIT.md`, `docs/CORPORATE_ACTION_PROCESSOR.md` | `CA-01`–`CA-13` | Corporate actions / reconciliation overwrite semantics |
| `docs/STALE_DATA_AUDIT.md`, `docs/STALE_DATA_DETECTOR.md` | `SD-xx` | OHLCV/quote staleness detection |

New findings introduced by *this* audit use the prefix **`FS-NN`** (Failure Scenario), chosen
to avoid collision with all of the above prefixes.

### `bot/` is legacy and disabled — out of scope

`docker-compose.yml` lines 80-110 define a `kis-bot` service (the legacy `bot/main.py` +
`bot/scheduler.py` BlockingScheduler engine) but the **entire service block is commented
out**, with an explicit operator warning in the surrounding comments that running it
alongside `kis-worker` would place duplicate orders. The live production trading entrypoint
is exclusively:

- `kis-worker` → `python -m backend.worker.runner` (`backend/worker/runner.py` +
  `backend/worker/scheduler.py`, `BackgroundScheduler`), `restart: unless-stopped`
- `kis-api` → gunicorn `backend.api.server:app` (`backend/api/gunicorn_conf.py`),
  `restart: unless-stopped`
- `kis-ws` → `python -m backend.websocket.server`, `restart: unless-stopped`

Any finding scoped purely to `bot/` is **LOW/informational** in this audit — it cannot fire
in the deployed configuration. `bot/` is mentioned only where it shares code paths with the
live pipeline (e.g., `bot/notifier.py`'s `alert_emergency`/`alert_daily_summary`, which
`backend/worker/heartbeat.py` and `backend/worker/scheduler.py` import and call directly).

---

## §1 Purpose & Scope

### In scope

- A full trace of `backend/worker/scheduler.py` → `backend/worker/runner.py` →
  `backend/strategy/{base,indicator/strategy}.py` →
  `backend/execution/{order_machine,position_tracker,order_poller,reconciler,circuit_breaker}.py`
  → `backend/brokers/kis.py` → `kis_adapter/{client,auth}.py` → `backend/database/models.py`,
  under the 10 named failure scenarios: Redis down, Worker restart, Process kill, Network
  timeout, Broker API failure, Polling failure, Reconciliation failure, Stale data, Duplicate
  event, Server restart.
- Cross-referencing (not re-deriving) `DO-`/`EX-`/`F-`/`CA-`/`SD-` findings that are still
  present in the **current** code, and explicitly flagging ones that have since been
  **RESOLVED**.
- New `FS-NN` IDs for previously-undocumented infrastructure/cross-cutting gaps surfaced by
  walking the 10 scenarios end-to-end (cross-process interactions, retry-boundary gaps,
  in-memory state loss on restart, etc.).
- Side processes that participate in these scenarios: `kis-api` (REST API + per-gunicorn-worker
  `WorkerWatchdog`), `kis-ws` (WebSocket push), Redis (Pub/Sub + heartbeat + risk counters),
  Postgres (`backend/database/models.py`).

### Out of scope

- Re-numbering or re-litigating existing CRITICAL findings already tracked in the five prior
  docs — they are cited, not re-derived.
- Implementing fixes or failure-injection tests (that is **TASK 4-1B**, sketched only in §8).
- The legacy `bot/` engine, beyond the LOW/informational note above.
- KR-specific OHLCV adjustment issues already covered by `CA-06`/`STALE_DATA_AUDIT.md`.
- The top-level `api/` package (`api/database.py`, `api/routers/*`, `api/main.py`). This audit
  verified via `backend/api/server.py:15-17,55-58` that the live `kis-api` process imports
  `init_db_factory` from `backend/database/models.py` and reads `DB_URL` — the **same**
  factory/env-var as `kis-worker`. The separate top-level `api/` package (which reads
  `DATABASE_URL` and configures `pool_size=10, max_overflow=20` in `api/database.py:10-15`) is
  **not** part of the live trading pipeline traced here (it appears to belong to the QuantDinger
  frontend API referenced in `CLAUDE.md`). No DB-pool-inconsistency finding is raised against
  it in this audit.

---

## §2 Current Structure Analysis

### 2.1 Pipeline diagram (confirmed-live path)

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│ kis-worker container (python -m backend.worker.runner, restart: unless-stopped)       │
│                                                                                         │
│  backend/worker/scheduler.py (BackgroundScheduler, Asia/Seoul)                         │
│   ├─ kr_session      mon-fri 09:05 KST   → _trigger_kr_session()                      │
│   ├─ us_session      mon-fri 09:30 NY    → _trigger_us_session()                      │
│   ├─ risk_reset      daily 06:01 KST     → _reset_daily_risk()                        │
│   ├─ equity_snapshot daily 23:50 KST     → _save_equity_snapshot()                    │
│   └─ periodic_reconcile  */30min, mon-fri 09-15,22-23 KST → _periodic_reconcile()     │
│        │ (each session-open job calls _publish_session_signal(): writes DB Command   │
│        │  row FIRST, then best-effort Redis PUBLISH)                                 │
│        ▼                                                                              │
│  backend/worker/runner.py — main()                                                    │
│   ├─ StartupRecovery(...).run()  (backend/worker/recovery.py, 9-step sequence)        │
│   │    1 DB health (fatal)  2 Redis ping (non-fatal)  3 risk state (non-fatal)        │
│   │    4 broker balance (30s timeout, fatal)  5 broker positions (30s timeout, fatal) │
│   │    6 reconcile "startup" (non-fatal)  7 pending-order recovery (non-fatal,        │
│   │      shared-poller registration + F1/F7 fixes)  8 state validation (observability)│
│   │    9 enable trading (LivePromotionGuard + kill-switch restore)                    │
│   ├─ WorkerHeartbeat(redis).start()  — 30s setex worker:heartbeat, TTL 90s            │
│   └─ StrategyWorker / WorkerSession                                                   │
│        ├─ self._redis = redis.from_url(REDIS_URL)   (lazy — no eager connect)         │
│        ├─ self._poller = OrderFillPoller(get_kis_broker()); .start()  (shared)        │
│        └─ _run_with_pubsub()                                                          │
│             ├─ pubsub.subscribe(strategy:start, strategy:stop,                       │
│             │                   session:kr_open, session:us_open)                    │
│             ├─ on session:*_open → _handle_market_open(market)  (5-min dedup, "F5")  │
│             │     → _build_strategy() → IndicatorStrategy(...)                       │
│             │          → _scan_and_trade() (per-symbol try/except, staleness gate)    │
│             │               → _execute_buy/_execute_sell()                           │
│             │                    → OrderStateMachine.register/transition             │
│             │                    → PositionTracker.try_mark_pending/unmark_pending   │
│             │                    → backend/brokers/kis.py KISBroker.place_order()    │
│             │                         → kis_adapter/client.py KISClient.post()       │
│             │                              (auth.get_hashkey + auth.get_headers       │
│             │                               BEFORE the 3x retry loop; 3x retry,       │
│             │                               1s backoff, 10s timeout, inside loop)     │
│             │                    → self._poller.register(order, on_filled=...)       │
│             ├─ on redis.ConnectionError → _enter_db_polling_mode()                    │
│             │     (30s loop: self._redis.ping() to detect recovery; else poll        │
│             │      Command table WHERE status='pending', dispatch, mark              │
│             │      processed/error — rows never purged)                              │
│             └─ OrderFillPoller._loop()  (5s tick, daemon thread "order-poller")       │
│                  └─ _poll_one(entry): get_order_status() →                            │
│                       FILLED: pop entry THEN on_filled()  ("EX-02")                   │
│                       PARTIAL: advance last_reported_qty THEN on_filled(partial)      │
│                       TIMEOUT: pop entry THEN on_timeout()                            │
│                  on_filled → _make_fill_callback (6 steps):                           │
│                    machine.process_fill → tracker.on_fill → P&L/kill-switch check     │
│                    → _persist_order/_persist_fill (idempotency_key / dedup query)     │
│                    → _upsert_position_db → WS push                                   │
└─────────────────────────────────────────────────────────────────────────────────────┘
                          │                         │                          │
                          ▼                         ▼                          ▼
                  Postgres (Order, Fill,     Redis (pub/sub +          kis-api container
                  Position, Command,         worker:heartbeat +        (gunicorn, 2 workers,
                  DailyRiskState, AuditLog,  risk:* counters)          restart: unless-stopped)
                  EquitySnapshot — engine                               post_fork →
                  via init_db_factory(DB_URL),                          WorkerWatchdog(redis)
                  Base.metadata.create_all(),                           ├─ 60s check_interval
                  default pool_size, no Alembic)                       │   HeartbeatMonitor
                                                                         │   .is_alive(redis)
                                                                         └─ on dead: DB
                                                                            DailyRiskState.
                                                                            kill_switch=True
                                                                            + Telegram +
                                                                            WS alert
                                                                            (kis-ws)
```

### 2.2 Side processes

- **`kis-api`** — gunicorn `backend.api.server:app`, `GUNICORN_WORKERS` default `"2"`
  (`backend/api/gunicorn_conf.py:15`). Each gunicorn worker process runs its own
  `post_fork()` hook (`gunicorn_conf.py:24-34`) which constructs a Redis client and starts a
  **separate** `WorkerWatchdog` instance — i.e. **2 watchdog threads run concurrently**,
  each independently polling `HeartbeatMonitor.is_alive()` every 60s (see FS-06).
  `backend/api/server.py` exposes `/health`-style metrics including `worker_alive` /
  `worker_ttl_seconds` via `HeartbeatMonitor` (`server.py:372-374`), and uses the **same**
  `init_db_factory(DB_URL)` / `get_db()` pattern as the worker (`server.py:15-17, 55-62`).
- **`kis-ws`** — `python -m backend.websocket.server`, used as the cross-process channel for
  `publish_alert(...)` calls from `WorkerWatchdog._alert_dead_worker`/`_alert_recovery`
  (`backend/worker/heartbeat.py:165-169, 172-176`) and for live position/fill push from the
  worker's fill callback.
- **Redis** — three independent uses, each with its own failure-handling posture:
  1. Pub/Sub session signals + `strategy:start`/`strategy:stop` (worker subscribes,
     scheduler/API publish) — guarded by DB `Command`-table fallback both ways.
  2. `worker:heartbeat` TTL key (`backend/worker/heartbeat.py`) — write side fails silently
     (try/except logs only), read side (`HeartbeatMonitor.is_alive`) treats *any* exception
     (including "Redis itself unreachable") identically to "key expired" → returns `False`.
  3. Daily risk counters `risk:daily_loss_pct`, `risk:trading_halted`,
     `risk:daily_pnl:{date}` (`backend/worker/scheduler.py:81-89`, and
     `backend/quant/risk/engine.py`'s `PersistentLossTracker`, which receives an
     **injected** `redis_client` — no eager `redis.from_url()` call of its own).
- **Postgres** — single logical database (`quantdinger`), reached via `DB_URL`
  (`postgresql://quantdinger:${POSTGRES_PASSWORD}@postgres:5432/quantdinger` per
  `docker-compose.yml`), through `backend/database/models.py:init_db_factory()`. Both
  `kis-api` and `kis-worker` create their **own** SQLAlchemy engine via this same factory —
  `create_engine(db_url, pool_pre_ping=True, echo=False)` (`models.py:147`), i.e. SQLAlchemy
  default pool sizing (no explicit `pool_size`/`max_overflow`), and
  `Base.metadata.create_all(engine)` (`models.py:148`), which is a **no-op for tables that
  already exist** — it does not `ALTER TABLE` to add columns that were added to an ORM model
  after the table was first created (see FS-04, ROADMAP.md P0-14).

### 2.3 StartupRecovery — 9-step sequence (`backend/worker/recovery.py:83-108`)

| # | Step | Fatal on failure? | Notes |
|---|---|---|---|
| 1 | DB health (`SELECT 1`) | **Fatal** — process exits | |
| 2 | Redis ping (`_step_redis`, 126-135) | Non-fatal | logs warning only |
| 3 | Risk state load | Non-fatal | |
| 4 | Broker balance (`_step_balance`, 154-184) | **Fatal** — 30s `ThreadPoolExecutor` timeout | |
| 5 | Broker positions (`_step_positions`, 154-184) | **Fatal** — same 30s timeout | |
| 6 | Reconcile `"startup"` (`_step_reconcile`, 186-217) | Non-fatal | broker = ground truth |
| 7 | Pending-order recovery (`_step_pending_orders`, 219-322) | Non-fatal | shared poller (234-239), F1 fix (266, 324-348), F7 dedup (250-258) |
| 8 | State validation (`_step_validate_state`, 369-432) | Non-fatal, observability-only | 3 checks: nonpositive-qty positions, orphaned pending orders, stale pending orders (`_RECOVERY_STALE_ORDER_HOURS=24`) |
| 9 | Enable trading (`_step_enable_trading`, 434-467) | — | `LivePromotionGuard` (KIS_ENV=real) + kill-switch restore from `DailyRiskState` |

Step 7's **F1 fix is confirmed present and correct**: `_make_recovery_fill_cb`'s `on_filled`
closure (241-275) calls `self._apply_fill_to_position_db(sess, row.symbol, row.side,
fill_qty, fill_price)` at line 266, and that method is fully defined at 324-348. The original
`RECONCILIATION_ENGINE.md` F1 finding ("`_apply_fill_to_position_db` undefined → NameError")
is **RESOLVED** — see §10.

Step 7 also resolves the original `ORDER_POLLING_RELIABILITY.md` **EX-06** finding ("recovery
and runner both register the same order with their own pollers → two callbacks → two
fills"): `_step_pending_orders` now reuses a **shared poller**
(`self._shared_poller`, 234-239) injected from the live `WorkerSession`, and
`OrderFillPoller._entries` is a `dict[str, _PollEntry]` keyed by `order.id`
(`order_poller.py:63`) — `register()` **overwrites** any prior entry for the same broker
order id (`order_poller.py:91`) rather than adding a second one. `runner.py`'s
`_register_recovered_order` (661-701) re-registers the same orders with the full pipeline
callback (`_guarded_on_filled`, 679-699), which itself re-checks the DB for
`status == FILLED` before running the pipeline (685-698) — closing the narrow race window
between the startup DB-only callback and the live re-registration. **EX-06 is RESOLVED** —
see §10.

---

## §3 Failure Scenario Matrix

### 3.1 Redis down

| Detection | Handling (file:line) | Blast Radius | Silent Propagation? | Recovery Status | Test Coverage | Cross-Refs |
|---|---|---|---|---|---|---|
| `redis.ConnectionError` in pubsub loop; `Exception` in `_beat`/`is_alive`; non-fatal ping in recovery step 2 | `runner.py:233-237,243-282` (DB-polling fallback); `recovery.py:126-135` (non-fatal); `heartbeat.py:43-48,59-64` (silent-fail / treat-as-dead) | Worker: graceful degradation (DB polling). Cross-process: **false "dead worker" detection** in `kis-api`'s `WorkerWatchdog` | **Yes** — `_alert_recovery` does not clear `kill_switch` | Worker side: automatic. Watchdog side: **manual** (`DailyRiskState.kill_switch` reset) | None found (no `test_heartbeat.py`, no Redis-down test) | **FS-01 (NEW)** |

`self._redis = redis.from_url(_REDIS_URL)` (`runner.py:143`) does **not** connect eagerly —
constructing a Redis client never raises, so the worker process does not crash at boot if
Redis is unreachable (an earlier hypothesis for this scenario, since revised). The actual
in-pipeline gaps are:

1. **Worker-side degradation is good.** `_run_with_pubsub()` (207-242) catches
   `redis.ConnectionError`, applies exponential backoff (2s → 64s cap), and calls
   `_enter_db_polling_mode()` (243-282), which polls the `Command` table every 30s and
   periodically retries `self._redis.ping()` (250) to detect Redis recovery and resume
   Pub/Sub. `_publish_session_signal()` (`scheduler.py:135-159`) writes the `Command` row to
   Postgres **before** attempting the Redis `PUBLISH`, so session-open events are not lost
   even if Redis is down at signal time.
2. **Cross-process heartbeat/watchdog interaction is the real gap — FS-01.** See §4 chain 1
   and §6 for the full chain: a Redis outage silences `WorkerHeartbeat._beat()`
   (`heartbeat.py:43-48`, exception caught and logged only) **and** causes
   `HeartbeatMonitor.is_alive()` (`heartbeat.py:59-64`) to return `False` on the *next* check
   from `kis-api`'s `WorkerWatchdog` — not because the worker died, but because Redis itself
   is unreachable for the `.exists()` call. `_alert_dead_worker()`
   (`heartbeat.py:128-169`) then writes `DailyRiskState.kill_switch = True` with
   `kill_reason = "Worker 하트비트 없음 — 프로세스 재시작 필요"` — a message that points the
   operator at the wrong root cause. When Redis recovers and the heartbeat resumes,
   `_check()` (113-126) flips back to `alive` and calls `_alert_recovery()` (171-176), which
   only publishes an informational WS message — **it never clears `kill_switch`**. The next
   `_reset_daily_risk()` run (`scheduler.py:108-132`) sees yesterday's `kill_switch == True`
   and explicitly **blocks** `SAFE_MODE` re-arm ("어제 킬스위치 활성 — SAFE_MODE 재활성화
   차단. 수동 해제 필요."). Net effect: a transient Redis blip, fully recovered within
   minutes, can leave live trading halted until a human manually clears
   `DailyRiskState.kill_switch` in Postgres — and the alert they receive tells them to check
   the worker process, not Redis.
3. `_periodic_reconcile()` (`scheduler.py:194-211`) and `_reset_daily_risk()`
   (`scheduler.py:78-90`) both construct their own `redis.from_url(...)` client and wrap all
   Redis access in `try/except` — both are non-fatal and well-guarded.

### 3.2 Worker restart

| Detection | Handling (file:line) | Blast Radius | Silent Propagation? | Recovery Status | Test Coverage | Cross-Refs |
|---|---|---|---|---|---|---|
| `kis-worker` container restart (`restart: unless-stopped`, deploy, OOM) | `recovery.py:83-108` (9-step `StartupRecovery`) | In-memory state reset: `ConsecutiveFailureBreaker`, 5-min market-open dedup cache, `OrderFillPoller._entries`. DB/positions restored via recovery | Breaker-state loss is silent (no "fresh vs reset" distinction in logs) | **Automatic** for orders/positions/risk-state via `StartupRecovery`; **FS-02** for breaker state | `backend/worker/tests/test_recovery_safety.py` (`TestRestorePendingBrokerScope`, `TestRestorePendingPollerRegistration`, `TestPersistFillIdempotency`, `TestPersistOrderIntegrity`, `TestValidateState`) | F1 (**RESOLVED**), EX-06 (**RESOLVED**), **FS-02 (NEW)** |

`StartupRecovery` (§2.3) is the dominant recovery path and is well-tested for its core
concern (orders/positions/fills). Two gaps remain:

1. **`ConsecutiveFailureBreaker` is purely in-memory (`backend/execution/circuit_breaker.py:14-49`)**
   — `self._failures` and `self._tripped_at` are plain instance attributes with no Redis/DB
   persistence. If the breaker was **open** (cooling down after 3-5 consecutive broker-call
   failures, depending on caller — `IndicatorStrategy` uses threshold=3/cooldown=30min,
   `KISBroker` uses threshold=5/cooldown=10min) at the moment of a worker restart, the
   restart **silently resets it to closed** — `self._failures = 0`, `self._tripped_at =
   None` on the new instance. If the underlying broker/network issue that tripped the
   breaker is *still ongoing*, the freshly-restarted worker immediately resumes hitting the
   same failing broker calls instead of respecting the remaining cooldown. This is **FS-02**
   (new, MEDIUM — see §6).
2. The **heartbeat/watchdog TTL window (90s)** means any worker restart that takes longer
   than ~90s between the last heartbeat write and the new process's first heartbeat write
   can trigger the **same FS-01 false-positive `kill_switch=True`** chain described in §3.1
   — even for a clean, intentional restart/redeploy. `WorkerHeartbeat.start()`
   (`heartbeat.py:28-34`) does call `_beat()` immediately on start, but only *after*
   `StartupRecovery.run()` completes (per `main()`'s ordering) — and steps 4/5 each have a
   30s `ThreadPoolExecutor` timeout, so a slow broker response during recovery alone can push
   the gap past 90s.

### 3.3 Process kill (SIGKILL / OOM-kill, no graceful shutdown)

| Detection | Handling (file:line) | Blast Radius | Silent Propagation? | Recovery Status | Test Coverage | Cross-Refs |
|---|---|---|---|---|---|---|
| Heartbeat TTL expiry (90s) → `WorkerWatchdog` (correctly, this time) | No SIGTERM/graceful-shutdown hook found in `runner.py:main()` (746-800) | Any order mid-flight in the 6-step fill pipeline (`_make_fill_callback`, 429-511) at kill time loses its in-memory `PositionTracker`/`OrderStateMachine` update; DB `Fill` row may never be written | **Yes** — CA-03's reconciliation overwrite repairs the position `qty` but never recreates the missing `Fill` audit row | Position qty: automatic (via reconciliation, ground-truth = broker). Fill audit trail: **none** | None — no SIGKILL/process-kill test exists | EX-02 (extended — "process-kill mid-pipeline" variant), CA-03, CA-04 (**CONFIRMED PRESENT**), FS-01 (correct detection in this case) |

The 6-step fill pipeline order is: `machine.process_fill` (try/except, non-fatal) →
`tracker.on_fill` (try/except, non-fatal) → P&L/kill-switch check → `_persist_order`/
`_persist_fill` (DB write, idempotency-keyed) → `_upsert_position_db` → WS push
(`runner.py:429-609`). If the process is killed **after** `tracker.on_fill` mutates the
in-memory `PositionTracker` but **before** `_persist_fill`/`_upsert_position_db` commit, the
in-memory mutation is destroyed by the kill (so it does not itself cause drift), but the DB
`Position`/`Fill` rows remain at their pre-fill values. On restart:

- `reconcile("startup")` (`runner.py:201-204`, `recovery.py` step 6) compares DB positions
  against the broker (ground truth, which already reflects the fill) → detects a
  `qty_mismatch` → **CA-03's unconditional overwrite** (`reconciler.py:206-231`) corrects
  `Position.qty`/`avg_price` to match the broker.
- However, **no `Fill` row is ever created** for the fill that happened during the kill
  window — the qty is right, but the audit trail (`Fill` table, used for P&L attribution and
  `EquitySnapshot` reconciliation) is permanently missing that fill, with no alert
  distinguishing "expected CA-03 repair" from "repair masking a lost fill." This is the same
  *failure mode* as EX-02 (silent fill loss) but triggered by **process termination** rather
  than the poller's pop-before-callback ordering — both converge on the same blast radius
  (CA-03 masks the gap, CA-04 means there's no `corporate_action`/audit field to record
  *why*).
- `_step_validate_state` (`recovery.py:369-432`) is observability-only and would not surface
  this either — it checks for nonpositive-qty positions, orphaned pending orders, and stale
  pending orders, none of which match "qty correct but Fill row missing."

`WorkerWatchdog` correctly detects this scenario (heartbeat genuinely stops), but its
response (`DailyRiskState.kill_switch=True`, requiring manual reset — §3.1) is the *same*
heavy-handed response used for the Redis-false-positive case, conflating "the worker is
genuinely down and needs investigation" with "Redis blipped for 10 seconds."

### 3.4 Network timeout

| Detection | Handling (file:line) | Blast Radius | Silent Propagation? | Recovery Status | Test Coverage | Cross-Refs |
|---|---|---|---|---|---|---|
| `requests` `timeout=10` on every HTTP call (`client.py:44,68`, `auth.py:72,87,97`) | `client.py:41-54` (GET, 3x retry/1s), `client.py:56-96` (POST, 3x retry/1s) | GET: bounded ~33s worst case, no side effect. POST: retry of a non-idempotent call → possible duplicate order (**DO-01**). Token/hashkey refresh: **no retry at all** | FX fallback (**SD-04**) is silent (log warning only); token-refresh timeout (**FS-07**) is silent until the *next* strategy cycle | GET/POST body retries: automatic. FX fallback: automatic but possibly stale/wrong. Token/hashkey refresh: **not retried within the call** | None — no test for `client.py` retry/timeout or `_get_fx` fallback | DO-01 (**CONFIRMED PRESENT**), SD-04 (**CONFIRMED PRESENT**), **FS-07 (NEW)** |

Three distinct network-timeout surfaces:

1. **GET/POST body retries (existing, cross-ref DO-01).** `KISClient.get()`/`post()`
   (`client.py:37-96`) retry the HTTP request itself up to 3x with a 1s sleep and 10s
   per-attempt timeout. For GET this is safe (idempotent). For POST
   (`client.py:56-96`), a network timeout *after* the broker has already accepted and
   processed the order, but *before* the response reaches the client, causes a retry that
   submits a **second** order — the original `IDEMPOTENT_EXECUTION.md` DO-01 "ghost order"
   finding. This audit confirms the retry loop in current `client.py` is unchanged and DO-01
   remains present.
2. **`_get_fx()` fallback chain — SD-04, confirmed present, not re-derived here.**
   `backend/brokers/kis.py:300-317` fetches USD/KRW via `yfinance` with a 1-hour TTL cache;
   if the live fetch fails (including on network timeout) it falls back to the cached value,
   logging a warning if that cached value is itself >30 minutes stale (314-316) — this is
   exactly `STALE_DATA_AUDIT.md`'s **SD-04** ("FX rate cache silent-stale fallback >30min",
   MEDIUM), whose own text notes "킬스위치 계산 부정확 가능". This audit's network-timeout
   trace confirms `yfinance` connection timeouts are one of the triggers SD-04 already
   covers — no new ID needed.
3. **Token/hashkey refresh bypasses the retry loop entirely — FS-07 (NEW, MEDIUM-HIGH).**
   `KISClient.get()` computes `headers = self.auth.get_headers(tr_id)` (`client.py:38`) —
   and `KISClient.post()` computes `hashkey = self.auth.get_hashkey(body)` then `headers =
   self.auth.get_headers(tr_id)` (`client.py:57-58`) — **before** entering the `for attempt
   in range(MAX_RETRIES)` loop (`client.py:41`, `client.py:65`). `get_headers()` calls
   `get_token()` (`auth.py:101-110` → `32-63`), whose final fallback is `_issue_token()`
   (`auth.py:65-75`): a single `requests.post(url, json=body, timeout=10)` with **no
   try/except and no retry**. `get_hashkey()` (`auth.py:90-99`) is the same shape — a single
   `requests.post(..., timeout=10)`, no retry. If either of these times out (e.g. KIS's
   `/oauth2/tokenP` or `/uapi/hashkey` endpoint is slow during a network blip, while the main
   data/order endpoint would have been fine), the **entire** `get()`/`post()` call raises
   immediately — the 3x/1s retry that protects the data/order call itself never executes.
   This is a materially different failure mode than DO-01 (which is about the *order*
   endpoint being retried too aggressively): here, a transient timeout on the
   *authentication* endpoint **fails the call with zero retries**, even though the calling
   code (`_scan_and_trade`'s per-symbol try/except, `place_order`'s try/except→REJECTED) will
   treat it the same as any other broker error. Net effect is a "wasted" strategy cycle or a
   spurious REJECTED order on what was, from the trading endpoint's perspective, a
   non-event.

### 3.7 Reconciliation failure

| Detection | Handling (file:line) | Blast Radius | Silent Propagation? | Recovery Status | Test Coverage | Cross-Refs |
|---|---|---|---|---|---|---|
| Non-blocking lock skip (`reconciler.py:117`); outer try/except (136-149) | `_reconcile_positions` (167-277), `_reconcile_pending_orders` (291-348), `_sync_order_status` (375-401), `_audit_position_change` (405-418, fire-and-forget) | `qty_mismatch` ⇒ unconditional overwrite (CA-03); broker `get_positions()` failure ⇒ **whole reconcile aborts**, no partial repair | **Yes** — CA-03 masks root cause; reconcile-skip-on-lock-contention is log-only | Position-qty repair: automatic (masks). Lock contention / mid-loop crash: **no alert** | `backend/execution/tests/test_reconciler.py` (`TestMissingInDB`, `TestQtyMismatch`, `TestStalePosition`, `TestDryRun`, `TestKiwoomNoOp`, `TestBrokerScoping`) — repair logic well-covered | CA-03, CA-04 (**CONFIRMED PRESENT**), EX-04, EX-11 (**CONFIRMED PRESENT**) |

`PositionReconciler.reconcile()` (`reconciler.py:112-151`) is invoked from four call sites:
the scheduler's `_periodic_reconcile` job (every 30 min during market hours,
`max_instances=1, coalesce=True`), `StartupRecovery._step_reconcile` (`"startup"`, step 6,
synchronous), the `_post_recovery_reconcile` daemon thread (`"post_recovery"`, spawned at the
end of step 7), and ad-hoc operator triggers (if any exist via the API). All four share the
same `threading.Lock().acquire(blocking=False)` (117) — if two of these overlap (e.g. a
worker restart during market hours, where `"startup"` + `"post_recovery"` could in principle
overlap with a `_periodic_reconcile` tick that fires moments later), the later one **skips
entirely** with a log message — there is no queueing, retry, or alert for a skipped
reconciliation cycle. A worker that is restarting repeatedly (e.g. a crash loop) could
therefore go an extended period with **zero successful reconciliation cycles**, each one
preempted by the next restart's `"startup"` reconcile contending for the same lock, with no
alert that reconciliation itself has stopped running.

Within a single reconcile run:

- `_reconcile_positions()` (167-277): `qty_mismatch` between DB and broker is repaired via
  **unconditional overwrite** (206-231) — `CA-03`, confirmed present, unchanged from
  `CORPORATE_ACTION_AUDIT.md`. `avg_price`-only drift is also auto-fixed (232-245). Neither
  path records *why* the value changed (`CA-04` — no `corporate_action_type`/`adjustment_factor`
  field in the schema, confirmed present).
- `_reconcile_pending_orders()` (291-348): per-order `get_order_status` failures are caught
  and `continue` (330-332) — one broker error does not abort the whole pending-order pass.
  Orders pending >1h (`_STALE_MIN_AGE_HOURS=1.0`, line 99) are flagged as `lost_order`
  (334-340) — this is the *only* detection mechanism for EX-02/EX-10 fill loss (§3.6),
  and itself only an audit-log entry.
- `_sync_order_status()` (375-401): fill-row insertion is deduplicated via an app-level
  `existing_fill` query (392-393) — there is still **no DB unique constraint** on the `Fill`
  table (`database/models.py:48-55`), so this remains `EX-04` (confirmed present, app-level
  only). No call to an `is_registered()`-style guard against the live `OrderFillPoller`
  exists — `order_poller.py`'s public surface is `register`/`unregister`/`pending_count`
  only (60-100) — so a fill could in principle be inserted by both the reconciler's
  `_sync_order_status` and the poller's `on_filled` callback for the same underlying broker
  fill, racing on the `existing_fill` dedup query (`EX-11`, confirmed present as a TOCTOU
  window, not fully closed).

### 3.8 Stale data

| Detection | Handling (file:line) | Blast Radius | Silent Propagation? | Recovery Status | Test Coverage | Cross-Refs |
|---|---|---|---|---|---|---|
| Four independent, inconsistent checks (SD-06) | `loader.py:83-97` (26h WARN), `strategy/base.py:80-101` (`_is_bar_stale`, 600s, dormant path), `indicator/strategy.py:103-118` (3-day gate, bare except) | Stale OHLCV feeds signal generation; stale FX feeds risk-threshold equity calc | **Yes** — SD-01/SD-09 are warn-or-swallow by design | Partial — `_BAR_STALE_SECONDS` gate works for the (currently dormant) `on_bar` path | Covered extensively by `docs/STALE_DATA_AUDIT.md`/`STALE_DATA_DETECTOR.md` (SD-01..SD-13) | SD-01, SD-03, SD-04, SD-05, SD-06, SD-09, SD-12 (**all CONFIRMED PRESENT**, not re-derived) |

This audit does not re-derive the staleness findings — `STALE_DATA_AUDIT.md` already provides
a 13-item (`SD-01`..`SD-13`) breakdown with severities. The pipeline-trace-relevant
highlights, confirmed still present in the current code walked for this audit:

- **SD-09** — `_scan_and_trade()`'s staleness gate (`indicator/strategy.py:103-118`) is the
  gate that sits directly in this audit's traced pipeline (between Strategy and Execution).
  Its `age_days = (...).days` truncation and bare `except Exception: pass` mean a genuinely
  malformed timestamp index silently **disables the staleness check entirely** for that
  symbol's scan — the strategy proceeds to evaluate signals and potentially place orders on
  data of unknown age.
- **SD-05** — directly intersects this audit's §3.1/§3.3 heartbeat analysis: `WorkerWatchdog`
  (`heartbeat.py`) is a **process-liveness** signal, completely orthogonal to **data
  freshness**. A worker that is alive, heartbeating normally, with Redis/DB healthy, but
  whose `yfinance`/`pykrx` data sources have been silently failing over to stale tier-4
  cache (SD-03) for hours, produces **zero alerts** anywhere in this audit's traced pipeline
  — heartbeat is green, reconciliation only checks order/position quantities (not data
  recency), and SD-01/SD-09's warnings are log-only.
- **SD-06** — the "four fragmented staleness checks" finding generalizes beyond OHLCV: this
  audit additionally observes that **order/pending-order staleness** has its own
  independent, differently-scoped thresholds — `reconciler.py`'s `_STALE_MIN_AGE_HOURS=1.0`
  (lost-order detection, §3.7) vs. `recovery.py`'s `_RECOVERY_STALE_ORDER_HOURS=24`
  (startup orphan/stale-order check, §2.3 step 8). These are different *concerns* (an order
  pending >1h during live trading vs. an order still pending >24h across a restart) so
  different thresholds are arguably correct — but per SD-06's own framing, this is one more
  instance of the same "no shared staleness configuration" pattern, now extended from
  *data* staleness to *order* staleness. No new ID is raised; this is noted as additional
  evidence for SD-06's existing scope.

### 3.9 Duplicate event

| Detection | Handling (file:line) | Blast Radius | Silent Propagation? | Recovery Status | Test Coverage | Cross-Refs |
|---|---|---|---|---|---|---|
| `_handle_market_open` 5-min dedup (`runner.py:316-345`, "F5"); `_persist_order`/`_persist_fill` idempotency (514-609) | DB-level: `Order.idempotency_key` UniqueConstraint + `IntegrityError` catch; `Fill` dedup via `existing_fill` query | DB-level duplicates are caught. **In-memory** `PositionTracker.on_fill()` has no fill-id dedup | **Yes** — in-memory double-apply is corrected (and masked) by the next reconciliation's CA-03 overwrite | DB: automatic (idempotency key / dedup query). In-memory: **none** | `test_recovery_safety.py::TestPersistFillIdempotency`, `TestPersistOrderIntegrity` cover the DB layer | F5 (**RESOLVED**), EX-04 (**CONFIRMED PRESENT**), **FS-05 (NEW)** |

Two layers of duplicate-event defense exist, and they are **not symmetric**:

1. **Session-open duplicate dispatch — F5, resolved.** `_handle_market_open`
   (`runner.py:316-345`) carries an explicit comment: "F5: dedup check and sessions snapshot
   must share the same lock acquisition" — confirming this fix already landed. A
   session-open signal that arrives twice (e.g. once via Redis Pub/Sub and once via the DB
   `Command`-table fallback, if Redis recovers mid-poll — §3.1) within the 5-minute window is
   deduplicated before any strategy logic runs.
2. **Fill-event duplicate dispatch — `FS-05` (NEW, MEDIUM-HIGH).** If `on_filled()` is
   invoked twice for the *same underlying broker fill* — plausible via the EX-02/EX-10
   mechanisms in §3.6, or via the reconciler/poller race in EX-11 (§3.7) — the 6-step fill
   pipeline (`runner.py:429-511`) runs `tracker.on_fill()` (step 2) **before**
   `_persist_fill()`'s DB-level dedup (step 4). `PositionTracker.on_fill()`
   (`position_tracker.py:80-116`) has **no fill-id/order-id-level idempotency check** of its
   own — a second invocation for the same fill mutates `self._positions[symbol]` a second
   time (e.g. a sell fill decrements `pos.qty` again; per 105-115, if `pos.qty <= 0` the
   position is deleted from the in-memory dict entirely). By the time step 4's
   `existing_fill` query correctly identifies the second `_persist_fill` call as a duplicate
   and skips the DB write, the **in-memory** `PositionTracker` state has *already* diverged
   from the DB/broker truth — e.g. a position that should still exist with `qty=10` may have
   been deleted from `self._positions` by an erroneous double-decrement. This divergence is
   silent until the next `_periodic_reconcile` tick, whose `_reconcile_positions` (§3.7) sees
   the DB/broker state (correct) and would re-seed `PositionTracker` only if
   `restore_positions()` is called again — which it is **not** during normal operation (only
   at session construction). In practice the in-memory tracker could remain wrong for up to
   30 minutes (until the next reconcile) or until the next restart, during which
   `try_mark_pending`/`unmark_pending` logic (which reads `self._positions`) could make
   incorrect pending-order decisions for that symbol.

### 3.10 Server restart

| Detection | Handling (file:line) | Blast Radius | Silent Propagation? | Recovery Status | Test Coverage | Cross-Refs |
|---|---|---|---|---|---|---|
| Combination of §3.2 (Worker restart) + §3.1 (Redis-at-boot, non-fatal) + StartupRecovery | `recovery.py:83-108` (9-step), `database/models.py:145-149` (`init_db_factory`, `create_all`) | Schema drift after an ORM-model change surfaces as a **runtime error on first access**, not at the `SELECT 1` health check; `Command` table rows accumulate unbounded across restarts | **Yes** — both FS-03 and FS-04 are silent until a specific code path is hit | create_all: **none** (no migration framework). Command-table growth: **none** (no purge job) | None — no schema-drift or Command-table-retention test | F1, EX-06 (**RESOLVED**), **FS-03 (NEW)**, **FS-04 (NEW)** |

Server restart is the union of every restart-time effect already discussed in §3.1-§3.3, plus
two additional findings that only manifest at the **database** layer on restart:

1. **`Base.metadata.create_all(engine)` is a silent no-op on schema drift — FS-04 (NEW,
   CRITICAL).** `backend/database/models.py:148` calls `Base.metadata.create_all(engine)`
   inside `init_db_factory()`, which both `kis-api` and `kis-worker` call independently on
   every process start (`models.py:145-149`). SQLAlchemy's `create_all()` only issues
   `CREATE TABLE IF NOT EXISTS` — it never issues `ALTER TABLE` for tables that already
   exist. If a future change adds a column to e.g. `Order`, `Position`, or `Fill` (the ORM
   models), and the deployed Postgres database already has those tables from a prior
   version, **the new column simply does not exist in the database** after the restart —
   with no error, no warning, nothing in the logs. `StartupRecovery` step 1 ("DB health")
   only runs `SELECT 1` (a trivial connectivity check), so both `kis-api` and `kis-worker`
   report "DB healthy" and proceed to enable trading. The first ORM query or insert that
   references the new column then raises a runtime `sqlalchemy.exc.ProgrammingError`
   (`UndefinedColumn` in Postgres) — potentially in the middle of the fill pipeline, hours or
   days after the restart that introduced the drift, and only on the specific code path that
   touches the new column. This is exactly the gap tracked in `ROADMAP.md` **P0-14** (lines
   218-229, "no Alembic, `create_all()` silently skips existing tables", citing `AUDIT.md
   DB-01`) — this audit confirms `models.py:145-149` is unchanged and the gap remains live in
   the restart path of both `kis-api` and `kis-worker`.
2. **`Command` table has no retention/purge — FS-03 (NEW, LOW-MEDIUM).** Every session-open
   signal writes a row to the `Command` table (`scheduler.py:144-153`,
   `Command` model at `database/models.py:103-111`, no TTL/expiry field). `_enter_db_polling_mode()`
   (`runner.py:243-282`) marks rows `processed`/`error` but never `DELETE`s them. None of
   `build_scheduler()`'s 5 jobs (`scheduler.py:213-261`) include a `Command`-table purge.
   Under normal operation this table grows by ~2 rows/day (one `kr_session`, one
   `us_session`); during an **extended Redis outage** (§3.1), the worker stays in
   `_enter_db_polling_mode()`'s 30s loop, but new `Command` rows are only created by the
   scheduler's session-open jobs (still 2/day) — so the table does not grow *faster* during
   an outage, but it never shrinks, ever, under any configuration. Combined with FS-04 (no
   migration framework to later add an index/partition/retention policy without a manual
   `ALTER`), this is a low-urgency but **permanent** accumulation with no operational
   visibility (no metric, no alert threshold).

Finally, both `kis-api` and `kis-worker` independently call
`create_engine(db_url, pool_pre_ping=True, echo=False)` (`models.py:147`) — i.e. SQLAlchemy's
default pool sizing (`pool_size=5, max_overflow=10`) for **each** process. On a coordinated
server restart, both processes reconnect to Postgres at roughly the same time; `kis-api` runs
`GUNICORN_WORKERS` (default 2) separate processes, each with its own pool, plus
`kis-worker`'s pool, plus the `_post_recovery_reconcile` daemon thread's `_session()` calls
(`recovery.py:297-319`) and the parallel `_step_balance`/`_step_positions`
`ThreadPoolExecutor` calls (154-184) — all racing for new connections within the same
restart window. `pool_pre_ping=True` mitigates *stale* connections but does not increase
capacity. This is a soft observation (not assigned an `FS-` ID — no evidence of an actual
pool-exhaustion incident, and default pool sizing is often adequate for this workload's
concurrency) but is worth monitoring if the number of gunicorn workers or concurrent
strategies grows.

---

## §4 Failure Propagation Map

The following chains trace how a **single** initiating failure propagates across process and
module boundaries. Each step cites the file:line where the propagation happens and the
finding ID (existing or new) that applies.

### Chain 1 — FS-01: Redis blip → false "dead worker" → persistent trading halt

```
1. Redis becomes unreachable for ~2 minutes (network blip, Redis container restart, etc.)
     redis container / network                                              [infra]

2. WorkerHeartbeat._beat() fails silently every 30s — `self._redis.setex()` raises,
   caught and logged at WARNING only. worker:heartbeat TTL key (90s) is not refreshed.
     backend/worker/heartbeat.py:43-48                                      [FS-01]

3. kis-worker's own pubsub loop catches redis.ConnectionError, enters DB-polling
   fallback — the WORKER ITSELF continues operating normally (session signals via
   Command table). This step does NOT fail.
     backend/worker/runner.py:233-237, 243-282                              [handled]

4. ~90-150s later, kis-api's WorkerWatchdog._check() (separate gunicorn process,
   60s interval) calls HeartbeatMonitor.is_alive() — Redis itself is unreachable for
   the `.exists()` call, exception caught, returns False. Watchdog cannot distinguish
   "Redis down" from "worker process dead".
     backend/worker/heartbeat.py:59-64, 117-122                             [FS-01]

5. _alert_dead_worker() writes DailyRiskState.kill_switch=True, kill_reason=
   "Worker 하트비트 없음 — 프로세스 재시작 필요" (a misleading diagnosis — the worker
   process is fine). Sends Telegram emergency alert + WS critical alert.
     backend/worker/heartbeat.py:128-169                                    [FS-01]

6. Redis recovers within minutes. kis-worker's _enter_db_polling_mode() ping
   succeeds, returns to pubsub mode; WorkerHeartbeat resumes; HeartbeatMonitor.is_alive()
   returns True again; WorkerWatchdog._check() calls _alert_recovery() — but this ONLY
   publishes an informational WS message. kill_switch in DB is NEVER cleared.
     backend/worker/runner.py:250-252; heartbeat.py:123-126, 171-176        [FS-01]

7. At the next 06:01 KST _reset_daily_risk(), yesterday's DailyRiskState.kill_switch
   is still True → SAFE_MODE re-arm is explicitly blocked ("어제 킬스위치 활성 —
   SAFE_MODE 재활성화 차단. 수동 해제 필요."). Live trading remains halted until a human
   manually clears kill_switch in Postgres.
     backend/worker/scheduler.py:108-132                                    [FS-01]
```

A 1-2 minute Redis network blip — fully self-healed at every layer that detects it directly —
results in live trading being halted until manual operator intervention, with an alert that
points at the wrong subsystem.

### Chain 2 — DO-01/FS-05: Network timeout → ghost order → in-memory double-apply

```
1. POST /uapi/.../order succeeds at the broker; response is lost to a network
   timeout before reaching the client (10s timeout).
     kis_adapter/client.py:68 (resp = requests.post(..., timeout=10))       [infra]

2. KISClient.post()'s retry loop (3x, 1s backoff) resubmits the SAME order body —
   broker accepts it as a NEW order (no idempotency token sent — DO-05).
     kis_adapter/client.py:65-96                                            [DO-01, DO-05]

3. Both orders eventually fill. OrderFillPoller registers both broker_order_ids
   (different IDs from the broker's perspective) under self._entries, each with its
   own on_filled callback.
     backend/execution/order_poller.py:76-92                                [DO-01]

4. Both fills flow through _make_fill_callback's 6-step pipeline. Step 2
   (tracker.on_fill) runs for BOTH fills before step 4's DB-level dedup — but
   _persist_fill's dedup is keyed on (order_id, qty, price) where order_id differs
   between the two ghost orders, so DB-level dedup does NOT catch this case (the two
   Fill rows are legitimately different broker orders).
     backend/worker/runner.py:429-511, 563-609                              [FS-05]

5. PositionTracker.on_fill() applies BOTH fills — position is double-sized (e.g. a
   buy for qty=10 becomes qty=20 in both DB Position and broker reality, since the
   broker DID execute both orders).
     backend/execution/position_tracker.py:80-116                          [FS-05]

6. Next reconciliation cycle compares DB Position (qty=20, now correct — both fills
   really happened) against broker (qty=20) — NO qty_mismatch is raised, because the
   double-fill is now the broker's ground truth too. CA-03 does not "catch" this —
   there is nothing to repair. The error is now a real, doubled position with no
   record of it being unintended.
     backend/execution/reconciler.py:206-231                                [CA-03 — does not apply]
```

This chain is notable because it shows DO-01's ghost-order risk converging on a state that
**reconciliation cannot detect** — both the DB and the broker agree on a position that is
2x the strategy's intended size, because the broker really did execute both orders. The only
defense is preventing the duplicate POST in the first place (DO-05's broker-side idempotency
token, still absent).

### Chain 3 — FS-04: ORM model change → server restart → silent schema drift → runtime error

```
1. A future code change adds a new column to an ORM model, e.g. Order.client_tag
   (hypothetical), and is deployed.
     backend/database/models.py (Order class)                              [dev change]

2. kis-api and kis-worker containers restart (deploy). Both independently call
   init_db_factory(DB_URL) → create_engine(...) → Base.metadata.create_all(engine).
   The `orders` table already exists in Postgres from a prior deploy — create_all()
   issues NO ALTER TABLE for the new column.
     backend/database/models.py:145-149                                    [FS-04]

3. StartupRecovery step 1 ("DB health") runs `SELECT 1` only — succeeds. Steps 4/5
   (broker balance/positions) succeed. _step_enable_trading() proceeds — trading is
   enabled. No error anywhere in the startup sequence.
     backend/worker/recovery.py:83-108                                     [FS-04]

4. Hours later, a fill callback or strategy code path reads/writes Order.client_tag
   for the first time — sqlalchemy.exc.ProgrammingError (UndefinedColumn) raised from
   Postgres, inside whichever try/except wraps that specific call (e.g. the 6-step
   fill pipeline's per-step try/except, runner.py:429-511 — non-fatal but the SPECIFIC
   operation referencing the new column silently fails every time it's hit).
     backend/worker/runner.py (whichever step references the new column)   [FS-04]
```

The gap between step 2 (drift introduced, undetected) and step 4 (first runtime failure) can
be arbitrarily long — the drift is invisible until a specific, possibly rare, code path
executes. This is `ROADMAP.md` **P0-14** (`AUDIT.md DB-01`), confirmed still open.

### Chain 4 — FS-02: Worker restart mid-outage → circuit breaker silently re-armed

```
1. KIS broker API degrades (elevated error rate on place_order / get_order_status).
     kis_adapter/client.py (repeated failures)                              [infra]

2. ConsecutiveFailureBreaker.record_failure() increments self._failures; after 5
   consecutive failures (KISBroker's breaker, kis.py:48), self._tripped_at is set —
   breaker OPENS for a 10-minute cooldown. logger.error logs the trip; no Telegram/
   WS alert.
     backend/execution/circuit_breaker.py:23-30                            [new, §3.5]

3. While the breaker is open (cooldown in progress) AND the underlying broker
   degradation is STILL ONGOING, kis-worker restarts (deploy, OOM, crash-loop).
     [restart — §3.2]                                                       [FS-02]

4. StartupRecovery constructs a fresh KISBroker (recovery.py `_step_balance`/
   `_step_positions` use self._broker), which constructs a fresh
   ConsecutiveFailureBreaker(threshold=5, cooldown_minutes=10) — self._failures=0,
   self._tripped_at=None. The breaker's "memory" of the ongoing outage is gone.
     backend/execution/circuit_breaker.py:17-22                            [FS-02]

5. The first strategy scan after restart immediately calls place_order() again
   against the still-degraded broker — the breaker provides ZERO protection for the
   first 5 consecutive failures post-restart, even though the SAME outage that
   tripped the pre-restart breaker is still active.
     backend/brokers/kis.py:106-150                                        [FS-02]
```

### Chain 5 — EX-02/EX-10: Poller-thread crash → total silent fill loss across all strategies

```
1. An unhandled exception occurs inside OrderFillPoller._loop()'s per-tick body,
   OUTSIDE the try/except scopes inside _poll_one/_handle_timeout — e.g. while
   computing `due = [e for e in self._entries.values() if e.next_poll_at <= now]`
   during a concurrent register()/unregister() (lock IS held for the read at
   105-106, so this specific example is unlikely — but ANY such exception has this
   effect).
     backend/execution/order_poller.py:102-114                             [EX-10]

2. The "order-poller" daemon thread terminates. Python logs a traceback to stderr;
   the kis-worker PROCESS continues running — WorkerHeartbeat is on a DIFFERENT
   thread and continues beating normally. HeartbeatMonitor.is_alive() returns True.
     backend/worker/heartbeat.py (unaffected)                              [handled — but masks EX-10]

3. EVERY order registered with self._poller — across every active strategy/symbol —
   stops receiving FILLED/PARTIAL_FILLED/timeout callbacks. New orders placed after
   this point are still register()'d (the dict write itself doesn't fail) but will
   never be polled.
     backend/execution/order_poller.py:76-92 (register, now inert)        [EX-10]

4. The only detection is _periodic_reconcile's lost_order check (pending orders aged
   >1h), which runs at most every 30 minutes during market hours.
     backend/execution/reconciler.py:334-340                               [EX-02, EX-10]

5. lost_order entries are logged/audited (_audit_position_change-style fire-and-
   forget) but do NOT restart the poller thread — the poller remains dead until the
   next full process restart (§3.2), at which point StartupRecovery re-registers
   pending orders on a NEW poller instance.
     backend/worker/recovery.py:219-322 (next restart only)                [EX-10]
```

Up to ~1.5h of total silent fill-tracking loss for **every in-flight order in the system**,
with the worker process reporting healthy throughout.

---

## §5 Test Coverage Gaps

| Scenario | Existing test file(s) | What's tested | What's NOT tested |
|---|---|---|---|
| Redis down | none | — | `WorkerHeartbeat`/`HeartbeatMonitor`/`WorkerWatchdog` behavior when `redis_client` calls raise; `_run_with_pubsub` → `_enter_db_polling_mode` transition and recovery; FS-01's cross-process `kill_switch` write/non-clear |
| Worker restart | `backend/worker/tests/test_recovery_safety.py` (`TestRestorePendingBrokerScope`, `TestRestorePendingPollerRegistration`, `TestPersistFillIdempotency`, `TestPersistOrderIntegrity`, `TestValidateState`) | Pending-order recovery, broker-scoping, fill/order idempotency on restore, `_step_validate_state`'s 3 checks | `ConsecutiveFailureBreaker` state across a `KISBroker`/`IndicatorStrategy` re-construction (FS-02); heartbeat-gap-exceeds-TTL during a slow recovery (FS-01 interaction) |
| Process kill (SIGKILL/OOM) | none | — | Any kill-mid-pipeline scenario; whether `Fill` rows are correctly absent-but-position-correct after CA-03 repair (§3.3) |
| Network timeout | none | — | `KISClient.get`/`post` retry/backoff behavior under simulated `requests.Timeout`; `_get_fx` cache/fallback chain (SD-04); `_issue_token`/`get_hashkey` timeout bypassing the retry loop (FS-07) |
| Broker API failure | none | — | `ConsecutiveFailureBreaker` open/close/cooldown transitions; absence of alert on breaker trip; `get_order_status` → `None` handling by the poller |
| Polling failure | none | — | `OrderFillPoller._loop` thread-crash + no restart (EX-10); pop-before-callback for FILLED/PARTIAL_FILLED/timeout (EX-02) |
| Reconciliation failure | `backend/execution/tests/test_reconciler.py` (`TestMissingInDB`, `TestQtyMismatch`, `TestStalePosition`, `TestDryRun`, `TestKiwoomNoOp`, `TestBrokerScoping`) | Position/pending-order repair logic, dry-run mode, broker-scoping, Kiwoom no-op | Lock-contention skip (concurrent `reconcile()` calls); reconciler raising mid-loop with partial DB writes; `_sync_order_status`/poller race (EX-11) |
| Stale data | covered by `docs/STALE_DATA_AUDIT.md`/`STALE_DATA_DETECTOR.md` design docs — implementation status of `SG-01..SG-07` safeguards not verified in this audit | — | (see `STALE_DATA_AUDIT.md`/`STALE_DATA_DETECTOR.md` for current gaps; SD-01..SD-13) |
| Duplicate event | `test_recovery_safety.py::TestPersistFillIdempotency`, `TestPersistOrderIntegrity`; `tests/execution/test_order_machine_new_statuses.py` (state-transition validity, not duplication) | DB-level idempotency (`Order.idempotency_key`, `Fill` dedup query) | `PositionTracker.on_fill()` double-invocation (FS-05); `_handle_market_open` F5 dedup under Redis-recovery-mid-poll timing |
| Server restart | `test_recovery_safety.py` (recovery-specific subset above) | Recovery's order/position restoration | `create_all()` vs. schema-drift detection (FS-04); `Command` table growth over multiple restarts (FS-03); combined Redis-down + restart timing |

Two existing test files are general-purpose and provide indirect coverage:

- `tests/execution/test_order_machine_new_statuses.py` — `test_submitted_to_expired`,
  `test_partial_filled_to_expired`, `test_expired_is_terminal`, `test_submitted_to_unknown`,
  `test_partial_filled_to_unknown`, `test_unknown_to_filled`, `test_unknown_to_canceled`,
  `test_unknown_to_expired`, `test_filled_to_unknown_is_invalid`,
  `test_active_orders_includes_unknown` — these validate `OrderStateMachine`'s state-transition
  table including `UNKNOWN`/`EXPIRED` statuses that would result from e.g. a broker returning
  an unrecognized status during a Broker API failure (§3.5) or a `get_order_status` timeout —
  but do not simulate the actual network/timeout condition that produces those statuses.
- `tests/brokers/test_semantic_mapper.py` and `tests/data/test_calendar.py` are out of this
  audit's scope (broker status-code mapping and market-calendar logic respectively, not
  failure-injection).

**Summary**: every scenario that touches **order/position state restoration** (Worker
restart, Reconciliation, Duplicate event at the DB layer) has solid existing coverage. Every
scenario that is purely **infrastructure-level** (Redis down, Process kill, Network timeout,
Polling-thread crash, schema drift) has **zero** existing test coverage — these are exactly
the gaps `TASK 4-1B`'s failure-injection harness (§8) should target first.

---

## §6 Risk Classification

| ID | Scenario(s) | Severity | Status | File:line | Description |
|---|---|---|---|---|---|
| **FS-01** | Redis down, Worker restart, Process kill | **HIGH** | NEW | `heartbeat.py:43-48,59-64,128-169,171-176`; `scheduler.py:108-132` | Redis outage (or any heartbeat-write gap >90s) causes `WorkerWatchdog` to falsely conclude the worker is dead, writes a persistent `kill_switch=True` that `_alert_recovery` never clears, blocking `SAFE_MODE` re-arm until manual DB reset — with a misleading "워커 재시작 필요" alert |
| **FS-02** | Worker restart, Process kill, Broker API failure | MEDIUM | NEW | `circuit_breaker.py:14-49` | `ConsecutiveFailureBreaker` is purely in-memory; a worker restart during an open-breaker cooldown silently resets the breaker to closed, removing protection while the underlying broker outage may still be ongoing |
| **FS-03** | Server restart, Redis down | LOW-MEDIUM | NEW | `runner.py:243-282, 266-278`; `database/models.py:103-111` | `Command` table rows are marked `processed`/`error` but never purged — permanent, unbounded (if slow) growth with no retention policy or metric |
| **FS-04** | Server restart | **CRITICAL** | NEW | `database/models.py:145-149` | `Base.metadata.create_all()` is a no-op for existing tables; an ORM model change that adds a column surfaces as a runtime `ProgrammingError` on first access post-restart, not at the DB-health check. Matches `ROADMAP.md` P0-14 / `AUDIT.md DB-01` |
| **FS-05** | Duplicate event, Polling failure | MEDIUM-HIGH | NEW | `position_tracker.py:80-116`; `runner.py:429-511` | `PositionTracker.on_fill()` has no fill-id-level idempotency; a duplicate `on_filled` invocation (via EX-02/EX-10/EX-11 mechanisms) double-applies to in-memory position state before DB-level dedup runs, and the divergence is not corrected until the next reconciliation (up to 30 min) |
| **FS-06** | Process kill, Redis down (cross-cutting) | LOW | NEW | `gunicorn_conf.py:15,24-34`; `heartbeat.py:86-177` | `GUNICORN_WORKERS` default `"2"` → 2 independent `WorkerWatchdog` instances, each polling the same heartbeat key every 60s and racing to write `kill_switch`/send alerts. Idempotent (`if not row.kill_switch`) but doubles DB writes and can send duplicate Telegram/WS alerts |
| **FS-07** | Network timeout | MEDIUM-HIGH | NEW | `client.py:38,56-58,65`; `auth.py:65-75,90-99` | `get_hashkey()`/`get_headers()`→`_issue_token()` are computed before `KISClient`'s 3x retry loop and have no retry of their own; a single network timeout on KIS's auth/hashkey endpoints fails the entire `get()`/`post()` call with zero retries, unlike the data/order endpoint itself |
| DO-01 | Network timeout, Broker API failure | CRITICAL | **CONFIRMED PRESENT** | `kis_adapter/client.py:56-96` | POST retry (3x) on a non-idempotent order-placement call can submit duplicate orders ("ghost orders") if the first response is lost to a timeout |
| DO-05 | Broker API failure, Network timeout | HIGH | **CONFIRMED PRESENT** | `backend/brokers/kis.py:106-150` | `place_order()` sends no broker-side idempotency token; only app-level fingerprinting guards against duplicate submission |
| EX-02 | Polling failure, Process kill | CRITICAL | **CONFIRMED PRESENT** (extended to PARTIAL_FILLED) | `order_poller.py:130-149,151-165,170-178` | Entry popped / `last_reported_qty` advanced BEFORE `on_filled`/`on_timeout` callback; an exception in the callback permanently loses that fill/timeout |
| EX-04 | Duplicate event, Reconciliation failure | HIGH | **CONFIRMED PRESENT** | `database/models.py:48-55`; `reconciler.py:392-393` | No UNIQUE constraint on `Fill` table; dedup is app-level query only |
| EX-06 | Worker restart, Server restart | CRITICAL | **RESOLVED** | `recovery.py:234-239,290-292`; `order_poller.py:63,91`; `runner.py:679-701` | Shared-poller injection + dict-keyed-by-`order.id` overwrite semantics + `_guarded_on_filled`'s DB status re-check close the original double-registration/double-fill gap |
| EX-10 | Polling failure | HIGH | **CONFIRMED PRESENT** | `order_poller.py:102-114` | No supervisor detects or restarts a crashed `order-poller` thread; only signal is `lost_order` via reconciliation (≤ every 30 min, ≥1h age) |
| EX-11 | Reconciliation failure, Duplicate event | HIGH | **CONFIRMED PRESENT** (TOCTOU, not fully closed) | `reconciler.py:375-401`; `order_poller.py:60-100` (no `is_registered()`) | Reconciler's `_sync_order_status` and the live poller's `on_filled` can both attempt to insert a `Fill` for the same broker fill; app-level `existing_fill` dedup narrows but does not eliminate the race |
| F1 | Worker restart, Server restart | CRITICAL | **RESOLVED** | `recovery.py:266, 324-348` | `_apply_fill_to_position_db()` is fully defined and correctly invoked — original "undefined function" finding no longer applies |
| F5 | Duplicate event | (not separately rated in source doc) | **RESOLVED** | `runner.py:316-345` | Session-open dedup and sessions-snapshot now share the same lock acquisition |
| CA-03 | Reconciliation failure, Process kill | HIGH | **CONFIRMED PRESENT** | `reconciler.py:206-231` | `qty_mismatch` repaired via unconditional overwrite — masks the root cause of any drift (lost fills, corporate actions, double-fills) |
| CA-04 | Reconciliation failure, Process kill, Duplicate event | HIGH | **CONFIRMED PRESENT** | `database/models.py` (no CA fields) | No `corporate_action_type`/`adjustment_factor`/`ex_date` field anywhere — CA-03's repairs (and any other qty change) carry no "why" |
| SD-04 | Network timeout, Stale data | MEDIUM | **CONFIRMED PRESENT** | `backend/brokers/kis.py:300-317` | FX-rate cache silently serves a >30min-stale (or hardcoded `1350.0`) rate into equity/kill-switch calculations on `yfinance` failure |
| SD-05 | Stale data, Redis down | MEDIUM | **CONFIRMED PRESENT** | `heartbeat.py` (whole file) | Heartbeat is a process-liveness signal only; a worker that is alive but trading on stale data produces zero alerts |
| SD-09 | Stale data | MEDIUM | **CONFIRMED PRESENT** | `indicator/strategy.py:103-118` | `.days`-truncated staleness gate + bare `except: pass` can silently disable the staleness check entirely |

Summary of this table: **8 cross-referenced existing IDs confirmed present**
(DO-01, DO-05, EX-02, EX-04, EX-10, EX-11, CA-03, CA-04), **3 stale-data IDs confirmed
present** (SD-04, SD-05, SD-09 — full detail remains in `STALE_DATA_AUDIT.md`'s own
SD-01..SD-13 scope), **3 RESOLVED** since their source audits (F1, F5, EX-06), and
**7 new `FS-01`..`FS-07`** findings raised by this audit.

---

## §7 Affected Modules Table

| Module/file | Role in pipeline | Failure scenarios implicated | Existing safeguards | Gaps |
|---|---|---|---|---|
| `backend/worker/scheduler.py` | Cron entrypoints (session signals, risk reset, equity snapshot, periodic reconcile) | Redis down, Duplicate event, Server restart | DB-first `Command` write before Redis publish (135-159); `max_instances=1, coalesce=True` on reconcile job (256-257); all Redis access try/except | No `Command`-table purge job (FS-03); `_reset_daily_risk` blocks `SAFE_MODE` re-arm on stale `kill_switch` (FS-01 interaction) |
| `backend/worker/runner.py` | `StrategyWorker`/`WorkerSession` — Pub/Sub + DB-polling, strategy lifecycle, fill pipeline | Redis down, Worker restart, Process kill, Duplicate event | `redis.ConnectionError` backoff + DB-polling fallback (207-282); F5 session-open dedup (316-345); idempotency-keyed `_persist_order`/`_persist_fill` (514-609); shared-poller `_register_recovered_order` (661-701) | No SIGTERM/graceful-shutdown hook; `_enter_db_polling_mode` has no Command-table purge (FS-03) |
| `backend/worker/recovery.py` | `StartupRecovery` — 9-step boot sequence | Worker restart, Server restart, Process kill | F1 fix (324-348, called at 266); F7 fill-dedup (250-258); EX-06 shared-poller fix (234-239); 30s `ThreadPoolExecutor` timeouts on balance/positions (154-184); `_step_validate_state` observability (369-432) | `ConsecutiveFailureBreaker` not part of recovery's restored state (FS-02) |
| `backend/worker/heartbeat.py` | `WorkerHeartbeat`/`HeartbeatMonitor`/`WorkerWatchdog` — cross-process liveness | Redis down, Process kill, Server restart | TTL-based liveness (90s); idempotent `kill_switch` write (`if not row.kill_switch`, 145); recovery alert (171-176) | `is_alive()` conflates "Redis unreachable" with "worker dead" (FS-01); `_alert_recovery` never clears `kill_switch` (FS-01); 2x instances under multi-worker gunicorn (FS-06) |
| `backend/api/gunicorn_conf.py` | `post_fork()` — starts one `WorkerWatchdog` per gunicorn worker | Process kill, Redis down | — | `GUNICORN_WORKERS=2` default → duplicate watchdogs (FS-06) |
| `backend/strategy/base.py` | `StrategyBase` — `_live_trade_allowed`, `_is_bar_stale`, `buy`/`sell` gates | Stale data | `_is_bar_stale` (80-101, `_BAR_STALE_SECONDS=600`, dormant `on_bar` path); `_live_trade_allowed` (14-41, SAFE_MODE + `ENABLE_LIVE_TRADING` gates) | SD-10 (no live bar producer wires into `_is_bar_stale` yet) |
| `backend/strategy/indicator/strategy.py` | `IndicatorStrategy._scan_and_trade` — per-symbol signal/order loop | Broker API failure, Stale data | Own `ConsecutiveFailureBreaker(threshold=3, cooldown=30min)` (51); per-symbol try/except (95-99, 119-123); atomic `try_mark_pending`/`unmark_pending` (148-209) | SD-09 (`.days` truncation + bare except staleness gate, 103-118); FS-02 (breaker reset on restart) |
| `backend/execution/order_machine.py` | `OrderStateMachine` — status transitions, `process_fill` | Process kill, Duplicate event | Rollback on `_on_change` exception (46-59, 62-76); overfill guard (83-87); F4 orphaned-temp-key fix (119) | none new — covered via EX-02/FS-05 upstream |
| `backend/execution/position_tracker.py` | `PositionTracker` — in-memory position state, pending-order locks | Duplicate event, Process kill | `qty<=0` deletes position cleanly (105-115, sell path); `restore_positions()` called once per session at construction | No fill-id-level idempotency in `on_fill()` (FS-05) |
| `backend/execution/circuit_breaker.py` | `ConsecutiveFailureBreaker` — N-consecutive-failure trip + cooldown | Broker API failure, Worker restart, Process kill | Auto-reset after cooldown elapses (38-49) | Purely in-memory — lost on restart (FS-02); trip is log-only, no alert |
| `backend/execution/order_poller.py` | `OrderFillPoller` — 5s-tick polling, fill/timeout callbacks | Polling failure, Process kill, Duplicate event | Dict keyed by `order.id` with overwrite semantics (63, 91, supports EX-06 fix); per-callback try/except (142, 163-164, 176-178) | Pop-before-callback for FILLED/PARTIAL_FILLED/timeout (EX-02); no thread-crash supervisor (EX-10); no `is_registered()` (EX-11) |
| `backend/execution/reconciler.py` | `PositionReconciler` — periodic + on-demand position/order reconciliation | Reconciliation failure, Process kill, Stale data | Non-blocking lock (112-122); per-order try/except continue (323-332); broker-scoped (317); `lost_order` aging check (334-340) | CA-03 unconditional overwrite masks root cause; lock-skip is silent (no alert); EX-11 TOCTOU |
| `backend/brokers/kis.py` | `KISBroker` — `BrokerAdapter` implementation, FX cache | Broker API failure, Network timeout, Stale data | `ConsecutiveFailureBreaker(threshold=5, cooldown=10min)` (48); `MarketClosedError` excluded from breaker (138-144); FX 1h-TTL cache + >30min stale warning (300-317, SD-04) | FS-02 (breaker reset on restart); SD-04 hardcoded `1350.0` fallback |
| `kis_adapter/client.py` | `KISClient` — HTTP GET/POST, rate limiting, retry | Network timeout, Broker API failure | 3x retry/1s backoff/10s timeout on GET and POST body (37-96); `MarketClosedError` special-cased (81-93); `RateLimiter` (11-22) | DO-01 (POST retry → ghost orders); FS-07 (`get_headers`/`get_hashkey` outside retry loop) |
| `kis_adapter/auth.py` | `KISAuth` — token cache (Redis + in-memory), hashkey | Network timeout, Redis down | 3-tier token cache (Redis → in-memory → fresh issue, 32-63); lazy Redis client (no eager connect) | FS-07 (`_issue_token`/`get_hashkey` no retry, single 10s timeout, `auth.py:65-75,90-99`) |
| `backend/database/models.py` | SQLAlchemy ORM models + `init_db_factory` | Server restart, Duplicate event | `Order.idempotency_key` UniqueConstraint (28); `Position` UniqueConstraint on symbol+broker (80-82); `pool_pre_ping=True` (147) | FS-04 (`create_all()` no-op on drift, 148); `Fill` no UNIQUE constraint (EX-04); `Command` no TTL/purge field (103-111, FS-03) |
| `bot/scheduler.py`, `bot/main.py` | Legacy `BlockingScheduler` engine | — (LOW, disabled) | `docker-compose.yml:80-110` — service commented out, explicit duplicate-order warning | None actionable — informational only per audit scope |
| `api/database.py`, `api/routers/*` | Separate top-level `api/` package (`DATABASE_URL`, `pool_size=10`) | — (out of scope) | — | Not part of the traced pipeline — `backend/api/server.py` uses `backend/database/models.py`'s `init_db_factory(DB_URL)` instead (`server.py:15-17,55-58`); no DB-pool-inconsistency finding raised |

---

## §8 Insertion Points for Failure-Scenario Tests

This section is a **sketch for TASK 4-1B** (a future failure-injection test harness) — it
proposes file paths, mocking strategy, and assertions per scenario. **No test code is
written in this task.**

### 8.1 Redis down — `tests/failure/test_redis_down.py`

- **Mock**: a `redis_client` stub whose `.setex()`, `.exists()`, `.get()`, `.ttl()`,
  `.pubsub()`, `.ping()` all raise `redis.ConnectionError` for a configurable window, then
  recover.
- **Assert**:
  - `WorkerHeartbeat._beat()` does not raise (heartbeat.py:43-48).
  - `HeartbeatMonitor.is_alive()` returns `False` while the stub raises (59-64) — **this is
    the FS-01 assertion that should change**: a fixed version would distinguish "Redis
    unreachable" from "heartbeat key expired" (e.g. by having the monitor itself catch
    `ConnectionError` specifically and return a third state, or by having the watchdog
    independently verify Redis health before concluding the worker is dead).
  - `WorkerSession._run_with_pubsub` transitions to `_enter_db_polling_mode` on
    `ConnectionError` (233-237) and returns to pubsub mode once the stub stops raising (250-252).
  - `WorkerWatchdog._alert_dead_worker` writes `kill_switch=True`; after recovery,
    `_alert_recovery` is called — assert (post-fix) that `kill_switch` is also cleared, or
    that the kill_reason is distinguishable from a genuine process-death reason.

### 8.2 Worker restart — `tests/failure/test_worker_restart.py`

- **Mock**: instantiate `ConsecutiveFailureBreaker`, trip it (5x `record_failure()`), then
  construct a *new* `KISBroker`/`IndicatorStrategy` instance (simulating restart) and assert
  `is_open()` on the new instance.
- **Assert** (documents FS-02 as a known gap, or verifies a fix): post-restart breaker state
  either (a) is restored from a persisted source (Redis/DB), or (b) the test explicitly
  documents "breaker resets on restart — acceptable because StartupRecovery's 30s broker
  timeouts in steps 4/5 act as an independent gate."
- Extend `backend/worker/tests/test_recovery_safety.py` with a case that constructs
  `StartupRecovery` twice in sequence against the same DB fixture and asserts no duplicate
  `Fill`/`Position` rows (regression-guard for EX-06/F1 staying resolved).

### 8.3 Process kill — `tests/failure/test_process_kill.py`

- **Mock**: a fake `_make_fill_callback` that raises `os._exit(1)`-style termination
  (or, more practically, simulate by truncating the 6-step pipeline mid-way — call
  `tracker.on_fill()` then stop, without calling `_persist_fill`).
- **Assert**:
  - DB `Position`/`Fill` reflect the **pre-kill** state (no partial writes).
  - A subsequent `reconcile("startup")` against a broker fixture that reflects the
    **post-fill** state corrects `Position.qty` (CA-03) — assert the corrected qty is
    right, AND assert (documenting the gap) that no `Fill` row exists for the lost fill.
  - `WorkerHeartbeat` stops (simulating process death); `WorkerWatchdog` correctly fires
    `_alert_dead_worker` (this is the ONE Redis-down-shaped test that SHOULD result in
    `kill_switch=True`).

### 8.4 Network timeout — `tests/failure/test_network_timeout.py`

- **Mock**: patch `requests.get`/`requests.post` (or the `requests.Session` used by
  `KISClient`/`KISAuth`) to raise `requests.exceptions.Timeout` on the Nth call.
- **Assert**:
  - `KISClient.get()`/`post()` retry exactly `MAX_RETRIES=3` times with the documented 1s
    backoff when the **data/order** call times out.
  - When `KISAuth.get_hashkey()` or `KISAuth._issue_token()` (via `get_headers()`) times out
    — patch `requests.post` to raise only for URLs containing `/uapi/hashkey` or
    `/oauth2/tokenP` — assert that `KISClient.post()` raises **immediately, with zero
    retries** (documents FS-07; a fix would wrap these calls in their own retry or move
    `get_headers()`/`get_hashkey()` inside the retry loop).
  - `_get_fx()` with a mocked `yfinance.Ticker(...).history()` raising `Timeout`: assert the
    cached value is returned, and that a >30min-stale cache logs the SD-04 warning (existing
    behavior — regression guard).

### 8.5 Broker API failure — `tests/failure/test_broker_api_failure.py`

- **Mock**: a `BrokerAdapter` stub whose `place_order`/`get_order_status` raise for N
  consecutive calls.
- **Assert**:
  - After `threshold` consecutive failures, `ConsecutiveFailureBreaker.is_open()` is `True`
    and subsequent `place_order` calls short-circuit to `REJECTED` without calling the
    broker stub (verifies §3.5's breaker-open short-circuit).
  - `record_failure()`'s `logger.error` fires exactly once at the threshold crossing (not on
    every subsequent failure while open) — and (documenting FS-02's sibling gap) assert no
    Telegram/WS alert is sent, as a prompt to add one.
  - `get_order_status` returning `None` leaves the poller entry registered with an unchanged
    `next_poll_at` schedule (not removed), until `is_timed_out`.

### 8.6 Polling failure — `tests/failure/test_order_poller_crash.py`

- **Mock**: construct `OrderFillPoller`, register an order whose `on_filled` callback raises
  `RuntimeError` (EX-02), and separately, monkeypatch `_PollEntry.is_timed_out` (or the
  `due` list comprehension) to raise on the second `_loop()` tick (EX-10).
- **Assert**:
  - EX-02: after the FILLED poll where `on_filled` raises, `pending_count()` no longer
    includes that order (entry was popped) — documenting the permanent-loss behavior as a
    regression target for a future "pop after success" fix.
  - EX-10: after the injected exception, assert `self._thread.is_alive()` is `False` and
    `pending_count()` for a SECOND, healthy order registered before the crash never changes
    again — documenting "no supervisor restarts the poller thread." A fix would add a
    supervisor (e.g. the heartbeat thread or a dedicated watchdog) that checks
    `poller._thread.is_alive()` and calls `poller.start()` again.

### 8.7 Reconciliation failure — `tests/failure/test_reconcile_lock_contention.py`

- **Mock**: two threads both calling `PositionReconciler.reconcile()` on the same instance,
  the first holding the lock via a `threading.Event` the test controls.
- **Assert**:
  - The second `reconcile()` call returns immediately (skip, not block) — existing behavior,
    regression guard.
  - (Documenting the gap) no counter/metric/log distinguishes "skipped due to contention"
    from "ran and found nothing to do" — a fix would add a `reconcile_skipped_total`-style
    counter and alert if skips exceed N in a row.
  - Extend `test_reconciler.py`'s `TestQtyMismatch` with a case where the broker
    `get_positions()` call raises mid-way through a multi-symbol reconcile — assert the
    *already-processed* symbols' repairs were committed (or rolled back — whichever the
    implementation does) rather than left in an undefined partial state.

### 8.8 Stale data — `tests/failure/test_stale_data_pipeline.py`

- See `docs/STALE_DATA_DETECTOR.md`'s own `SG-01..SG-07` insertion points for the
  loader/fusion layers. This audit's pipeline-specific addition:
- **Mock**: `_scan_and_trade()` fed a DataFrame whose index is a non-`DatetimeIndex` (e.g.
  plain integers), to hit the bare `except Exception: pass` at `indicator/strategy.py:103-118`.
- **Assert**: (documenting SD-09) the symbol is **not skipped** — i.e. the staleness check
  silently no-ops and the scan proceeds. A fix would `continue`/skip the symbol and log a
  warning when the staleness check itself fails, per `STALE_DATA_DETECTOR.md`'s SG-04
  proposal.

### 8.9 Duplicate event — `tests/failure/test_duplicate_fill_callback.py`

- **Mock**: call `_make_fill_callback`'s returned `on_filled` TWICE with the same `Order`
  object (simulating EX-02/EX-11's double-invocation), against a real `PositionTracker` and
  an in-memory DB session.
- **Assert**:
  - `_persist_fill`'s `existing_fill` dedup query prevents a second `Fill` row (existing
    behavior, regression guard for EX-04's app-level mitigation).
  - (Documenting FS-05) `PositionTracker.on_fill()` IS called twice and the in-memory
    `self._positions[symbol].qty` reflects a **double** application — assert this currently
    happens, as the regression target for adding a fill-id-level guard to
    `PositionTracker.on_fill()`.

### 8.10 Server restart — `tests/failure/test_create_all_drift.py`, `tests/failure/test_command_table_growth.py`

- **`test_create_all_drift.py`**:
  - **Mock**: create a SQLite/Postgres test DB, run `Base.metadata.create_all(engine)` against
    an OLDER version of the `Order` model (missing a column), then swap in the CURRENT model
    (with the column) and call `create_all(engine)` again.
  - **Assert**: (documenting FS-04) the column is **absent** from the live table — querying
    `Order.<new_column>` raises `ProgrammingError`/`OperationalError`. This is the regression
    target for adopting Alembic (`ROADMAP.md` P0-14).
- **`test_command_table_growth.py`**:
  - **Mock**: insert N `Command` rows with `status="processed"`, run
    `_enter_db_polling_mode`'s query-and-mark logic in a loop.
  - **Assert**: (documenting FS-03) row count is non-decreasing — regression target for
    adding a purge job (e.g. `DELETE FROM commands WHERE status != 'pending' AND
    processed_at < now() - interval '7 days'`, scheduled alongside the existing 5 jobs in
    `build_scheduler()`).

---

## §9 Operational Risk Level

**Overall assessment: MEDIUM-HIGH.** The order/position state-management core (recovery,
idempotency, reconciliation repair logic) is well-engineered and well-tested — F1, F5, and
EX-06 are all confirmed RESOLVED, and `test_recovery_safety.py`/`test_reconciler.py` give
real regression protection for the highest-value invariants (no duplicate fills, no
duplicate poller registration, broker-scoped recovery). The risk concentration is in
**infrastructure-boundary code that has never been exercised under failure**: Redis-down
cross-process interactions, schema migrations, and thread-crash recovery — none of which have
any test coverage (§5), and two of which (FS-01, FS-04) can independently halt live trading
or corrupt runtime behavior without any startup-time signal.

| Scenario | Risk Level | Justification |
|---|---|---|
| Redis down | **HIGH** | FS-01: self-healing at every layer that detects it directly, but the cross-process watchdog interaction converts a transient blip into a manually-resolved trading halt with a misleading alert. No test coverage. |
| Worker restart | MEDIUM | StartupRecovery is strong (F1/EX-06 resolved, well-tested); FS-02 (breaker reset) and the FS-01 heartbeat-gap interaction are the residual risk. |
| Process kill | MEDIUM | Correctly detected (heartbeat/watchdog); CA-03 repairs qty but loses the `Fill` audit row (CA-04) — financially self-correcting but audit-trail-incomplete. No test coverage. |
| Network timeout | MEDIUM | DO-01 (ghost orders) is the headline risk and is unchanged/confirmed; FS-07 (auth-endpoint timeout bypasses retry) is a narrower, lower-probability window. SD-04 is already tracked. |
| Broker API failure | MEDIUM | Breaker provides real protection but is silent (no alert on trip) and resets on restart (FS-02); DO-05 remains the deeper structural gap. |
| Polling failure | **HIGH** | EX-02 (confirmed, now also covering PARTIAL_FILLED) and EX-10 (confirmed, no thread supervisor) combine for up to ~1.5h of *total* silent fill-loss across *all* strategies with zero test coverage — among the largest single blast radii in this audit. |
| Reconciliation failure | MEDIUM | Core repair logic is well-tested; CA-03/CA-04 masking and EX-11's TOCTOU are known, tracked gaps; lock-contention-skip has no alerting. |
| Stale data | MEDIUM | Comprehensively tracked in `STALE_DATA_AUDIT.md` (SD-01..SD-13); SD-05's "alive-but-stale produces zero alerts" framing is the one item this audit adds emphasis to via the heartbeat cross-reference. |
| Duplicate event | MEDIUM | DB-layer dedup (idempotency key, `existing_fill`) is solid and tested; FS-05 (in-memory `PositionTracker` double-apply) is a real but bounded (≤30min) divergence window. |
| Server restart | **CRITICAL** | FS-04 (schema drift via `create_all()` no-op) is the highest-priority NEW finding in this entire audit — it is silent at startup, has no test, no migration framework, and the failure mode (runtime `ProgrammingError` on an arbitrary future code path) is the hardest of all findings here to diagnose after the fact. FS-03 (Command-table growth) is comparatively minor. |

### Why FS-01 and FS-04 are the top two priorities

- **FS-04** is rated CRITICAL because it is a **latent** defect: it does nothing wrong
  *today* (current schema matches current models), but the *next* schema-changing deploy
  will introduce silent drift with no detection until an arbitrary future runtime error —
  and by then, the connection between "that deploy" and "this error" may not be obvious. It
  affects **both** `kis-api` and `kis-worker` identically and has zero mitigation (no
  Alembic, no startup schema-diff check).
- **FS-01** is rated HIGH (bordering CRITICAL operationally) because it is the only finding
  in this audit where a **fully-recovered infrastructure blip** (Redis back up within
  minutes) results in a **persistent, manually-resolved production state** (trading halted
  via `kill_switch`) — and the alert text actively misdirects the on-call operator. Every
  other finding in §6 either self-heals, requires an *ongoing* underlying condition to keep
  causing harm, or has a bounded/small blast radius.

Both FS-01 and FS-04 directly affect the **live** `kis-worker`/`kis-api` processes (no `bot/`
involvement), have **no existing tracking ID** in any prior audit, and have **no test
coverage** — they are the two findings this audit recommends addressing first, ahead of
TASK 4-1B's broader harness.

---

## §10 Cross-References

| ID | Source doc | Status in this audit |
|---|---|---|
| F1 | `RECONCILIATION_ENGINE.md` | **RESOLVED** — `_apply_fill_to_position_db()` defined at `recovery.py:324-348`, called correctly at 266 |
| F5 | `RECONCILIATION_ENGINE.md` | **RESOLVED** — dedup + sessions-snapshot share one lock acquisition, `runner.py:316-345` |
| DO-01 | `IDEMPOTENT_EXECUTION.md` | confirmed present — `kis_adapter/client.py:56-96` POST retry |
| DO-05 | `IDEMPOTENT_EXECUTION.md` | confirmed present — `backend/brokers/kis.py:106-150`, no broker-side idempotency token |
| EX-02 | `ORDER_POLLING_RELIABILITY.md` | confirmed present, **extended** to PARTIAL_FILLED — `order_poller.py:130-165` |
| EX-04 | `ORDER_POLLING_RELIABILITY.md` | confirmed present — `database/models.py:48-55`, no UNIQUE on `Fill` |
| EX-06 | `ORDER_POLLING_RELIABILITY.md` | **RESOLVED** — shared poller + dict-overwrite + `_guarded_on_filled`, `recovery.py:234-239`, `order_poller.py:63,91`, `runner.py:679-701` |
| EX-10 | `ORDER_POLLING_RELIABILITY.md` | confirmed present — `order_poller.py:102-114`, no thread-crash supervisor |
| EX-11 | `ORDER_POLLING_RELIABILITY.md` | confirmed present (TOCTOU narrowed, not closed) — `reconciler.py:375-401` |
| CA-03 | `CORPORATE_ACTION_AUDIT.md` | confirmed present — `reconciler.py:206-231`, unconditional `qty_mismatch` overwrite |
| CA-04 | `CORPORATE_ACTION_AUDIT.md` | confirmed present — no corporate-action schema fields |
| SD-04 | `STALE_DATA_AUDIT.md` | confirmed present — `kis.py:300-317`, FX stale/hardcoded fallback |
| SD-05 | `STALE_DATA_AUDIT.md` | confirmed present — `heartbeat.py`, liveness ≠ freshness |
| SD-09 | `STALE_DATA_AUDIT.md` | confirmed present — `indicator/strategy.py:103-118`, `.days` truncation + bare except |
| DB-01 / P0-14 | `ROADMAP.md` (citing `AUDIT.md`) | confirmed present, restated as **FS-04** in this audit's pipeline-trace framing |

---

## §11 Future Work

1. **Immediate priorities (per §9): FS-01 and FS-04.**
   - FS-01: either make `HeartbeatMonitor.is_alive()` distinguish "Redis unreachable" from
     "key expired" (e.g. a separate Redis-health probe the watchdog checks before concluding
     "dead"), and/or have `_alert_recovery()` clear `DailyRiskState.kill_switch` when the
     `kill_reason` matches the heartbeat-specific string it itself wrote.
   - FS-04: adopt Alembic per `ROADMAP.md` P0-14, and add a startup schema-diff check (even a
     coarse "does each ORM-declared column exist" check) to `StartupRecovery` step 1 so drift
     is fatal-at-boot rather than silent-until-hit.
2. **EX-10 / EX-02 (polling, §3.6, §4 chain 5)** — add a poller-thread supervisor (the
   existing `WorkerHeartbeat` thread or a new lightweight one) that calls
   `poller._thread.is_alive()` and restarts the poller if it has died; reconsider the
   pop-before-callback ordering in `_poll_one`/`_handle_timeout` for both FILLED and
   PARTIAL_FILLED branches.
3. **FS-02 (circuit breaker persistence)** — persist `ConsecutiveFailureBreaker` state
   (Redis, mirroring the heartbeat/risk-counter pattern already used elsewhere) so a restart
   during an open-breaker cooldown does not silently re-arm trading against a still-failing
   broker.
4. **FS-05 (PositionTracker fill idempotency)** — add a fill-id/`(order_id, qty, price)`
   dedup check to `PositionTracker.on_fill()` itself, mirroring `_persist_fill`'s existing
   DB-level check, so in-memory and DB state cannot diverge even transiently.
5. **FS-07 (auth-endpoint retry)** — wrap `KISAuth.get_hashkey()`/`_issue_token()` in their
   own short retry (or move `get_headers()`/`get_hashkey()` computation inside
   `KISClient`'s existing retry loop) so an auth-endpoint timeout doesn't fail the whole
   call with zero retries.
6. **FS-03 (Command table retention) / FS-06 (duplicate watchdogs)** — low-urgency cleanup:
   add a `Command`-table purge job to `build_scheduler()`; consider gating
   `WorkerWatchdog.start()` to only the first gunicorn worker (e.g. via a Redis lock or
   `SERVER_SOFTWARE`/worker-index check in `post_fork`).
7. **EX-11 (`is_registered()` guard)** — implement the originally-proposed
   `OrderFillPoller.is_registered(order_id)` and wire it into
   `PositionReconciler._sync_order_status()` before any fill insert, fully closing the TOCTOU
   window narrowed (but not closed) by the existing `existing_fill` dedup query.
8. **TASK 4-1B** — build the failure-injection harness sketched in §8, prioritizing §8.1
   (Redis down) and §8.10 (`create_all` drift) given §9's risk ranking.

---

## §12 Verification

### Re-check commands

```bash
# F1 resolved — _apply_fill_to_position_db is defined and called
grep -n "_apply_fill_to_position_db" backend/worker/recovery.py

# EX-06 resolved — shared poller + dict-keyed register with overwrite
grep -n "_shared_poller\|_entries\[order.id\]" backend/worker/recovery.py backend/execution/order_poller.py

# FS-01 — heartbeat/watchdog conflation + no kill_switch clear on recovery
grep -n "is_alive\|kill_switch\|_alert_recovery" backend/worker/heartbeat.py backend/worker/scheduler.py

# FS-02 — circuit breaker is in-memory only
grep -n "self\._failures\|self\._tripped_at\|redis" backend/execution/circuit_breaker.py

# FS-03 — Command table has no TTL/purge
grep -n "class Command" -A 10 backend/database/models.py
grep -rn "DELETE FROM commands\|Command).*delete\|purge" backend/worker/

# FS-04 — create_all is the only schema mechanism, no Alembic
grep -n "create_all\|pool_size" backend/database/models.py
find . -iname "*alembic*" -not -path "./node_modules/*" -not -path "./mobile/*"

# FS-05 — PositionTracker.on_fill has no fill-id dedup
grep -n "def on_fill" -A 40 backend/execution/position_tracker.py | grep -i "existing\|dedup\|fill_id"

# FS-06 — GUNICORN_WORKERS default + per-worker watchdog
grep -n "GUNICORN_WORKERS\|WorkerWatchdog" backend/api/gunicorn_conf.py

# FS-07 — get_hashkey/get_headers outside retry loop, _issue_token no retry
grep -n "get_hashkey\|get_headers\|MAX_RETRIES\|for attempt" kis_adapter/client.py
grep -n "def _issue_token\|def get_hashkey" -A 10 kis_adapter/auth.py
```

### Final deliverable checklist

- [x] `docs/FAILURE_SCENARIO_AUDIT.md` exists, ~1,160 lines, no "TBD"/placeholder content.
- [x] All 7 analysis items addressed: test coverage (§5), failure-handling paths (§3),
      blast radius (§3, §4), silent propagation (§3 "Silent Propagation?" column, §4),
      missing recovery (§3 "Recovery Status" column), affected modules (§7), operational
      risk level (§9).
- [x] All 10 named failure scenarios have a §3 subsection with the 7-column table
      (Detection / Handling / Blast Radius / Silent Propagation? / Recovery Status / Test
      Coverage / Cross-Refs).
- [x] §4 has 5 numbered propagation chains, each with file:line + finding ID at every step.
- [x] §6 risk table includes confirmed-present existing IDs (DO-01, DO-05, EX-02, EX-04,
      EX-10, EX-11, CA-03, CA-04, SD-04, SD-05, SD-09), RESOLVED IDs (F1, F5, EX-06), and
      7 new `FS-01`..`FS-07` IDs — none collide with existing `DO-`/`EX-`/`F`/`CA-`/`SD-`
      prefixes.
- [x] §8 gives concrete test-file insertion points (10 proposed files/paths) with
      mock/assert detail per scenario.
- [ ] Commit `docs/FAILURE_SCENARIO_AUDIT.md`, push to `claude/trading-platform-philosophy-yNHQK`,
      open a draft PR.
- [x] No `.py` files created or modified — this audit is documentation-only.

### 3.5 Broker API failure

| Detection | Handling (file:line) | Blast Radius | Silent Propagation? | Recovery Status | Test Coverage | Cross-Refs |
|---|---|---|---|---|---|---|
| `ConsecutiveFailureBreaker.is_open()` (`circuit_breaker.py:38-49`); per-call try/except | `kis.py:48` (`threshold=5, cooldown_minutes=10`); breaker-open short-circuit → REJECTED; `place_order` try/except→REJECTED (106-150); `get_order_status` try/except→`None` (193-283) | Breaker open ⇒ **all** orders REJECTED for the cooldown window; `get_order_status`→`None` ⇒ poller entry simply isn't updated this cycle | Breaker-trip is logged via `logger.error` at the threshold-crossing transition only (`circuit_breaker.py:27-30`) — no Telegram/WS alert | Breaker auto-closes after cooldown elapses (`circuit_breaker.py:38-47`); **FS-02** — breaker state lost on restart | None — no `test_circuit_breaker.py` | DO-05 (**CONFIRMED PRESENT**), **FS-02 (NEW, cross-ref from §3.2)** |

`place_order()` (`kis.py:106-150`) has a special case for `MarketClosedError` (138-144) that
deliberately does **not** increment the breaker's failure counter — a market-closed rejection
is expected/benign and should not contribute toward tripping the breaker. All other
exceptions call `record_failure()`. Two gaps:

1. **No alert on breaker trip.** `record_failure()` (`circuit_breaker.py:23-30`) logs at
   `logger.error` only when the failure count crosses `threshold` and transitions
   `_tripped_at` from `None` to a value — there is no Telegram/WS notification that trading
   has been automatically paused for the cooldown window (10 min for `KISBroker`, 30 min for
   `IndicatorStrategy`'s own breaker at `indicator/strategy.py:51`). An operator watching
   Telegram would not know the platform stopped placing orders for up to 30 minutes unless
   they are also tailing logs.
2. **`get_order_status()` returning `None`** (`kis.py:193-283`, per-market try/except) is
   consumed by `OrderFillPoller._poll_one()` — a `None` result means "no update this poll
   cycle," so the entry remains registered and is retried on the next 5s tick. If the broker
   API is failing *persistently* (not just one call), repeated `None` results accumulate
   until `entry.is_timed_out` triggers `_handle_timeout()` (`order_poller.py:170-178`,
   pop-before-callback — see §3.6). DO-05 (no broker-side idempotency token on
   `place_order`) remains present unchanged from `IDEMPOTENT_EXECUTION.md`.

### 3.6 Polling failure

| Detection | Handling (file:line) | Blast Radius | Silent Propagation? | Recovery Status | Test Coverage | Cross-Refs |
|---|---|---|---|---|---|---|
| None for thread-crash; per-order `get_order_status` try/except→`None` for broker errors | `order_poller.py:102-114` (`_loop`, no top-level try/except); `_poll_one` (116-168) | FILLED/timeout: pop-before-callback → permanent fill/timeout-handling loss if callback raises. Thread crash: **all** registered orders stop polling silently | **Yes**, both cases | None for either case — masked downstream by `_periodic_reconcile`'s `lost_order` check (age > 1h) | None — no `test_order_poller.py` | EX-02 (**CONFIRMED PRESENT**, extended), EX-10 (**CONFIRMED PRESENT**), CA-03/CA-04 (masking) |

Two independent gaps, both **confirmed present** in current code:

1. **EX-02 — pop-before-callback, extended to the PARTIAL_FILLED path.** The FILLED case
   (`_poll_one`, 130-144) pops `entry` from `self._entries` at line 137 **before** calling
   `entry.on_filled(updated)` at line 140; the `try/except` around the callback (142) only
   logs — if `on_filled` raises, the fill is never retried (the original EX-02 finding,
   confirmed unchanged). The **PARTIAL_FILLED** case (151-165) does not pop the entry, but
   sets `entry.last_reported_qty = updated.filled_qty` (154) **before** calling
   `on_filled(partial)` (161-164) — if that callback raises, the *next* poll computes the
   incremental fill qty as `updated.filled_qty - entry.last_reported_qty`, which is now
   `0` (since `last_reported_qty` was already advanced), so the lost partial-fill increment
   is never re-delivered. This is the same silent-loss shape as EX-02 via a different field
   (`last_reported_qty` vs. dict removal) — it should be tracked as **part of EX-02's scope**
   in any future fix (not a new ID), but this audit notes it explicitly because a fix that
   only reorders the FILLED branch (pop *after* callback) would not address the
   PARTIAL_FILLED branch. `_handle_timeout()` (170-178) has the identical pop-before-callback
   shape (173-174 pop, 175-178 try/except-log).
2. **EX-10 — no poller-thread crash recovery, confirmed present.** `_loop()`
   (`order_poller.py:102-114`) runs in a daemon thread (`order-poller`, started via
   `start()`, 68-71) with **no top-level try/except** around the per-tick body. `_poll_one`
   and `_handle_timeout` each have internal try/except around their callback invocations, but
   any exception raised *outside* those — e.g. in computing `due = [e for e in
   self._entries.values() if e.next_poll_at <= now]` (105-106), or in
   `entry.is_timed_out` (109) — would propagate out of `_loop`, silently terminating the
   `order-poller` thread (Python prints a traceback to stderr but the process continues
   running). No code anywhere calls `self._thread.is_alive()` to detect or restart a dead
   poller thread. From that point on, **every** registered order — across every active
   strategy — stops receiving fill/timeout callbacks. The only detection is
   `_periodic_reconcile`'s `lost_order` check (`reconciler.py:334-340`, age > 1h), which runs
   at most every 30 minutes during market hours — i.e. up to ~1.5h of total silent fill loss
   for *every in-flight order* before any signal fires, and `lost_order` itself is an
   audit/log entry, not a poller-thread restart.
