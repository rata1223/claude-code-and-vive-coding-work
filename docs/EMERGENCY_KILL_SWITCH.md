# Emergency Kill Switch — Audit & Design Specification

## 1. Purpose and Scope

This document audits every kill-switch-like mechanism that currently exists in
the KIS Trading Platform, identifies where they disagree or leave gaps, and
designs a single unified `EmergencyKillSwitch` controller that owns the full
**trip → contain → recover** lifecycle.

**Scope:** Audit + design only. No code in this document has been implemented;
§9 lists the concrete changes a future implementation task must make.

**Why this document exists:** across several independent sessions, the codebase
accumulated `PersistentLossTracker.kill_switch`, `SAFE_MODE`,
`EmergencyFlattenManager`, `WorkerWatchdog`, `ConsecutiveFailureBreaker`, and
`StaleDataWatchdog` — six mechanisms that each looked complete in isolation but
were never audited together. This document is that audit.

**Guiding principle carried over from `RECONCILIATION_ENGINE.md`:**
**Broker is always ground truth for positions. DB is always ground truth for
trade intent.** The kill switch's job is to freeze the system in a known-safe
state the instant either side becomes untrustworthy — not to guess which side is
right.

---

## 2. Current Structure

### 2.1 Order entry call chain and existing gates

```
Strategy.on_bar() / on_market_open()
    │  IndicatorStrategy._scan_and_trade() → _execute_buy / _execute_sell
    │  ConsecutiveFailureBreaker.is_open() check (per-strategy instance,
    │  3 consecutive failures → 30-minute cooldown)               circuit_breaker.py:14-50
    ▼
PositionTracker.try_mark_pending(symbol)                          position_tracker.py:50-68
    ▼
StrategyBase.buy() / sell()                                       strategy/base.py:107-125
    │
    │  ── _live_trade_allowed(broker, name, symbol, side) ──      strategy/base.py:14-41
    │     GATE 1: SAFE_MODE.can_trade                             base.py:27
    │             → not allowed: returns (False, Order(status=REJECTED))
    │     GATE 2: ENABLE_LIVE_TRADING env var ("shadow" mode)     base.py:36
    │             → not "true": returns (False, Order(status=REJECTED))
    │     (both gates skipped entirely when broker.is_live is False —
    │      i.e. SimulatedBroker / backtests, base.py:21-22)
    ▼
KISBroker.place_order()                                           brokers/kis.py:106-133
    │  GATE 3: broker-level ConsecutiveFailureBreaker
    │          → tripped: raises RuntimeError("circuit breaker open")
    │             caught upstream → order returned as REJECTED
    ▼
KISOrders.buy_kr / sell_kr / buy_us / sell_us                      kis_adapter/orders.py
    ▼
KISClient.post()  (rate-limited: 5/s paper, 15/s real; 3 retries)  kis_adapter/client.py:56-75
    ▼
KIS REST API
```

Three gates already exist in this chain (GATE 1–3). Crucially, **all three
reject with an `Order(status=REJECTED)` rather than silently dropping the call**
— this is correct behavior that the new design must preserve.

**Insertion-point analysis — where should a unified kill switch live?**

| Candidate | Pros | Cons |
|---|---|---|
| `StrategyBase.buy()/sell()` (current GATE 1/2 — **chosen**) | Earliest point that also halts signal-driven flow; gates are already wired here; rejects with a typed `Order`, not a silent drop | Does not cover `quick_trade` API direct-order paths (those need their own check) or `EmergencyFlattenManager` (which must be **exempt** — see §2.3) |
| `BrokerAdapter.place_order()` (current GATE 3, partial) | Catches every path including API/manual/recovery orders | Strategy still computes signals and burns cycles before being rejected; duplicate logging |
| `KISClient.post()` | Closest to the wire — guarantees nothing reaches KIS | Cannot distinguish order placement from balance/price queries; would also block `EmergencyFlattenManager`'s own liquidation sells, defeating the switch's purpose |

**Decision:** keep `StrategyBase.buy()/sell()` (already there) as the primary
gate. Add a parallel check to the `quick_trade` API router (already flagged as
gap DO-10 in `IDEMPOTENT_EXECUTION.md` §11). `EmergencyFlattenManager` calls
`broker.place_order()` **directly**, bypassing `StrategyBase` — this must remain
true and be documented as an intentional, audited exception (§2.3).

### 2.2 Current state model — THREE DISJOINT "is trading enabled" FLAGS

| Flag | Storage | Scope | Set by | Read by | Survives restart? |
|---|---|---|---|---|---|
| `SAFE_MODE.can_trade` | In-process singleton `SafeModeState` | **Per-process** (worker and API process each have their own instance) | `StartupRecovery._step_enable_trading()` (`recovery.py:~464`); `LossTracker._fire_kill_switch_alert()` (`engine.py:281`) | `_live_trade_allowed()` (`base.py:27`) | **No** — constructed as `_can_trade=False, _reason="초기화 중"` (`recovery.py:45-46`) on every process start |
| `DailyRiskState.kill_switch` (Postgres) | DB row, 1 per trade date | Cross-process, durable | `LossTracker._evaluate()` → `PersistentLossTracker._persist()`; **also** `WorkerWatchdog._alert_dead_worker()` — a **second, independent writer** (`heartbeat.py:145-149`) | `StartupRecovery._step_risk()` at boot only; `/api/status`, `/api/metrics` (read-only display) | **Yes** |
| `ENABLE_LIVE_TRADING` (env var) | Container env / `.env` | Process-wide, immutable at runtime | Operator (requires redeploy) | `_live_trade_allowed()` GATE 2; `EmergencyFlattenManager.dry_run`; `quick_trade` dry-run flag | N/A — not a runtime switch |

