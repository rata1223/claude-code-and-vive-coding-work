# Systems Audit & Risk Analysis — KIS Trading Platform

> Independent, conservative systems audit. Pre-implementation — **nothing is fixed here, only
> documented.** No runtime or safety-critical code is changed by this document.
> Audit date: 2026-06-21. Branch: `claude/systems-audit-risk-analysis-jtcux1`.

This audit complements the existing `AUDIT.md` defect register (D-1…D-10+, R-03, May 2026)
rather than duplicating it. It gives a current-state read focused on what most threatens
correctness and capital safety, and cross-references existing IDs where they overlap. All
findings below were verified against source at the cited locations.

---

## 1. Current State Summary

- **Three partially-overlapping systems on one KIS account** (`AUDIT.md:13-33`):
  - **A** — legacy `bot/main.py` (commented out in `docker-compose.yml`; not active).
  - **B** — `backend/worker/runner.py` (**ACTIVE** execution path).
  - **C** — `api/main.py` FastAPI on port 8000 (**ACTIVE**, separate ORM + connection pool).

  B and C share one Postgres database but use entirely different ORM models and know nothing of
  each other's schemas.
- **Live order path:** `mobile → api(8000) → kis-api(5001) → Redis PubSub → kis-worker →
  KISBroker → KIS API`. The mobile app has no direct path to order placement — this is correct.
- **Maturity is high but asymmetric.** The execution layer is strong:
  - Order state machine with enforced transitions + over-fill guard
    (`backend/execution/order_machine.py`).
  - SHA256 idempotency with Redis primary + DB fallback (`backend/execution/idempotency.py`).
  - Three-stage broker↔DB reconciliation (`backend/execution/reconciler.py`,
    `backend/execution/reconciliation.py`).
  - Fail-closed 8-step startup recovery (`backend/worker/recovery.py`).
  - Broad automated coverage (~39 test files) over these paths.
- **Live-trade gating today** is the *older split mechanism*, verified in
  `backend/strategy/base.py:13-40`:
  1. `SAFE_MODE.can_trade` gate (startup recovery must complete first), then
  2. `ENABLE_LIVE_TRADING` env gate (orders rejected unless `="true"`).

  Endpoint routing is by `KIS_ENV` (`kis_adapter/auth.py`, paper vs real base URL).
  `backend/worker/runner.py` enforces a startup consistency check: `KIS_ENV=real` +
  `ENABLE_LIVE_TRADING=false` → `sys.exit(1)`; `paper` + `true` → warning only. A 4-week
  paper-run promotion guard exists (`backend/worker/promotion_guard.py`).
- **Brokers:** KIS is live-ready (KR + US, paper/real TR_IDs selected at construction). Kiwoom
  is an intentional stub (`raise NotImplementedError`, Windows COM incompatible with Docker) —
  a documented limitation, not a bug.
- **Secrets:** `.env` is correctly gitignored (`.gitignore:151`); no committed credentials found.

---

## 2. Key Risks (ranked)

### R-A — HIGH (systemic): Unified `KillSwitch` is built and tested but not wired in
`backend/risk/kill_switch.py:21-26` states in its own docstring that integration is "a
deliberate follow-up task." A repository-wide search confirms `KillSwitch(`, `check_order`, and
`report_*` appear **only** in the module and its tests — never in `strategy/base.py`,
`backend/quant/risk/engine.py`, or `backend/worker/runner.py`.

The live halt today therefore relies on the legacy split mechanism with two independent writers
of `DailyRiskState.kill_switch`:
- `PersistentLossTracker` in `engine.py` (process-local; also toggles SAFE_MODE in the worker), and
- `WorkerWatchdog._alert_dead_worker` in `backend/worker/heartbeat.py` (runs in the API process).

These can race to set `kill_reason`, and — critically — **the worker never re-reads
`kill_switch` after boot**, so a halt set by the watchdog does not stop a live worker until the
next restart.

### R-B — HIGH: Runtime reconciliation CRITICAL gaps only alert; they do not halt
Position mismatches block trading at *startup* (`recovery.py` blocks `SAFE_MODE.enable()` on a
critical gap), but at runtime a CRITICAL-severity reconciliation gap only logs and alerts while
the strategy keeps trading (`backend/execution/reconciler.py`, per
`docs/EMERGENCY_KILL_SWITCH.md`).

