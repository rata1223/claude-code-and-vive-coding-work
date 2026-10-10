# Deployment-Hardening Roadmap

**Version**: 1.0  
**Date**: 2026-05-29  
**Status**: IN PROGRESS — every item audited against the code on 2026-10-09; see [Status at a glance](#status-at-a-glance-2026-10-09-main-f02dbf7)  
**Depends on**: [`PHILOSOPHY.md`](PHILOSOPHY.md), [`AUDIT.md`](AUDIT.md), [`BROKER_SEMANTICS.md`](BROKER_SEMANTICS.md)

> This roadmap translates the audit findings in `AUDIT.md` into a phased, dependency-respecting action plan.
> Every task references the originating defect, risk, or constraint code from the audit.
> Capital context: ₩2,000,000 (~$1,500 USD). A single duplicate order or unrecovered crash is material.

---

## Status at a glance (2026-10-09, main `f02dbf7`)

Every item below was checked against the code at this commit (P6-05, PR #220): the heading carries the status and an **Audit** note cites the file and line. Unmarked items no longer exist. Since then: P2-03 ⚠️→✅ (PR #221), P1-10 ⚠️→✅ (PR #222), P3-02 ⚠️→✅ (PR #223), P0-09 ⚠️→✅ (PR #224), P2-06 ⚠️→✅ (PR #225), P1-12 ❌→✅ (PR #226); corporate-action gate admits exits sized from the broker (PR #227).

| Status | Count |
|---|---|
| ✅ DONE (some shipped differently from the prescription — the note says how) | 40 |
| ⚠️ PARTIAL | 6 |
| ❌ OPEN | 3 |
| ⏸ DEFERRED | 2 |
| **Total** | **51** |

**What is left — the next-work candidates**, by phase:

| Item | Status | Gap |
|---|---|---|
| P0-02 Pre-submission fence | ⚠️ | Worker strategy orders are recorded only after `place_order` returns (the app's quick-trade path reserves first) |
| P0-13 FK constraints | ❌ | No foreign keys; `fills.order_id` is unconstrained (schema change on an existing table) |
| P1-03 Client order id before submit | ❌ | KIS has no client order id; tied to P0-02 |
| P1-08 Legacy bot removal | ⚠️ | `kis-bot` disabled, but `bot/` stays — `bot/notifier.py` is the worker's alert path |
| P2-01 Order event log | ⚠️ | Phase 1 (the log) done; deriving state from it and dropping `orders.status` is P6 |
| P6-02 Deploy gated on tests | ⚠️ | `deploy.yml` is disabled; manual deploys are not gated |
| P5-03 Risk system unification | ❌ | The app's quick-trade halt gate reads legacy Redis keys **nothing writes** — it never trips on losses |
| P6-01 State-machine tests | ⚠️ | No exhaustive all-pairs transition test |
| P6-03 Compose health checks | ⚠️ | None on frontend, kis-worker, kis-ws |

Deferred: P0-04 (per-broker SAFE_MODE — single-broker worker) and P4-04 (FX in the disabled legacy `bot/main.py`).

---

## Section 1 — Prioritized Roadmap

### Phase P0 — Deployment Blockers

These tasks MUST be complete before the system touches real capital.
Any single incomplete P0 item is sufficient to block the paper→real transition.

---

#### P0-01 — Fix `KISClient.post()` retry logic on order endpoints — ✅ DONE

> **Implemented differently from the prescription below, deliberately.** The
> plan said `except (requests.ConnectionError, requests.Timeout)` — i.e. keep
> retrying those. But a `Timeout` is exactly the case where KIS may already have
> booked the order and only the answer was lost, so retrying it is the duplicate
> this item exists to prevent. What shipped: a **new order is sent once and
> never re-sent**; an indeterminate outcome stays recoverable through the
> existing `QT_RESERVED` reservation (`reserve_and_submit`) and is resolved by
> `KISOrders.inquire_orders()`. Only replayable requests — the cancels, keyed to
> `ORGN_ODNO` — retry, via `post(..., idempotent=True)`.


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

#### P0-02 — Pre-submission fence: write PENDING before every `place_order()` call — ⚠️ PARTIAL

> **Audit (2026-10-09):** The app's quick-trade path reserves before it sends: `reserve_and_submit` writes a `QT_RESERVED` row first (`api/services/quick_trade_service.py:118`).
>
> The **worker** strategy path does not: `StrategyBase.buy/sell` call `place_order` directly (`backend/strategy/base.py:172`, `:196`) and the order reaches the state machine only afterwards (`_register_order`, `backend/strategy/indicator/strategy.py:217` — "after placement"). A crash between the two leaves an order the DB never saw; reconciliation (broker = ground truth) is what finds it.

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

#### P0-03 — Fix `EmergencyFlattenManager`: change `dry_run` default to `False`, wire into production path — ✅ DONE

> **The description below was wrong about the gap.** Flipping the default was a
> no-op: every construction site already passed `dry_run` explicitly (the API
> from `ENABLE_LIVE_TRADING`). The real gap was that **nothing automatic called
> the flatten at all** — the MDD kill switch closed `SAFE_MODE` and alerted.
>
> **Evidence**:
> * `backend/quant/risk/engine.py` — `LossTracker._evaluate` checks **MDD first**
>   and calls `_request_mdd_flatten`. MDD used to be checked last behind early
>   returns, so a crash day breaking the daily limit and MDD together never
>   evaluated MDD. The request is sent once per breach (`_mdd_flatten_requested`),
>   re-armed when the kill switch is cleared (`manual_reset`, an operator clear
>   adopted after the breach stops holding) or when the worker's flatten sent no
>   order and failed. A halt adopted from another process never requests one, and
>   an MDD halt restored at boot counts as already requested (R-CRIT-07: no
>   liquidation on startup — a crash before the flatten ran is left to the alert
>   and the manual endpoint).
> * `backend/worker/runner.py` — `StrategyWorker._on_mdd_breach` runs
>   `EmergencyFlattenManager.flatten_all` on a tracked thread (`shutdown()` joins
>   it), selling straight to the KIS broker; `SAFE_MODE` does not gate it.
> * `backend/worker/emergency.py` — `flatten_dry_run()`: dry run unless
>   `ENABLE_LIVE_TRADING=true`; `EMERGENCY_FLATTEN_DRY_RUN=true` forces a dry run
>   (the R-CRIT-07 rollback lever). `dry_run` is now a required keyword — no
>   silent default. The API uses the same helper.
> * ⚠️ **The automatic path is dry-run until armed**: `auto_flatten_dry_run()`
>   also requires `MDD_AUTO_FLATTEN=true`. The equity MDD is computed from
>   (`KISBroker.get_balance().total_eval_krw`) is unverified and may read low
>   (issue #178); misread, it would liquidate the book. Arm it after paper
>   trading shows no `[DRY RUN] 비상청산` without a real drawdown. A failed live
>   flatten that sent no order alerts the operator and retries on the next fill.
>   Even armed, a reading flagged `Balance.equity_verified=False` (a summary
>   field missing, USD cash held, a router leg failed) holds the flatten and
>   alerts — `StrategyWorker._equity_verified_for_flatten`.
> * Tests: `backend/worker/tests/test_mdd_flatten.py` — including the paper-mode
>   criterion below (sell orders for held symbols only, nothing else).
>
> Daily and weekly halts deliberately do **not** flatten ("당일 매매 중단").
> Not covered: flatten orders are not registered with the fill poller / order
> rows (same as the manual endpoint), and cross-process duplicate flattens remain
> the risk noted in `docs/EMERGENCY_FLATTEN_VALIDATION.md`.
>
> Dependency on P0-04: not needed — see P0-04.

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

#### P0-04 — Per-broker `SAFE_MODE`: replace global singleton with per-broker instance map — ⏸ DEFERRED

> **Not needed while the worker is single-broker.** Every broker the worker
> builds comes from `get_kis_broker()`; a per-broker map would hold one entry and
> change nothing. The reason P0-03 listed it as a dependency — a flatten must not
> be blocked by another broker's halt, and a KIS breach must not flatten Kiwoom —
> is met without it: the flatten sells directly on the KIS broker instance and
> never consults `SAFE_MODE`. **Revisit when Kiwoom is wired into the worker**;
> P0-12's in-process release shipped without it (one gate for one broker).

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

#### P0-05 — Thread-safe `PersistentLossTracker`: add `RLock` around all read-modify-write operations — ✅ DONE

> **Audit (2026-10-09):** `PersistentLossTracker._lock` is an `RLock` (`backend/quant/risk/engine.py:616`) held around each read-modify-write: `record_pnl` evaluates under it (`:833`), so does `reset_daily` (`:841`), and `_write_db` snapshots under it (`:1041`) — issue #158/#163.
>
> Test gap: the concurrent test (`backend/database/tests/test_postgres_compat.py:109`) asserts no error and one row, not that the summed loss is exact.

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

#### P0-06 — Fix US order status lookup: remove `output[0]` fallback — ✅ DONE

> **Audit (2026-10-09):** The status lookups match the requested `odno` and never fall back to another row; a read failure raises instead of returning `None` (PR #196).
>
> Both lookups match on `odno` (`backend/brokers/kis.py:390` KR, `:435` US). Shipped as `None` = "read every page, no such order" rather than an `OrderNotFound` exception. Test gap: `backend/brokers/tests/test_kr_order_status.py:35` pins the KR path only; no test asserts the US match.

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

#### P0-07 — Populate `idempotency_key` in `_persist_order()` using deterministic schema — ✅ DONE

> **Audit (2026-10-09):** `_persist_order` sets a deterministic key (broker order id, symbol, side, Seoul date — PR #215) and the column is unique (`backend/worker/runner.py:1667`, `backend/database/models.py:105`). A duplicate is skipped, not re-inserted.
>
> The key is `NULL` only for an order with no broker id, which is never registered (P3-02B orphan path).

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

#### P0-08 — Add `UniqueConstraint("symbol", "broker")` to `positions` table — ✅ DONE

> **Audit (2026-10-09):** `uq_position_symbol_broker` (`backend/database/models.py:184`). Test: `tests/postgres/test_pg_regression.py:57`.

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

#### P0-09 — Fix `db.merge()` → explicit upsert using `ON CONFLICT DO UPDATE` — ✅ DONE (PR #224)

> **Audit (2026-10-09):** `db.merge()` was gone and the unique constraint (P0-08) ruled out duplicate rows, but all three writers still did SELECT then UPDATE/INSERT: a racing first insert threw its write away with an `IntegrityError`.
>
> **Done in PR #224.** The writers are not `position_tracker.py` (in-memory only) but the fill pipeline, startup recovery and the reconciler. Every write now goes through helpers in `backend/database/models.py` (`upsert_position` = `INSERT … ON CONFLICT (symbol, broker) DO UPDATE`, `insert_position_if_missing` = `DO NOTHING`, `lock_position` = `SELECT … FOR UPDATE`; same pattern as `lock_risk_row`, #164):
> - **Fill pipeline** (`backend/worker/runner.py` `_upsert_position_db`): absolute upsert, delete via the locked row. The tracker is read inside a per-symbol lock (`_position_db_lock`) — the symbol's pending lock is released at step 2, so two fills on one symbol could otherwise commit an older tracker read last. Per symbol so a row-lock wait does not stall other symbols' fills (code-review).
> - **Recovery** (`backend/worker/recovery.py` `_apply_fill_to_position_db`): a first buy creates the row with `DO NOTHING`; every delta is applied to a locked row. If the row is deleted between the conflict and the lock (DO NOTHING does not lock), it inserts again.
> - **Reconciler** (`backend/execution/reconciler.py`, operator-approved): the pass reads the table first and commits once at the end. The missing-in-DB insert is `DO NOTHING`, and every update and delete first locks the row and checks it still holds what the pass read (`_lock_unchanged`). A row another writer inserted, changed or deleted in between is newer than the broker value the pass holds, so it is left for the next pass and reported as one gap (`position_appeared_during_reconcile` / `position_changed_during_reconcile`). Before, the pass overwrote a fresh fill with a stale broker value, and a plain add or an update of a deleted row failed the single commit and rolled back every other repair.
>
> Tests: `backend/worker/tests/test_position_upsert.py`, Postgres overlapping sessions `tests/postgres/test_position_upsert_db.py`, static guard `backend/worker/tests/test_position_writers.py`. A failed position write is still a warning (the fill itself is recorded, #222; recovery and the reconciler correct positions from the broker). **Left as is** (pre-existing, code-review): a recovery-poller fill adds a delta to the row while strategy trackers restored earlier do not know it, so the next strategy fill's absolute write can drop it until a reconcile; the reconciler's audit rows are written in their own session before the pass commits.

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

#### P0-10 — SIGTERM handler for graceful shutdown — ✅ DONE

> **Evidence**: `backend/worker/runner.py` — `install_signal_handlers()` registers
> SIGTERM **and** SIGINT; the handler only raises a flag (it runs in the main
> thread at an arbitrary bytecode, so a teardown there would take locks the
> interrupted frame holds), and a second signal exits immediately.
> `StrategyWorker.shutdown()` runs the ordered teardown inside one
> `_SHUTDOWN_BUDGET_SEC = 8.0` budget: scheduler → strategies → auxiliary threads
> → poller drain → equity checkpoint → heartbeat, then a `worker_shutdown`
> `AuditLog` row (the "recovery record" this item asks for). `run()` calls it from
> a `finally`, so it happens on a crash out of the loop too.
> Tests: `backend/worker/tests/test_graceful_shutdown.py` (41).
>
> **Three things here are counter-intuitive and are pinned by their own tests:**
>
> * The teardown stops sessions with `deactivate=False`. `WorkerSession._run`'s
>   `finally` normally flips `strategy_runs.is_active` to `False`, and
>   `_restore_active()` only restores rows where it is `True` — so the obvious
>   implementation would switch every strategy off on every deploy.
> * The teardown does **not** delete `worker:heartbeat`. `WorkerWatchdog` runs in
>   the API process (`backend/api/gunicorn_conf.py:31`) and sets
>   `DailyRiskState.kill_switch` the moment that key is missing. Letting the 90s
>   TTL lapse is what lets a restart come back unnoticed.
> * The equity checkpoint writes the three equity columns **directly** rather than
>   calling `PersistentLossTracker._persist()`, which would also write
>   `kill_switch` from the tracker's in-memory value (issue #158). At shutdown
>   that direction is fail-**open**: Redis down → heartbeat expires → the API
>   watchdog sets `kill_switch=True` while this worker is alive holding an
>   in-memory `False` → the teardown writes `False` back and the restart trades
>   with the halt erased.
>
> Also required: the pub/sub loop moved from `pubsub.listen()` to
> `get_message(timeout=1.0)`. PEP 475 resumes a blocking socket read once the
> handler returns, so a flag raised by SIGTERM would otherwise never be read and
> the process would still sit there until SIGKILL.
>
> **Two more things the budget has to actually cover** (both from review):
>
> * `WorkerSession.stop()` sets the stop event and returns. `strategy.stop()` —
>   which calls the overridable `on_stop()`, and `ScriptStrategy` runs a sandboxed
>   *user script* there — happens in `_run`'s cleanup on the session's own thread,
>   so the bounded `join()` limits it. Calling it from `stop()` put user code
>   ahead of and outside the deadline, and worse, ran it on the pub/sub loop
>   thread (`_handle_stop`), where a wedged script froze the whole command loop.
>   In that cleanup `_mark_stopped()` runs **first**: the durable record of an
>   operator's stop must not be held hostage by code that may never return.
> * `StartupRecovery` takes a `should_abort` callback and checks it before each of
>   its nine steps; `main()` passes the worker's flag and, if a stop was requested
>   during boot, skips `scheduler.start()` and tears down directly. Without it a
>   SIGTERM during startup was only seen after the whole boot — the two broker
>   probes wait `_BROKER_STARTUP_TIMEOUT` (30s default) each.
>
> **Not covered**: in-flight *order submissions* are not individually drained.
> `IndicatorStrategy._scan_and_trade()` never checks `is_running()`, so a scan
> already in flight keeps submitting after its strategy is stopped; the teardown
> caps its wait at `_AUX_JOIN_CAP_SEC = 3.0` rather than spending the poller's
> budget on it. Orders it did submit are persisted and re-registered with the
> poller by `StartupRecovery` on the next boot
> (`backend/worker/recovery.py:333`), so abandoning that thread costs tracking
> until restart, not the order. The existing `QT_RESERVED` reservation and the
> boot-time `PositionReconciler` remain what resolves an indeterminate submit.
>
> A recovery step **already in flight** is also still not interruptible:
> `_step_balance`/`_step_positions` use `with ThreadPoolExecutor(...) as ex:`, and
> `__exit__` waits for the submitted task even after `.result(timeout=...)` has
> raised. The cancellation check above is per step boundary only. Tracked
> separately.

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

#### P0-11 — Enforce `KIS_CREDENTIAL_KEY` non-empty at startup — ✅ DONE

> **The item as written was stale.** `docker-compose.yml` already refuses to
> start the `api` service without the key (`${KIS_CREDENTIAL_KEY:?…}`), and the
> worker never decrypts stored credentials (only the `api/` package imports
> `api/crypto.py` — its routers and `api/main.py`), so a worker-side check would
> add a failure mode for nothing.
>
> **What was actually open, and is now closed** (`api/crypto.py`, `api/main.py`):
> * A **malformed** key used to surface only on the first credential request,
>   as a 500. `crypto.validate_key()` now runs first in the API lifespan and
>   fails startup.
> * A **valid key that does not match** the stored data (rotated, mistyped) was
>   silent — `decrypt` swallowed the error and callers sent an empty app key to
>   the broker. Once the database answers, a background check test-decrypts
>   every encrypted field of up to 20 stored credentials (off the startup path)
>   and logs
>   `N/M` mismatches CRITICAL (counts only), and `decrypt` warns once per
>   process on `InvalidToken`. A mismatch does **not** refuse startup:
>   re-entering credentials through this API is the fix.
> * Tests: `tests/integration/test_credential_key.py` (the lifespan case needs
>   FastAPI and runs locally only — #127).
>
> **Follow-up (#182):** the broker paths no longer take `decrypt(...) or ""`.
> A stored field that does not open raises `CredentialUnreadable`
> (`api/crypto.py` `decrypt_required` / `kis_credential_fields`) before any KIS
> client is built, and QuickTrade `place_order` checks it **before reserving** —
> found inside `broker_submit` it left a never-sent order RESERVED. An absent
> (NULL) field is still allowed. Tests: `tests/integration/test_credential_unreadable.py`,
> `api/tests/test_quick_trade_credential_unreadable.py`.

| Field | Value |
|---|---|
| **Purpose** | Credentials are stored encrypted with `KIS_CREDENTIAL_KEY`. Compose already refuses an empty key; the gaps were a malformed key surfacing only on first use, and a key that no longer matches the stored data failing silently. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Low — `crypto.validate_key()` first in the API lifespan (fails startup); a background stored-data check logs mismatches |
| **Dependencies** | None |
| **Operational Impact** | Prevents silent credential exposure; fails fast on misconfigured deployments |
| **Affected Files** | `api/crypto.py`, `api/main.py` |
| **Deployment Priority** | 11 of 15 |
| **Audit Reference** | AUDIT.md DB-02 |

---

#### P0-12 — Kill-switch reset API endpoint — ✅ DONE

> **Done (PR #218).** Both halves: the durable release (below) and resuming
> without a restart. This item was written about
> the in-memory `SAFE_MODE` ("no programmatic way to clear it without restarting
> the process"). Investigating it surfaced a **worse, different gap** that was
> closed first:
>
> `DailyRiskState.kill_switch` (Postgres) is set on an MDD breach and **a restart
> does not clear it** — `StartupRecovery._step_risk` re-reads it and
> `_step_enable_trading` re-halts, logging *"수동 해제 후 재시작 필요."* The only
> writer of `False` was `KillSwitch._clear_halt_in_db`, inside a class **never
> constructed in production**. So the documented "manual release" meant editing
> the row by hand in SQL.
>
> ✅ **Shipped**: `api/routers/risk.py` — `GET /api/risk/kill-switch` (status) and
> `POST /api/risk/kill-switch/reset` (clear, mandatory written reason, `AuditLog`
> row naming the operator). Tests: `api/tests/test_risk_killswitch_reset.py`.
>
> ✅ **Also closed (issue #158)**: the reset used to be *silently* undone —
> `PersistentLossTracker._write_db` overwrote the column from its in-memory value
> on every PnL write. It now re-reads the row and asserts the flag only when it
> has a decision of its own to record, so an external clear survives and the
> tracker converges to it. The old "stop the worker → reset → start" procedure is
> no longer needed. Tests:
> `backend/worker/tests/test_kill_switch_convergence.py` (24).
>
> Four things review turned up while closing it, all fixed in the same change:
>
> * A row that does not exist yet is **not** an external opinion — its
>   `kill_switch` is `None` before flush, and adopting `bool(None)` cleared a live
>   halt at the first write against each new date key, i.e. every day boundary.
> * A **failed** write leaves the intent pending. Re-asserting a stale *halt* is
>   fail-closed and kept; re-asserting a stale *clear* over a halt set meanwhile
>   is fail-open and is now dropped.
> * Adopting a halt into memory changed nothing on its own — `can_buy()` has no
>   production callers and the real order gate is `SAFE_MODE`. Adoption now closes
>   that gate (adopting a *clear* deliberately does not re-open it).
> * Pre-existing, found here: `self._lock` was a plain `Lock`, and
>   `record_pnl()` → `reset_daily()` re-enters it at the Seoul date rollover — a
>   permanent hang on the first fill after KST midnight, inside the US session.
>   Now an `RLock`.
>
> ✅ **Closed in PR #218 — resume without a restart, and what a release accepts**
> (operator decisions, 2026-10-08):
>
> 1. **Resume.** `StrategyWorker._resume_if_released` (`backend/worker/runner.py`,
>    every 60 s, job `risk_resume`) reopens `SAFE_MODE` once no row is halted
>    (`risk_days_in_play`) and the tracker, settled with the row
>    (`PersistentLossTracker.refresh_from_db`), is clear — **only** for a
>    `RISK_BREACH` halt in a worker whose recovery succeeded or stopped only at
>    a restored halt (`allow_risk_resume`). Untrusted state still needs a
>    restart. The check and the reopening run under the tracker's lock
>    (`if_clear`). Not P0-04: the worker is single-broker, so one gate is the
>    right granularity until Kiwoom joins it.
>    - Found on the way: `StartupRecovery` recorded a restored halt as
>      untrusted state (`_step_enable_trading`, then `run()` overwrote it again),
>      so no poll could ever reopen it. It is `RISK_BREACH` now
>      (`halted_by_risk`); an unreadable risk state stays untrusted.
>    - The 07:01 job no longer reopens `SAFE_MODE` at all — it did so for any
>      cause, so a worker whose recovery failed began trading at 07:01. A failed
>      recovery now raises a Telegram alert ("재시작 필요") instead.
> 2. **Re-halt only if it gets worse.** A breach on an already-halted tracker
>    decides nothing new (`LossTracker._halt`), so the next write adopts the
>    release instead of re-asserting the halt, and adopting it sets a baseline
>    (`_set_release_baseline`): a daily or weekly limit past its setting at the
>    release halts again only after another `release_step_pct` (1%) of capital
>    is lost (one not reached stays as configured) — the daily floor
>    for that risk day only; the weekly one counts only the accepted loss still
>    inside the rolling window, so it lapses as that loss rolls out — and an
>    MDD breach is rebased on current equity (on the first reading, if none
>    yet). Floors persist as an `AuditLog` row (`risk_release_baseline`) and
>    are restored at boot; no schema change. A release *is* the app's
>    `kill_switch_reset` audit row (written with the clear), so one made while
>    the worker was down, or before its tracker held the halt, is still applied
>    — at boot, or by the poll. A halt restored from an older row
>    is written to today's row at boot (`write_pending`) so the carry cannot
>    overwrite a later release.
>
> Tests: `backend/worker/tests/test_release_baseline.py`,
> `tests/postgres/test_release_baseline_db.py`, and the updated
> `test_kill_switch_convergence.py` / `test_mdd_flatten.py`.

| **Purpose** | Once `SAFE_MODE` activates, there is no programmatic way to clear it without restarting the process. Operators must SSH in and restart the worker, which creates a window of uncontrolled state. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low — add `POST /admin/safe-mode/reset` with admin token auth; call `safe_mode_map[broker].clear()` |
| **Dependencies** | ~~P0-04~~ — not needed while the worker is single-broker (see above) |
| **Operational Impact** | Controlled recovery path without process restart; audit log of who reset which broker |
| **Affected Files** | `backend/api/routers/` (new `admin.py`), `backend/worker/recovery.py` |
| **Deployment Priority** | 12 of 15 |
| **Audit Reference** | AUDIT.md DB-07 |

---

#### P0-13 — Add FK constraints: `fills.order_id → orders.id`, `trades.order_id → orders.id` — ❌ OPEN

> **Audit (2026-10-09):** No `ForeignKey` anywhere in `backend/database/models.py`; `fills.order_id` (and `order_events.order_id`, PR #219) are plain integers. Adding one is a schema change on an existing table — plan it with the `create_all` caveat (#194).

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

#### P0-14 — Alembic migration framework — ✅ DONE

> **Audit (2026-10-09):** `alembic/` with four revisions (head `e2f3a4b5c6d7`, PR #219). CI runs upgrade → downgrade → upgrade on a fresh database (`.github/workflows/ci-postgres.yml`).
>
> Production tables still come from `create_all` (`init_db_factory`); Alembic is the reviewed record of the schema, not what boots it.

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

#### P0-15 — Fix CORS: replace `allow_origins=["*"]` with env-var allowlist — ✅ DONE

> **Audit (2026-10-09):** Allowlist from `CORS_ORIGINS`/`CORS_ALLOWED_ORIGINS`, and `*` is refused while credentials are allowed (`api/main.py:165`–`183`).

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

#### P1-01 — Fix Kiwoom base URL — ✅ DONE

> **Audit (2026-10-09):** `KIWOOM_BASE = "https://openapi.kiwoom.com:10000"` (`kiwoom_adapter/client.py:12`). Whether it is reachable without an SSL error needs a live call; `backend/brokers/kiwoom.py` is still a stub (`NotImplementedError`) — see known issue 2.

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

#### P1-02 — `OrderStateMachine` callback outside lock — ✅ DONE

> **Audit (2026-10-09):** `OrderStateMachine` calls `on_state_change` after leaving its lock in `register`, `transition` and `process_fill` (`backend/execution/order_machine.py:54`, `:70`, `:110`).

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

#### P1-03 — Fix order ID mutation: generate client order ID before submission — ❌ OPEN

> **Audit (2026-10-09):** KIS takes no client order id (`backend/brokers/models.py:44`, `retry_safe_on_submit` False for both brokers), so the id exists only after the broker answers. The worker records the order after placement (see P0-02). The quick-trade reservation covers the app path.

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

#### P1-04 — `StaleDataWatchdog`: TTL-based staleness rejection on all market data reads — ✅ DONE

> **Audit (2026-10-09):** Shipped as `FreshnessGate` (`backend/data/freshness_gate.py:69`), raising `StaleFeedError` (`:189`) in the strategy buy/sell path (`backend/strategy/base.py:136`). The old `StaleDataWatchdog` was dead code and was removed (R-11). Tests: `backend/data/tests/test_freshness_gate.py`.

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

#### P1-05 — Redis reconnect resilience — ✅ DONE

> **Audit (2026-10-09):** `_run_with_pubsub` reconnects on `redis.ConnectionError` with backoff 2 → 64 s (`backend/worker/runner.py:1037`–`1084`). The 30 s target holds for the first attempts; a long outage waits up to 64 s between tries.

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

#### P1-06 — Worker heartbeat: periodic liveness signal to Redis — ✅ DONE

> **Audit (2026-10-09):** `WorkerHeartbeat` and the API-side `WorkerWatchdog` (`backend/worker/heartbeat.py`), which halts on an expired beat. `/api/admin/heartbeat` (`backend/api/server.py:447`) answers `alive: false` with 200 rather than 503.

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

#### P1-07 — SQLAlchemy session safety: per-operation sessions, no long-lived session reuse — ✅ DONE

> **Audit (2026-10-09):** Worker code opens a session per operation (`_get_session_factory`/`_session`, `backend/worker/runner.py:68`–`80`). Nothing passes the legacy `db_session=` to the tracker, and `init_db()` has no callers.

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

#### P1-08 — Decommission legacy bot: remove `kis-bot` from docker-compose — ⚠️ PARTIAL

> **Audit (2026-10-09):** `kis-bot` is commented out of `docker-compose.yml` (`:93`–`100`). `bot/` is not archived: `bot/notifier.py` is a live dependency of the worker (every alert), and `bot/main.py`/`bot/scheduler.py` remain as dead legacy.

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

#### P1-09 — Single scheduler: unify duplicate APScheduler instances — ✅ DONE

> **Audit (2026-10-09):** One `BackgroundScheduler` in the running services (`backend/worker/scheduler.py:331`); `bot/scheduler.py` runs only in the disabled `kis-bot`.

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

#### P1-10 — `on_filled` exception propagation: remove bare except in fill callback — ✅ DONE (PR #222)

> **Audit (2026-10-09):** The poller no longer swallows a failed fill callback — it keeps the entry and retries (`backend/execution/order_poller.py:371`–`377`).
>
> **Done in PR #222.** A fill that cannot be recorded — the write raised, or there is no order row to file it under — calls `report_fill_write_failure` (`backend/worker/recovery.py:297`): `SAFE_MODE` is **latched** until a restart (`SafeModeState.latch`, `:266`) with the new cause `RECORD_FAILURE` (`backend/risk/halt_policy.py:48`) — entries blocked, exits, emergency flatten and cancels allowed (the in-memory tracker has the fill; only the database is behind). While latched, `enable()` refuses (`:233`) — neither the kill-switch resume poll nor the end of startup recovery can reopen it — and a later halt can neither soften the cause (untrusted wins; anything else stays `RECORD_FAILURE`) nor drop the unrecorded fill from the reason. A redelivered last fill whose closed row (today's) already has fills reaching the broker total is a duplicate, not a failure. One emergency alert per process, an ERROR log, and a `fill_write_failed` audit through its own session when the database allows. The startup-recovery fill stub reports the same way, and `_step_enable_trading` checks the latch before any other branch (`:925`). The quantity reported is what is missing after the broker-total adjustment (`backend/worker/runner.py:1789`, `:1865`). A skipped duplicate or a refused overfill (P2-03) is a decision, not a failure. Restart is the recovery: startup recovery reconciles orders, fills and positions with the broker. Tests: `backend/worker/tests/test_fill_write_failure.py`. Exceptions still do not propagate out of `on_filled` — each step catches its own, which is what keeps the poller from re-running steps that already applied.

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

#### P1-11 — Mask `hts_id` in API credential response — ✅ DONE

> **Audit (2026-10-09):** `"hts_id": "****"` in the credential response (`api/routers/credentials.py:25`).

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

#### P1-12 — `_QTY_TOLERANCE` fractional: replace hardcoded `1` share with dynamic calculation — ✅ DONE (PR #226, differently from the prescription)

> **Audit (2026-10-09):** `_QTY_TOLERANCE = 1` was a fixed share count (`backend/execution/reconciler.py:96`): a quantity was repaired only when it differed by **more than one share**, so a one-share divergence was never repaired or reported (and if the average also drifted, only the average was fixed). A restart then restored the wrong row into the tracker (`_restore_positions`).
>
> **Done in PR #226 — tolerance 0, not the 0.5 % prescription** (operator decision). KIS quantities are whole shares on both sides (`int(hldg_qty)`/`int(ovrs_cblc_qty)`, `positions.qty` is `Integer`), and a difference from an order still filling is already deferred by `_has_pending_order` (`qty_mismatch_pending`) — so any remaining difference is a real divergence. On this account one share can be 30–100 % of a position; a percentage tolerance would hide even larger gaps on larger positions. A one-share gap now takes the existing mismatch path: open order → defer; otherwise corporate-action classification (a known split ratio with value preserved is CONFIRMED, anything else UNKNOWN → the symbol is gated, fail-closed), then the row is set to the broker's values under `_lock_unchanged` (#224) with a `reconcile_fix_qty` audit. The class attribute stays for a future fractional-share broker. **The open-order check now covers every in-flight change** (code-review): with no tolerance, the fill pipeline's own gap — order row FILLED in step 1, position written in step 5 after a broker balance call — would be read as a mismatch and gate the symbol; `_has_pending_order` also counts an order that turned FILLED within `_RECENT_FILL_SEC` (600 s) and `unknown`-status orders (the order reconciliation already treats those as open). **Remaining** (pre-existing, more frequent now): an UNKNOWN classification gates the symbol until an operator resolves it (~~exits included~~ — since PR #227 the gate blocks entries only: a strategy exit on a gated symbol goes through `PositionTracker.claim_ca_exit`: sized from a live broker lookup — the broker's sellable qty — with the broker's held qty and split-adjusted average adopted into the tracker first (so realized P&L and partial fills use the broker's figures), and the stop-loss measured against the broker's average; any doubt about the broker figure skips the exit, tests `backend/strategy/tests/test_ca_gate_exit.py`. Found while doing it, pre-existing: nothing in production calls `IndicatorStrategy.on_bar`, so the worker never runs the 7 % stop-loss `_check_exit` — exits come only from the market-open signal scan); the reconciler fixes the DB row but not the in-memory tracker, so a later fill's absolute write can reintroduce the gap until the next pass; while an order is open an average-price drift is not fixed either. Tests: `backend/execution/tests/test_reconciler.py::TestQtyToleranceExact`.

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

#### P2-01 — Append-only `order_events` table: replace mutable status with event log — ⚠️ PARTIAL (phase 1 of 2 done, PR #219)

> **Phase 1 shipped: the log.** Every insert of an `orders` row and every change
> of its status, fill (`filled_qty`, `avg_fill_price`) or broker order number
> appends an `order_events` row — written by one session hook
> (`backend/database/order_history.py`, `after_flush`) on the flush's own
> connection, so the event commits or rolls back with the change. No writer
> logs by hand (runner, recovery, reconciler, terminal events, harness were not
> touched), so none can forget to. Append-only: the ORM refuses to update or
> delete an event, and on Postgres a trigger refuses UPDATE/DELETE/TRUNCATE —
> installed where the worker and kis-api open the database
> (`init_db_factory` → `ensure_db_guard`, since production tables come from
> `create_all`) and by the Alembic migration `e2f3a4b5c6d7`. Events record the
> row as the flush left it, read back, not the session's possibly stale
> object. An AST guard keeps production code from writing `orders` around a
> session (Core, bulk, `__table__` DML, `query(...).update()`, raw SQL). Startup
> recovery audits an order updated in the last week whose status disagrees
> with its latest event (`order_status_event_mismatch`); orders from before the
> log are only counted.
>
> **Phase 2 — deferred to P6 (R-CRIT-03):** derive current state from the log
> and drop the `orders.status` shadow. Readers still read `orders`.

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

#### P2-02 — `StartupRecovery` 8-gate validation sequence — ✅ DONE

> **Audit (2026-10-09):** `StartupRecovery.run()` runs nine gates — DB, Redis, risk state, balance, positions, reconcile, pending orders, consistency, enable trading (`backend/worker/recovery.py`). A failed gate keeps SAFE_MODE shut (the process stays up rather than refusing to start); the consistency gate is observability by design.

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

#### P2-03 — Fill idempotency: deduplicate on `(order_id, seq_no)` before inserting — ✅ DONE (PR #221, shipped differently)

> **Audit (2026-10-09):** The audit found the dedup key too weak: `_persist_fill` skipped any fill with the same `(order_id, qty, price)`, so a second fill whose increment and price equalled the first was dropped — from `fills`, and where the state machine did not know the order, from `orders.filled_qty` too.
>
> **Fixed in PR #221, differently from the prescription.** There is no broker fill number in our model and no `fills` schema change, so the identity is **the total a fill brings its order to**. The poller hands each fill over as an increment against its watermark and now also passes that total (`Order.cumulative_filled_qty`, `backend/execution/order_poller.py:370`, `:433`). `_persist_fill` locks the order row and re-reads it under the lock (`SELECT … FOR UPDATE`), sums the fills on file, and skips the fill only when they already reach its total; if the fills on file and the total disagree (a fallback row on file, or an earlier fill write that failed) it files what the total says is missing, so the fills always add up to `orders.filled_qty`, and `orders.filled_qty` is the broker's total (`backend/worker/runner.py:1758`–`1794`). A redelivery carries the same total; a second fill of the same size a larger one. Without a total (no poller in between) it refuses only a fill that would push the order past its quantity, and audits `fill_overfill_rejected`. The poller watermark stays the first line. Tests: `backend/worker/tests/test_fill_identity.py` (equal fills both land with and without the state machine, redelivery skipped, through the real poller including a callback retried after its write), `tests/postgres/test_fill_identity_db.py` (a concurrent copy waits for the row lock and is skipped; the order totals start from what the other session committed).

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

#### P2-04 — `PositionReconciler`: broker-wins reconciliation with divergence logging — ✅ DONE

> **Audit (2026-10-09):** `PositionReconciler.reconcile()` is broker-wins with divergence audit (`backend/execution/reconciler.py:116`). Tests: `backend/execution/tests/test_reconciler.py`.

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

#### P2-05 — `BrokerCapabilities` dataclass + `BrokerSemanticMapper` ABC — ✅ DONE

> **Audit (2026-10-09):** `BrokerCapabilities` (`backend/brokers/models.py:14`) and `BrokerSemanticMapper` (`backend/brokers/semantic_mapper.py:58`). Market routing is enforced by `BrokerCapabilityValidator` (`backend/brokers/validator.py:81`), raising `UnsupportedCapabilityError` rather than `MarketMismatchError`.

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

#### P2-06 — KIS polling loop: structured `OrderFillPoller` with exponential backoff and circuit breaker — ✅ DONE (PR #225)

> **Audit (2026-10-09):** Per-order escalating intervals 10 → 300 s (`advance()`), a per-app-key rate limit (PR #154), and a circuit breaker on order placement (`backend/brokers/kis.py:130` — `get_order_status` neither checks nor feeds it). Poll failures themselves tripped nothing: ten in a row only logged CRITICAL, and `PollingHealth.is_healthy` had no production reader.
>
> **Done in PR #225.** `backend/execution/order_poller.py`: `_BREAKER_THRESHOLD` (5) consecutive poll failures across all orders open a breaker in `PollingHealthMonitor` (`record_poll_error`). While open the loop (`_poll_due`) makes no lookups; after `_BREAKER_COOLDOWN_SEC` (60) one lookup — the earliest-due order — tests the broker, a failed probe doubles the cooldown up to `_BREAKER_MAX_COOLDOWN_SEC` (300), a success closes it and polling resumes. The tick stops as soon as the breaker opens. `is_healthy` reflects it. The worker wires `on_circuit_open` / `on_circuit_close` to Telegram (`backend/worker/runner.py` `_alert_poll_circuit_open`: emergency; close: info with the downtime). Callback errors (retried fills) do not count.
>
> **Found while doing it — a timeout needs a fresh read:** an order was timed out 30 minutes after registration whether or not its status could be read, and the worker's `on_timeout_cb` converges it to CANCELED even when the cancel fails (P3-02B H1). A KIS outage over 30 minutes therefore marked every pending order cancelled and released its symbol lock, though the broker may have filled it. Now an expired order is timed out only after a lookup **in the same tick** succeeded and left it open (`_poll_due`) — no lookups while the breaker is open, every order is read again after it closes (the probe reads only one), and an order whose own lookup keeps failing is not timed out: it is reported once (ERROR + `poller_timeout_deferred` audit) and stays tracked (code-review). Telegram is sent on a separate thread so a slow send does not stall polling or the shutdown join. **Remaining**: with a fresh read, a timeout whose cancel fails is still converged to CANCELED (operator decision); new orders are not gated while the breaker is open (their fills are seen once it closes); `is_healthy` now turns false at 5 consecutive failures (no production reader yet). Tests: `backend/execution/tests/test_poller_circuit_breaker.py`.

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

#### P3-01 — Replace crypto exchanges with KIS + Kiwoom in `exchanges.js` — ✅ DONE

> **Audit (2026-10-09):** KIS and Kiwoom only, in both apps (`frontend/src/constants/exchanges.js:1`–`14`, `mobile/src/constants/exchanges.js`; PR #152).

| Field | Value |
|---|---|
| **Purpose** | Mobile app shows 11 cryptocurrency exchanges. This platform trades Korean equities only. Wrong exchange list creates confusion and dead code paths. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Trivial |
| **Dependencies** | None |
| **Affected Files** | `mobile/src/constants/exchanges.js` |

---

#### P3-02 — `CredentialForm.vue`: KIS + Kiwoom fields, paper/real toggle — ✅ DONE (PR #223)

> **Audit (2026-10-09):** The web form was still the crypto form (API Key / Secret / Passphrase, no account number), so a KIS credential saved from the web could not trade (`CANO`, `kis_adapter/orders.py:46`). The mobile form had the fields but demanded **12** characters — a KIS account is 10 digits (CANO 8 + product code 2, split `[:8]`/`[8:]`).
>
> **Done in PR #223.** One internationalised form, identical in both apps (`frontend/` = `mobile/` `src/views/profile/CredentialForm.vue`): App Key, App Secret, account number (`50123456-01` or `5012345601`, sent as 10 digits), optional HTS ID, paper on by default; Kiwoom shows "not supported yet" and cannot be saved; no passphrase. The API stores the account as 10 digits and refuses another length for KIS (`api/compat.py` `_kis_account`; a missing account is still accepted — the forms require it). `kis_adapter/auth.py` `normalize_account_no` drops `-` and whitespace where the account enters `KISAuth`, and the worker's `KISBroker` (whose cancel and order-status calls split the account themselves) reads it through `auth.require_account()` — so the `.env.example` form `50123456-01` no longer sends `-01` as the product code. An account that is still not 10 digits (rows saved through the old 12-character mobile form) is logged as a warning. Kiwoom accounts are not normalised (`kiwoom_adapter` splits on the hyphen). Tests: `tests/integration/test_frontend_credential_form.py`, `api/tests/test_compat_credentials.py::TestKisAccountFormat`, `kis_adapter/tests/test_account_no_format.py`.

| Field | Value |
|---|---|
| **Purpose** | Current form likely mirrors QuantDinger's crypto credential schema. Need KIS-specific fields (app key, secret, account no, HTS ID, paper/real toggle) and Kiwoom fields. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Medium |
| **Dependencies** | P3-01 |
| **Affected Files** | `mobile/src/views/profile/CredentialForm.vue` |

---

#### P3-03 — Remove dead routes: `profile/referral`, `profile/credits`, `market/*` — ✅ DONE

> **Audit (2026-10-09):** No `profile/referral`, `profile/credits` or `market/*` route in `mobile/src/router/index.js` or `frontend/src/router/index.js` (PR #152).

| Field | Value |
|---|---|
| **Purpose** | QuantDinger-inherited routes serve features that do not exist in this platform. Dead routes produce 404s and confuse operators. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Trivial |
| **Dependencies** | None |
| **Affected Files** | `mobile/src/router/` |

---

#### P3-04 — Pinia store split: auth, broker, strategy, market, websocket — ✅ DONE

> **Audit (2026-10-09):** PR #200: eight store modules, identical in both apps; guard `tests/integration/test_frontend_store_parity.py`.

| Field | Value |
|---|---|
| **Purpose** | All state likely in a single monolithic store inherited from QuantDinger. Split enables independent state updates and reduces re-render scope. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Medium |
| **Dependencies** | P3-02 |
| **Affected Files** | `mobile/src/stores/` |

---

#### P3-05 — Fix `DEFAULT_SERVER_URL`: set to empty string, configure from build-time env — ✅ DONE

> **Audit (2026-10-09):** `DEFAULT_SERVER_URL = ''` in both apps (`frontend/src/config/index.js:1`, `mobile/src/config/index.js:1`).

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

#### P4-01 — Deduplicate `EXCD_MAP`: single canonical source in `universe.py` — ✅ DONE

> **Audit (2026-10-09):** One `EXCD_MAP` in `backend/quant/data/universe.py`, imported by `backend/market/symbols.py:29` (PRs #151, #153, #197).

| Field | Value |
|---|---|
| **Purpose** | Exchange code mapping defined in both `backend/quant/data/universe.py` and `backend/brokers/kis.py`. Divergence causes wrong exchange codes on orders. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low — keep `universe.py` as canonical; import from there in `kis.py` |
| **Dependencies** | None |
| **Affected Files** | `backend/quant/data/universe.py`, `backend/brokers/kis.py` |
| **Audit Reference** | AUDIT.md C-05 |

---

#### P4-02 — `SimulatedBroker`: same `BrokerAdapter` interface for backtesting and live — ✅ DONE

> **Audit (2026-10-09):** `SimulatedBroker(BrokerAdapter)` with commission and slippage (`backend/strategy/runtime/simulator.py:19`, `:67`–`68`); also `backend/brokers/paper_broker.py` for the paper harness.

| Field | Value |
|---|---|
| **Purpose** | Strategy code that works in backtest must work in live without modification. `SimulatedBroker` provides a paper-trading stub that satisfies `BrokerAdapter` with realistic fills (0.015% KIS commission, slippage model). |
| **Risk Level** | LOW |
| **Implementation Complexity** | Medium |
| **Dependencies** | P2-05 (BrokerCapabilities defines the interface) |
| **Affected Files** | New: `backend/strategy/runtime/simulator.py` |

---

#### P4-03 — `IndicatorStrategy` backtest endpoint — ✅ DONE

> **Audit (2026-10-09):** `POST /api/strategies/backtest` (`api/routers/strategies.py:591`): indicator strategies through `IndicatorStrategy.from_config`, scripts in a time- and memory-bounded child process (PR #191). Uses the project's own `Backtester`, not `backtesting.py`.

| Field | Value |
|---|---|
| **Purpose** | Mobile strategy builder needs server-side backtest execution. Wraps `backtesting.py` with JSON condition input and returns `{sharpe, mdd, win_rate, cagr, equity_curve, trades}`. |
| **Risk Level** | LOW |
| **Implementation Complexity** | High |
| **Dependencies** | P4-02 (SimulatedBroker) |
| **Affected Files** | New: `backend/strategy/indicator/backtest.py`, `backend/api/routers/backtest.py` |

---

#### P4-04 — Wire `TradingEngine` real FX rate with graceful degradation — ⏸ DEFERRED

> **Audit (2026-10-09):** Only the legacy `bot/main.py` converts FX, falling back to a hardcoded 1350 (`bot/main.py:26`–`33`), and `kis-bot` is disabled. The live worker's equity comes from the KIS balance, whose USD gaps are tracked as #178 (known issue 10).

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

#### P5-01 — `StrategyBase` event methods: `on_start`, `on_bar`, `on_fill`, `on_market_open/close`, `on_stop` — ✅ DONE

> **Audit (2026-10-09):** `on_start`, `on_stop`, `on_market_open`, `on_market_close`, `on_bar`, `on_fill` (`backend/strategy/base.py:106`–`146`).

| Field | Value |
|---|---|
| **Purpose** | Current `StrategyBase` lacks lifecycle hooks. Strategy code directly calls broker methods rather than declaring intent via events. Event methods enable simulation, replay, and safe interception. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Medium |
| **Dependencies** | P2-05 (broker routing must be correct before strategies run) |
| **Affected Files** | `backend/strategy/base.py` |

---

#### P5-02 — `ScriptStrategy` sandbox: RestrictedPython + AST whitelist + timeout — ✅ DONE

> **Audit (2026-10-09):** `compile_restricted` with guarded builtins (`backend/strategy/script/sandbox.py:16`, `:157`; legacy `strategy/script_strategy.py:14`, PR #187); backtests run in a killed-on-budget child process (`strategy/script_backtest.py`, PR #191).

| Field | Value |
|---|---|
| **Purpose** | User-submitted Python strategy scripts could contain `import os; os.remove("/")` or network calls. Sandbox must block dangerous imports and enforce execution timeout. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | High — RestrictedPython + AST node visitor whitelist; `RESTRICTED_BUILTINS`; 30-second execution timeout via `concurrent.futures` |
| **Dependencies** | P5-01 (StrategyBase) |
| **Affected Files** | New: `backend/strategy/script/sandbox.py` |

---

#### P5-03 — Dual risk system unification: route all state through `backend/quant/risk/engine.py` + DB — ❌ OPEN

> **Audit (2026-10-09):** Two risk systems remain. The worker uses `backend/quant/risk/engine.py` + `DailyRiskState`. The app's quick-trade halt gate still asks the legacy `strategy/risk.py` `RiskManager` (`api/routers/quick_trade.py:27`, `:49`), whose Redis keys and peak file (`strategy/risk.py:47`) **nothing in the running services writes** — no live caller of `record_daily_loss`. So that gate never trips on a loss.

| Field | Value |
|---|---|
| **Purpose** | `strategy/risk.py` writes `peak_equity` to a local file. `backend/quant/risk/engine.py` writes to Redis + DB. Two systems can disagree on peak equity, causing incorrect MDD calculations. |
| **Risk Level** | HIGH |
| **Implementation Complexity** | Medium — migrate file-based peak equity to DB; remove `strategy/risk.py` file reader after one-sprint shadow period |
| **Dependencies** | P0-05 (thread-safe loss tracker must be correct before unification) |
| **Affected Files** | `strategy/risk.py`, `backend/quant/risk/engine.py` |
| **Audit Reference** | AUDIT.md C-07 |

---

#### P5-04 — API/Worker process separation via Redis PubSub — ✅ DONE

> **Audit (2026-10-09):** kis-api publishes `strategy:start`/`strategy:stop` (`backend/api/server.py:306`, `:327`); the worker subscribes (`_run_with_pubsub`, `backend/worker/runner.py:1037`). Separate `kis-api`/`kis-worker`/`kis-ws` services.

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

#### P6-01 — Unit tests: `OrderStateMachine` all valid/invalid transitions + duplicate fill rejection — ⚠️ PARTIAL

> **Audit (2026-10-09):** Transition tests exist (`tests/execution/test_order_machine_new_statuses.py`, ten, invalid ones included) and the machine is exercised across the worker suites; over-fill is refused in `process_fill` (`backend/execution/order_machine.py:78`).
>
> No exhaustive all-pairs transition test against `VALID_TRANSITIONS`.

| Field | Value |
|---|---|
| **Purpose** | Core execution state machine has no tests. Any regression in transition logic is invisible until a live order misbehaves. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Medium |
| **Dependencies** | P2-01 (append-only events) |
| **Affected Files** | New: `tests/execution/test_order_machine.py` |

---

#### P6-02 — Integration test: paper trade dry-run must pass before any deploy — ⚠️ PARTIAL

> **Audit (2026-10-09):** The paper-trading harness and its E2E suites (`backend/testing/`, `tests/postgres/test_paper_e2e_db.py`, PRs #99/#102) run in CI, and `deploy.yml` is gated on that workflow (PR #88). **But `deploy.yml` is disabled** (`disabled_manually`) and deploys are manual over SSH, so today nothing stops a deploy on a red suite. `scripts/test_paper_trade.py` belongs to the legacy `kis-bot` and is not a gate.

| Field | Value |
|---|---|
| **Purpose** | `scripts/test_paper_trade.py` should be a mandatory CI gate. Currently it is advisory only. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Low — add to GitHub Actions workflow as required check |
| **Dependencies** | P0-01 through P0-03 (safe order path required) |
| **Affected Files** | `scripts/test_paper_trade.py`, `.github/workflows/` |

---

#### P6-03 — Docker Compose health checks for all services — ⚠️ PARTIAL

> **Audit (2026-10-09):** `healthcheck:` on postgres, redis, api and kis-api (`docker-compose.yml:14`, `:32`, `:87`, `:154`); none on frontend, kis-worker or kis-ws.

| Field | Value |
|---|---|
| **Purpose** | No `healthcheck:` directives in docker-compose. Services report "Up" even when internally broken. Container orchestration cannot restart unhealthy services automatically. |
| **Risk Level** | LOW |
| **Implementation Complexity** | Low |
| **Dependencies** | P1-06 (worker heartbeat endpoint) |
| **Affected Files** | `docker-compose.yml` |

---

#### P6-04 — Gate `quantdinger` dependency behind env flag — ✅ DONE

> **Audit (2026-10-09):** Nothing builds from `./quantdinger` any more — `docker-compose.yml` builds `./frontend` and `.` only. The upstream name survives only in container names and `QUANTDINGER_*` variables (CLAUDE.md known issue 14). `scripts/setup_oracle_cloud.sh:50` still clones the upstream repo and its comment still calls it required — not needed any more, and the clone lands inside the `.` build context (no `.dockerignore`), so every build ships it to the Docker daemon. Left for a deploy-script change.

| Field | Value |
|---|---|
| **Purpose** | `docker-compose.yml` requires `./quantdinger/` to exist (external git clone). Deployments fail if this directory is absent. |
| **Risk Level** | MEDIUM |
| **Implementation Complexity** | Low — add `profiles: ["quantdinger"]` to the quantdinger service; or use `ENABLE_QUANTDINGER=true` env gate |
| **Dependencies** | None |
| **Affected Files** | `docker-compose.yml` |
| **Audit Reference** | AUDIT.md DB-03 |

---

#### P6-05 — Update `CLAUDE.md`: mark completed stages, advance next-work pointers — ✅ DONE

> **Audit (2026-10-09):** This audit (PR #220): every item carries a status and evidence; see "Status at a glance" at the top.

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
| P0-11 Credential key check | API refuses to start with a missing or malformed `KIS_CREDENTIAL_KEY`; stored credentials the key cannot open are logged |
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
| P1-12 QTY tolerance | Tolerance is 0 for whole-share brokers (PR #226 — not `max(1, round(qty * 0.005))`, which would hide larger gaps on larger positions) |

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