**The critical desync (this is the single biggest structural gap in the current
system):** `WorkerWatchdog` runs **inside the API process**
(`server.py:394 _start_watchdog()`), detects that the **worker** process has
gone silent, and can only write `DailyRiskState.kill_switch=True` — its own
in-process `SAFE_MODE` is irrelevant because nothing in that process places
orders. The code even contains a self-aware comment acknowledging this
(`heartbeat.py:129-131`):

> *"SAFE_MODE is a process-local singleton so calling SAFE_MODE.disable() here
> (in the API process) has no effect on the worker process. DB is the only
> cross-process channel available without additional infrastructure."*

But **nothing on the worker side ever re-reads `DailyRiskState.kill_switch` at
runtime** — `StartupRecovery._step_risk()` consults it exactly once, at boot.
**A worker that is alive-but-degraded can keep trading for an entire session
after another process has already declared a kill condition**, until its next
restart surfaces the stale DB flag. Closing this gap (via cross-process pub/sub,
§6) is the design's top priority.

### 2.3 Existing kill-switch-like mechanisms — full inventory

| # | Mechanism | File:Line | Trigger | Action taken | Escalates to halt? | Persists? | Auto-recovers? |
|---|---|---|---|---|---|---|---|
| KS-M1 | `LossTracker._evaluate()` — daily −3% / weekly −6% / MDD −15% | `quant/risk/engine.py:248-274` | `record_pnl()` after each **sell** fill (`runner.py:490`) | sets `kill_switch=True`, `kill_reason`, calls `_fire_kill_switch_alert()` | **Yes** | DB + Redis via `PersistentLossTracker` (25h TTL) | **No** |
| KS-M2 | `_fire_kill_switch_alert()` | `engine.py:276-293` | KS-M1 trip | `SAFE_MODE.disable(...)` (same-process only, line 281) + Telegram `alert_emergency()` + WebSocket `publish_alert(level="critical")` | — | — | — |
| KS-M3 | `SafeModeState` / `SAFE_MODE` singleton | `worker/recovery.py:41-67` | Calls from KS-M1, `StartupRecovery`, validation failures | gates `_live_trade_allowed()` (the actual enforcement point) | **Yes** (per-process) | **No** — pure in-memory | Only via process restart + `StartupRecovery.run()` returning `True` |
| KS-M4 | `StartupRecovery` 9-step boot sequence | `recovery.py:83-108` | Worker process start | calls `SAFE_MODE.enable()` **only if all 9 steps pass**, including an explicit "kill_switch was active in prior session → stay disabled" check | **Yes** (gatekeeper) | — | N/A — boot-time only |
| KS-M5 | `EmergencyFlattenManager.flatten_all()` | `worker/emergency.py:30-99` | Manual: `POST /api/admin/flatten` (`X-API-Key` + `confirm=true`, rate-limited 3 calls / 5 min, `server.py:318-339`) | iterates `broker.get_positions()`, market-sells every position via `broker.place_order(symbol, "sell", qty, price)` **directly** (bypassing `StrategyBase`); respects `dry_run = (ENABLE_LIVE_TRADING != "true")` | N/A — this is the *response* to a halt, not a trigger | per-order `AuditLog` (`emergency_flatten_order`) | — |
| KS-M6 | `StaleDataWatchdog` | `worker/emergency.py:134-171` | — | `is_stale(df)` / `check_all(dfs)` — pure detection, returns bool/list | **No — DEAD CODE.** Fully implemented; the *only* reference to it anywhere in the codebase is its own usage example inside its class docstring (`emergency.py:140`). Zero call sites in `strategy/` or `worker/`. | — | — |
| KS-M7 | `ConsecutiveFailureBreaker` (per-strategy) | `execution/circuit_breaker.py:14-50` | 3 consecutive order failures for one strategy instance | `is_open()=True` blocks that strategy's orders for a 30-minute cooldown, then auto-resets | **No** — scoped to a single strategy instance, never escalates system-wide | No | Yes (cooldown timer) |
| KS-M8 | KIS broker-level `ConsecutiveFailureBreaker` | `brokers/kis.py` (instantiated with threshold≈5, cooldown≈10 min) | Repeated KIS API failures across `get_balance` / `place_order` / `get_price` | raises `RuntimeError("KIS circuit breaker open...")`; callers catch it and return `Order(status=REJECTED)` | **No** — recovers silently after cooldown; no `kill_switch`, no alert, no audit entry | No | Yes |
| KS-M9 | `WorkerHeartbeat` / `HeartbeatMonitor` / `WorkerWatchdog` | `worker/heartbeat.py:19-177` | Worker silent for 90s (3 missed 30s beats), detected by `WorkerWatchdog._check()` running in the **API process** | **directly writes** `DailyRiskState.kill_switch=True, kill_reason="Worker 하트비트 없음..."` (`heartbeat.py:140-149`) — **bypassing `LossTracker` entirely; this is a second, uncoordinated writer to the same DB column as KS-M1** — plus Telegram + WebSocket alert | **Yes** (DB-only — see §2.2 desync) | DB | **No** — `_was_dead` resets to `False` on the next successful heartbeat (`heartbeat.py:123-126`) and fires an "info"-level recovery alert, but **never clears `kill_switch`** |
| KS-M10 | `_reset_daily_risk()` scheduled job | `worker/scheduler.py:78-~100` | Daily cron | resets `daily_pnl` and Redis daily counters | **Explicitly does not clear `kill_switch`** — inline comment: *"kill_switch intentionally NOT cleared — requires manual operator reset"* | — | — |
| KS-M11 | `PositionReconciler` CRITICAL-severity gaps | `execution/reconciler.py` | Position/order mismatch above threshold | logs `reconcile_critical`, publishes a WebSocket alert | **Only at startup** — `StartupRecovery._step_reconcile()` blocks `SAFE_MODE.enable()` on a critical gap. **At runtime, the same severity of gap only logs and alerts; the strategy keeps trading on the mismatched data.** | — | — |
| KS-M12 | Admin endpoints | `api/server.py:296-352` | `/api/admin/reconcile`, `/api/admin/flatten`, `/api/admin/heartbeat` exist | — | — | — | **There is no `POST /api/admin/kill-switch/reset`.** `LossTracker.manual_reset()` (`engine.py:303-306`) exists in code but has zero callers outside unit tests — the only way an operator can clear `kill_switch` today is a raw `UPDATE daily_risk_state SET kill_switch = false` SQL statement. |

