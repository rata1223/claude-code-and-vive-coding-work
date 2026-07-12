# P4-01A — Operational Readiness Audit

**Scope**: audit only. No runtime behavior, features, or architecture were changed to produce this document.
**Baseline (authoritative, not re-litigated)**: `docs/PAPER_TRADING_CERTIFICATION.md` (P3-03B) and `docs/P3_04_LIVE_READINESS.md` (P3-04).
**Question this document answers**: given P3-04's certified trading/execution stack, is the surrounding operational scaffolding (startup, health, restart, secrets, config, monitoring) ready to run that stack unattended for an extended KIS paper-trading window?

---

## 1. Executive summary

P3-04 issued a **CONDITIONAL "ready for limited live"** verdict for a single-process, small-capital, `KIS_ENV=paper` deployment, with four accepted residual risks (R1, R3, R5, R7 — restated in §5). This audit does not revisit that verdict. It finds the *operational* layer around the certified stack is **mostly ready with two HIGH-severity gaps** that specifically affect an *extended, largely unattended* run: no Docker-level healthcheck/auto-restart on the `kis-worker` container, and no ongoing external monitoring of the one endpoint (`/api/metrics`) that actually reports real health. Everything else is MEDIUM/LOW polish, not a blocker.

**Documentation drift noted**: `CLAUDE.md` states PR #116 (P3-02C-B) is "in progress" with 3 files conflicting against #109. `git merge-base --is-ancestor` against current `main` (HEAD past PR #123) confirms #119, #121 (P3-02C-B/D), #122 (P3-03B), and #123 (P3-04) are all already merged, and the reconciler→poller `resync()` routing described as pending in CLAUDE.md is live on `main` (`backend/execution/reconciler.py:427-453` → `backend/execution/order_poller.py:307`). This is a documentation-accuracy issue only — `CLAUDE.md` should be refreshed in a follow-up, but it doesn't affect runtime readiness.

---

## 2. Runtime dependency graph

Two engine implementations exist in the repo; only one is live.

- **Legacy** (`bot/main.py` + `bot/scheduler.py`): explicitly disabled — the `kis-bot` service in `docker-compose.yml` is commented out (lines ~80-109), with an in-file warning that running it alongside `kis-worker` would double-submit orders on the same KIS account.
- **Live path**: three separate Docker services — `kis-api` (Flask + gunicorn, `backend/api/server.py`), `kis-worker` (`backend/worker/runner.py`), `kis-ws` (`backend/websocket/`). All findings below are for this path.

```
KIS Paper API
  └─ kis_adapter/auth.py:24-63 (token, Redis cache + in-mem fallback)
       └─ Market Data: kis_adapter/market_data.py, rate-limited (client.py:11-30, 5/s paper · 15/s real),
                        retried (MAX_RETRIES=3), circuit-breaker guarded (backend/execution/circuit_breaker.py)
            └─ Scheduler: backend/worker/scheduler.py:215-262 (cron: KR 09:05 KST, US 09:30 America/New_York DST-safe,
                           daily risk reset 06:01 KST, equity snapshot 23:50 KST, reconcile every 30min in session)
                 └─ Worker: backend/worker/runner.py:821-876 main()
                      ├─ StartupRecovery (backend/worker/recovery.py, 8-step gate, see §4)
                      ├─ Strategy: backend/strategy/base.py:43 StrategyBase → IndicatorStrategy / ScriptStrategy
                      │    └─ SignalFusion: backend/quant/signals/fusion.py, called live inside
                      │                      IndicatorStrategy.on_bar (not backtest-only)
                      │    └─ RiskEngine gate: SAFE_MODE.can_trade checked in strategy/base.py before place_order
                      │                        (backend/quant/risk/engine.py — daily/MDD/trailing enforcement)
                      └─ Execution: backend/execution/order_machine.py (full state transition table)
                           └─ BrokerAdapter: backend/brokers/kis.py (paper/real TR_ID switch),
                                             semantic_mapper.py, validator.py
                                └─ Order Poller: backend/execution/order_poller.py
                                                  (10→30→60→120→300s backoff, 30min auto-cancel,
                                                   PollingHealth trips after 10 consecutive errors)
                                     └─ PositionTracker: backend/execution/position_tracker.py
                                          └─ Reconciliation: backend/execution/reconciler.py:427-453
                                                              → OrderFillPoller.resync() (P3-02C-B/D, merged)
                                               └─ Audit: backend/database/models.py
                                                          (Order/Fill/StrategyRun/AuditLog/ReconciliationLog,
                                                           live-written from runner.py/reconciler.py)
                                                    └─ Performance: EquitySnapshot, written live from scheduler.py
```

