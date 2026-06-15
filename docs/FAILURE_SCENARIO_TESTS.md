# Failure Scenario Test Suite — Design Specification

> **Design-only deliverable (TASK 4-1B).** This document defines, for each of the 10
> failure scenarios named in TASK 4-1A's audit (`docs/FAILURE_SCENARIO_AUDIT.md`, ~1,216
> lines, merged via PR #76), the expected behavior, recovery expectations, validation
> assertions, fail-closed rules, and audit-logging expectations that a future test suite
> must implement. The companion file `tests/integration/test_failure_scenarios.py` is a
> **design skeleton**: 10 test classes with `@pytest.mark.skip`-marked stub methods and
> docstrings citing the finding IDs each stub will eventually regression-guard or document.
> **No fixtures, mocks, assertion bodies, or fixes are implemented in this task.** That is
> deferred to a future implementation task (see §9 "Relationship to Future Work").
>
> All finding IDs cited below (`FS-NN`, `DO-NN`, `EX-NN`, `CA-NN`, `SD-NN`, `F1`/`F5`) are
> defined in `docs/FAILURE_SCENARIO_AUDIT.md` and its own cross-referenced source docs
> (`RECONCILIATION_ENGINE.md`, `IDEMPOTENT_EXECUTION.md`, `ORDER_POLLING_RELIABILITY.md`,
> `CORPORATE_ACTION_AUDIT.md`, `STALE_DATA_AUDIT.md`). They are **cross-referenced, not
> re-derived**, here.

---

## §1 Purpose & Scope

### Purpose

TASK 4-1A traced the live pipeline (`Scheduler → Worker → Strategy → Execution → Broker →
Polling → Reconciliation → DB`) under 10 infrastructure-level failure scenarios and produced
a risk classification (8 confirmed-present existing findings, 3 stale-data findings, 3
RESOLVED findings, 7 new `FS-01..FS-07` findings). Its §8 sketched 10 candidate test files
(`tests/failure/test_*.py`) with rough mock/assert ideas, explicitly marked "sketch for TASK
4-1B."

TASK 4-1B turns that sketch into a **concrete design** for one consolidated integration
suite — `tests/integration/test_failure_scenarios.py` — and defines, per scenario, the five
categories the task requires:

1. **Expected behavior** — what the code does today (file:line, cross-referencing audit §3).
2. **Recovery expectations** — what "recovered" means, and whether recovery is automatic,
   manual, or absent.
3. **Validation assertions** — which of the 7 cross-cutting validation dimensions (§3 below)
   apply to this scenario, and what concrete DB/state/log check each maps to.
4. **Fail-closed rules** — what the system *should* do when uncertain, marked `[CURRENT]`
   (already true) or `[TARGET]` (a gap this suite would regression-test once fixed).
5. **Audit logging expectations** — which `AuditLog.event_type` values must appear.

### In Scope

- Design of `docs/FAILURE_SCENARIO_TESTS.md` (this document) — 10 per-scenario
  specifications (§4) plus 3 cross-scenario summary tables (§6-§8).
- Design of `tests/integration/test_failure_scenarios.py` — a skeleton: module docstring,
  shared trivial fixtures (DB factory, mock broker/redis shells — see §2's note on the
  "trivial scaffolding vs. scenario logic" boundary), 10 test classes, and
  `@pytest.mark.skip`-marked stub methods with descriptive docstrings.
- A consolidated **fail-closed rules** table (§6) and **audit logging expectations** table
  (§7) spanning all 10 scenarios — these did not exist before; the audit's §3 covered each
  scenario individually but did not roll them up.
- A **known-gaps map** (§8) tying each skeleton stub to a CURRENT-vs-FIXED expectation pair,
  so a future implementer knows exactly which assertions are "regression guards for good
  behavior" vs. "currently-failing assertions documenting a gap."

### Out of Scope

- Writing working fixtures, mocks, or assertion logic for any of the 10 scenarios — that is
  the next task (§9).
- Fixing any of `FS-01..FS-07`, `DO-01`, `DO-05`, `EX-02`, `EX-04`, `EX-10`, `EX-11`, `CA-03`,
  `CA-04`, `SD-04`, `SD-05`, `SD-09` — cite `docs/FAILURE_SCENARIO_AUDIT.md` §11 "Future
  Work" instead of re-deriving fix designs here.
- Re-deriving the pipeline trace, propagation chains, or risk classification — all of that
  lives in `docs/FAILURE_SCENARIO_AUDIT.md` §2-§6 and is cited by reference.
- Stale-data implementation detail beyond cross-referencing `STALE_DATA_AUDIT.md`/
  `STALE_DATA_DETECTOR.md`'s `SD-01..SD-13` and `SG-01..SG-07` — §4.8 only maps the pipeline
  intersection points already identified by the audit.

---

## §2 Test Suite Architecture

### File Layout

```
tests/
└── integration/
    ├── __init__.py                  # new, empty — matches tests/execution/__init__.py etc.
    └── test_failure_scenarios.py    # new — skeleton, ~10 classes / ~35-45 stub methods
```

`tests/integration/` is a new top-level test package, sibling to the existing
`tests/brokers/`, `tests/data/`, `tests/execution/` (each with its own `__init__.py`, no
shared `conftest.py` anywhere in the repo — see below).

### Fixture & Mocking Conventions Reused From Existing Tests

The codebase has **no `conftest.py`** — every test module defines its own
`@pytest.fixture()` functions. `tests/integration/test_failure_scenarios.py` follows the
same pattern, reusing the exact shapes already proven in
`backend/execution/tests/test_reconciler.py` and `backend/worker/tests/test_recovery_safety.py`:

| Fixture / Helper | Pattern source | Purpose in this suite |
|---|---|---|
| `db_factory()` | `test_reconciler.py:32-38`, `test_recovery_safety.py:34-37` | In-memory SQLite (`create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})`, `Base.metadata.create_all(engine)`, `sessionmaker(bind=engine, expire_on_commit=False)`). Used by every scenario class that touches `Order`/`Fill`/`Position`/`AuditLog`/`DailyRiskState`/`Command`. |
| `mock_broker()` | `test_reconciler.py` (`broker = MagicMock()`) | `MagicMock` with `.get_positions`, `.get_order_status`, `.place_order`, `.get_balance` configurable per test (4.3-4.7, 4.9, 4.10). |
| `mock_redis()` | new, same MagicMock idiom | `MagicMock` whose `.setex`/`.exists`/`.get`/`.ttl`/`.pubsub`/`.ping` can be set to `side_effect=redis.ConnectionError(...)` for a configurable window, then recover (4.1, 4.2, 4.10). |
| `flaky_broker(n_failures)` | new, built on `mock_broker()` | Returns a broker whose `.place_order`/`.get_order_status` raise for the first `n_failures` calls, then succeed — drives `ConsecutiveFailureBreaker` (4.2, 4.5) and `DO-01`/`DO-05` (4.4, 4.5) scenarios. |
| `crashing_poller()` | new, built on `OrderFillPoller` | An `OrderFillPoller` whose `_loop` body is monkeypatched to raise on a configurable tick — drives `EX-10` (4.6). |
| `_insert_db_position()`, `_insert_pending_order()`, `_count_audit()`, `_count_recon_logs()` | `test_reconciler.py:41-107` | Seed/assert helpers reused verbatim for §4.7/§4.9/§4.10's DB-state checks. |
| `_bare_worker()`, `_tracker()`, `_insert_order()` | `test_recovery_safety.py` helpers | Construct `StrategyWorker`/`PositionTracker` without going through `__init__`'s Redis/broker setup — reused for §4.2/§4.3/§4.9/§4.10. |

### The "Trivial Scaffolding vs. Scenario Logic" Boundary

This task's "do not implement code yet" instruction governs **scenario-specific
failure-injection and assertions** — the part of each stub that would actually exercise
`heartbeat.py`, `circuit_breaker.py`, `order_poller.py`, etc. under a simulated failure and
check the outcome. It does **not** prohibit the few lines of `db_factory()`/`MagicMock()`
scaffolding that every existing test file in this repo already contains verbatim — writing
that scaffolding is "design," not "implementation," because it contains zero
scenario-specific logic and is identical to code already merged in `test_reconciler.py`/
`test_recovery_safety.py`. The skeleton therefore:

- **Implements** (trivially, ~40 lines total): `db_factory()`, `mock_broker()`,
  `mock_redis()` — plain constructors/fixtures with no failure-injection wiring.
- **Stubs** (`@pytest.mark.skip`, body `pass`, docstring only): every test method, plus
  `flaky_broker()` / `crashing_poller()` (these *are* scenario-specific — their whole purpose
  is failure injection — so they remain design-only, described in §2's table above but not
  implemented).

### Module-Level Imports (design)