### 2.4 What this inventory tells us

The platform is **not missing kill-switch primitives** — it has six of them. What
it lacks is:

1. A **single writer** for the shared `kill_switch` state (KS-M1 and KS-M9 both
   write `DailyRiskState.kill_switch` independently — a lost-update race is
   possible, and `kill_reason` from one can silently overwrite the other's).
2. **Cross-process propagation** at runtime, not just at boot (§2.2 desync).
3. **Severity tiers** — every trigger maps to the same binary
   `kill_switch = True/False`; there is no distinction between "stop opening new
   positions" and "liquidate everything immediately."
4. **An automatic response proportionate to the trigger** — the single most
   severe trigger (MDD breach, KS-M1) produces the *least* automatic protection:
   it disables new entries and sends an alert, but does **not** cancel resting
   orders or liquidate the position that is actively losing money. A human must
   notice the alert and separately call `/api/admin/flatten`.
5. **A safe, audited path back to `NORMAL`** (§2.3, KS-M12 gap).

---

## 3. Trigger Taxonomy and Severity

The new design classifies every trigger into exactly one of two levels. This
directly maps to "trigger candidates and severity" from the audit brief.

### 3.1 SOFT — pause new entries only

Rationale: the system is showing early warning signs, or a *subset* of its
capability is degraded, but existing positions and the broker connection are not
yet known to be in danger. Selling, canceling, and monitoring continue normally
— only **new BUY orders** are blocked.

| Trigger | Detection source | Detection latency | Why SOFT and not HARD |
|---|---|---|---|
| Daily loss reaches 80% of the −3% limit | `LossTracker.can_buy()` (`engine.py:295-301` — already computes this, but the result is currently **never consulted**; it is dead logic exactly like `StaleDataWatchdog`) | Immediate (computed on every `record_pnl`) | Still inside the configured limit; a full halt here would be premature |
| `ConsecutiveFailureBreaker` (KS-M7) trips for **N ≥ 2 strategies/symbols simultaneously** within a rolling window | New: aggregation layer in `EmergencyKillSwitch` subscribing to per-strategy breaker trips | Seconds | A single symbol's failures are routine; correlated failures across symbols suggest a systemic broker/network issue worth a pause, not yet a liquidation |
| `StaleDataWatchdog.check_all()` reports > 30% of the trading universe stale | New wiring of KS-M6 into `IndicatorStrategy._scan_and_trade()` | One scan cycle | Bad data should stop *new* decisions; it says nothing about whether currently-held positions are at risk |
| Redis unavailable for > 10 minutes | New: `WorkerSession` tracks consecutive Redis ping failures | Minutes | Degrades propagation speed and loss-tracker persistence redundancy, but DB fallback keeps the system functionally correct |
| KIS broker-level circuit breaker (KS-M8) trips | Existing — currently silent; new design adds an `EmergencyKillSwitch` notification on trip | Immediate | First sign of broker-side trouble; KS-M8 already auto-recovers — escalate to a pause, not a liquidation, while it cools down |

### 3.2 HARD — full halt + contain + (optionally) liquidate

Rationale: continuing to operate risks compounding the loss, or the system can
no longer trust its own view of broker state. Stop everything; the only safe
default action is to freeze, cancel working orders, and — if the operator has
opted in — liquidate.

| Trigger | Detection source | Detection latency | Why HARD |
|---|---|---|---|
| Daily loss limit (−3%) or weekly loss limit (−6%) breached | `LossTracker._evaluate()` (KS-M1, `engine.py:252,260`) | Immediate, on next sell fill | Defined hard limit from the platform's risk policy (`CLAUDE.md` "리스크 규칙") — by definition, continuing to trade is policy violation |
| Maximum drawdown (−15%) breached | `LossTracker._evaluate()` (KS-M1, `engine.py:269-274`) | Immediate, on next sell fill | Same — and MDD is the platform's "전량 청산 + 긴급 알림" trigger per `CLAUDE.md` |
| `WorkerWatchdog` declares the worker process dead (KS-M9) | `heartbeat.py:117-126` | ≤ 90 seconds | If the worker is dead, no component is monitoring open positions or evaluating risk — operating blind is strictly worse than halting |
| `PositionReconciler` finds a CRITICAL-severity gap **at runtime** (not just startup) | New: wire KS-M11's existing severity classification into a runtime hook, not just `StartupRecovery._step_reconcile()` | One reconciliation cycle (≤ 30 min, or on-demand) | Per `RECONCILIATION_ENGINE.md` §7, CRITICAL means "state divergence that could cause significant financial loss... if left unresolved for > 5 minutes" — that document already specifies the correct response is "block new orders... emergency stop"; this design is what finally wires that response up |
| Sustained KIS authentication failure streak (e.g., expired/invalid token surviving the broker-level breaker's auto-recovery) | New: `EmergencyKillSwitch` tracks repeated breaker trips for the *same root cause* across cooldown cycles | Tens of minutes (multiple breaker cycles) | Auto-recovery assumes transient network blips; a token problem will not self-heal and will burn the rate-limit budget indefinitely if left alone |

**Escalation rule:** any **SOFT** condition that does not clear within its
configured window (default: 30 minutes, configurable per-trigger) escalates to
**HARD** automatically. This prevents "stuck in a pause forever" — a SOFT trip
must either resolve or escalate; it cannot be a silent permanent state.

---

## 4. State Diagram

```
                    ┌─────────────────────────────────────────────┐
                    │                                             │
                    ▼                                             │
              ┌───────────┐    SOFT trigger     ┌────────────┐    │
   ┌─────────►│  NORMAL   ├────────────────────►│ SOFT_HALT  │    │
   │          └───────────┘                     └─────┬──────┘    │
   │                │                                  │           │
   │                │ HARD trigger                     │ condition │
   │                │ (direct)                         │ clears    │
   │                ▼                                  │ within    │
   │          ┌───────────┐  ◄── escalation ───────────┘ window   │
   │          │ HARD_HALT │      (SOFT condition persists          │
   │          └─────┬─────┘       beyond its window)               │
   │                │                                              │
   │                │ trip() side effects:                         │
   │                │  • SAFE_MODE.disable() (all processes,       │
   │                │    via Redis pub/sub + DB)                   │
   │                │  • cancel every open order                   │
   │                │  • IF AUTO_FLATTEN_ON_KILL=true:             │
   │                │    EmergencyFlattenManager.flatten_all()     │
   │                │  • Telegram + WebSocket "critical" alert     │
   │                │  • AuditLog: kill_switch_triggered           │
   │                ▼                                              │
   │          ┌───────────┐                                        │
   │          │RECOVERING │ ◄── operator: POST /kill-switch/reset  │
   │          │           │     (X-API-Key + confirm=true +        │
   │          │           │      mandatory `reason` string)        │
   │          └─────┬─────┘                                        │
   │                │                                              │
   │                │ re-validation pass                           │
   │                │ (reuses StartupRecovery-style                │
   │                │  reconcile + balance + position check)       │
   │                │                                              │
   │      pass ─────┼───── fail: back to HARD_HALT,                │
   │                │             reason appended to audit trail   │
   │                ▼                                              │
   └────────────────┘ ── cooldown timer elapses, no new triggers ──┘
                          (re-arms NORMAL; see §7 Recovery Flow)
```

**Notes on the diagram:**

- `SOFT_HALT` only blocks **new BUY orders** (`_live_trade_allowed` returns
  `False` for `side == "buy"`, `True` for `"sell"`/cancel). `HARD_HALT` blocks
  everything routed through `StrategyBase` (both `buy` and `sell`) — the only
  exempted caller is `EmergencyFlattenManager`, which talks to `BrokerAdapter`
  directly (§2.3, KS-M5).
- There is **no direct edge from `HARD_HALT` to `NORMAL`**. Every HARD trip must
  pass through `RECOVERING`, which always requires an explicit operator action.
  This is a deliberate design choice — see §8, Rollback Policy.
- `SOFT_HALT → NORMAL` (the top-left loop-back arrow) is the **only** fully
  automatic transition in the diagram, and only for conditions that clear inside
  their configured window. Even this is logged to `AuditLog` as
  `kill_switch_soft_cleared` for traceability.

---

## 5. Component Design — `EmergencyKillSwitch`

**New file:** `backend/execution/kill_switch.py`

This controller becomes the **single writer** for kill-switch state, replacing
the duplicated logic currently spread across KS-M1 (`_fire_kill_switch_alert`)
and KS-M9 (`WorkerWatchdog._alert_dead_worker`'s direct DB write).

### 5.1 Public interface

```python
class EmergencyKillSwitch:
    def __init__(self, db_factory, redis_client, *, worker_id: str = "kis-worker"):
        ...

    def trip(self, level: Literal["SOFT", "HARD"], reason: str, source: str) -> None:
        """
        Single entry point for EVERY trigger in §3. Atomically:
          1. Writes DailyRiskState.kill_level / kill_switch / kill_reason (DB)
          2. Appends to kill_reason_history (audit trail — see §5.2)
          3. Publishes to Redis channel "kill:trip" — {level, reason, source, ts}
          4. Calls SAFE_MODE.disable(reason) in THIS process
          5. Writes AuditLog event_type="kill_switch_triggered"
          6. Fires Telegram alert_emergency() + WebSocket publish_alert(level="critical")
          7. IF level == "HARD": triggers contain() (see below)
        Idempotent: tripping an already-tripped switch at the same or lower
        level is a no-op (logged at DEBUG); tripping SOFT while already HARD
        does not downgrade.
        """

    def contain(self, reason: str) -> None:
        """
        HARD-trip side effect, called once by trip(). Order matters:
          1. Cancel every order with status in {SUBMITTED, PARTIAL_FILLED}
             via broker.cancel_order() — best-effort, audited per order
          2. IF os.environ["AUTO_FLATTEN_ON_KILL"] == "true":
                 EmergencyFlattenManager(broker, db_factory, dry_run=...).flatten_all(reason)
             ELSE:
                 log + alert "auto-flatten disabled — manual /api/admin/flatten required"
        """

    def reset(self, operator: str, reason: str) -> tuple[bool, str]:
        """
        Operator-initiated recovery. Transitions HARD_HALT/SOFT_HALT → RECOVERING,
        runs a re-validation pass (reuses StartupRecovery's reconcile + balance +
        position steps in isolation), and on success transitions to NORMAL.
        On failure, returns to HARD_HALT and appends the failure to the audit trail.
        Returns (success, message). Never silently no-ops — always audited.
        """

    def status(self) -> KillSwitchStatus:
        """Returns current level, reason, tripped_at, source, history — for
        /api/status, /api/metrics, and the mobile dashboard."""

    def subscribe(self, on_trip: Callable, on_reset: Callable) -> None:
        """Called once per WorkerSession at startup. Subscribes to Redis
        channels "kill:trip" / "kill:reset" for sub-second cross-process
        propagation (see §6). Falls back to DB polling if Redis is down."""
```

### 5.2 DB schema change

Add to `DailyRiskState` (`backend/database/models.py`):

```python
kill_level = Column(String(10), default="NONE")     # "NONE" | "SOFT" | "HARD"
kill_source = Column(String(50), nullable=True)     # e.g. "loss_tracker", "worker_watchdog",
                                                     #      "reconciler", "manual"
kill_reason_history = Column(Text, nullable=True)   # JSON array, append-only:
                                                     # [{"ts", "level", "reason", "source", "actor"}]
```

The existing `kill_switch: Boolean` column is **kept** for backward-compatible
reads (`/api/status` etc.) and is derived: `kill_switch = (kill_level != "NONE")`.
A migration backfills `kill_level = "HARD"` for any existing row where
`kill_switch = True`, and `kill_source = "migration_backfill"`.

`kill_reason_history` is **append-only** — `reset()` adds an entry, it never
deletes or overwrites prior entries. This directly answers "rollback policy":
the system's own history of what tripped it and who cleared it is itself
something that must never be silently rewritten (§8).

### 5.3 Redis channel contract

```
Channel "kill:trip"
  payload: {"level": "SOFT"|"HARD", "reason": str, "source": str,
            "ts": iso8601, "trip_id": uuid}

Channel "kill:reset"
  payload: {"operator": str, "reason": str, "ts": iso8601,
            "prior_trip_id": uuid}
```

Every `WorkerSession` subscribes to both channels at startup
(`EmergencyKillSwitch.subscribe`). On `kill:trip`, it immediately calls its
local `SAFE_MODE.disable(reason)` — closing the cross-process desync gap from
§2.2 within the Redis pub/sub latency (typically < 1 second), instead of waiting
for the next process restart.

---

## 6. Trip Flow

```
Detector (any of §3's triggers)
    │
    ▼
classify_severity() → "SOFT" | "HARD"
    │
    ▼
EmergencyKillSwitch.trip(level, reason, source)
    │
    ├─► [DB]    UPDATE daily_risk_state SET kill_level=?, kill_switch=?,
    │           kill_reason=?, kill_source=?,
    │           kill_reason_history = kill_reason_history || new_entry
    │           (single transaction — this row is the source of truth)
    │
    ├─► [Redis] PUBLISH "kill:trip" {level, reason, source, ts, trip_id}
    │           (fire-and-forget; DB write above is authoritative even if
    │            this fails — see §7 "Redis unavailable while tripped")
    │
    ├─► [local] SAFE_MODE.disable(reason)
    │           (every OTHER process learns via the Redis subscription,
    │            or — if Redis is down — via its next DB poll, see §7)
    │
    ├─► [audit] AuditLog(event_type="kill_switch_triggered",
    │           detail={level, reason, source, trip_id})
    │
    ├─► [alert] Telegram alert_emergency() + WebSocket publish_alert("critical")
    │
    └─► IF level == "HARD":
            EmergencyKillSwitch.contain(reason)
              │
              ├─► 1. Cancel every order in {SUBMITTED, PARTIAL_FILLED}
              │      for sym, order in open_orders: broker.cancel_order(order.id)
              │      (best-effort — failures logged + audited individually,
              │       do NOT block the next step)
              │
              └─► 2. IF AUTO_FLATTEN_ON_KILL == "true":
                         EmergencyFlattenManager.flatten_all(reason)
                         (calls broker.place_order() directly — EXEMPT from
                          _live_trade_allowed(), as documented in §2.3 KS-M5)
                     ELSE:
                         alert_emergency("자동청산 비활성 — 수동 /api/admin/flatten 필요")
```

**Why cancel-before-flatten:** flattening sells the *current* position; it does
nothing about a resting limit BUY order that could fill moments later and
recreate the very position just liquidated. Canceling first closes that race.
This step does not exist in the current `EmergencyFlattenManager.flatten_all()`
(§2.3 KS-M5) — it is a required addition, listed in §9.

**Why `AUTO_FLATTEN_ON_KILL` is opt-in, not automatic-by-default:** see §8
Operational Risks — automatically selling into a flash-crash or a temporarily
wide spread can realize a worse loss than holding through a transient dip. The
platform's risk policy (`CLAUDE.md`: "MDD 15% → 전량 청산 + 긴급 알림") names
liquidation as the correct response to MDD specifically — the env flag lets an
operator honor that policy for MDD while still reviewing other HARD triggers
(e.g., a dead-worker detection, where the position itself may be perfectly
fine and selling it would be the wrong move) by hand.

---

## 7. Recovery Flow

```
Operator notices alert (Telegram "🚨 긴급 알림" / WebSocket critical / dashboard)
    │
    ▼
Operator investigates root cause using:
    • AuditLog.kill_reason_history (full append-only trail — §5.2)
    • /api/status, /api/metrics (current kill_level, kill_reason, kill_source)
    • broker statements (ground truth for what actually happened to positions)
    │
    ▼
Operator: POST /api/admin/kill-switch/reset
    Headers: X-API-Key: <admin key>
    Body:    {"confirm": true, "reason": "<mandatory free-text root-cause note>"}
    │  (rate-limited identically to /api/admin/flatten — 3 calls / 5 min,
    │   server.py:_check_admin_rate_limit pattern)
    │
    ▼
EmergencyKillSwitch.reset(operator, reason)
    │
    ├─► transitions kill_level: current → "RECOVERING" (visible in /api/status
    │   so a concurrent dashboard view never shows a misleading "NORMAL")
    │
    ├─► re-validation pass — REUSES existing, already-audited logic:
    │     • StartupRecovery._step_balance()    (broker reachable, balance sane)
    │     • StartupRecovery._step_positions()  (broker positions retrievable)
    │     • StartupRecovery._step_reconcile()  (no CRITICAL gap remains)
    │   run in isolation (NOT the full 9-step sequence — the worker process
    │   itself may still be running normally; only the conditions that caused
    │   the trip need re-checking)
    │
    ├─► PASS:
    │     • kill_level → "NONE", kill_switch → False
    │     • kill_reason_history append: {"action": "reset", "operator",
    │       "reason", "validation": "passed", "ts"}
    │     • PUBLISH "kill:reset" {operator, reason, ts, prior_trip_id}
    │       → every WorkerSession's local SAFE_MODE.enable() fires
    │     • AuditLog(event_type="kill_switch_reset", actor=operator)
    │     • cooldown timer starts (default 15 min — configurable); during
    │       cooldown the system is in NORMAL but under heightened logging
    │       (every trip-eligible evaluation is logged at INFO even when it
    │       doesn't fire, to give the operator early visibility into whether
    │       the root cause truly resolved)
    │
    └─► FAIL (e.g. reconcile still finds a CRITICAL gap, or balance fetch
        still times out):
          • kill_level reverts to its PRE-reset value (HARD stays HARD)
          • kill_reason_history append: {"action": "reset_failed", "operator",
            "reason", "validation_failure": "<detail>", "ts"}
          • alert_emergency("킬스위치 해제 실패 — 근본 원인 미해결: <detail>")
          • the attempt itself is preserved in history — an operator's failed
            reset attempt is exactly the kind of signal a future investigator
            needs (§8: append-only history, never overwritten)
```

**Why re-validation reuses `StartupRecovery` steps instead of just trusting the
operator's word:** an operator who *believes* the root cause is fixed (e.g.,
"I rotated the KIS API key") could still be wrong (typo in the new key). Running
the same broker-connectivity and reconciliation checks that gate normal startup
ensures the system only returns to `NORMAL` when it can independently confirm it
is safe to do so — not merely when a human asserts it.

**Why a cooldown timer after reset:** prevents a trip → reset → trip → reset
thrash loop if the underlying condition is intermittent (e.g., a flapping
network link). The cooldown does not block trading — it only raises the logging
verbosity so the next trip (if any) carries more diagnostic context.

---

## 8. Cross-Process Synchronization

This section directly closes the §2.2 desync gap.

**Fast path — Redis pub/sub:**
Every `WorkerSession` subscribes to `kill:trip` / `kill:reset` at startup
(`EmergencyKillSwitch.subscribe`, §5.3). On receipt, it calls its local
`SAFE_MODE.disable()`/`enable()` directly — typically sub-second propagation.
This is what makes it possible for `WorkerWatchdog` (running in the API process)
to actually halt the worker process at runtime, not just at its next restart.

**Slow path — DB poll fallback:**
Each `WorkerSession`'s main loop (the same loop that already checks
`_stop_event.is_set()` every second, `runner.py:117-125`) additionally polls
`DailyRiskState.kill_level` once per iteration **whenever it has not received a
pub/sub message in the last `KILL_SWITCH_POLL_INTERVAL_SEC` (default 30s)**.
This guarantees correctness even if:

- Redis is down (§3.1 SOFT trigger — itself one of the things that can cause a
  trip, creating a "what watches the watcher" requirement)
- A subscription silently drops (network blip between subscribe and publish)
- The process started after the trip was published (subscribe-after-publish race)

**DB remains authoritative at all times.** The Redis publish in `trip()` (§6) is
explicitly fire-and-forget — if it fails, the DB write (which happens first, in
the same transaction as the audit history append) has already landed, and every
process will observe the new `kill_level` within one poll interval at worst.
This mirrors the platform's existing "Redis unavailable → fall back to DB,
never block on Redis" philosophy already established in
`IDEMPOTENT_EXECUTION.md` §4 (Distributed Lock) and `scheduler.py`'s session
signal dual-write.

**What happens during a Redis outage *while already tripped*:** nothing
changes — the DB-poll fallback is already the active path (no pub/sub messages
are arriving), `SAFE_MODE` stays disabled in every process via its last known
state plus continued polling, and `EmergencyFlattenManager` (if running) is
unaffected because it talks to the broker directly, not through Redis.

---

## 9. Operational Risks

| Risk | Description | Mitigation in this design |
|---|---|---|
| **False-positive HARD trip from a transient balance-fetch failure** | `LossTracker.record_pnl()` could be fed a stale or wrong `current_equity` if `get_kis_broker().get_balance()` times out and `_last_known_equity` is stale (the existing fallback at `runner.py:481`) | `trip()` records `source` and the *exact* equity figures used in `kill_reason_history`; the re-validation pass in `reset()` independently re-fetches balance — a stale-equity false trip will fail to reproduce on reset and the operator sees that explicitly in the validation detail |
| **Auto-flatten selling at terrible prices during a flash crash or wide-spread event** | `EmergencyFlattenManager.flatten_all()` already falls back to `pos.avg_price` when `get_price()` fails (`emergency.py:70-79`) — in a real crash this could be wildly wrong, locking in a worse loss than holding | `AUTO_FLATTEN_ON_KILL` defaults to `false` (operator opt-in per §6); when enabled, `contain()` cancels resting orders *first* (preventing re-entry) and lets the operator decide on liquidation timing for non-MDD triggers; only the MDD-specific policy in `CLAUDE.md` mandates automatic flattening |
| **Trip → reset → trip thrash loop** | An intermittent root cause (flapping network, borderline loss threshold) could cause rapid trip/reset cycles, each one disrupting trading and spamming alerts | Post-reset cooldown timer (§7) plus the escalation rule (§3.2: unresolved SOFT → HARD) bounds how long any oscillation can continue before it becomes a single sustained HARD halt requiring manual investigation |
| **Split-brain during the migration window** (old code path vs. new `EmergencyKillSwitch` running side-by-side during a rolling deploy) | If `LossTracker._fire_kill_switch_alert()` and the new controller both attempt to write `kill_switch` during a deploy that spans both versions | The DB migration backfills `kill_level` from `kill_switch` (§5.2) so both old and new readers see a consistent value; the new controller becomes the *only* writer once deployed — this is a one-way migration, not a dual-write period, and should be deployed during a maintenance window with trading paused |
| **Alert fatigue from SOFT-level trips** | If SOFT thresholds are tuned too sensitively (e.g., the 80%-of-daily-loss pre-warning fires often in normal volatile trading), operators may start ignoring "🚨 긴급 알림" messages, including real HARD trips | SOFT and HARD use visually and textually distinct alert templates (`alert_emergency` vs. a new lower-urgency `alert_warning` for SOFT); SOFT trips do not use the `level="critical"` WebSocket channel reserved for HARD; thresholds are configurable via env vars so they can be tuned post-deployment based on observed SOFT-trip frequency |
| **Operator resets without understanding root cause** | The mandatory `reason` field (§5.1, §7) could be filled with a placeholder like "ok" under pressure, defeating its diagnostic purpose | Not solvable purely in code — but the append-only `kill_reason_history` (§5.2) ensures that even a low-quality reset reason is preserved alongside the validation outcome, the original trip reason, and the timestamps; a future audit can always see exactly what was (or wasn't) said at each step |

---

## 10. Rollback Policy

This section answers: *what state changes can be safely auto-reverted, what
must always require a human, and how do we guarantee the kill switch's own
history is trustworthy?*

### 10.1 What MAY auto-revert

- **SOFT → NORMAL**, automatically, when the triggering condition clears within
  its configured window (§3.2 escalation rule; the diagram's top-left loop-back
  arrow). This is the **only** fully automatic state transition in the entire
  design, and it is still logged (`kill_switch_soft_cleared` audit event) so it
  remains traceable even though it required no human action.

### 10.2 What must ALWAYS require a human

- **Any HARD trip.** There is no code path, env flag, or timer that transitions
  `HARD_HALT → NORMAL` without an operator calling
  `POST /api/admin/kill-switch/reset` with a `reason`. This is intentional and
  non-negotiable: a HARD trip means either real money was lost beyond policy
  limits, or the system can no longer trust its own view of broker state — both
  conditions where "wait and see if it clears itself" is the wrong default.
- **Auto-flatten** (`contain()`'s liquidation step) is itself gated behind an
  explicit operator-set environment flag (`AUTO_FLATTEN_ON_KILL`), not a
  default-on behavior — see §9.
- **Any reconciliation repair that the `RECONCILIATION_ENGINE.md` policy already
  classifies as "manual review required"** remains manual-review-required even
  while the kill switch is tripped; the kill switch does not grant the
  reconciler new auto-repair authority.

### 10.3 What is NEVER reverted — the audit trail itself

- `kill_reason_history` (§5.2) is **append-only**. `reset()` adds an entry; it
  never edits or deletes a prior one — including entries recording a *failed*
  reset attempt (§7). The existing `AuditLog` table (already used identically by
  `EmergencyFlattenManager`, `PositionReconciler`, and the order-polling
  components per `ORDER_POLLING_RELIABILITY.md` §3.7) is the durable backing
  store; this design adds no new persistence mechanism, only a new event
  vocabulary (`kill_switch_triggered`, `kill_switch_reset`,
  `kill_switch_soft_cleared`, `kill_switch_reset_failed`).
- **`kill_reason` is replaced, never merged, on each new trip** — but the
  *history* of every prior `kill_reason` remains in `kill_reason_history`. This
  resolves the KS-09 risk (two writers silently overwriting each other's reason)
  by making the overwrite itself a recorded, attributable event
  (`kill_source` distinguishes "loss_tracker" from "worker_watchdog" from
  "reconciler" from "manual").

### 10.4 Single-writer guarantee

Both `LossTracker._evaluate()` (KS-M1) and `WorkerWatchdog._alert_dead_worker()`
(KS-M9) currently write `DailyRiskState.kill_switch` independently — a
last-write-wins race with no arbitration. Under this design, **both call
`EmergencyKillSwitch.trip()` instead of writing the DB directly.** The
controller serializes trips through the same DB transaction that appends to
`kill_reason_history`, so concurrent triggers from different sources produce two
ordered, attributed history entries rather than one silently clobbering the
other. This is the rollback policy's foundation: you cannot safely roll back a
state change whose origin you cannot reconstruct.

---

## 11. New Code Requirements

| File | Change |
|---|---|
| `backend/execution/kill_switch.py` | **NEW** — `EmergencyKillSwitch`: `trip()`, `contain()`, `reset()`, `status()`, `subscribe()`; Redis pub/sub publish + subscribe; single-writer DB transaction wrapping `DailyRiskState` + `kill_reason_history` |
| `backend/database/models.py` | Add `kill_level`, `kill_source`, `kill_reason_history` columns to `DailyRiskState`; migration backfilling `kill_level` from existing `kill_switch` boolean |
| `backend/quant/risk/engine.py` | `LossTracker._evaluate()` / `_fire_kill_switch_alert()` call `EmergencyKillSwitch.trip("HARD", reason, source="loss_tracker")` instead of directly calling `SAFE_MODE.disable()` + alerting (KS-M1/KS-M2 logic moves into the controller) |
| `backend/worker/heartbeat.py` | `WorkerWatchdog._alert_dead_worker()` calls `EmergencyKillSwitch.trip("HARD", reason, source="worker_watchdog")` instead of its current direct `DailyRiskState` write (`heartbeat.py:140-149`) — removes the second uncoordinated writer (KS-M9 → single-writer per §10.4) |
| `backend/worker/emergency.py` | `EmergencyFlattenManager.flatten_all()` gains a cancel-all-open-orders pass *before* the position-selling loop (§6); `StaleDataWatchdog` is instantiated and wired into `IndicatorStrategy._scan_and_trade()` (closing KS-05 — currently dead code) |
| `backend/worker/runner.py` / `WorkerSession` | Subscribe to `kill:trip`/`kill:reset` Redis channels at session startup (`EmergencyKillSwitch.subscribe`); add `DailyRiskState.kill_level` poll to the existing `_stop_event` loop as fallback (§8) |
| `backend/strategy/base.py` | `_live_trade_allowed()` checks `kill_level`: `"SOFT"` blocks `side == "buy"` only; `"HARD"` blocks both `buy` and `sell` (current binary `SAFE_MODE.can_trade` check becomes level-aware) |
| `backend/execution/circuit_breaker.py` | `ConsecutiveFailureBreaker.record_failure()` optionally reports trips to an injected `EmergencyKillSwitch` for the cross-symbol SOFT-escalation aggregation described in §3.1 |
| `backend/execution/reconciler.py` | `PositionReconciler` calls `EmergencyKillSwitch.trip("HARD", reason, source="reconciler")` on a CRITICAL-severity gap detected **at runtime** (not just inside `StartupRecovery`), closing KS-M11's "only at startup" gap |
| `backend/api/server.py` | New `POST /api/admin/kill-switch/reset` (X-API-Key + `confirm=true` + mandatory `reason`, rate-limited identically to `/api/admin/flatten`); extend `/api/status` and `/api/metrics` to surface `kill_level`, `kill_source`, and `kill_reason_history` (truncated) |
| `api/routers/quick_trade.py` | Add a parallel `kill_level` check for direct API order placement (this path does not go through `StrategyBase.buy/sell`, so it needs its own gate — also flagged as gap DO-10 in `IDEMPOTENT_EXECUTION.md` §11) |

---

## 12. Verification

```bash
# Severity classification: each §3 trigger maps to the documented level
pytest tests/execution/test_kill_switch.py -v -k severity_classification

# Single writer: LossTracker + WorkerWatchdog both route through trip();
# concurrent trips produce two ordered history entries, not a lost update
pytest tests/execution/test_kill_switch.py -v -k single_writer_no_race

# Cross-process propagation: trip() in process A flips SAFE_MODE in process B
# within the pub/sub latency window (Redis available)
pytest tests/execution/test_kill_switch.py -v -k cross_process_propagation_redis

# DB poll fallback: same scenario with Redis unavailable — propagation completes
# within KILL_SWITCH_POLL_INTERVAL_SEC via DailyRiskState.kill_level polling
pytest tests/execution/test_kill_switch.py -v -k cross_process_propagation_db_fallback

# SOFT only blocks buys; sells, cancels, and EmergencyFlattenManager remain allowed
pytest tests/execution/test_kill_switch.py -v -k soft_blocks_buy_only

# HARD blocks both buy and sell via StrategyBase, but EmergencyFlattenManager
# (calling broker.place_order directly) remains the documented exemption
pytest tests/execution/test_kill_switch.py -v -k hard_exempts_flatten_manager

# Escalation: a SOFT condition that persists beyond its window auto-escalates to HARD
pytest tests/execution/test_kill_switch.py -v -k soft_escalates_to_hard_on_timeout

# contain(): orders are canceled BEFORE flatten_all() liquidates positions
pytest tests/worker/test_emergency.py -v -k cancel_before_flatten

# Auto-flatten gated by AUTO_FLATTEN_ON_KILL — disabled by default
pytest tests/worker/test_emergency.py -v -k auto_flatten_opt_in_default_false

# Reset requires operator + reason + passing re-validation; failure preserves
# prior HARD state and appends a "reset_failed" history entry
pytest tests/api/test_admin.py -v -k kill_switch_reset_requires_validation_pass

# Reset re-validation reuses StartupRecovery steps (no duplicated logic)
pytest tests/worker/test_recovery.py -v -k kill_switch_reset_reuses_recovery_steps

# Rollback policy: kill_reason_history is append-only — no entry is ever
# edited or deleted across trip → reset → trip → reset sequences
pytest tests/execution/test_kill_switch.py -v -k history_append_only_invariant

# StaleDataWatchdog wired in: a >30%-stale universe produces a SOFT trip
pytest tests/strategy/test_indicator_strategy.py -v -k stale_universe_triggers_soft_halt

# quick_trade API respects kill_level (closing the parallel-path gap)
pytest tests/api/test_quick_trade.py -v -k kill_switch_blocks_direct_order
```
