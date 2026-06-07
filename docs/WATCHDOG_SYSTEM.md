# Worker Watchdog & Process Health Monitoring — System Design

**Status:** Implemented (standalone library) — not yet wired into `runner.py`.
**Module:** `backend/worker/watchdog.py`
**Tests:** `backend/worker/tests/test_watchdog.py` (53 tests)
**Task:** TASK 2-4A — Phase P0 Operational Survivability

---

## 1. Current structure

Before this task, worker process health was observable only through coarse,
disconnected signals:

| Component | File:line | Granularity | Watched by |
|---|---|---|---|
| `WorkerHeartbeat` | `backend/worker/heartbeat.py:19-48` | Process-level only — one Redis key `worker:heartbeat`, 30s interval / 90s TTL | `WorkerWatchdog` (cross-process) |
| `WorkerWatchdog` | `backend/worker/heartbeat.py:86-177` | Polls that single Redis key every 60s from the **API process**; on expiry sets `DailyRiskState.kill_switch=True` + Telegram/WebSocket alert | Nothing — it *is* the watcher, but it is blind to *why* the worker died |
| `PollingHealth` / `PollingHealthMonitor` | `backend/execution/order_poller.py:52-129` | In-memory poll-cycle counters (`consecutive_poll_errors`, `is_healthy`) | Nothing — `consecutive_poll_errors >= 10` only `logger.critical(...)`, no alert/recovery |
| `WorkerSession` strategy threads | `backend/worker/runner.py:87-136` | Named `f"strategy-{run_id}"`, `_stop_event` + `is_alive()` | Nothing — a hung `strategy.on_bar()` loop is invisible; `is_alive()` stays `True` forever with zero progress signal |
| `StrategyRun` (DB) | `backend/database/models.py` | No `worker_id` / heartbeat / status-detail columns | — |
| `StaleDataWatchdog` | `backend/worker/emergency.py:120-171` | Market-*data* freshness (price staleness), unrelated to *process* health | — |

**The gap**: nothing inside the worker process watches its own internal
components — poller thread, strategy threads, scheduler, Redis connection —
with graduated severity, structured alerting, recovery recommendations, and
operational metrics. The cross-process `WorkerWatchdog` can only tell you
"the heartbeat stopped"; it cannot tell you "the poller thread died" vs.
"strategy-42 is stuck in an infinite loop" vs. "Redis is flaky but the worker
is fine."

### How the new `Watchdog` composes the existing building blocks

`backend.worker.watchdog.Watchdog` (the new in-process orchestrator — named
`Watchdog`, *not* `WorkerWatchdog`, to avoid confusion with the existing
cross-process class in `heartbeat.py`) is a **standalone library**: it imports
nothing from `runner.py` / `order_poller.py` / strategy code. Every signal
source is dependency-injected as a duck-typed callable or object:

```
Watchdog
  ├── HeartbeatRegistry        (per-worker_id/strategy_id heartbeat records; in-memory + best-effort Redis mirror)
  ├── HealthMonitor            (assembles HealthSnapshot from injected probes)
  │     ├── thread_provider()         -> {name: threading.Thread-like}    (duck-typed: any object with .is_alive())
  │     ├── poller_health_provider()  -> Optional[bool]                   (duck-typed: PollingHealth.is_healthy)
  │     └── redis_client.ping()
  ├── DeadWorkerDetector       (HealthSnapshot -> Optional[Detection], graduated WARNING/CRITICAL)
  ├── RecoveryExecutor         (Detection -> RecoveryAction -> optional injected callback + AuditLog)
  ├── AlertSystem              (structured Alert: error/stacktrace/last_heartbeat -> ring buffer + AuditLog + notifier)
  └── WatchdogMetrics          (uptime, restart_count, failure_count, heartbeat_interval — same shape as PollingHealth)
```

This mirrors the design philosophy already established by
`backend/execution/idempotency.py` (TASK 2-3C): independently testable with
plain fakes (no real Redis/DB/threads required), Redis-optional with safe
fallbacks, every collaborator dependency-injected, `_now` injection for
deterministic time-based tests.

---

## 2. Failure scenarios

The detector/executor combination handles seven required scenarios. Each is
covered by a dedicated test class in `test_watchdog.py`.