The skeleton imports (for type-reference in docstrings and future implementer convenience,
even though stub bodies don't call them):

```python
import pytest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.models import (
    Base, Order, Fill, Position, AuditLog, DailyRiskState, Command,
)
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import PositionTracker
from backend.execution.order_poller import OrderFillPoller
from backend.execution.circuit_breaker import ConsecutiveFailureBreaker
from backend.worker.heartbeat import WorkerHeartbeat, HeartbeatMonitor, WorkerWatchdog
```

`kis_adapter.auth.KISAuth` / `kis_adapter.client.KISClient` are referenced by name in §4.4's
docstrings but not imported at module scope (their construction requires
`KIS_APP_KEY`/`KIS_APP_SECRET` env vars — a future implementer will need an env-var fixture
or constructor-arg refactor, noted as an open question in §8).

---

## §3 Cross-Cutting Validation Framework

The task names 7 validation dimensions. Each subsection below defines what the dimension
means **concretely** (so a future assertion is unambiguous) and how the suite measures it —
which DB query, in-memory state check, or `AuditLog.event_type` it maps to. §4 then states,
per scenario, which of these 7 dimensions apply.

### §3.1 Safe shutdown

Process termination (SIGKILL/SIGTERM, OOM, crash) at any point must not leave the DB in an
ambiguous half-written state — every `Order`/`Fill`/`Position` row is either fully committed
(all columns populated, transaction committed) or entirely absent. SQLAlchemy commits are
atomic, so "partial row" in practice means *sequencing* risk, not column-level corruption:
the dangerous case is an `Order` row committed as `status="submitted"` before/after the
broker call without the broker call's outcome being known.

- **Measured by**: post-restart query of `orders`/`fills`/`positions`/`audit_logs` — assert
  no row violates its NOT NULL schema (already enforced), and specifically assert that any
  `Order` with `status IN ("pending","submitted")` and a kill mid-flight is one that
  `StartupRecovery._step_reconcile`/`_step_pending_orders` (recovery.py steps 6-7) can
  re-derive truth for via `broker.get_order_status()`.
- **Primary scenario**: §4.3 Process Kill — kill the worker mid `place_order()` → DB-write
  sequence (`backend/execution/order_machine.py`) and verify the DB lands in one of two valid
  states: (a) no `Order` row (died before insert), or (b) `Order` row with
  `status="pending"`/`"submitted"`, `broker_order_id` set-or-NULL — both recoverable on
  restart.
- **Out of scope**: whether the broker-side order was actually placed before the kill — that
  is DO-01 territory (network timeout after broker accepted but before the HTTP response),
  covered under §3.6 and §4.4, not here.

### §3.2 Safe recovery

`StartupRecovery.run()` (`backend/worker/recovery.py:83`) must complete its 9-step sequence
without an uncaught exception, and — critically — must be **idempotent**: running it twice in
a row against an unchanged DB+broker state produces the same `reconcile_actions()` list and
creates **zero** additional `Fill`/`Position`/`AuditLog` rows on the second run. This is the
regression guard for **F1** and **EX-06** (both RESOLVED per the audit).

- **Measured by**: count `AuditLog` rows where
  `event_type IN ("reconcile_insert","reconcile_fix_qty","reconcile_fix_avg_price",
  "reconcile_delete","recovery_inconsistency")` before/after two consecutive `run()` calls —
  the second call's delta must be zero once the system has converged.
- **F1 guard**: `_step_pending_orders`'s `_make_recovery_fill_cb` (recovery.py:241-275) checks
  for an existing `Fill` row by `(order_id, qty, price)` before inserting — re-running
  recovery against the same broker-reported fill must not double-insert.
- **EX-06 guard**: cross-ref audit §6 for the mechanism; §4.10's
  `TestServerRestartScenario` re-validates no regression.
- **`_step_validate_state` is the exception to "no new rows on rerun"**: it is explicitly
  non-mutating (recovery.py:369) and does NOT dedup its own `recovery_inconsistency` writes —
  if the underlying bad row (e.g. `qty<=0` position) is not cleaned up between runs, running
  validate twice legitimately produces *two* `recovery_inconsistency` rows. The suite must
  not conflate this stateless-observability behavior with the F1/EX-06 dedup requirement,
  which applies only to the *mutating* steps (5-7).
- **"Completes without raising"**: every step already catches its own exceptions
  (recovery.py:96-107 also wraps each step) — `run()` should not raise even when every
  dependency (DB/Redis/broker) is a failing mock; the only way it raises is a bug in the
  step-iteration loop itself.

### §3.3 State consistency

Two axes: **(a) DB vs. broker** — `positions.qty`/`avg_price` must match
`broker.get_positions()` within `PositionReconciler._QTY_TOLERANCE` (±1 share,
`reconciler.py:96`) after `reconcile()` runs. **(b) in-memory vs. DB** —
`PositionTracker._positions` (`position_tracker.py:36`) must match the `positions` table
after `restore_positions()` (`position_tracker.py:118-124`) is called with DB-sourced
`Position` objects.

- **Measured by (a)**: call `reconciler.reconcile("manual")` against a mock broker with known
  positions, query `db.query(DBPosition).all()`, assert qty/avg_price within tolerance, and
  assert any repairs produced matching `reconcile_fix_qty`/`reconcile_fix_avg_price`
  `AuditLog` rows (ties to §3.4).
- **Measured by (b)**: construct `PositionTracker`, call `restore_positions([...])`, then
  `tracker.get_position(symbol)` for each seeded symbol and assert dataclass-field equality.
- **Edge case (FS-05, cross-ref §3.7)**: between reconcile cycles, axis (b) can diverge from
  axis (a) if a fill is processed in-memory but its DB write races or fails on a separate
  path. This divergence is bounded by the periodic reconcile interval (30 min,
  `reconciler.py:7`) — it is the *documented exception*, not a violation of this dimension.
- §4.7 (Reconciliation Failure) and §4.9 (Duplicate Event) are the primary scenarios.

### §3.4 Audit log generation

Every state-changing operation in the reconcile/recovery/fill path writes an `AuditLog` row
(`backend/database/models.py:127-136`: `event_type`, `symbol`, `order_id`, `actor`, `detail`
JSON, `created_at`). This dimension asserts the **presence** of the correct `event_type`
values per scenario — not exact `detail` JSON contents (left to TASK 4-1C).

- **Canonical taxonomy observed**: `reconcile_insert` / `reconcile_fix_qty` /
  `reconcile_fix_avg_price` / `reconcile_delete` (`reconciler.py:194-270`,
  `actor=f"reconciler:{broker_name}"`); `recovery_inconsistency`
  (`recovery.py:350-367`, `actor="recovery"`); `kill_switch` (risk engine, cross-ref audit);
  `fill` (order-fill persistence path, cross-ref EX-04/EX-11).
- **Measured by**: `_count_audit(db, event_type=...)`-style helper (pattern from
  `test_reconciler.py:78-107`) — `db.query(AuditLog).filter(AuditLog.event_type == X).count()`.
- §7 consolidates, per scenario, which `event_type` values MUST appear vs. which are
  `[TARGET]`-only (proposed, not yet emitted by any code path).
- **Gap to flag, not fix**: `_audit_position_change`/`_audit_inconsistency` are both
  "fire-and-forget" (try/except around the AuditLog write itself —
  `reconciler.py:405-418` / `recovery.py:350-367`). If the AuditLog write itself fails (DB
  momentarily unavailable at that instant, while the main operation succeeded), the calling
  step still reports success and the audit row is silently lost. No scenario in §4 models
  "main op succeeds, audit write specifically fails" — noted as an assertion this suite
  cannot make, not a gap to fix here.

### §3.5 Kill switch behavior

`DailyRiskState` (`backend/database/models.py:92-101`): `trade_date` (PK), `kill_switch:
bool`, `kill_reason: str`, one row per day. Set by `WorkerWatchdog._alert_dead_worker()`
(`heartbeat.py:128-159`, FS-01 path) and by the risk engine on daily-loss/MDD breach
(cross-ref audit, not re-read this task).

- **Measured by**: `db.get(DailyRiskState, date.today())` — assert `.kill_switch is True` and
  `.kill_reason` is a non-empty string matching the triggering condition.
- **FS-01 gap, called out explicitly**: `WorkerWatchdog._alert_recovery()`
  (`heartbeat.py:171-176`) only publishes a websocket "복구됨" (recovered) alert — it does
  **not** clear `kill_switch` or annotate `kill_reason` with a resolution marker. Once a
  Redis-outage-triggered kill-switch fires, the row stays `kill_switch=True` for the rest of
  the `trade_date` even after the heartbeat resumes, requiring **manual** clear to resume
  same-day trading. `[CURRENT]` = "kill switch set, never auto-cleared, fail-closed by
  default (correct)"; `[TARGET]` (§4.1) = "recovery path additionally annotates
  `kill_reason` with a resolution timestamp, but staying OFF until a human confirms remains
  the default" — i.e. the gap is about *annotation*, not about *unsafely* auto-clearing.
- `_step_enable_trading` (`recovery.py:434-466`) reads a kill-switch set in a *previous*
  session (via `_step_risk`'s `_kill_switch_active`) and refuses to re-enable `SAFE_MODE` —
  this is the cross-restart enforcement that makes "sticky until manual clear" fail-closed
  across process restarts too (§4.10 regression guard).
- §4.1 (Redis Down) and §4.10 (Server Restart) are the primary scenarios; §6 cross-references
  the `[CURRENT]`/`[TARGET]` split per scenario.

### §3.6 No duplicate orders

`Order.idempotency_key` (`backend/database/models.py:28`,
`UniqueConstraint("idempotency_key", name="uq_orders_idempotency")`) is the DB-level
guarantee: two `place_order()` calls with the same idempotency key cannot both insert an
`Order` row (the second raises `IntegrityError`, caught by the calling code — cross-ref
EX-04/audit for the exact catch site).

- **Measured by**: insert two `Order` rows with the same `idempotency_key` against
  `db_factory()`'s in-memory SQLite; assert the second raises `sqlalchemy.exc.IntegrityError`
  (or that application code catches it and returns the existing row — whichever the actual
  call site does, cross-ref §4.9).
- **DO-01 boundary, called out explicitly**: this dimension only proves the DB has at most
  one `Order` row per idempotency key. It says nothing about whether the **broker** placed a
  duplicate for that same logical intent — e.g. DO-01 (network timeout after the broker
  accepts an order but before the HTTP response reaches the client) can cause a client retry
  with a *new* idempotency key (because the first was never recorded), producing two
  broker-side orders for one DB-tracked intent, or zero DB rows for an order the broker did
  execute ("ghost order"). No DB-only assertion can detect this; §4.4's
  `TestNetworkTimeoutScenario` documents (does not fix) this boundary as a `[TARGET]` for
  future work (cross-ref audit §11).
- §4.9 (Duplicate Event) is the primary scenario for the DB-level guarantee; §4.2/§4.5 (Worker
  Restart / Broker API Failure) are secondary — a retried `place_order` after a broker-error
  response is the mechanism that could create the duplicate.

### §3.7 No position damage

Deliberately narrow definition: `positions.qty` must never go negative, and must never
diverge from broker truth for longer than **one reconcile cycle** (30 minutes — the periodic
reconcile interval, `reconciler.py:7`). This is **not** "no divergence ever" —
`PositionTracker._positions` (in-memory) can transiently disagree with DB/broker between a
fill and the next reconcile (FS-05), and that transient divergence is accepted as correct,
bounded behavior.

- **Measured by**:
  (a) `db.query(DBPosition).filter(DBPosition.qty < 0).count() == 0` at all times — the
  schema has no `CheckConstraint` on `qty`, so this is an application-level invariant checked
  by inspection, not a DB constraint;
  (b) after injecting a known divergence (e.g. directly mutate `PositionTracker._positions`
  to simulate a missed fill), call `reconciler.reconcile()` and assert DB `Position.qty`
  converges to the mock broker's reported qty within `_QTY_TOLERANCE` — i.e. "ground truth
  recovers within one reconcile cycle."
- `_step_validate_state` (`recovery.py:388`,
  `db.query(DBPosition).filter(DBPosition.qty <= 0).all()`) is the existing **detection**
  mechanism for non-positive qty (catches the `==0` "should have been deleted on flat" case
  too) — it only writes a `recovery_inconsistency` AuditLog row (§3.4), it does not auto-fix.
  The suite should assert detection fires, not that the row is corrected.
- §4.5 (Broker API Failure), §4.6 (Polling Failure), §4.7 (Reconciliation Failure), and §4.9
  (Duplicate Event) are the primary scenarios. §4.6 (EX-02/EX-10, up to ~1.5h fill-loss per
  audit §9) is most likely to *approach* the 30-min bound: if the poller thread itself is
  dead (EX-10), no fill arrives to even create a qty divergence — reconcile's
  `qty_mismatch` repair (`reconciler.py:216-231`) still catches the position drift within one
  cycle (satisfying this dimension), but the underlying `Fill`/`AuditLog` row for that
  missed fill event is permanently lost. That loss is an EX-10-specific audit-trail gap
  (tracked in §8), not a "position damage" violation under this dimension's DB-qty-focused
  definition.

---

## §4 Per-Scenario Test Specifications

Each subsection cites finding IDs + severities from audit §6, describes current behavior with
file:line, states recovery expectations, maps applicable §3 dimensions to concrete
assertions, lists fail-closed rules (`[CURRENT]`/`[TARGET]`), lists expected `AuditLog`
`event_type`s, and maps to the skeleton's test class/methods (Deliverable 2, §5).

### §4.1 Redis Down

**Audit Cross-Refs**: `FS-01` (HIGH, NEW) — audit §3.1, §6, §8.1.

**Expected Behavior** (current code):
- Worker-side degrades gracefully: `_run_with_pubsub()` (`runner.py:207-242`) catches
  `redis.ConnectionError`, exponential backoff (2s→64s cap), falls into
  `_enter_db_polling_mode()` (`runner.py:243-282`), polling the `Command` table every 30s and
  retrying `redis.ping()` to detect recovery.
- `WorkerHeartbeat._beat()` (`heartbeat.py:43-48`) catches and logs any exception — the
  heartbeat TTL key silently stops refreshing during the outage.
- Cross-process: `HeartbeatMonitor.is_alive()` (`heartbeat.py:59-64`) calls
  `redis_client.exists(_HB_KEY)`; during the outage this raises, and `except Exception: return
  False` makes "Redis unreachable" indistinguishable from "worker process dead."
- `WorkerWatchdog._check()` (`heartbeat.py:117-126`) sees `alive=False`, `_was_dead` flips
  False→True, calls `_alert_dead_worker()` (`heartbeat.py:128-169`) → writes
  `DailyRiskState.kill_switch=True`, `kill_reason="Worker 하트비트 없음 — 프로세스 재시작
  필요"`.
- On Redis recovery, `_check()` sees `alive=True`, `_was_dead` True→False, calls
  `_alert_recovery()` (`heartbeat.py:171-176`) — publishes a WS `"Worker 하트비트 복구됨"`
  info-level alert ONLY.

**Recovery Expectations**:
- Worker side: **automatic** — pubsub resumes once `redis.ping()` succeeds
  (`runner.py:250-252`).
- Kill-switch side: **manual** — `kill_switch` stays `True`; `_reset_daily_risk()`
  (`scheduler.py:108-132`) sees a stale `kill_switch==True` and refuses to re-arm `SAFE_MODE`
  until an operator clears the `DailyRiskState` row in Postgres.

**Validation Assertions** (§3 dimensions):
- **§3.5 (Kill switch)** — primary. Drive `mock_redis().exists.side_effect =
  redis.ConnectionError(...)`, call `WorkerWatchdog._check()`, assert
  `DailyRiskState.kill_switch is True` and `kill_reason ==
  "Worker 하트비트 없음 — 프로세스 재시작 필요"`.
- **§3.5 (cont.)** — restore `mock_redis().exists.return_value = 1`, call `_check()` again;
  assert `_alert_recovery()`'s WS publish fires (`level="info"`) but `kill_switch` remains
  `True` — this is the `[CURRENT]` FS-01 gap assertion.
- **§3.4 (Audit log)** — assert NO `AuditLog` row is written for either the kill-switch set
  or the recovery (`_alert_dead_worker`/`_alert_recovery` mutate `DailyRiskState`/WS only —
  contrast with the risk engine's separate `event_type="kill_switch"` AuditLog write,
  `backend/quant/risk/engine.py:442`, a different trigger not exercised by this scenario; §7
  documents the distinction).
- §3.1/§3.2/§3.6/§3.7 — n/a; no `Order`/`Fill`/`Position` mutation occurs in this scenario.

**Fail-Closed Rules**:
- `[CURRENT]` `kill_switch` defaults to staying SET when in doubt (fail-closed — trading
  stays blocked). Correct, conservative default.
- `[CURRENT]` `kill_reason` text says "프로세스 재시작 필요" even when the true cause is a
  Redis blip — misdirects operator triage.
- `[TARGET]` `HeartbeatMonitor.is_alive()`/`WorkerWatchdog._check()` should distinguish "Redis
  itself unreachable" (its own `ping()`/`exists()` raised `ConnectionError`) from "heartbeat
  key genuinely expired while Redis is reachable" — two different root causes currently
  collapse to the same `alive=False`.
- `[TARGET]` `_alert_recovery()` should additionally annotate `kill_reason` with a resolution
  marker (e.g. append `" — Redis 복구됨 (워커 생존 추정)"`) so an operator's manual-clear
  decision is informed. `kill_switch` itself should **remain `True`** (fail-closed) until a
  human confirms — auto-clearing is explicitly NOT the target.

**Audit Logging Expectations**:
- None of this scenario's state changes go through `AuditLog` in current code. `[TARGET]`-
  proposed `event_type="watchdog_kill_switch"` / `"watchdog_recovery"` (proposed, not
  implemented) would close this gap — see §7.

**Skeleton Mapping**: → `TestRedisDownScenario` —
`test_heartbeat_survives_redis_outage`, `test_watchdog_sets_kill_switch_on_connection_error`,
`test_recovery_alert_does_not_clear_kill_switch` (FS-01 gap).

### §4.2 Worker Restart

**Audit Cross-Refs**: `FS-02` (MEDIUM, NEW); `F1`, `EX-06` (RESOLVED — regression guards) —
audit §3.2, §6, §8.2.

**Expected Behavior** (current code):
- `StartupRecovery.run()` (`recovery.py:83-108`) executes its 9-step sequence on every worker
  boot: DB check → Redis check → restore `DailyRiskState` → broker balance → broker positions
  → `reconcile("startup")` → re-register pending orders with the poller →
  `_step_validate_state` → `SAFE_MODE.enable()`.
- In-memory state reset by any restart: `ConsecutiveFailureBreaker._failures`/`_tripped_at`
  (`circuit_breaker.py:20-21`, plain instance attributes, no persistence), the 5-minute
  market-open dedup cache, `OrderFillPoller._entries` (rebuilt from DB by step 7).
- If the breaker was OPEN (mid-cooldown after threshold-consecutive failures) at restart time,
  the new `ConsecutiveFailureBreaker` instance starts `_failures=0, _tripped_at=None` —
  closed — silently, with no "fresh vs. reset" log distinction.

**Recovery Expectations**:
- Orders/positions/risk-state: **automatic** via `StartupRecovery` (well-tested per
  `test_recovery_safety.py`).
- Breaker state: **none** — FS-02. If the underlying broker/network issue that tripped the
  breaker is still ongoing, the restarted worker immediately resumes hitting the same failing
  calls instead of respecting the remaining cooldown.
- Heartbeat/watchdog interaction: a restart taking >90s between the last pre-restart heartbeat
  write and the first post-restart write can trigger the same FS-01 chain (§4.1) even for a
  clean, intentional restart — `WorkerHeartbeat.start()` (`heartbeat.py:28-34`) beats
  immediately but only AFTER `StartupRecovery.run()` completes, and steps 4/5 each carry a
  30s `ThreadPoolExecutor` timeout (`_BROKER_STARTUP_TIMEOUT`, `recovery.py:26`).

**Validation Assertions**:
- **§3.2 (Safe recovery)** — primary. Construct `StartupRecovery` against a seeded DB (one
  pending order, one position) plus a mock broker reflecting matching/mismatching state; call
  `run()` twice; assert the second run's `AuditLog` delta for
  `reconcile_*`/`recovery_inconsistency` is zero (F1/EX-06 regression guard, §3.2).
- **§3.6 (No duplicate orders)** — re-run `_step_pending_orders` twice; assert no second
  `Fill` row for the same `(order_id, qty, price)` (F1 guard, `recovery.py:250-258`).
- **§3.5 (Kill switch, cross-ref §4.1)** — if `_step_risk` restores `kill_switch=True` from a
  prior session, assert `_step_enable_trading` returns `False` and `SAFE_MODE.can_trade is
  False` (sticky-kill-switch-across-restart, regression guard).
- **FS-02 (no §3 dimension maps directly — documents an in-memory gap, not DB)**: construct
  `ConsecutiveFailureBreaker`, call `record_failure()` 3x to trip it, discard the instance
  (simulating restart), construct a NEW instance, assert `is_open() is False` —
  `[CURRENT]` documents this as the gap.

**Fail-Closed Rules**:
- `[CURRENT]` breaker resets to closed on restart. `[TARGET]`: either (a) persist
  `_failures`/`_tripped_at` to Redis/DB keyed by broker name and restore it in
  `StartupRecovery`, or (b) rely on `_step_balance`/`_step_positions`'s independent 30s
  broker-timeout gates (`recovery.py:154-184`) as a substitute health check — if the broker is
  still down, these steps fail and `SAFE_MODE.disable()` is called, which is itself
  fail-closed even without the breaker. The suite should document which of (a)/(b) is
  assumed once implemented.
- `[CURRENT]` a heartbeat-write gap during a slow recovery can falsely trigger FS-01's
  kill-switch chain even for intentional restarts. `[TARGET]`: `WorkerHeartbeat.start()`'s
  immediate `_beat()` (`heartbeat.py:29`) should be called BEFORE `StartupRecovery.run()`
  begins, not after, so the 90s TTL clock starts at process-start rather than
  post-recovery.

**Audit Logging Expectations**:
- `reconcile_insert`/`reconcile_fix_qty`/`reconcile_fix_avg_price`/`reconcile_delete` (from
  step 6's `PositionReconciler.reconcile("startup")`), `recovery_inconsistency` (step 8) —
  all MUST appear exactly once per genuinely-new issue, ZERO times on a no-op second `run()`.

**Skeleton Mapping**: → `TestWorkerRestartScenario` —
`test_recovery_idempotent_second_run` (F1/EX-06 guard),
`test_pending_order_fill_dedup_across_restart` (F1),
`test_breaker_resets_on_restart` (FS-02, documents gap),
`test_kill_switch_blocks_reenable_across_restart` (§3.5 cross-ref).

### §4.3 Process Kill

**Audit Cross-Refs**: `EX-02` (CRITICAL, CONFIRMED PRESENT — extended to "process-kill
mid-pipeline" variant); `CA-03`/`CA-04` (HIGH, CONFIRMED PRESENT); `FS-01` (correct detection
in this case) — audit §3.3, §6, §8.3.

**Expected Behavior** (current code):
- No SIGTERM/graceful-shutdown hook exists in `runner.py:main()`. A SIGKILL/OOM-kill can land
  anywhere in the 6-step fill pipeline (`_make_fill_callback`, `runner.py:429-511`):
  `machine.process_fill` → `tracker.on_fill` → P&L/kill-switch check →
  `_persist_order`/`_persist_fill` (DB write, idempotency-keyed) → `_upsert_position_db` → WS
  push.
- If killed AFTER `tracker.on_fill()` mutates the in-memory `PositionTracker` but BEFORE
  `_persist_fill`/`_upsert_position_db` commit: the in-memory mutation is destroyed by the
  kill itself (no lasting effect), but the DB `Position`/`Fill` rows remain at their pre-fill
  values.
- `WorkerHeartbeat` stops; `WorkerWatchdog` correctly detects this (heartbeat genuinely
  stops) — `_alert_dead_worker()` fires as designed. This is the ONE Redis-down-shaped
  response that is CORRECT for this scenario.

**Recovery Expectations**:
- On restart, `reconcile("startup")` (recovery step 6) compares DB positions against the
  broker (ground truth, already reflecting the fill) → detects `qty_mismatch` → CA-03's
  unconditional overwrite (`reconciler.py:206-231`) corrects `Position.qty`/`avg_price` —
  **automatic**.
- NO `Fill` row is ever created for the fill that happened during the kill window — qty
  becomes correct, but the audit trail (used for P&L attribution, `EquitySnapshot`
  reconciliation) permanently lacks that fill, with NO alert distinguishing "expected CA-03
  repair" from "repair masking a lost fill."
- `_step_validate_state` (`recovery.py:369-432`) does NOT surface this — it checks `qty<=0`
  positions, orphaned pending orders, and stale pending orders, none of which match "qty
  correct but Fill row missing."

**Validation Assertions**:
- **§3.1 (Safe shutdown)** — primary. Simulate the kill by calling `tracker.on_fill(fill)`
  then stopping (no `_persist_fill`/`_upsert_position_db`); assert DB `Position`/`Fill`/`Order`
  reflect the PRE-kill state — `db.query(DBFill).count()` unchanged,
  `db.query(DBPosition)...qty` unchanged.
- **§3.3 (State consistency)** — after the simulated kill, run `reconciler.reconcile(
  "startup")` against a mock broker reflecting the POST-fill qty; assert `DBPosition.qty` now
  matches the broker (CA-03 repair) and a `reconcile_fix_qty` `AuditLog` row exists.
- **§3.7 (No position damage)** — assert the repaired `qty` is correct and non-negative —
  "ground truth recovers within one reconcile cycle" holds.
- **§3.4 (Audit log)** — `[TARGET]` assertion documenting the CA-03/CA-04 gap: assert
  `db.query(DBFill).filter(DBFill.order_id == order_id).count() == 0` even AFTER the
  `reconcile_fix_qty` repair — the qty is fixed, but the `Fill` row for that fill is
  PERMANENTLY missing, with no audit trail distinguishing this from any other qty-drift
  cause.
- **§3.5 (Kill switch)** — assert `WorkerHeartbeat` stopping causes
  `WorkerWatchdog._alert_dead_worker()` to fire and `DailyRiskState.kill_switch=True` — unlike
  §4.1, this IS the correct response, so this is a "should remain True" regression guard, not
  a gap.

**Fail-Closed Rules**:
- `[CURRENT]` CA-03's unconditional `qty`/`avg_price` overwrite is fail-closed in the sense
  that DB always converges to broker truth — correct for position-qty purposes.
- `[CURRENT]` the missing `Fill` row after a CA-03 repair is silently masked — no alert, no
  audit-trail entry distinguishing "repair due to lost-fill-during-kill" from any other qty
  drift cause (corporate action, manual trade, etc. — CA-04's missing "why" field).
- `[TARGET]` when `_reconcile_positions()` performs a `reconcile_fix_qty` repair AND no
  corresponding `Fill` row exists for the qty delta, emit a distinct `AuditLog` event (e.g.
  `reconcile_fix_qty_no_fill_record`) flagging "position corrected but fill/P&L history may
  be incomplete for this symbol" — flag the uncertainty rather than silently normalizing it.
- `[CURRENT]` `WorkerWatchdog`'s response to a genuine process death is identical to its
  response to a Redis blip (`kill_switch=True`, manual reset) — conflates "investigate the
  worker" with "investigate Redis" (cross-ref §4.1's `[TARGET]`).

**Audit Logging Expectations**:
- `reconcile_fix_qty` (CA-03 repair) MUST appear. `fill` event for the lost fill MUST NOT
  appear (documents the gap). `[TARGET]`-proposed `reconcile_fix_qty_no_fill_record` —
  proposed, not implemented.

**Skeleton Mapping**: → `TestProcessKillScenario` —
`test_kill_mid_pipeline_no_partial_db_write` (§3.1),
`test_startup_reconcile_repairs_qty_after_kill` (CA-03, §3.3/§3.7),
`test_missing_fill_row_after_repair_undetected` (CA-03/CA-04 gap, §3.4 `[TARGET]`),
`test_watchdog_correctly_flags_genuine_death` (§3.5 regression guard).

### §4.4 Network Timeout

**Audit Cross-Refs**: `DO-01` (CRITICAL, CONFIRMED PRESENT); `SD-04` (MEDIUM, CONFIRMED
PRESENT — cross-ref `STALE_DATA_AUDIT.md`, not re-derived); `FS-07` (MEDIUM-HIGH, NEW) —
audit §3.4, §6, §8.4.

**Expected Behavior** (current code) — three independent surfaces, all using `requests` with
`timeout=10`:
1. **GET/POST body retries** (`client.py:41-54` GET, `56-96` POST) — 3x retry, 1s sleep, 10s
   per-attempt timeout. GET is idempotent (safe). POST (`place_order`) retried after a timeout
   that occurred AFTER the broker accepted but BEFORE the response arrived → DO-01 "ghost
   order": a second order submitted for the same logical intent.
2. **`_get_fx()`** (`backend/brokers/kis.py:300-317`) — `yfinance` fetch with a 1h TTL cache;
   on failure (incl. timeout) falls back to the cached rate, logging a warning only if the
   cache itself is >30min stale (SD-04) — feeds the kill-switch equity calculation ("킬스위치
   계산 부정확 가능" per the source comment).
3. **`KISAuth.get_hashkey()`/`get_headers()`→`_issue_token()`** (`auth.py:65-75,90-99`) — a
   single `requests.post(..., timeout=10)`, NO try/except, NO retry — computed BEFORE
   `KISClient.get()`/`post()`'s retry loop even begins (`client.py:38,57-58,65`). A timeout
   here fails the ENTIRE call immediately, with zero retries — even though the data/order
   endpoint itself would have been fine.

**Recovery Expectations**:
- GET/POST body retries: **automatic** (existing 3x/1s).
- FX fallback: **automatic but possibly stale/wrong** (no escalation beyond a log warning).
- Auth/hashkey timeout: **not retried within the call** — the calling code (`_scan_and_trade`'s
  per-symbol try/except, or `place_order`'s try/except→`REJECTED`) treats it the same as any
  other broker error — net effect: a "wasted" strategy cycle for that symbol, or a spurious
  `REJECTED` order, on what was — from the trading endpoint's perspective — a non-event.

**Validation Assertions**:
- **§3.6 (No duplicate orders)** — primary for DO-01. Patch `requests.post` to raise
  `Timeout` on the FIRST call to the order-placement URL while the mock broker records the
  order as having succeeded server-side; assert the retry submits a SECOND
  `idempotency_key`-bearing request — `[CURRENT]` documents that the DB can end up with two
  `Order` rows with DIFFERENT `idempotency_key`s for one logical intent (ghost order), per
  §3.6's DO-01 boundary note (the mock broker stub stands in for "broker truth" the DB cannot
  see directly).
- **§3.4 (Audit log)** — for surface 3 (FS-07): patch `requests.post` to raise `Timeout` only
  for URLs containing `/oauth2/tokenP` or `/uapi/hashkey`; assert `KISClient.post()` raises
  IMMEDIATELY with `requests.post` called exactly ONCE (zero retries) — `[CURRENT]`,
  documents FS-07.
- **§3.4 (cont., SD-04 cross-ref only)** — mock `yfinance.Ticker(...).history()` to raise
  `Timeout`; assert `_get_fx()` returns the cached rate, and if cache age >30min, assert the
  existing SD-04 warning log fires (regression guard, not a new assertion).
- §3.1/§3.2/§3.7 — n/a (no process restart/shutdown in this scenario); §3.5 — n/a directly
  (a SD-04-skewed daily-loss calc tripping the kill switch is theoretically possible but out
  of scope for this scenario's assertions).

**Fail-Closed Rules**:
- `[CURRENT]` GET retries are safe (idempotent) — correct, no change needed.
- `[CURRENT]` POST retries can create ghost orders (DO-01). `[TARGET]`: cross-ref audit §11
  for the broker-side idempotency-token fix (out of scope here, cited only).
- `[CURRENT]` FX fallback never blocks/raises even when stale. `[TARGET]`: cross-ref SD-04's
  own fix recommendation in `STALE_DATA_AUDIT.md` (not re-derived here).
- `[CURRENT]` auth/hashkey timeout fails the whole call with zero retries (FS-07). `[TARGET]`:
  wrap `get_hashkey()`/`_issue_token()` in their own try/except + retry (e.g. 2x/1s,
  matching the spirit of `client.py`'s existing retry), OR move `get_headers()`/
  `get_hashkey()` computation INSIDE `client.get()`/`post()`'s existing retry loop so a
  transient auth-endpoint blip is retried like any other transient failure.

**Audit Logging Expectations**:
- No dedicated `event_type` exists for "possible ghost order detected" (DO-01) —
  `[TARGET]`-proposed `event_type="duplicate_order_suspected"` (proposed, not implemented).
  FS-07 timeout: no `AuditLog` event currently — `[TARGET]`-proposed
  `event_type="auth_endpoint_timeout"` (proposed, not implemented).

**Skeleton Mapping**: → `TestNetworkTimeoutScenario` —
`test_post_retry_after_timeout_may_duplicate_order` (DO-01, `[CURRENT]`),
`test_auth_hashkey_timeout_fails_without_retry` (FS-07, `[CURRENT]`),
`test_fx_fallback_on_timeout_uses_stale_cache` (SD-04, regression guard).

### §4.5 Broker API Failure

**Audit Cross-Refs**: `DO-05` (HIGH, CONFIRMED PRESENT); `FS-02` (cross-ref §3.2/§4.2) —
audit §3.5, §6, §8.5.

**Expected Behavior** (current code):
- `KISBroker` (`kis.py:48`) owns a `ConsecutiveFailureBreaker(threshold=5,
  cooldown_minutes=10)`; `IndicatorStrategy` (`indicator/strategy.py:51`) owns its own
  `ConsecutiveFailureBreaker(threshold=3, cooldown_minutes=30)` — two independent breakers,
  different thresholds/cooldowns.
- `place_order()` (`kis.py:106-150`): on exception, calls `record_failure()`; special-cases
  `MarketClosedError` (138-144) which does NOT increment the failure counter (expected/benign,
  should not trip the breaker).
- Once `is_open()` returns `True` (≥threshold consecutive failures within the cooldown
  window), subsequent `place_order` calls short-circuit to `REJECTED` WITHOUT calling the
  broker.
- `get_order_status()` (`kis.py:193-283`) try/except → returns `None` on failure;
  `OrderFillPoller._poll_one()` treats `None` as "no update this cycle" — the entry remains
  registered and is retried on the next 5s tick until `is_timed_out` (30min).
- `record_failure()` (`circuit_breaker.py:23-30`) logs `logger.error` ONLY at the
  threshold-crossing transition (`_tripped_at` None→value) — no Telegram/WS alert.
- DO-05: `place_order()` sends no broker-side idempotency token — only app-level
  `idempotency_key` fingerprinting guards against duplicates.

**Recovery Expectations**:
- Breaker auto-closes after `cooldown_minutes` elapses (`circuit_breaker.py:38-47`,
  `is_open()` checks `elapsed >= self._cooldown` and resets `_failures=0,
  _tripped_at=None`) — **automatic**.
- FS-02 (cross-ref §4.2): breaker state is lost on worker restart — if restart happens
  mid-cooldown, the new instance starts closed.
- `get_order_status()`→`None` accumulation: masked downstream by `_periodic_reconcile`'s
  `lost_order` check (age >1h, `reconciler.py:334-340`) — the ONLY detection mechanism,
  itself only a `ReconciliationLog`/`gap` entry, not an alert.

**Validation Assertions**:
- **§3.7 (No position damage) / breaker behavior** — primary. Using `flaky_broker(
  n_failures=5)` (or driving `ConsecutiveFailureBreaker` directly with a mock broker whose
  `place_order` raises 5x), call `record_failure()` 5 times; assert `is_open() is True` and
  the 6th `place_order` attempt short-circuits to `REJECTED` WITHOUT the mock broker's
  `place_order` being called (`mock.assert_not_called()` after the trip).
- **§3.4 (Audit log)** — assert `logger.error` (or equivalent) fires EXACTLY ONCE at the
  threshold-crossing call (5th `record_failure()`), not on subsequent calls while `is_open()`
  remains `True` — AND (documenting the gap) assert NO Telegram/WS `publish_alert`/
  `alert_emergency` call occurs — `[TARGET]` would add one.
- **§3.6 (No duplicate orders, DO-05 cross-ref)** — n/a as a DB-level assertion (DO-05 is
  about broker-side idempotency, the same boundary as §3.6's DO-01 note) — cite, don't
  re-assert.
- **§3.3 (State consistency)** — `get_order_status()` returning `None` for N consecutive
  polls: assert the `_PollEntry` remains in `OrderFillPoller._entries` with `next_poll_at`
  advanced each time (not removed) — until `is_timed_out` after 30min.
- **Cooldown recovery** — advance past `_cooldown` (via `time.monotonic` patch or a
  test-only short `cooldown_minutes`); assert `is_open()` transitions back to `False` and
  `_failures` resets to `0` — automatic recovery, regression guard.

**Fail-Closed Rules**:
- `[CURRENT]` breaker-open correctly blocks ALL orders (fail-closed — no orders placed while
  the broker is suspected unhealthy). Correct.
- `[CURRENT]` breaker trip is `logger.error`-only. `[TARGET]`: emit a Telegram/WS alert
  (`alert_emergency`/`publish_alert`) at the threshold-crossing transition, mirroring the
  pattern already used by `WorkerWatchdog._alert_dead_worker()` (`heartbeat.py:160-169`) for a
  structurally similar "something is wrong, notify operator" event.
- `[CURRENT]` `MarketClosedError` correctly excluded from the failure counter — correct, no
  change.
- `[CURRENT]`/FS-02 — breaker state not persisted across restart (cross-ref §4.2's
  `[TARGET]`).

**Audit Logging Expectations**:
- No dedicated `AuditLog.event_type` exists for "breaker tripped"/"breaker recovered" —
  `[TARGET]`-proposed `event_type="breaker_tripped"`/`"breaker_recovered"` (proposed, not
  implemented). `lost_order` gaps (from `get_order_status()`→`None` accumulation) are visible
  only via `ReconciliationLog.detail` JSON (`gaps` list, `kind="lost_order"`), not a
  dedicated `AuditLog` row.

**Skeleton Mapping**: → `TestBrokerApiFailureScenario` —
`test_breaker_opens_after_threshold_failures`,
`test_breaker_open_short_circuits_without_broker_call`,
`test_breaker_trip_logs_but_does_not_alert` (`[TARGET]` gap),
`test_breaker_auto_recovers_after_cooldown`,
`test_get_order_status_none_keeps_entry_registered`.

### §4.6 Polling Failure

**Audit Cross-Refs**: `EX-02` (CRITICAL, CONFIRMED PRESENT, extended to PARTIAL_FILLED),
`EX-10` (HIGH, CONFIRMED PRESENT), `CA-03`/`CA-04` (masking, cross-ref §4.3/§4.7) — audit §3.6,
§6, §8.6.

**Expected Behavior** (current code):
- `_loop()` (`order_poller.py:102-114`) has no top-level try/except around the per-tick body.
- FILLED case (`order_poller.py:130-144`): pops the entry from `_entries` at line 137 BEFORE
  invoking `on_filled` at line 140 (EX-02) — if the callback raises, the fill is lost with no
  way to redeliver it.
- PARTIAL_FILLED case (151-165): advances `last_reported_qty` at line 154 BEFORE calling
  `on_filled(partial)` at 161-164 — if that raises, the next poll computes
  `incremental = filled_qty - last_reported_qty == 0`, so the lost increment is never
  redelivered (EX-02 extended).
- `_handle_timeout()` (170-178) has the same pop-before-callback shape.
- EX-10: an exception raised while building `due=[...]` (105-106) or evaluating
  `entry.is_timed_out` (109) propagates out of `_loop`, silently killing the daemon thread —
  no `is_alive()` check exists anywhere in the codebase. ALL registered orders stop receiving
  fill callbacks from that point on.
- The only detection mechanism for EX-10 is `_periodic_reconcile`'s `lost_order` check (age
  >1h, ≤30min cadence) — up to ~1.5h of silent fill loss across ALL strategies sharing the
  poller.

**Recovery Expectations**:
- EX-02 / PARTIAL_FILLED loss — **NONE**; the fill is permanently lost from the poller's
  perspective, later masked by `reconciler.py`'s CA-03 unconditional qty overwrite (cross-ref
  §4.3/§4.7), which corrects `Position.qty` in the DB but never creates the missing `Fill`
  row.
- EX-10 thread death — **NONE**; the thread never restarts; only `_periodic_reconcile`'s
  `lost_order` gap (cross-ref §4.7) eventually surfaces the staleness, with up to ~1.5h delay.

**Validation Assertions** (§3 dims):
- **§3.7 (No position damage)** — primary. (a) FILLED: make `on_filled` raise on the first
  call; assert the order is popped from `OrderFillPoller._entries` (i.e. `pending_count()` no
  longer includes it) despite the callback failure — `[CURRENT]` documents permanent loss.
- (b) PARTIAL_FILLED: make `on_filled(partial)` raise; assert `last_reported_qty` has ALREADY
  been advanced to the new `filled_qty` before the exception propagates, so a subsequent poll
  computes `incremental == 0` — `[CURRENT]` documents the lost increment.
- (c) EX-10, via `crashing_poller()`: inject an exception on the loop's 2nd tick for one order;
  assert `poller._thread.is_alive() is False` afterward, AND that a second, healthy order
  registered on the same poller never has its `on_filled`/timeout callbacks invoked again
  after the crash tick.
- (d) §3.3/§3.7 (reconcile masking) — after (a)/(b)'s injection, run `reconciler.reconcile()`
  against a mock broker reporting the correct post-fill qty; assert `Position.qty` (DB)
  converges to the broker truth (CA-03, cross-ref §4.3) but assert NO corresponding `Fill` row
  was created for the lost fill.
- (e) §3.4 (audit log) — after >1h with the lost order still "pending" per DB, assert a
  `lost_order` gap entry appears in `ReconciliationLog.detail` (existing mechanism, cross-ref
  §4.7).

**Fail-Closed Rules**:
- `[CURRENT]` FILLED/timeout: pop-then-callback. `[TARGET]`: pop/mark the entry only AFTER the
  callback succeeds; on callback exception, retry the callback a bounded number of times, then
  alert (entry remains registered until success or alert).
- `[CURRENT]` PARTIAL_FILLED: `last_reported_qty` advances before the callback. `[TARGET]`:
  advance `last_reported_qty` only after `on_filled(partial)` succeeds.
- `[CURRENT]` EX-10: no supervisor for the poller thread. `[TARGET]`: a periodic check (e.g.
  from `_periodic_reconcile` or a dedicated watchdog) calls `poller._thread.is_alive()`; on
  `False`, restart the thread and emit an alert (`event_type="poller_thread_crash"`,
  proposed).
- `[CURRENT]` `lost_order` gaps are silent (`ReconciliationLog.detail` only). `[TARGET]`:
  escalate to a Telegram/WS alert if N `lost_order` gaps accumulate.

**Audit Logging Expectations**:
- `lost_order` gap entries (existing, `ReconciliationLog.detail`, NOT `AuditLog`).
- `reconcile_fix_qty` (CA-03, cross-ref §4.3) — present when reconcile masks the lost fill.
- `[TARGET]`-proposed `event_type="poller_thread_crash"` (EX-10) and
  `event_type="fill_callback_failed"` (EX-02/PARTIAL_FILLED) — both proposed, not implemented.

**Skeleton Mapping**: → `TestPollingFailureScenario` —
`test_filled_callback_exception_loses_fill` (EX-02, `[CURRENT]`),
`test_partial_filled_callback_exception_loses_increment` (EX-02 extended, `[CURRENT]`),
`test_thread_crash_no_supervisor` (EX-10, `[CURRENT]` — cross-ref §8 known-gaps example),
`test_reconcile_masks_lost_fill_via_qty_repair` (CA-03 masking, §3.3/§3.7),
`test_lost_order_gap_logged_after_one_hour` (§3.4).

### §4.7 Reconciliation Failure

**Audit Cross-Refs**: `CA-03`/`CA-04` (HIGH, CONFIRMED PRESENT), `EX-04`/`EX-11` (HIGH,
CONFIRMED PRESENT) — audit §3.7, §6, §8.7.

**Expected Behavior** (current code):
- `reconcile()` (`reconciler.py:112-151`) has 4 call sites: the periodic 30-min job
  (`max_instances=1, coalesce=True`), `StartupRecovery._step_reconcile` ("startup" trigger,
  step 6), `_post_recovery_reconcile` ("post_recovery" daemon thread), and ad-hoc
  operator-triggered runs — all share a single `_reconcile_lock.acquire(blocking=False)`
  (line 117).
- A later overlapping call SKIPS ENTIRELY (returns immediately with
  `result.errors == ["조정 이미 진행 중 (스킵)"]`, `result.ok is False`) — log-only, no
  queue/retry/alert. A crash-loop worker could go an extended period with ZERO successful
  reconciles if every attempt overlaps with a stuck prior run.
- `_reconcile_positions` (167-277): `qty_mismatch` triggers an unconditional DB overwrite
  (206-231, CA-03); `avg_price`-only drift is auto-fixed too (232-245); neither path records
  WHY the drift occurred — no schema field for cause (CA-04).
- `_reconcile_pending_orders` (291-348): per-order `get_order_status` failures are caught and
  `continue`d (330-332); orders pending >1h produce a `lost_order` gap.
- `_sync_order_status` (375-401): `existing_fill` dedup at 392-393 prevents duplicate `Fill`
  rows (EX-04 partial mitigation), but there's no DB-level `UNIQUE` constraint on `Fill` and no
  `is_registered()` coordination with the live `OrderFillPoller` — a TOCTOU race remains
  (EX-11).
- `_fetch_broker_positions` (155-165): a broker exception (other than `NotImplementedError`)
  returns `None`, causing `_reconcile_positions`/`_reconcile_pending_orders` to be SKIPPED
  ENTIRELY for that run — `_persist_log`/`_publish_ws` still execute in the `finally` block, so
  a `ReconciliationLog` row IS written (with `error` populated) even though nothing was
  repaired.

**Recovery Expectations**:
- Lock-contention skip — automatic retry on the next scheduled trigger (≤30min later); no "N
  consecutive skips" detection exists.
- Broker-exception abort — the current run aborts cleanly (no partial writes, since
  `_reconcile_positions`/`_reconcile_pending_orders` are gated behind a non-`None`
  `broker_positions`); the next run starts fresh ("abort and retry next cycle," not "resume
  mid-way").
- EX-04/EX-11 — automatic DB-level dedup via `existing_fill`, but the underlying TOCTOU race
  between reconciler and poller is narrowed, not closed.

**Validation Assertions** (§3 dims):
- **§3.3 (State consistency)** — primary. Using a `threading.Event` to hold `_reconcile_lock`
  in one thread, call `reconcile()` from a second thread; assert the second call returns
  immediately with `result.ok is False` and `result.errors == ["조정 이미 진행 중 (스킵)"]`
  (regression guard for the lock behavior).
- **§3.4 (Audit log)**, `[TARGET]` — assert that NO counter/field currently distinguishes
  "skipped due to lock contention" from "ran successfully with nothing to repair"; both
  currently look like a normal `ReconciliationLog` row absent inspection of `result.errors`.
- **§3.1/§3.3** — mock `broker.get_positions()` to raise; since `_fetch_broker_positions` is
  called ONCE per `reconcile()` (not per-symbol), this whole run aborts before any repair,
  regardless of how many symbols would have been processed; assert `db.query(DBPosition).all()`
  is UNCHANGED and `ReconciliationLog.error` is populated.
- **§3.6 (No duplicate orders, EX-04/EX-11)** — simulate both `_sync_order_status` and the
  poller's `on_filled` inserting a `Fill` for the same `(order_id, qty, price)` sequentially;
  assert only ONE `Fill` row exists (regression guard for `existing_fill` dedup) —
  `[TARGET]`-document that a TRUE concurrent race is not closed by this single-threaded SQLite
  test.
- **§3.4 (CA-04)** — after a `qty_mismatch` repair, assert the resulting
  `reconcile_fix_qty`/`reconcile_fix_avg_price` `AuditLog.detail` JSON has NO
  `corporate_action_type`/`adjustment_factor` field — `[CURRENT]` documents the missing-cause
  gap.

**Fail-Closed Rules**:
- `[CURRENT]` lock-contention skip is silent. `[TARGET]`: add a `reconcile_skipped_total`-style
  counter and alert if N consecutive skips occur.
- `[CURRENT]` broker-exception abort is itself fail-closed (no partial repairs), but the
  resulting `ReconciliationLog` row (`gaps_found=0, repairs_made=0, error=<msg>`) looks like a
  successful no-op unless `error` is specifically checked. `[TARGET]`: surface aborted runs
  distinctly from successful no-op runs.
- `[CURRENT]` CA-03/CA-04 unconditional-overwrite-without-cause → `[TARGET]` cross-ref audit
  §11 (out of scope here).
- `[CURRENT]` EX-11 TOCTOU narrowed-not-closed → `[TARGET]` cross-ref audit §11 (DB `UNIQUE` on
  `(order_id, qty, price)` for `Fill`, or `is_registered()` coordination between reconciler and
  poller — out of scope here).

**Audit Logging Expectations**:
- `reconcile_fix_qty`/`reconcile_fix_avg_price`/`reconcile_insert`/`reconcile_delete` (existing,
  `actor=f"reconciler:{broker_name}"`).
- `lost_order` gap entries (existing, `ReconciliationLog.detail`, NOT `AuditLog`).
- `[TARGET]`-proposed `event_type="reconcile_skipped"`/`"reconcile_aborted"` — proposed, not
  implemented; currently distinguishable only via `ReconciliationLog.error`/`result.errors`.

**Skeleton Mapping**: → `TestReconciliationFailureScenario` —
`test_concurrent_reconcile_second_call_skips` (§3.3, regression guard),
`test_lock_skip_not_distinguished_from_noop` (§3.4, `[TARGET]`),
`test_broker_exception_aborts_with_no_partial_writes` (§3.1/§3.3),
`test_fill_dedup_across_reconciler_and_poller` (EX-04/EX-11, §3.6),
`test_ca03_repair_has_no_reason_field` (CA-04, §3.4).

### §4.8 Stale Data

**Audit Cross-Refs**: `SD-01`/`SD-03`/`SD-04`/`SD-05`/`SD-06`/`SD-09`/`SD-12` (cross-ref
`docs/STALE_DATA_AUDIT.md`; audit §3.8 raises no new finding IDs) — audit §3.8, §6, §8.8.

**Expected Behavior** (current code):
- Four independent, inconsistent staleness checks exist (SD-06): `loader.py:83-97` (26h,
  WARN-only — SD-01); an intraday zero-bar check (SD-02, broader `STALE_DATA_AUDIT.md` scope);
  `strategy/base.py:80-101` (`_is_bar_stale`, 600s threshold, on the dormant `on_bar` path —
  SD-10 cross-ref); `indicator/strategy.py:103-118` (3-day gate wrapped in a bare
  `except Exception: pass` — SD-09).
- SD-09: a malformed timestamp index (e.g. non-`DatetimeIndex`) raises inside the 3-day gate's
  comparison, hits the bare `except`, and SILENTLY DISABLES the staleness check entirely for
  that symbol's scan — the strategy proceeds normally as if the data were fresh.
- SD-05: `WorkerWatchdog` is process-liveness-only and orthogonal to data freshness — a worker
  can be alive/heartbeating with healthy Redis/DB while its market data is silently stale
  (SD-03's tier-4 cache), producing ZERO alerts.
- SD-04: `_get_fx()` (cross-ref §4.4) falls back to a >30min-stale cached FX rate silently;
  that stale rate feeds the kill-switch equity calculation.
- SD-06 (extended): order-staleness has yet another independent threshold pair
  (`_STALE_MIN_AGE_HOURS=1.0` in `reconciler.py` vs. `_RECOVERY_STALE_ORDER_HOURS=24` in
  `recovery.py`) — one more instance of "no shared staleness configuration."

**Recovery Expectations**:
- SD-01 — NONE; 26h-stale data is WARN-logged but still feeds signal generation unflagged.
- SD-09 — NONE; the staleness check is silently disabled for the affected symbol, with no
  degraded/skip mode.
- SD-05 — NONE; no mechanism cross-checks heartbeat health against data-freshness.
- SD-04 — cross-ref §4.4 (`test_fx_fallback_on_timeout_uses_stale_cache`).

**Validation Assertions** (§3 dims):
- **§3.4 (primary, SD-09)** — feed `_scan_and_trade()` a DataFrame with a non-`DatetimeIndex`
  (e.g. a plain `RangeIndex`), triggering the bare `except` at `indicator/strategy.py:103-118`;
  assert the symbol is NOT skipped and proceeds to signal evaluation — `[CURRENT]` documents
  the silent-disable gap.
- **§3.5 (SD-04)** — n/a as a new assertion here; cross-ref §4.4's
  `test_fx_fallback_on_timeout_uses_stale_cache`.
- **§3.4 (SD-05)**, `[TARGET]` — documents the ABSENCE of a mechanism: there is no current code
  path to exercise (heartbeat fresh + no tracked "last successful data fetch" timestamp
  anywhere means nothing CAN check this) — the stub documents the gap rather than asserting a
  behavior.
- **§3.6/§3.7** — n/a; stale-data findings are several hops removed from the DB-focused
  duplicate-order/position-damage checks, out of scope per §1.
- **§3.1/§3.2** — n/a; no process restart/recovery involved in this scenario.

**Fail-Closed Rules**:
- `[CURRENT]` SD-01/SD-09: WARN-or-silently-swallow. `[TARGET]` (per audit's fail-closed target
  for SD-09): a malformed-index error should SKIP the symbol with a warning, not silently
  disable the check — i.e. the bare `except Exception: pass` should `continue` past the
  symbol, not fall through to signal evaluation. Cross-ref `STALE_DATA_DETECTOR.md` SG-04 (not
  re-derived here).
- `[CURRENT]` SD-05: no heartbeat↔data-freshness cross-check. `[TARGET]`: cross-ref
  `STALE_DATA_AUDIT.md`/`STALE_DATA_DETECTOR.md` (out of scope per §1).
- `[CURRENT]` SD-06: fragmented/inconsistent staleness thresholds across modules. `[TARGET]`:
  cross-ref `STALE_DATA_AUDIT.md` (out of scope per §1).

**Audit Logging Expectations**:
- None of SD-01..SD-13 write `AuditLog` rows — all are `logger.warning` or silent.
- `[TARGET]`-proposed `event_type="stale_data_detected"` (per `STALE_DATA_DETECTOR.md`
  proposals, cross-ref only, not implemented).

**Skeleton Mapping**: → `TestStaleDataScenario` —
`test_sd09_malformed_index_does_not_skip_symbol` (SD-09, `[CURRENT]`, primary),
`test_sd05_heartbeat_green_does_not_imply_fresh_data` (SD-05, documents absence),
`test_sd06_order_staleness_thresholds_differ_by_caller` (SD-06, documents inconsistency).

### §4.9 Duplicate Event

**Audit Cross-Refs**: `F5` (RESOLVED — regression guard), `EX-04` (HIGH, CONFIRMED PRESENT,
cross-ref §4.7), `FS-05` (MEDIUM-HIGH, NEW) — audit §3.9, §6, §8.9.

**Expected Behavior** (current code):
- Two asymmetric layers exist. (1) F5 RESOLVED: `_handle_market_open` (`runner.py:316-345`)
  dedups session-open signals arriving twice within a 5-min window (Redis pub/sub + DB
  `Command` fallback, cross-ref §4.1) via a shared lock-and-snapshot check BEFORE strategy
  logic runs.
- (2) FS-05 NEW: if `on_filled()` is invoked TWICE for the same broker fill (possible via
  EX-02/EX-10, §4.6, or the EX-11 TOCTOU race, §4.7), the 6-step fill pipeline
  (`runner.py:429-511`) runs `tracker.on_fill()` (step 2) BEFORE `_persist_fill()`'s DB-level
  dedup (step 4).
- `PositionTracker.on_fill()` (`position_tracker.py:80-116`) has NO fill-id/order-id
  idempotency check — a second invocation mutates `self._positions[symbol]` again: a sell fill
  decrements `pos.qty` a second time, and if `qty <= 0` the position is DELETED from the
  in-memory dict (105-115).
- By the time step 4's `existing_fill` check correctly skips the duplicate DB write, the
  IN-MEMORY `PositionTracker` state has already diverged from DB/broker truth.
- This divergence is silent until the next `_periodic_reconcile` (DB-vs-broker axis is
  unaffected since the DB was never double-written); `restore_positions()` is NOT called again
  during normal operation (only at session construction), so the in-memory tracker could
  remain wrong for up to 30min or until restart — during which `try_mark_pending`/
  `unmark_pending` (which read `self._positions`) could make incorrect pending-order decisions.

**Recovery Expectations**:
- F5 — automatic, RESOLVED; regression guard only.
- DB-level fill dedup (EX-04) — automatic; the second `Fill` row is never inserted.
- In-memory `PositionTracker` divergence (FS-05) — NONE within the current cycle.
- **Open question (→ §8)**: does `reconcile()`'s DB-level position repair (CA-03, §4.3/§4.7)
  ever propagate to a LIVE `PositionTracker._positions` dict absent a restart? If not, FS-05's
  divergence could persist past the assumed 30-min "one reconcile cycle" bound used by
  §3.3/§3.7.

**Validation Assertions** (§3 dims):
- **§3.2 (F5)** — call the session-open dedup logic twice within the 5-min window (simulating
  the Redis-path and DB-path triggers); assert strategy logic runs exactly ONCE (regression
  guard).
- **§3.6 (EX-04)** — call the fill-persistence path twice with the same
  `(order_id, qty, price)`; assert `db.query(DBFill).filter(...).count() == 1` (regression
  guard).
- **§3.7 (FS-05)**, primary — construct a real `PositionTracker`, seed it via
  `restore_positions([Position(symbol, qty=10, ...)])`, then call
  `tracker.on_fill(Fill(side="sell", qty=10, ...))` TWICE; assert that after the 2nd call
  `tracker.get_position(symbol) is None` — `[CURRENT]` documents that in-memory state now
  disagrees with DB.
- **§3.3 (cross-ref)** — after the FS-05 divergence above, assert the DB `Position` table is
  UNCHANGED by the 2nd `on_fill` call — i.e. axis (a) DB-vs-broker is fine, but axis (b)
  in-memory-vs-DB is broken, which is precisely §3.3's documented edge case.
- **§3.4** — assert exactly ONE `fill` event/`Fill` row exists for the TWO `on_filled`
  invocations (DB-level correctness holds despite the in-memory FS-05 divergence).

**Fail-Closed Rules**:
- `[CURRENT]` F5 — correct, fail-closed; regression guard only.
- `[CURRENT]` DB-level fill dedup (EX-04, app-level) — correct; regression guard only.
- `[CURRENT]`/FS-05 — `PositionTracker.on_fill()` has no idempotency guard; a duplicate
  invocation silently corrupts in-memory state for up to 30min or until restart. `[TARGET]`:
  `on_fill()` should accept a fill identifier (`(order_id, qty, price)` tuple or dedicated
  `fill_id`) and maintain its OWN seen-fills set, mirroring `_persist_fill`'s DB-level
  `existing_fill` check.
- `[CURRENT]` open question re: reconcile→live-tracker propagation — see §8.

**Audit Logging Expectations**:
- `fill` (existing, DB-level — correct count even under FS-05).
- `[TARGET]`-proposed `event_type="position_tracker_duplicate_fill"` — the FS-05 fix's own
  audit trail, proposed, not implemented.

**Skeleton Mapping**: → `TestDuplicateEventScenario` —
`test_session_open_dedup_within_window` (F5, regression guard),
`test_fill_db_dedup_on_duplicate_callback` (EX-04, regression guard),
`test_position_tracker_double_apply_on_duplicate_fill` (FS-05, `[CURRENT]`, primary),
`test_db_position_unaffected_by_in_memory_double_apply` (§3.3 edge case),
`test_reconcile_propagation_to_live_tracker_unknown` (§8 open question).

### §4.10 Server Restart

**Audit Cross-Refs**: `F1`/`EX-06` (RESOLVED — regression guards), `FS-03` (LOW-MEDIUM, NEW),
`FS-04` (CRITICAL, NEW) — audit §3.10, §6, §8.10.

**Expected Behavior** (current code):
- Server restart = the union of §4.2 (worker restart) + §4.1's Redis-at-boot non-fatal path +
  `StartupRecovery`, PLUS two DB-layer findings.
- FS-04 (CRITICAL): `Base.metadata.create_all(engine)` (`models.py:148`, called by
  `init_db_factory`, run independently by BOTH `kis-api` and `kis-worker` on every start,
  `models.py:145-149`) is a SILENT NO-OP on schema drift — `create_all()` only does
  `CREATE TABLE IF NOT EXISTS`, it NEVER `ALTER TABLE`s an existing table.
- If a future ORM change adds a column to `Order`/`Position`/`Fill` and the deployed Postgres
  already has those tables, the new column DOES NOT EXIST after restart, with no error or
  warning. `StartupRecovery._step_db` only runs `SELECT 1`, so both processes report "DB
  healthy." The first ORM query/insert referencing the new column raises
  `ProgrammingError`/`UndefinedColumn` hours or days later, mid-pipeline. Matches `ROADMAP.md`
  P0-14 / `AUDIT.md DB-01`.
- FS-03 (LOW-MEDIUM): the `Command` table (`models.py:103-111`, no TTL field) is written by
  every session-open signal (`scheduler.py:144-153`, ~2/day), marked `processed`/`error` by
  `_enter_db_polling_mode()` (`runner.py:243-282`), but NEVER `DELETE`d. None of
  `build_scheduler()`'s 5 jobs purge it — permanent unbounded growth, no metric/alert.
- Both `kis-api` and `kis-worker` independently `create_engine(db_url, pool_pre_ping=True,
  echo=False)` (`models.py:147`) + `create_all` on every restart.

**Recovery Expectations**:
- F1/EX-06 — automatic, RESOLVED; regression guards (cross-ref §4.2).
- FS-04 — NONE; only a manual `ALTER`/Alembic migration closes the drift. A runtime error from
  drift is swallowed/logged like any other DB error, not specifically surfaced as "schema
  drift."
- FS-03 — NONE; monotonic growth, no purge anywhere.

**Validation Assertions** (§3 dims):
- **§3.2 (F1/EX-06)**, primary regression guard, restated from §4.2 in the "both processes
  restart together against the same DB" framing — assert `StartupRecovery.run()` executed
  twice in sequence (simulating `kis-api` + `kis-worker` both calling `init_db_factory`+
  recovery against the same DB at boot) produces NO duplicate `Fill`/`Position`/`AuditLog`
  rows.
- **§3.4 (FS-04)**, `[TARGET]`/documents-gap — in an in-memory SQLite DB, call
  `create_all(engine)` against a locally-defined `DeclarativeBase` subclass whose `Order` model
  is missing a column (e.g. `idempotency_key`); then, on the SAME engine, call
  `create_all(engine)` again with the CURRENT `Order` model (column present); assert the column
  is ABSENT from the live table — a subsequent query/insert referencing it raises
  `OperationalError`/`ProgrammingError`. `[CURRENT]` = drift is silent at both `create_all` AND
  `_step_db`'s `SELECT 1`.
- **§3.1 (FS-03 framing)** — insert N `Command` rows with `status="processed"`; run
  `_enter_db_polling_mode`'s query-and-mark loop; assert the row count is NON-DECREASING (i.e.
  processed rows are never purged) — `[CURRENT]` documents unbounded growth.

**Fail-Closed Rules**:
- `[CURRENT]` F1/EX-06 — correct, RESOLVED; regression guards only.
- `[CURRENT]` FS-04: schema drift silent at boot. `[TARGET]`: `StartupRecovery._step_db` (or a
  new step) should do a coarse schema-diff — e.g. compare ORM-declared columns
  (`Base.metadata.tables[...].columns`) against `information_schema.columns` (or
  `inspect(engine)`) for each table — and treat drift as FATAL-AT-BOOT (refuse to enable
  trading), not silent. `event_type="schema_drift_detected"` (proposed).
- `[CURRENT]` FS-03: unbounded `Command` growth. `[TARGET]`: add a periodic purge job (e.g.
  delete `processed`/`error` rows older than N days) to `build_scheduler()`'s job list.

**Audit Logging Expectations**:
- `recovery_inconsistency` (existing, `actor="recovery"`, from `_step_validate_state` — covers
  orphaned-pending/stale-order/negative-qty cases, cross-ref §4.2/§4.3).
- `[TARGET]`-proposed `event_type="schema_drift_detected"` (FS-04) — proposed, not implemented.
- FS-03 has no `AuditLog` expectation — it's a hygiene/growth issue, not a correctness event.

**Skeleton Mapping**: → `TestServerRestartScenario` —
`test_dual_process_startup_recovery_idempotent` (F1/EX-06, regression guard),
`test_schema_drift_undetected_after_create_all` (FS-04, `[CURRENT]`, primary),
`test_command_table_rows_never_purged` (FS-03, `[CURRENT]`).

---

## §5 Test File Skeleton Overview

This section is the "table of contents" for Deliverable 2
(`tests/integration/test_failure_scenarios.py`). It lists the module docstring, imports,
shared fixtures, and — per test class — the stub method names fixed by §4's "Skeleton
Mapping" fields. The skeleton itself (40 stub methods across 10 classes, within the
"~35-45 stub methods" estimate from §2) is the executable-shaped artifact; this section is
its outline.

### Module Docstring (design)

```python
"""
Design skeleton for the consolidated failure-scenario integration suite (TASK 4-1B).

See docs/FAILURE_SCENARIO_TESTS.md for the full design specification: per-scenario
expected behavior, recovery expectations, validation assertions, fail-closed rules
([CURRENT]/[TARGET]), and audit-logging expectations.

All test methods are @pytest.mark.skip(reason="TASK 4-1B design skeleton — see
docs/FAILURE_SCENARIO_TESTS.md §4.N; not yet implemented"). Fixtures db_factory(),
mock_broker(), and mock_redis() are implemented trivially (plain SQLite/MagicMock
construction, no failure-injection logic, per §2's "trivial scaffolding vs. scenario
logic" boundary). flaky_broker() and crashing_poller() are stubs.
"""
```

### Imports (cross-ref §2)

Per §2's "Module-Level Imports (design)" code block: `pytest`, `MagicMock`,
`date`/`datetime`/`timedelta`/`timezone`, `sqlalchemy.create_engine`/`sessionmaker`,
`backend.database.models` (`Base`, `Order`, `Fill`, `Position`, `AuditLog`,
`DailyRiskState`, `Command`), `OrderStateMachine`, `PositionTracker`, `OrderFillPoller`,
`ConsecutiveFailureBreaker`, `WorkerHeartbeat`/`HeartbeatMonitor`/`WorkerWatchdog`.
`KISAuth`/`KISClient` referenced by name in §4.4 docstrings only (not imported at module
scope — see §2's closing note and §8's env-var open question).

### Shared Fixtures

| Fixture | Implementation | Used by |
|---|---|---|
| `db_factory()` | **Implemented** — in-memory SQLite, `Base.metadata.create_all(engine)`, `sessionmaker(expire_on_commit=False)`, per `test_reconciler.py:32-38` | all 10 classes |
| `mock_broker()` | **Implemented** — `MagicMock()` with empty `.get_positions.return_value = []` default | §4.3-§4.7, §4.9, §4.10 |
| `mock_redis()` | **Implemented** — `MagicMock()` with `.exists.return_value = 1`, `.ping.return_value = True` defaults | §4.1, §4.2, §4.10 |
| `flaky_broker(n_failures)` | **Stub** (`@pytest.mark.skip` on any test using it; helper itself documented, not implemented) | §4.2, §4.5 |
| `crashing_poller()` | **Stub** | §4.6 |

### Test Classes

**`TestRedisDownScenario`** (§4.1, FS-01):
- `test_heartbeat_survives_redis_outage` — `WorkerHeartbeat._beat()` swallows
  `redis.ConnectionError` without raising.
- `test_watchdog_sets_kill_switch_on_connection_error` — `WorkerWatchdog._check()` sets
  `DailyRiskState.kill_switch=True` / `kill_reason` when `HeartbeatMonitor.is_alive()`
  raises via `mock_redis().exists`.
- `test_recovery_alert_does_not_clear_kill_switch` — FS-01 gap: `_alert_recovery()` fires a
  WS info alert but `kill_switch` remains `True`.

**`TestWorkerRestartScenario`** (§4.2, FS-02, F1/EX-06):
- `test_recovery_idempotent_second_run` — `StartupRecovery.run()` twice produces zero
  `AuditLog` delta (F1/EX-06 regression guard).
- `test_pending_order_fill_dedup_across_restart` — `_step_pending_orders` re-run does not
  double-insert `Fill` (F1).
- `test_breaker_resets_on_restart` — new `ConsecutiveFailureBreaker` instance starts closed
  regardless of pre-restart cooldown (FS-02 gap).
- `test_kill_switch_blocks_reenable_across_restart` — `_step_enable_trading` refuses to
  re-enable `SAFE_MODE` when a prior-session `kill_switch=True` is restored.

**`TestProcessKillScenario`** (§4.3, EX-02 extended, CA-03/CA-04):
- `test_kill_mid_pipeline_no_partial_db_write` — simulated kill between `tracker.on_fill()`
  and `_persist_fill()` leaves DB at pre-fill state.
- `test_startup_reconcile_repairs_qty_after_kill` — `reconcile("startup")` corrects
  `Position.qty` via CA-03, writes `reconcile_fix_qty`.
- `test_missing_fill_row_after_repair_undetected` — `[TARGET]` gap: no `Fill` row and no
  flag after the CA-03 repair.
- `test_watchdog_correctly_flags_genuine_death` — `WorkerWatchdog` correctly sets
  `kill_switch=True` on genuine process death (regression guard).

**`TestNetworkTimeoutScenario`** (§4.4, DO-01, SD-04, FS-07):
- `test_post_retry_after_timeout_may_duplicate_order` — `[CURRENT]` POST retry after
  timeout can submit a second `idempotency_key`-bearing order (DO-01).
- `test_auth_hashkey_timeout_fails_without_retry` — `[CURRENT]` `_issue_token`/`get_hashkey`
  raise immediately, zero retries (FS-07).
- `test_fx_fallback_on_timeout_uses_stale_cache` — `_get_fx()` returns cached rate on
  `yfinance` timeout (regression guard, SD-04 cross-ref).

**`TestBrokerApiFailureScenario`** (§4.5, DO-05, FS-02 cross-ref):
- `test_breaker_opens_after_threshold_failures` — `is_open() is True` after 5 consecutive
  `record_failure()` calls.
- `test_breaker_open_short_circuits_without_broker_call` — open breaker rejects without
  calling `broker.place_order`.
- `test_breaker_trip_logs_but_does_not_alert` — `[TARGET]` gap: `logger.error` fires, no
  Telegram/WS alert.
- `test_breaker_auto_recovers_after_cooldown` — `is_open()` returns `False` after cooldown
  elapses (regression guard).
- `test_get_order_status_none_keeps_entry_registered` — `_PollEntry` remains registered
  across `None`-returning polls.

**`TestPollingFailureScenario`** (§4.6, EX-02 extended, EX-10):
- `test_filled_callback_exception_loses_fill` — `[CURRENT]` order popped from `_entries`
  despite `on_filled` raising (EX-02).
- `test_partial_filled_callback_exception_loses_increment` — `[CURRENT]`
  `last_reported_qty` advances before a raising `on_filled(partial)` (EX-02 extended).
- `test_thread_crash_no_supervisor` — `[CURRENT]` `poller._thread.is_alive() is False`
  after an injected crash tick, never restarted (EX-10).
- `test_reconcile_masks_lost_fill_via_qty_repair` — CA-03 repairs `Position.qty` with no
  corresponding `Fill` row.
- `test_lost_order_gap_logged_after_one_hour` — `lost_order` gap appears in
  `ReconciliationLog.detail` after >1h.

**`TestReconciliationFailureScenario`** (§4.7, CA-03/CA-04, EX-04/EX-11):
- `test_concurrent_reconcile_second_call_skips` — overlapping `reconcile()` call returns
  `result.ok is False` with the lock-skip error (regression guard).
- `test_lock_skip_not_distinguished_from_noop` — `[TARGET]` gap: skip vs. no-op
  `ReconciliationLog` rows are indistinguishable without inspecting `errors`.
- `test_broker_exception_aborts_with_no_partial_writes` — `broker.get_positions()` raising
  aborts the whole run with `db.query(DBPosition).all()` unchanged.
- `test_fill_dedup_across_reconciler_and_poller` — sequential duplicate `Fill` inserts from
  reconciler + poller produce one row (EX-04/EX-11 regression guard).
- `test_ca03_repair_has_no_reason_field` — `[CURRENT]` `reconcile_fix_qty`/
  `reconcile_fix_avg_price` `detail` JSON has no cause field (CA-04).

**`TestStaleDataScenario`** (§4.8, SD-01/03/04/05/06/09/12, cross-ref only):
- `test_sd09_malformed_index_does_not_skip_symbol` — `[CURRENT]` malformed-index bare
  `except` does not skip the symbol (SD-09, primary).
- `test_sd05_heartbeat_green_does_not_imply_fresh_data` — documents the absence of a
  heartbeat↔data-freshness cross-check (SD-05).
- `test_sd06_order_staleness_thresholds_differ_by_caller` — documents
  `_STALE_MIN_AGE_HOURS` vs. `_RECOVERY_STALE_ORDER_HOURS` inconsistency (SD-06).

**`TestDuplicateEventScenario`** (§4.9, F5, EX-04, FS-05):
- `test_session_open_dedup_within_window` — session-open dedup runs strategy logic exactly
  once within the 5-min window (F5 regression guard).
- `test_fill_db_dedup_on_duplicate_callback` — duplicate `(order_id, qty, price)` fill
  persistence produces one `Fill` row (EX-04 regression guard).
- `test_position_tracker_double_apply_on_duplicate_fill` — `[CURRENT]` second
  `tracker.on_fill()` call for the same fill deletes the in-memory position (FS-05,
  primary).
- `test_db_position_unaffected_by_in_memory_double_apply` — DB `Position` row unchanged by
  the in-memory double-apply (§3.3 edge case).
- `test_reconcile_propagation_to_live_tracker_unknown` — open question: does CA-03's DB
  repair propagate to a live `PositionTracker` (§8).

**`TestServerRestartScenario`** (§4.10, F1/EX-06, FS-03, FS-04):
- `test_dual_process_startup_recovery_idempotent` — two sequential `StartupRecovery.run()`
  calls (simulating `kis-api` + `kis-worker` both booting) produce no duplicate rows
  (F1/EX-06 regression guard).
- `test_schema_drift_undetected_after_create_all` — `[CURRENT]` `create_all(engine)` against
  a drifted model leaves the new column absent, undetected (FS-04, primary).
- `test_command_table_rows_never_purged` — `[CURRENT]` `Command` row count is
  non-decreasing across the polling/marking loop (FS-03).

---

## §6 Fail-Closed Rules Summary

Consolidated from each §4 subsection's "Fail-Closed Rules" bullets. `[CURRENT]` = already
true today; `[TARGET]` = proposed future behavior, not implemented by this task.

| §4 | Rule | Status | Cross-Ref |
|---|---|---|---|
| 4.1 | `kill_switch` stays set when in doubt (fail-closed default) | `[CURRENT]` correct | FS-01 |
| 4.1 | `kill_reason` text ("프로세스 재시작 필요") misdirects when the true cause is Redis | `[CURRENT]` gap | FS-01 |
| 4.1 | `is_alive()`/`_check()` should distinguish "Redis unreachable" from "heartbeat key expired" | `[TARGET]` | FS-01 |
| 4.1 | `_alert_recovery()` should annotate `kill_reason` with a resolution marker; `kill_switch` stays `True` until a human confirms | `[TARGET]` | FS-01 |
| 4.2 | Breaker resets to closed on restart regardless of pre-restart cooldown | `[CURRENT]` gap | FS-02 |
| 4.2 | Persist breaker state, or rely on `_step_balance`/`_step_positions`'s 30s broker-timeout gates as a substitute health check | `[TARGET]` | FS-02 |
| 4.2 | `WorkerHeartbeat.start()`'s first beat should precede `StartupRecovery.run()`, not follow it | `[TARGET]` | FS-01 cross-ref |
| 4.3 | CA-03's unconditional qty/avg_price overwrite converges DB to broker truth | `[CURRENT]` correct | CA-03 |
| 4.3 | Missing `Fill` row after a CA-03 repair is silently masked, no audit distinction from other drift causes | `[CURRENT]` gap | CA-03/CA-04 |
| 4.3 | Emit `reconcile_fix_qty_no_fill_record` when a qty repair has no matching `Fill` | `[TARGET]` | CA-03/CA-04 |
| 4.3 | `WorkerWatchdog` response is identical for genuine death vs. Redis blip | `[CURRENT]` gap | cross-ref §4.1 |
| 4.4 | GET retries are safe/idempotent | `[CURRENT]` correct | — |
| 4.4 | POST retries after timeout can create ghost orders | `[CURRENT]` gap | DO-01 |
| 4.4 | Broker-side idempotency-token fix (cited, not designed here) | `[TARGET]` | audit §11 item, DO-01 |
| 4.4 | FX fallback never blocks/raises even when stale | `[CURRENT]` gap | SD-04 |
| 4.4 | `_get_fx()`'s own fix per `STALE_DATA_AUDIT.md` (cited, not re-derived) | `[TARGET]` | SD-04 |
| 4.4 | Auth/hashkey timeout fails the whole call with zero retries | `[CURRENT]` gap | FS-07 |
| 4.4 | Wrap `get_hashkey()`/`_issue_token()` in their own retry, or move inside `client.py`'s retry loop | `[TARGET]` | FS-07 |
| 4.5 | Breaker-open blocks ALL orders (fail-closed) | `[CURRENT]` correct | — |
| 4.5 | Breaker trip is `logger.error`-only, no Telegram/WS alert | `[CURRENT]` gap | DO-05 cross-ref |
| 4.5 | Emit `alert_emergency`/`publish_alert` at the threshold-crossing transition, mirroring `WorkerWatchdog._alert_dead_worker()` | `[TARGET]` | DO-05 cross-ref |
| 4.5 | `MarketClosedError` correctly excluded from the failure counter | `[CURRENT]` correct | — |
| 4.5 | Breaker state not persisted across restart | `[CURRENT]` gap | FS-02 cross-ref |
| 4.6 | FILLED/timeout: entry popped before callback succeeds | `[CURRENT]` gap | EX-02 |
| 4.6 | Pop/mark only after callback success; retry bounded, then alert | `[TARGET]` | EX-02 |
| 4.6 | PARTIAL_FILLED: `last_reported_qty` advances before callback succeeds | `[CURRENT]` gap | EX-02 extended |
| 4.6 | Advance `last_reported_qty` only after `on_filled(partial)` succeeds | `[TARGET]` | EX-02 extended |
| 4.6 | No supervisor for the poller thread (EX-10) | `[CURRENT]` gap | EX-10 |
| 4.6 | Periodic `poller._thread.is_alive()` check, restart + alert (`poller_thread_crash`) | `[TARGET]` | EX-10 |
| 4.6 | `lost_order` gaps are silent | `[CURRENT]` gap | EX-10 cross-ref |
| 4.6 | Escalate to Telegram/WS alert if N `lost_order` gaps accumulate | `[TARGET]` | EX-10 cross-ref |
| 4.7 | Lock-contention skip is silent, no consecutive-skip detection | `[CURRENT]` gap | — |
| 4.7 | Add `reconcile_skipped_total`-style counter, alert on N consecutive skips | `[TARGET]` | — |
| 4.7 | Broker-exception abort looks like a successful no-op unless `error` is inspected | `[CURRENT]` gap | — |
| 4.7 | Surface aborted runs distinctly from successful no-op runs | `[TARGET]` | — |
| 4.7 | CA-03/CA-04 unconditional-overwrite-without-cause (cited, not designed here) | `[TARGET]` | audit §11, CA-03/CA-04 |
| 4.7 | EX-11 TOCTOU narrowed, not closed; DB `UNIQUE`/`is_registered()` fix (cited, not designed here) | `[TARGET]` | audit §11, EX-11 |
| 4.8 | SD-01/SD-09 WARN-or-silently-swallow | `[CURRENT]` gap | SD-01/SD-09 |
| 4.8 | SD-09: malformed-index `except` should `continue`/skip with a warning, not fall through | `[TARGET]` | SD-09 |
| 4.8 | SD-05/SD-06 fixes (cited, not re-derived here) | `[TARGET]` | `STALE_DATA_AUDIT.md` |
| 4.9 | F5 session-open dedup is correct | `[CURRENT]` correct | F5 |
| 4.9 | DB-level fill dedup (`existing_fill`) is correct | `[CURRENT]` correct | EX-04 |
| 4.9 | `PositionTracker.on_fill()` has no fill-id idempotency guard | `[CURRENT]` gap | FS-05 |
| 4.9 | `on_fill()` should maintain its own seen-fills set mirroring `_persist_fill`'s `existing_fill` check | `[TARGET]` | FS-05 |
| 4.10 | F1/EX-06 regression guards hold under dual-process restart | `[CURRENT]` correct | F1/EX-06 |
| 4.10 | `create_all()` schema drift is silent at boot | `[CURRENT]` gap | FS-04 |
| 4.10 | `StartupRecovery` coarse schema-diff, fatal-at-boot on drift (`schema_drift_detected`) | `[TARGET]` | FS-04 |
| 4.10 | `Command` table rows never purged | `[CURRENT]` gap | FS-03 |
| 4.10 | Periodic purge job for `processed`/`error` `Command` rows | `[TARGET]` | FS-03 |

---

## §7 Audit Logging Expectations Summary

Per scenario, the `AuditLog.event_type` values the suite checks for. "Currently Emitted?"
reflects what the code does TODAY; `[TARGET]`-proposed types are not implemented by any
code path and exist only as design notes for a future fix.

| §4 | Expected `event_type`(s) | Currently Emitted? | Notes |
|---|---|---|---|
| 4.1 Redis Down | none (state goes to `DailyRiskState` directly); `[TARGET]` `watchdog_kill_switch` / `watchdog_recovery` | No | Distinct from `AuditLog(event_type="kill_switch", actor="risk_engine")` (`backend/quant/risk/engine.py:442`) — a DIFFERENT trigger (daily-loss/MDD), not the heartbeat/watchdog path this scenario exercises. |
| 4.2 Worker Restart | `reconcile_insert`/`reconcile_fix_qty`/`reconcile_fix_avg_price`/`reconcile_delete`, `recovery_inconsistency` | Yes | Must be ZERO-delta on an idempotent second `run()` (F1/EX-06); `_step_validate_state` is the documented exception to "no new rows" (§3.2). |
| 4.3 Process Kill | `reconcile_fix_qty` (CA-03 repair) MUST appear; `fill` for the lost fill MUST NOT appear | Partial | `[TARGET]`-proposed `reconcile_fix_qty_no_fill_record` would flag the gap. |
| 4.4 Network Timeout | `[TARGET]` `duplicate_order_suspected` (DO-01), `auth_endpoint_timeout` (FS-07) | No | Both proposed only; SD-04 fallback is a `logger.warning`, not an `AuditLog` row. |
| 4.5 Broker API Failure | `[TARGET]` `breaker_tripped` / `breaker_recovered` | No | Current breaker trip is `logger.error`-only; `lost_order` gaps live in `ReconciliationLog.detail`, not `AuditLog`. |
| 4.6 Polling Failure | `lost_order` gap (`ReconciliationLog`, not `AuditLog`); `reconcile_fix_qty` (masking); `[TARGET]` `poller_thread_crash`, `fill_callback_failed` | Partial | The lost fill itself produces no `fill`/`AuditLog` row at all — only the masking `reconcile_fix_qty`. |
| 4.7 Reconciliation Failure | `reconcile_fix_qty`/`reconcile_fix_avg_price`/`reconcile_insert`/`reconcile_delete`, `lost_order` gap; `[TARGET]` `reconcile_skipped` / `reconcile_aborted` | Partial | Skipped vs. aborted vs. successful-no-op runs are currently distinguishable only via `result.errors`/`ReconciliationLog.error`, not a dedicated `event_type`. |
| 4.8 Stale Data | `[TARGET]` `stale_data_detected` | No | All of SD-01..SD-13 are `logger.warning` or silent; cross-ref `STALE_DATA_DETECTOR.md` (not re-derived). |
| 4.9 Duplicate Event | `fill` (correct count even under FS-05); `[TARGET]` `position_tracker_duplicate_fill` | Partial | The DB-level `fill` count is correct; the in-memory FS-05 divergence itself has no audit trail. |
| 4.10 Server Restart | `recovery_inconsistency` (existing, `_step_validate_state`); `[TARGET]` `schema_drift_detected` | Partial | FS-03 (`Command` growth) has no `AuditLog` expectation — a hygiene/growth issue, not a correctness event. |

**All `[TARGET]`-proposed `event_type` values introduced across §4-§7** (none implemented):
`watchdog_kill_switch`, `watchdog_recovery` (4.1); `reconcile_fix_qty_no_fill_record` (4.3);
`duplicate_order_suspected`, `auth_endpoint_timeout` (4.4); `breaker_tripped`,
`breaker_recovered` (4.5); `poller_thread_crash`, `fill_callback_failed` (4.6);
`reconcile_skipped`, `reconcile_aborted` (4.7); `stale_data_detected` (4.8, cross-ref only);
`position_tracker_duplicate_fill` (4.9); `schema_drift_detected` (4.10).

---

## §8 Known Gaps Carried Into Skeleton

For each row, "Current Expected Result" is what the stub would assert TODAY if implemented
as-is (documenting the gap, not a test failure to fix in this task); "What a Fix Would
Change" is the `[TARGET]` behavior from §4/§6 that a future implementation would instead
assert.

| Skeleton Test (class.method) | Current Expected Result | What a Fix Would Change | Tracking ID |
|---|---|---|---|
| `TestRedisDownScenario.test_recovery_alert_does_not_clear_kill_switch` | `kill_switch` stays `True` indefinitely after Redis recovers; `_alert_recovery()` only emits a WS info message | `_alert_recovery()` annotates `kill_reason` with a resolution marker; `kill_switch` still requires manual clear (fail-closed preserved) | FS-01 |
| `TestWorkerRestartScenario.test_breaker_resets_on_restart` | A fresh `ConsecutiveFailureBreaker()` after restart has `is_open() is False`, even if the pre-restart instance was mid-cooldown | Breaker state persisted (Redis) and restored at `StartupRecovery`, or broker-timeout gates (`_step_balance`/`_step_positions`) substitute | FS-02 |
| `TestProcessKillScenario.test_missing_fill_row_after_repair_undetected` | `db.query(DBFill).filter(DBFill.order_id == order_id).count() == 0` even after `reconcile_fix_qty` repairs the qty | `reconcile_fix_qty_no_fill_record` `AuditLog` event emitted alongside the qty repair when no matching `Fill` exists | CA-03/CA-04 |
| `TestNetworkTimeoutScenario.test_post_retry_after_timeout_may_duplicate_order` | Retry submits a second `idempotency_key`-bearing `Order`; DB can hold two rows for one logical intent | Broker-side idempotency token (cross-ref audit §11, out of scope for this suite) | DO-01 |
| `TestNetworkTimeoutScenario.test_auth_hashkey_timeout_fails_without_retry` | `requests.post` to `/oauth2/tokenP`/`/uapi/hashkey` called exactly once; `Timeout` propagates immediately | `get_hashkey()`/`_issue_token()` get their own retry, or are moved inside `KISClient`'s existing retry loop | FS-07 |
| `TestBrokerApiFailureScenario.test_breaker_trip_logs_but_does_not_alert` | `logger.error` fires once at the threshold-crossing; no `alert_emergency`/`publish_alert` call | Breaker trip additionally calls `alert_emergency`/`publish_alert`, mirroring `WorkerWatchdog._alert_dead_worker()` | DO-05 cross-ref |
| `TestPollingFailureScenario.test_filled_callback_exception_loses_fill` | Order removed from `OrderFillPoller._entries` even though `on_filled` raised; fill permanently lost | Entry popped only after `on_filled` succeeds; bounded retry then alert on persistent failure | EX-02 |
| `TestPollingFailureScenario.test_partial_filled_callback_exception_loses_increment` | `last_reported_qty` already advanced when `on_filled(partial)` raises; next poll's `incremental == 0` | `last_reported_qty` advances only after `on_filled(partial)` succeeds | EX-02 extended |
| `TestPollingFailureScenario.test_thread_crash_no_supervisor` | After an injected exception, `poller._thread.is_alive() is False`; no other order on the same poller receives further callbacks; nothing restarts it | A periodic supervisor (`is_alive()` check, e.g. from `_periodic_reconcile` or a dedicated thread) restarts the poller and emits `poller_thread_crash` | EX-10 |
| `TestReconciliationFailureScenario.test_lock_skip_not_distinguished_from_noop` | A lock-skipped run and a successful no-op run both produce an unremarkable `ReconciliationLog` row, distinguishable only via `result.errors`/`.error` | `reconcile_skipped`/`reconcile_aborted` event types plus a consecutive-skip counter/alert | — (`[TARGET]` only) |
| `TestReconciliationFailureScenario.test_ca03_repair_has_no_reason_field` | `reconcile_fix_qty`/`reconcile_fix_avg_price` `detail` JSON has no `corporate_action_type`/`adjustment_factor` key | Cross-ref audit §11 corporate-action schema work (out of scope for this suite) | CA-04 |
| `TestStaleDataScenario.test_sd09_malformed_index_does_not_skip_symbol` | A non-`DatetimeIndex` DataFrame hits the bare `except` at `indicator/strategy.py:103-118` and the symbol proceeds to signal evaluation as if fresh | The `except` branch `continue`s past the symbol with a `logger.warning`, per audit's SD-09 fail-closed target | SD-09 |
| `TestDuplicateEventScenario.test_position_tracker_double_apply_on_duplicate_fill` | After two `on_fill()` calls for the same sell fill, `tracker.get_position(symbol) is None` (position deleted in-memory) while DB `Position` is unchanged | `on_fill()` checks its own `(order_id, qty, price)`/fill-id seen-set before mutating `_positions`, mirroring `_persist_fill`'s `existing_fill` | FS-05 |
| `TestDuplicateEventScenario.test_reconcile_propagation_to_live_tracker_unknown` | **Open question**: it is not established whether `reconciler.reconcile()`'s CA-03 DB repair is ever pushed into a LIVE `PositionTracker._positions` dict without a restart | If the answer is "no," FS-05's divergence can exceed the 30-min "one reconcile cycle" bound assumed by §3.3/§3.7 — would need either a propagation mechanism or a revised bound | FS-05 cross-ref |
| `TestServerRestartScenario.test_schema_drift_undetected_after_create_all` | After `create_all(engine)` against a drifted model, the new column is absent from the live table; `StartupRecovery._step_db`'s `SELECT 1` reports healthy | `StartupRecovery` adds a coarse schema-diff step (ORM columns vs. `inspect(engine)`), treating drift as fatal-at-boot (`schema_drift_detected`) | FS-04 |
| `TestServerRestartScenario.test_command_table_rows_never_purged` | `Command` row count after marking `processed`/`error` is non-decreasing across repeated polling cycles | A periodic purge job (added to `build_scheduler()`) deletes old `processed`/`error` rows | FS-03 |
| *(suite-wide, not a single test)* `KISAuth`/`KISClient` construction requires `KIS_APP_KEY`/`KIS_APP_SECRET` env vars (§2) | §4.4's stubs reference these classes by name only; no fixture currently provides the required env vars in the test environment | TASK 4-1C+ needs either an env-var fixture (`monkeypatch.setenv`) or a constructor-arg refactor (`KISAuth(app_key=..., app_secret=...)`) before §4.4's stubs can be implemented | §2 open question |

---

## §9 Relationship to Future Work

This document and its companion skeleton (`tests/integration/test_failure_scenarios.py`)
are the design output of TASK 4-1B. **TASK 4-1C** (or the next-numbered task) is the
implementation task: turn each `@pytest.mark.skip`-marked stub into a working test —
fixtures, mocks, failure-injection helpers (`flaky_broker()`, `crashing_poller()`), and
assertion bodies — following the per-scenario specifications in §4 and the gap map in §8.

**Prioritization** follows the audit's §9 "Operational Risk Level" ranking and §11 "Future
Work" list (`docs/FAILURE_SCENARIO_AUDIT.md`, both cited not re-derived):

1. **FS-01 and FS-04 first** (audit §9: "Why FS-01 and FS-04 are the top two priorities" —
   FS-04 is CRITICAL/latent with zero mitigation, FS-01 is the only finding where a
   fully-recovered infrastructure blip leaves a persistent manually-resolved production
   state). Maps to `TestRedisDownScenario` (§4.1, all 3 stubs) and
   `TestServerRestartScenario.test_schema_drift_undetected_after_create_all` (§4.10).
2. **EX-10/EX-02 polling second** (audit §11 item 2 — "up to ~1.5h of total silent fill-loss
   across all strategies," among the largest single blast radii in the audit). Maps to
   `TestPollingFailureScenario` (§4.6, all 5 stubs), especially
   `test_thread_crash_no_supervisor` and the two callback-exception stubs.
3. **FS-02 (breaker persistence)** (audit §11 item 3). Maps to
   `TestWorkerRestartScenario.test_breaker_resets_on_restart` (§4.2) and
   `TestBrokerApiFailureScenario`'s breaker stubs (§4.5).
4. **FS-05 (`PositionTracker` fill idempotency)** (audit §11 item 4). Maps to
   `TestDuplicateEventScenario.test_position_tracker_double_apply_on_duplicate_fill` and
   `test_reconcile_propagation_to_live_tracker_unknown` (§4.9).
5. **FS-07 (auth-endpoint retry)** (audit §11 item 5). Maps to
   `TestNetworkTimeoutScenario.test_auth_hashkey_timeout_fails_without_retry` (§4.4) — also
   where the §8 `KISAuth`/`KISClient` env-var fixture work must land first.
6. **FS-03/FS-06 (Command retention, duplicate watchdogs)** — lower urgency (audit §11 item
   6). Maps to `TestServerRestartScenario.test_command_table_rows_never_purged` (§4.10).
   FS-06 (duplicate watchdog instances) is not separately addressed in this suite — no §4
   scenario isolates it; a future task may add a dedicated stub.
7. **EX-11 (`is_registered()` guard)** (audit §11 item 7). Maps to
   `TestReconciliationFailureScenario.test_fill_dedup_across_reconciler_and_poller` (§4.7) —
   the regression-guard stub would gain a true concurrency case once `is_registered()`
   exists.

Audit §11 item 8 ("TASK 4-1B — build the failure-injection harness sketched in §8,
prioritizing §8.1 and §8.10") is **fulfilled by this document and its companion skeleton** —
all 10 of the audit's §8 sketches are now mapped into §4's specifications and the skeleton's
10 test classes.

---

## §10 Verification

- [ ] `docs/FAILURE_SCENARIO_TESTS.md` (this file) exists, contains no "TBD"/placeholder
  content, and covers all 10 scenarios in §4, each with all 7 required fields (Audit
  Cross-Refs, Expected Behavior, Recovery Expectations, Validation Assertions, Fail-Closed
  Rules, Audit Logging Expectations, Skeleton Mapping).
- [ ] §3 defines all 7 cross-cutting validation dimensions named in the task (safe shutdown,
  safe recovery, state consistency, audit log generation, kill switch behavior, no duplicate
  orders, no position damage), each with a concrete "measured by" check.
- [ ] §6 (Fail-Closed Rules), §7 (Audit Logging Expectations), and §8 (Known Gaps) tables are
  populated and cross-reference §4/finding IDs — no empty cells.
- [ ] §9 names a successor task and cites audit §9/§11 for prioritization.
- [ ] Total length: this document runs to ~1,400+ lines — longer than the ~900-1100 initial
  estimate because §4's 10 per-scenario specifications (each ~75-110 lines, vs. the
  ~48-62 lines originally scoped) carry most of the substantive content; no section is a
  placeholder or stub.
- [ ] `tests/integration/__init__.py` exists (empty, matches `tests/execution/__init__.py`
  and sibling packages).
- [ ] `tests/integration/test_failure_scenarios.py` exists: module docstring, imports per
  §2/§5, trivially-implemented `db_factory()`/`mock_broker()`/`mock_redis()`, 10 test
  classes with the exact names and stub methods listed in §5, every test method
  `@pytest.mark.skip`-marked with a docstring citing the relevant §4.N subsection and
  finding ID(s).
- [ ] `python -m pytest tests/integration/test_failure_scenarios.py --collect-only -q`
  succeeds: all ~40 stub methods collected, all marked `skip`, 0 errors, 0 failures.
- [ ] No fixes to `FS-`/`EX-`/`DO-`/`CA-`/`SD-` findings are implemented anywhere in the
  repo; no `.py` files other than the two new test files are modified.
- [ ] Both new files committed, pushed to `claude/trading-platform-philosophy-yNHQK`, and a
  draft PR opened (next number after #76).