### R-C — HIGH: Order-submission duplicate / ambiguity risk
Cross-references `AUDIT.md`:
- **D-2** — `KISBroker.place_order()` catches all exceptions and returns
  `Order(status=REJECTED)`; a network timeout (order may have landed at the broker) is
  indistinguishable from a true rejection.
- **D-3** — `KISClient.post()` retries 3× on any exception, including order POSTs → potential
  duplicate live orders.

Idempotency (`backend/execution/idempotency.py`) mitigates this **only when Redis is up**;
under Redis unavailability it degrades to single-worker / fail-open.

### R-D — MEDIUM: Deploy pipeline has no gate
`.github/workflows/deploy.yml` auto-deploys on **every push to `main`** via SSH
(`git pull origin main && docker compose up -d --build`). Success of the test workflow is **not**
a prerequisite. The "health check" is `docker compose ps` after `sleep 10` — it does not validate
that the worker actually booted healthy. A bad commit reaching `main` is deployed to the trading
host automatically.

### R-E — MEDIUM: Operator misconfiguration is the highest-stakes irreversible action
Flipping `ENABLE_LIVE_TRADING=true` (together with `KIS_ENV=real`) in the server `.env` enables
real-money orders with no second confirmation once the promotion guard passes. There is no
2-person / approval control around this flip; it is guarded only by operator discipline and the
startup consistency check.

### R-F — MEDIUM: Kill-switch does not auto-liquidate (intentional)
On a loss/MDD breach, new orders are blocked but existing positions remain open until an operator
acts (`PHILOSOPHY.md`, `AUDIT.md` R-03). This is a deliberate design choice to avoid forced
liquidation triggered by transient API errors, but it means a breach does not flatten exposure.

### Hidden assumptions worth flagging
- Correctness of idempotency and reconciliation **assumes Redis + DB availability**.
- `SAFE_MODE` is **process-local** — not shared across `kis-api` and `kis-worker`.
- The 4-week promotion guard is checked at **worker startup**, not at deploy time, so a
  deploy can succeed while the worker silently stays in shadow mode.

---

## 3. Safest Recommendation

- **Do not transition to live.** Remain in paper/shadow (`ENABLE_LIVE_TRADING=false`,
  `KIS_ENV=paper`). The existing gates already make this the default — keep it.
- **Make no changes to safety-critical code as part of this audit.** Ship analysis as
  documentation only.
- Treat **R-A** (kill-switch wiring) and **R-B** (runtime reconciliation halt) as the two
  blockers that must be resolved **and validated under paper trading** before any live
  consideration — as **separate, explicitly-approved changes**, not bundled with this audit.
- The lowest-risk infrastructure hardening is the **R-D deploy gate** (require green tests
  before `deploy.yml` runs) — recommended, but also out of scope here.

---

## 4. Rejected Alternatives (and why)

- **Wire the `KillSwitch` in now, as part of the audit** — rejected. It changes safety-critical
  control flow in `strategy/base.py` / `engine.py` without staged paper validation; violates
  "do not write code until approved" and "prefer correctness over speed."
- **Bulk-fix the `AUDIT.md` defect list (D-1…D-10) in one pass** — rejected. Large surface in the
  order/fill path; high regression risk; not requested.
- **Flip to live once the 4-week promotion guard passes** — rejected. Passing the guard is
  necessary but not sufficient; R-A / R-B / R-C remain open.
- **Edit `deploy.yml` to add a test gate within this task** — rejected. Out of "analysis only"
  scope; recorded as a recommendation instead.

---

## 5. Next Action Checklist (validation before any future execution)

Recommendations recorded for future, separately-approved work — **not actions taken now**:

1. Wire `KillSwitch` into the `strategy/base.py` order path and `engine.py` reporting; verify the
   worker re-reads halt state and that the two legacy writers no longer race. Validate under paper.
2. Promote runtime reconciliation CRITICAL gaps from alert-only to a halt (and, if adopted,
   flatten) path.
3. Resolve D-2 / D-3: make order POSTs non-retrying or idempotency-keyed end-to-end; confirm no
   duplicate live orders under induced timeouts.
4. Add a CI gate so `deploy.yml` requires green tests, plus a real post-deploy worker health probe.
5. Add an approval / 2-person control around the `ENABLE_LIVE_TRADING` flip.
6. Confirm `SAFE_MODE` and rate-limit coordination across `kis-api` and `kis-worker` are shared,
   not process-local.
7. Only after 1–6 are validated under a full 4-week paper run: consider going live.