| # | Scenario | What triggers it | Detected as | Severity | Recovery action |
|---|---|---|---|---|---|
| 1 | **Worker kill** (`TestWorkerKill`) | Process-level heartbeat age exceeds `heartbeat_critical_sec` (default 300s) | `Finding(kind="heartbeat_missing")` | CRITICAL | `RESTART_WORKER(target=worker_id)` |
| 2 | **Thread stop** (`TestThreadStop`) | A named thread (e.g. `strategy-7`) reports `is_alive() == False` | `Finding(kind="thread_dead", component="strategy-7")` | CRITICAL | `DISABLE_STRATEGY(target="7")` — prefix `strategy-` stripped |
| 3 | **Redis disconnect** (`TestRedisDisconnect`) | `redis_client.ping()` raises or returns falsy | `Finding(kind="redis_disconnected")` | WARNING | **None** — fail-open; Redis is an observability aid, not a trading-safety gate |
| 4 | **Infinite loop** (`TestInfiniteLoop`) | Thread reports `is_alive() == True` **but** its progress heartbeat (`registry.age_seconds(worker_id, name)`) exceeds `thread_hang_sec` (default 180s) | `Finding(kind="thread_hung", component=name)` | CRITICAL | `DISABLE_STRATEGY` (strategy threads) / `EMERGENCY_STOP` (process components) |
| 5 | **Scheduler stop** (`TestSchedulerStop`) | A thread named e.g. `"scheduler"` (no `strategy-` prefix) reports `is_alive() == False` | `Finding(kind="thread_dead", component="scheduler")` | CRITICAL | `EMERGENCY_STOP(target="")` — halts new trading worker-wide |
| 6 | **Poller stop** (`TestPollerStop`) | `poller_health_provider()` returns `False`, **or** the poller's named thread is reported dead via `thread_provider` | `Finding(kind="thread_dead", component="poller")` | CRITICAL | `EMERGENCY_STOP(target="")` |
| 7 | **Process restart** (`TestProcessRestart`) | A CRITICAL `heartbeat_missing` finding maps to `RESTART_WORKER`; `WatchdogMetrics.restart_count` increments **only** when `RecoveryExecutor.execute()` actually runs a callback successfully (not on recommend-only) | — | — | proves the metrics/execution coupling is correct |

**Why "thread_dead" vs. "thread_hung" matters**: `is_alive() == True` alone
cannot detect a hung/looping thread — a deadlocked `strategy.on_bar()` loop
shows as perfectly alive forever. The distinction requires a *secondary*
progress signal: `ComponentCheck.heartbeat_age_sec`, looked up from
`HeartbeatRegistry` by `(worker_id, component_name)`. This is why the wiring
task (see §5) must instrument monitored loops to call `registry.record(...)`
on every iteration — without that instrumentation, `heartbeat_age_sec` stays
`None` and only `thread_dead` (not `thread_hung`) can be detected.

**Why Redis disconnect is WARNING, not CRITICAL**: Redis is used here purely
as an optional observability mirror (`HeartbeatRegistry._mirror`) and as one
input signal — never as the trading-safety source of truth (that remains
`DailyRiskState.kill_switch` in Postgres). A flaky Redis connection should not
trigger `EMERGENCY_STOP`; that would make the trading system *less* available
because of an *unrelated* infrastructure hiccup. `HealthSnapshot.redis_connected`
is `Optional[bool]`: `None` = not configured (never flagged), `False` =
configured-but-unreachable (WARNING, alert-only), `True` = healthy.

---

## 3. Health flow

```
 component loop                HeartbeatRegistry              HealthMonitor.check()
 (poller / strategy /   ─────► .record(worker_id,     ◄────  .age_seconds(...)
  scheduler — wiring          strategy_id, status)            per (worker_id, "")
  task instruments              │  in-memory dict             and per named thread
  these to call .record)        │  (source of truth)
                                ▼
                          best-effort Redis
                          mirror (try/except,
                          never blocks/raises)
                                                                       │
                                                                       ▼
                                                              HealthSnapshot
                                                  (worker_id, checked_at,
                                                   heartbeat_age_sec,
                                                   redis_connected: Optional[bool],
                                                   components: tuple[ComponentCheck, ...])
                                                                       │
                                                                       ▼
                                                          DeadWorkerDetector.evaluate()
                                                       ┌───────────────┴────────────────┐
                                                       │  age > critical_sec  → CRITICAL "heartbeat_missing"
                                                       │  age > warning_sec   → WARNING  "heartbeat_aging"
                                                       │  comp.alive == False → CRITICAL "thread_dead"
                                                       │  alive but stale hb  → CRITICAL "thread_hung"
                                                       │  redis_connected==False → WARNING "redis_disconnected"
                                                       └───────────────┬────────────────┘
                                                                       ▼
                                                     Optional[Detection]
                                          (severity = max(finding severities),
                                           findings: tuple[Finding, ...] — structured,
                                           never string-parsed downstream)
```