### Stage-by-stage verdicts

| Stage | Verdict | Evidence |
|---|---|---|
| KIS Paper Auth | **WIRED** | `kis_adapter/auth.py:24-63`; `runner.py:825-841` fails fast (`sys.exit(1)`) if `KIS_ENV=real` + `ENABLE_LIVE_TRADING=false` (prevents silent live no-op). No dedicated auth pre-flight distinct from the first live balance call in `recovery.py` step 4 — see §5. |
| Market Data | **WIRED** | `kis_adapter/market_data.py`, `client.py:11-30` rate limiter, `MAX_RETRIES=3`, `backend/execution/circuit_breaker.py` (`ConsecutiveFailureBreaker`, threshold 5 / cooldown 10min). |
| Scheduler | **WIRED, single-instance only** | `backend/worker/scheduler.py:215-262`; `max_instances=1, coalesce=True` is in-process dedup only — no cross-container lock. Safe today because topology is one `kis-worker` container by convention, not enforcement. |
| Worker | **WIRED** | `runner.py:821-876`; `StartupRecovery` gates `SAFE_MODE.can_trade` until 8 steps pass; `_restore_active()` (`runner.py:367-386`) replays in-flight `StrategyRun` rows; pending orders re-registered with idempotency guard `_guarded_on_filled` (`runner.py:737-757`). |
| Strategy | **WIRED**, not aspirational | `backend/strategy/base.py:43` `StrategyBase(ABC)` — `on_start/on_bar/on_fill/buy/sell` all implemented and called; `IndicatorStrategy`/`ScriptStrategy` both concretely invoked via `runner.py:447-461`. |
| SignalFusion | **WIRED** | `backend/quant/signals/fusion.py`; `IndicatorStrategy.on_bar` (`indicator/strategy.py:82-116`) builds `default_fusion()` and calls `evaluate()` per bar — this is the live signal source, shared with the offline backtest engine (same interface, no divergence). |
| RiskEngine | **PARTIAL** | `backend/quant/risk/engine.py` (`RiskConfig`: daily 3% / MDD 15% / trailing 7% / max position 5%, matches spec). `PersistentLossTracker` dual-writes peak equity to Redis+Postgres and restores it on boot. Enforced via a global `SAFE_MODE` flag checked before `place_order` — **not** a per-call `risk.check()`. A second, newer mechanism, `backend/risk/kill_switch.py` (`KillSwitch`/`TradingState`), is a fully-built, independently-tested standalone module whose own docstring states wiring it into `place_order()`/reconciliation "is a deliberate follow-up task" — it is not yet connected, so two halt mechanisms exist in the codebase (one live, one dormant). |
| Execution / order state machine | **WIRED** | `backend/execution/order_machine.py` (PENDING→SUBMITTED→{PARTIAL_FILLED,FILLED,CANCELED,REJECTED,EXPIRED,UNKNOWN}); `order_poller.py` handles broker terminal events (cancel/reject/expire, PR #109, confirmed merged). |
| BrokerAdapter | **WIRED** | `backend/brokers/base.py` ABC, `kis.py` (paper/real TR_ID switching keyed off `KIS_ENV`), `semantic_mapper.py`, `validator.py` (`BrokerCapabilityValidator`). No broker-layer dry-run flag independent of `KIS_ENV` — the `SAFE_MODE`/`ENABLE_LIVE_TRADING` gate at the strategy layer is the real protection against accidental live orders. |
| Order Poller | **WIRED** | Included above; `PollingHealth.is_healthy` trips to `logger.critical` after 10 consecutive poll errors (no external alert on this specific condition — see §5). |
| PositionTracker | **WIRED** | `backend/execution/position_tracker.py` — fill→position updates, restore-on-restart. |
| Reconciliation | **WIRED, merged (not "in progress")** | `reconciler.py:427-453` `_sync_order_status()` routes broker-discovered fills through `OrderFillPoller.resync()` — this is the P3-02C-B/D work (PRs #119/#121), confirmed merged to `main`, contradicting the "in progress" note in `CLAUDE.md`. |
| Audit | **PARTIAL** | `backend/database/models.py` defines `Trade`, `Order`, `Fill`, `StrategyRun`, `EquitySnapshot`, `Position`, `AuditLog`, `ReconciliationLog`, `DailyRiskState`, `CorporateAction*` — all live-written except `Trade`: the ORM class is never imported/written in the live path (`runner.py`, `reconciler.py`); a same-named dataclass in `backend/quant/backtest/engine.py` is unrelated. Live trade history is fully reconstructable from `orders` + `fills`, but any tooling that queries the `trades` table directly will find it empty. |
| Performance | **PARTIAL** | `EquitySnapshot` rows written live from `backend/worker/scheduler.py` (23:50 KST daily), equity pulled from `get_kis_broker().get_balance()` in `runner.py`. Operational, not backtest-only — but there is no live-facing performance dashboard/report beyond raw DB rows and `/api/metrics`'s `daily_pnl_pct`. |

**CorporateActionService**: confirmed live-wired — `backend/data/corporate_action_runtime.py:52`, instantiated from both `recovery.py` and `runner.py`.

**PaperBroker vs. actual KIS paper deployment**: `ScriptedPaperBroker` (`backend/brokers/paper_broker.py`, PR #99/#102) is a **test-only harness** — never instantiated by `runner.py`, only by `backend/testing/paper_harness.py` and tests. The real extended deployment runs `KISBroker` against KIS's actual paper sandbox, through the *same* execution/poller/reconciler stack as real trading (a genuine strength — no live/paper code fork at runtime). The implication: P3-03B/P3-04's scripted-scenario certification validates the stack's logic offline; it does not by itself validate the live KIS paper endpoint's actual behavior (latency, rate-limit edges, real market microstructure) — that validation is exactly what the upcoming extended paper run is for.

---

## 3. Deployment prerequisites

### Docker Compose services

| Service | Restart policy | Healthcheck |
|---|---|---|
| `postgres` | `unless-stopped` | `pg_isready` |
| `redis` | `unless-stopped` | `redis-cli ping` |
| `frontend` | `unless-stopped` | none |
| `api` (FastAPI, port 8000) | `unless-stopped` | `curl -f localhost:8000/health` |
| `kis-api` (Flask/gunicorn, port 5001) | `unless-stopped` | `curl -f localhost:5001/api/health` |
| `kis-worker` | `unless-stopped` | **none** |
| `kis-ws` | **none (no `restart:` line at all)** | none |
| `kis-bot` (legacy) | commented out | — |

`kis-worker` has crash-restart via `unless-stopped` but no Docker-level liveness probe, so Docker itself cannot distinguish "running fine" from "running but hung." Hang detection for `kis-worker` instead comes from a **separate, already-wired cross-process mechanism** — see §4.

### Health endpoints

- `backend/api/server.py:93-95` `GET /api/health` (kis-api) — returns `{"status":"ok"}` unconditionally, no real check. This is what the Docker healthcheck and `deploy.yml`'s initial post-deploy probe hit.
- `GET /health` (FastAPI `api` service, port 8000) — likewise the compose healthcheck target for that service; not independently deep-checked in this audit.
- `backend/api/server.py:379-414` `GET /api/metrics` — the endpoint with **real** checks: `redis_ok`, `db_ok` (`SELECT 1`), `worker_alive`/`worker_ttl_seconds` (via `HeartbeatMonitor`), `pending_orders`, `open_positions`, `kill_switch`, `daily_pnl_pct`. `deploy.yml` polls this once after deploy. **Nothing polls it on an ongoing basis** during the run itself (see §5).

### Environment variables

`.env.example` documents all KIS credentials (`KIS_APP_KEY/SECRET/ACCOUNT_NO/ENV/HTS_ID`), `ENABLE_LIVE_TRADING`, Kiwoom stubs, API-server security vars (`KIS_CREDENTIAL_KEY`, `JWT_SECRET_KEY`, `JWT_EXPIRE_MINUTES`, `QUANTDINGER_SECRET_KEY`), CORS, Telegram, DB/Redis URLs, and risk limits (`DAILY_LOSS_LIMIT_PCT/MDD_LIMIT_PCT/STOP_LOSS_PCT`) — comprehensive.

- **Fail-fast (compose-level `${VAR:?...}` guards)**: `POSTGRES_PASSWORD`, `JWT_SECRET_KEY`, `KIS_CREDENTIAL_KEY`, `QUANTDINGER_SECRET_KEY` — compose refuses to start without these, matching `.env.example`'s own "all required — refuses to start if unset" note for that section.
- **No fail-fast guard**: `KIS_APP_KEY` / `KIS_ACCOUNT_NO` — first failure is a bare `os.environ["KIS_APP_KEY"]` `KeyError` inside `kis_adapter/auth.py:26` at first use, not a clear boot-time compose error.
- **Used in code, undocumented in `.env.example` (all have safe defaults, so not fail-fast risks, just an ops-runbook gap)**: `BROKER_STARTUP_TIMEOUT` (default 30s, `recovery.py:26`), `RECOVERY_STALE_ORDER_HOURS` (default 24h, `recovery.py:27`), `GUNICORN_WORKERS` (default 2), `API_PORT` (default 5001), plus a handful of feature flags (`ALLOW_AFTERHOURS_ORDERS`, `ALLOW_PREMARKET_ORDERS`).

### Database migrations

Alembic exists (`alembic/versions/`: initial schema → corporate-action tables) and is exercised in CI (`ci-postgres.yml` runs an upgrade/downgrade/upgrade round-trip). **No Dockerfile/entrypoint runs Alembic automatically.** Production schema bootstrap is `Base.metadata.create_all(engine)` (`backend/database/models.py:199`) inside `init_db_factory`, which creates missing tables on every process start but does not apply *alterations* to existing tables — any future migration must be run manually against the deployed DB.

### Secrets

`.env`/`.envrc` are gitignored; no committed secrets found in tracked compose/scripts. One historical, still-open item: `mobile/signing/README.txt` documents a previously-committed Android signing key (removed in PR #110/#114) and states the keystore/passwords "must be treated as compromised," directing regeneration via Play App Signing — rotation is not evidenced in-repo. This is a mobile-release hygiene item, unrelated to the KIS paper-trading runtime itself.

---

## 4. Operational safety

**Startup validation**: `StartupRecovery.run()` (`backend/worker/recovery.py:85-108`) — 8 steps (DB connect → Redis connect → risk-state restore via `PersistentLossTracker` → broker balance → broker positions → DB/broker position reconcile → pending-order re-registration into `OrderFillPoller` → enable trading). `SAFE_MODE.can_trade` stays `False` until all 8 pass. Reconnect-safety (`pool_pre_ping=True`, non-fatal Redis degradation) is certified in P3-04 and covered by `tests/postgres/test_restart_recovery_p3_04.py`.

**Restart behavior**: `kis-worker` restarts via Docker's `unless-stopped` policy on crash; `StartupRecovery` re-runs on every boot and recovers pending orders / in-flight strategy runs, per the certified restart-recovery test above.

**Hang detection (not just crash detection)** — more thorough than initially assumed, and worth stating precisely:
- A cross-process **`WorkerHeartbeat`** (`backend/worker/heartbeat.py:19-48`) refreshes a Redis TTL key every 30s from inside `kis-worker` (`runner.py:198`).
- A separate **`WorkerWatchdog`** runs *inside the `kis-api` process*, started via gunicorn's `post_fork` hook (`backend/api/gunicorn_conf.py:29-40`) and also from `server.py:425` — it polls that Redis key every 60s. If the heartbeat goes stale (90s TTL), it writes `DailyRiskState.kill_switch=True` to Postgres (the only cross-process channel available), and fires a Telegram alert + WebSocket alert.
- **What this buys**: a genuinely hung (not just crashed) `kis-worker` process is detected within ~90-150s, new strategy starts are blocked via the DB-level kill switch, and operators are alerted.
- **What it does not buy**: nothing restarts the hung `kis-worker` container itself — Docker has no liveness probe on it, so a process that's alive-but-stuck keeps consuming resources and holding any in-flight order state until a human intervenes. The kill switch stops *new* strategy activity but does not itself terminate the hung process or flatten open positions.
- **Minor duplicate-alert risk**: `GUNICORN_WORKERS` defaults to 2, and `post_fork` starts one `WorkerWatchdog` thread per gunicorn worker — i.e., two independent watchdog threads poll the same Redis key and could both fire Telegram/WebSocket alerts on the same dead-worker event (the DB write itself is guarded by `if not row.kill_switch`, so no double state-corruption, just noisy duplicate notifications).
- A **separate, more granular** module, `backend/worker/watchdog.py` (component-level: which thread/poller/scheduler hung, not just "worker is dead"), is fully built and tested (`docs/WATCHDOG_SYSTEM.md`, 53 tests) but its own docstring states wiring it into `runner.py` "is a deliberate follow-up task." Its absence means an alert says "worker is dead" but not "why," which slows diagnosis but does not remove the safety net above.

**Recovery path**: pending-order re-registration on restart uses an idempotency guard (`_guarded_on_filled`, `runner.py:737-757`) to avoid double-processing a fill discovered both by the poller and by reconciliation.

**Audit coverage**: see the Audit row in §2 — comprehensive except for the unused `Trade` table.

---

## 5. Remaining deployment blockers

| Severity | Item |
|---|---|
| **HIGH** | `kis-worker` has no Docker-level healthcheck/liveness probe, and the cross-process `WorkerWatchdog` only halts *new* trading + alerts on a hung worker — it does not restart the container or flatten positions. For an extended, largely-unattended run, a hung (not crashed) worker requires manual intervention to actually recover, even though it's detected. |
| **HIGH** | No ongoing external monitoring polls `/api/metrics` (the endpoint with real `redis_ok`/`db_ok`/`worker_alive`/`kill_switch` checks) during the run — only `deploy.yml` hits it once, immediately post-deploy. `/api/health` (unconditional 200) is what Docker/uptime tooling would naturally watch instead, and would report healthy even during a DB outage or kill-switch trip. |
| **MEDIUM** | Two parallel risk-halt mechanisms exist in the codebase: the live `SAFE_MODE` flag / `DailyRiskState.kill_switch` (used by `PersistentLossTracker` and `WorkerWatchdog`) and the dormant `backend/risk/kill_switch.py` `KillSwitch` class (built, tested, explicitly not wired). Not a runtime bug today, but a maintenance/confusion risk, and directly related to P3-04's already-accepted R7 (no auto-liquidation on kill-switch trip). |
| **MEDIUM** | `KIS_APP_KEY` / `KIS_ACCOUNT_NO` have no compose-level `:?` fail-fast guard (unlike the other required secrets), so a missing credential surfaces as a runtime `KeyError` on first API call rather than a clear boot-time error. |
| **MEDIUM** | Schema alterations after initial deploy are untracked in production — `create_all()` bootstraps missing tables but Alembic is never invoked automatically, so a future migration requires a manual, easy-to-forget step. |
| **MEDIUM** | No cross-container scheduler lock (`max_instances=1` is in-process only) — safe under the current single-`kis-worker`-container convention, but not structurally enforced. |
| **LOW** | `kis-ws` has no `restart:` policy at all in `docker-compose.yml` — a crash silently drops real-time push to the mobile app until manually restarted (UX degradation, not a trading-safety issue). |
| **LOW** | `Trade` ORM table is defined but never written by the live path (orders + fills fully cover the audit trail); any tooling that queries `trades` directly will find it empty. |
| **LOW** | `CLAUDE.md` is stale about merged-PR state (says #116/P3-02C-B is "in progress"; it and #121/#122/#123 are merged). Documentation-only, no runtime impact. |
| **LOW** | No CI job simulates a full live-shaped paper-trading day; existing coverage is unit/integration tests plus P3-03B/P3-04's scripted-scenario harness, not an end-to-end smoke run against the real timing/scheduling path. |
| **LOW** | Two `WorkerWatchdog` threads run concurrently (one per gunicorn worker, default `GUNICORN_WORKERS=2`), which can produce duplicate dead-worker alerts (not duplicate harmful actions). |
| **Informational** (carried forward from P3-04, not re-analyzed here) | R1 — limit-sell flatten fill risk in fast markets; R3 — `_FLATTEN_LOCK` is in-process only, no cross-process lock; R5 — flatten orders are fire-and-forget, not poller-registered; R7 — kill switch halts new trading but does not auto-liquidate. See `docs/P3_04_LIVE_READINESS.md` §6 for full detail. |

No **CRITICAL** items were found — nothing blocks starting the extended paper-trading window itself; the HIGH items are about *unattended* operation over its full duration, not about the trading logic's correctness (which P3-03B/P3-04 already certified).

---

## 6. Operational risks

- **Unattended-hang blind spot**: over a multi-week run, the realistic failure mode isn't "process crashes" (already handled by `unless-stopped` + `StartupRecovery`) but "process hangs while technically alive." That's detected (§4) and halts *new* trading, but recovering it — restarting the container, verifying no orphaned pending orders — is a manual, on-call action today. Without someone actually watching Telegram alerts or `/api/metrics`, this blind spot is effectively unaddressed.
- **Config-error blast radius**: a bad or expired `KIS_APP_KEY`/`KIS_ACCOUNT_NO` fails at first live API call, not at boot — during an unattended run this could mean the worker starts, passes `StartupRecovery` steps that don't touch the broker deeply enough to catch it, and only fails once the market session begins.
- **Silent schema drift**: if a future change needs a real migration (not just a new table), nothing in the deploy path applies it automatically — an operator has to remember to run Alembic by hand against the live DB, or the deploy will run against a stale schema.
- **Kill-switch is a soft stop, not a circuit breaker**: per R7 (P3-04, accepted), tripping the kill switch prevents *new* strategy actions but does not flatten existing positions — during an extended run this means a triggered risk breach still requires a human to manually flatten, same as P3-04 already flagged for the shorter validation window.
- **Mobile signing-key rotation**: unrelated to trading but a real, still-open item — the previously-leaked Android keystore is documented as compromised with no evidenced rotation, worth closing out during any period of active repo hardening.

---

## 7. Startup checklist (pre-launch, for an operator)

1. `.env` populated: all `KIS_*` credentials, `ENABLE_LIVE_TRADING=false` initially (per `.env.example`'s own guidance — flip only after the validation window), `KIS_ENV=paper`, `POSTGRES_PASSWORD`/`JWT_SECRET_KEY`/`KIS_CREDENTIAL_KEY`/`QUANTDINGER_SECRET_KEY` all set (compose will refuse to start otherwise).
2. Confirm Alembic is at `head` against the target Postgres instance before first boot (`alembic upgrade head` — not automatic).
3. `docker compose up -d --build`; confirm `postgres`, `redis`, `api`, `kis-api` report healthy via `docker compose ps` (they have real healthchecks); `kis-worker`/`kis-ws` will only show "running," not "healthy" — check logs directly.
4. Manually curl `GET /api/metrics` on `kis-api` and confirm `redis_ok: true`, `db_ok: true`, `worker_alive: true`, `kill_switch: false` before considering the deployment live.
5. Confirm Telegram alerting actually reaches the operator (trigger a test alert path, or wait for the next scheduled heartbeat check) — this is the only channel that will report a hung worker.
6. Set up ongoing (not just at-deploy) monitoring against `/api/metrics`, not `/api/health` — the latter cannot detect the failure modes that matter here.
7. Only after the above, and only after the 4-week validation window per `.env.example`, consider `ENABLE_LIVE_TRADING=true` / `KIS_ENV=real` — out of scope for this audit, governed by P3-04's conditions.

---

## Report

### 1. Deployment blockers
No CRITICAL blockers. Two HIGH items specific to *extended, unattended* operation: (a) no Docker-level liveness probe or auto-restart-on-hang for `kis-worker` — hang is detected and halts new trading via the already-wired cross-process `WorkerWatchdog`, but recovery is manual; (b) no ongoing monitoring of `/api/metrics` (the endpoint with real health signals) after initial deploy. Five MEDIUM items (dormant duplicate kill-switch module, missing fail-fast guard on KIS credentials, untracked schema migrations, no cross-container scheduler lock enforcement) and several LOW items (see §5 table) round out the list — none block starting the run, all are worth closing before or during it.

### 2. Operational risks
Unattended-hang recovery is manual even though detection exists; a bad KIS credential fails late (first live call) rather than at boot; schema drift requires a human to remember Alembic; the kill switch is a soft stop that halts new trades but doesn't auto-flatten (this mirrors P3-04's already-accepted R7, not a new finding); mobile signing-key rotation remains open and undocumented as resolved.

### 3. Recommended implementation scope (future task, not part of this audit)
A follow-up P4-01B could: add a `kis-worker` Docker healthcheck (e.g., check the same Redis heartbeat key used by `WorkerWatchdog`) paired with a restart action, not just detection; wire the fine-grained `backend/worker/watchdog.py` into `runner.py` per its own documented follow-up plan; add compose-level `:?` fail-fast guards for `KIS_APP_KEY`/`KIS_ACCOUNT_NO`; stand up ongoing `/api/metrics` monitoring/alerting outside the deploy-time check; and either wire `backend/risk/kill_switch.py` into the order path or remove it to avoid two competing halt mechanisms. This audit does not implement any of these — it only scopes them for prioritization.