Every probe inside `HealthMonitor.check()` (`_check_threads`, `_check_poller`,
`_check_redis`) is independently wrapped in `try/except` — a raising
`thread_provider` surfaces as its own `ComponentCheck(alive=False, detail="probe
error: ...")`, never as a crash that masks the other signals. This was a
required test scenario (`test_failing_thread_provider_does_not_mask_other_probes`).

---

## 4. Recovery flow

```
 Detection (severity, findings: tuple[Finding, ...], snapshot)
        │
        ├─ metrics.record_failure()
        ├─ alerts.record(severity, "; ".join(reasons), last_heartbeat=...)
        │       ├─ ring buffer (deque, maxlen=200)
        │       ├─ AuditLog(event_type="watchdog_alert", actor="watchdog")
        │       └─ optional notifier callback (severity, message)  — injectable, never reaches Telegram in tests
        │
        └─ if severity is CRITICAL:
               Watchdog._map_to_actions(detection)   ◄── switches on Finding.kind / Finding.component
                                                          (structured — never parses human-readable message strings)
                   "heartbeat_missing"                          → RESTART_WORKER(target=worker_id)
                   "thread_dead"/"thread_hung" on "strategy-N"  → DISABLE_STRATEGY(target="N")   [prefix stripped]
                   "thread_dead"/"thread_hung" on other         → EMERGENCY_STOP(target="")
                       │
                       ▼
               RecoveryExecutor.execute(action)
                   ├─ _audit(action) — ALWAYS: AuditLog(event_type="watchdog_recovery_<type>", actor="watchdog")
                   ├─ matching callback present?  → call it (wrapped in try/except, never raises)
                   │       on_restart_worker / on_disable_strategy / on_emergency_stop
                   └─ no callback (default)        → log CRITICAL, "recommend-only", return False
                       │
                       ▼
               if executed and action_type is RESTART_WORKER:
                   metrics.record_restart()
```

**WARNING findings never trigger a recovery action** — only CRITICAL findings
reach `_map_to_actions`. This is the "graduated response": a single slow
heartbeat tick or a transient Redis blip produces an alert (so an operator can
investigate) but does not halt trading or restart anything.

**`RecoveryExecutor` can never touch broker/execution/position code** — by
construction it only knows about three action types, and its default
("recommend-only", no callback wired) performs nothing destructive: it logs
CRITICAL and writes an audit row. This satisfies the task's hard constraint:

> Allowed: restart worker, disable strategy, emergency stop
> Never: reissue orders, modify positions, auto-trade

A future wiring task can opt a specific action *type* into real execution by
injecting a callback — e.g. `on_disable_strategy` → publish to the existing
`strategy:stop` Redis channel (`runner.py` `_handle_stop`), or
`on_emergency_stop` → set `DailyRiskState.kill_switch = True` (the *exact* DB
write `WorkerWatchdog._alert_dead_worker` already performs in
`heartbeat.py:128-159`). Note `EMERGENCY_STOP` means *halt new trading*
(kill-switch) — it explicitly does **not** mean flatten existing positions.

---

## 5. Operational risks

1. **No alert/recovery debouncing across consecutive ticks — verified live.**
   Running `Watchdog` end-to-end with a real daemon thread and a real dead
   strategy thread (`check_interval_sec=0.3`) showed that **every** tick where
   `DeadWorkerDetector.evaluate()` returns a CRITICAL `Detection` re-records a
   full `Alert` (ring buffer + AuditLog + notifier callback) *and* re-dispatches
   a fresh `RecoveryAction` through `RecoveryExecutor` — three ticks of the same
   dead `strategy-99` thread produced three identical CRITICAL alerts, three
   notifier invocations, and three separate `DISABLE_STRATEGY` audit rows /
   callback calls. `Watchdog` carries **no transition-tracking state** the way
   the existing cross-process `WorkerWatchdog._check` does (`heartbeat.py:117-126`,
   `self._was_dead` — alerts only on dead→alive / alive→dead transitions, not
   on every poll while the state is unchanged). At the default
   `check_interval_sec=60`, an unresolved CRITICAL condition would: publish to
   `strategy:stop` every minute, write one `watchdog_alert` + one
   `watchdog_recovery_disable_strategy` AuditLog row every minute, and fire the
   Telegram notifier every minute — alert fatigue and AuditLog growth that scale
   with how long an incident goes unresolved. **A future wiring/tuning task
   should add transition-aware suppression** (e.g. only alert/act on a
   finding-set change, plus a periodic "still down" reminder on a much longer
   cadence) before connecting real notifier/recovery callbacks — exactly mirroring
   the `_was_dead` pattern already proven in `heartbeat.py`.

2. **Not yet wired into `runner.py` / `scheduler.py` (deliberate).** This task
   delivers the library and its tests only — exactly mirroring how
   `idempotency.py` (TASK 2-3C) was built standalone before being wired into
   the order-submission path. Wiring requires:
   - constructing a `Watchdog` inside `StrategyWorker.__init__` with real
     `thread_provider` (mapping session names → `WorkerSession` threads),
     `poller_health_provider` (`lambda: poller.health.is_healthy`), and the
     live `redis_client`;
   - instrumenting `WorkerSession._run`, `OrderFillPoller._loop`, and
     scheduler jobs to call `registry.record(worker_id, component_name, "alive")`
     each iteration — **without this, `heartbeat_age_sec` stays `None` for
     those components and `thread_hung` (infinite-loop) detection cannot fire**,
     only `thread_dead` can;
   - connecting `RecoveryExecutor` callbacks to the safe existing primitives
     named in §4.

3. **Redis mirror is best-effort / non-authoritative.** `HeartbeatRegistry`
   keeps an in-memory dict as the sole source of truth; the Redis `SETEX`
   mirror (`watchdog:hb:{worker_id}:{strategy_id or '_proc'}`) exists purely
   for cross-process observability (e.g. a future dashboard) and is wrapped in
   `try/except` — a Redis outage never affects detection, which always reads
   the in-memory registry.

4. **`RESTART_WORKER` cannot actually restart an OS process from in-process
   Python.** The action is a *recommendation* — by default it only logs
   CRITICAL and writes an audit row. Real process restart requires external
   supervision (systemd, Docker healthcheck/restart-policy, a process
   supervisor) that observes the audit log or a wired callback/exit signal.
   This module deliberately does not call `os.execv`/`sys.exit`/`SIGKILL` —
   doing so from inside the very process being monitored is fragile and could
   itself become a new failure mode (e.g. killing mid-transaction).

5. **Thread-hang detection quality depends entirely on instrumentation
   density.** A strategy that spends 170 of its 180-second `thread_hang_sec`
   budget inside a single `on_bar()` call without checkpointing will not be
   flagged as hung even though it is making slow progress — and conversely, a
   strategy that checkpoints right before hanging will take up to
   `thread_hang_sec` to be detected. The threshold is a tunable tradeoff
   between false positives (flagging slow-but-healthy strategies) and
   detection latency.

6. **`strategy_thread_prefix` routing is convention-based, not type-based.**
   `Watchdog._map_to_actions` decides `DISABLE_STRATEGY` vs. `EMERGENCY_STOP`
   purely by string-prefix match against the configured
   `strategy_thread_prefix` (default `"strategy-"`, matching `runner.py:97`'s
   `f"strategy-{self.run_id}"`). If the naming convention in `runner.py` ever
   changes, this constructor argument must be updated to match — otherwise
   strategy-thread failures would be misrouted to worker-wide `EMERGENCY_STOP`.

---

## Recommended next task

Wire `Watchdog` into the live worker process:

1. Construct a `Watchdog` instance inside `StrategyWorker.__init__`
   (`runner.py:142-202`), alongside the existing `OrderFillPoller`,
   `WorkerHeartbeat`, and `PositionReconciler` wiring.
2. Instrument `WorkerSession._run` (`runner.py:87-136`),
   `OrderFillPoller._loop`, and each scheduler job to call
   `registry.record(worker_id, component_name, "alive")` per iteration —
   this is the missing piece that makes `thread_hung` (infinite-loop)
   detection actually fire in production.
3. Connect `RecoveryExecutor` callbacks to existing safe primitives:
   - `on_disable_strategy` → publish to `strategy:stop` (the same channel
     `_handle_stop` already uses)
   - `on_emergency_stop` → `DailyRiskState.kill_switch = True` (the same
     write `WorkerWatchdog._alert_dead_worker` performs today)
   - `on_restart_worker` → left as recommend-only (log + audit) until a
     process supervisor (systemd/Docker) is configured to observe it
4. Add `worker_id` / `last_heartbeat` / `status_detail` columns to
   `StrategyRun` so the dashboard can show per-strategy liveness, not just
   process-level liveness.
