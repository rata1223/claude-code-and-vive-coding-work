# Corporate Action Runtime Integration — Design (TASK P2-02B, 2026-06-23)

**Design only. No code is implemented; no module is wired.** This document specifies *how* the
built-and-tested `backend/data/corporate_actions.py` (`CorporateActionService`, P2-01B as-built)
should integrate into the **live runtime**, per the approved ownership audit
`docs/CORPORATE_ACTION_RUNTIME_OWNERSHIP_AUDIT.md` (P2-02A, PR #92).

It is grounded in the wired code as it exists today:
`backend/execution/position_tracker.py`, `backend/execution/reconciler.py`
(`_reconcile_positions:167-277`), `backend/worker/recovery.py`, `backend/worker/runner.py`
(fill callback `:439-533`, reconciler `:201`, restore `:407/633`),
`backend/data/corporate_actions.py`, and `backend/database/models.py`
(`Position`, `AuditLog`; **no corporate-action tables exist**).

---

## 0. Core principle (inherited from P2-02A — non-negotiable)

> **The broker is the sole authority for position value (qty / avg_price). The provider is the sole
> authority for price/OHLCV. `CorporateActionService` is a detector + recorder + gate — it NEVER
> becomes a second adjuster.**

Every rule below follows from this. It makes the two headline requirements — *exactly one
adjustment authority* and *no double-adjustment* — true **by construction** rather than by careful
coordination.

---

## 1. Adjustment ownership rules

| Value | Sole authority | Sole writer (runtime) | Service's role |
|---|---|---|---|
| Position `qty` / `avg_price` | **broker (Copy0)** | the **reconciler** (`_reconcile_positions`) writing broker values into DB `positions` | detect / classify / record / gate — **never writes qty/avg** |
| Price / OHLCV (US) | **provider** (`yfinance auto_adjust=True`) | the loader (`DataLoader`) | **never re-scales live bars** |
| Price / OHLCV (KR) | provider (pykrx) — **must be made explicit at the loader** (prerequisite) | the loader | never re-scales live bars |
| CA classification / lifecycle / gate / audit | **`CorporateActionService`** | the service + new DB tables | this is what the service owns |

**R-1.** `PositionTracker` is a non-authority fill cache. It **never multiplies qty by a ratio**; it
only re-reads broker-adjusted DB values via the existing `restore_positions()` path.
**R-2.** The service's `PriceAdjuster` / `PositionAdjuster` are **out of the live path**. They run
only for explicit backtest / what-if inputs where *raw* bars are supplied (the live path already
receives provider-adjusted bars and broker-adjusted positions).
**R-3.** Exactly one writer per value: the reconciler for positions, the loader for prices. No code
path may add a second.

---

## 2. Integration points

### 2.1 Reconciliation flow — the detect-and-record seam (primary)

**Where:** `PositionReconciler._reconcile_positions()` qty/avg branch
(`reconciler.py:206-245`), the one place a broker-side quantity jump is already observed.

**Today:** `qty_diff > _QTY_TOLERANCE` → generic `qty_mismatch` gap → overwrite DB qty+avg to
broker, labeled `reconcile_fix_qty`. A split is absorbed silently and indistinguishably from a bug.

**Designed behavior:** before the absorb, the reconciler asks the service to **classify** the jump:

```
classification = service.classify_broker_jump(
    symbol, db_qty, db_avg, broker_qty, broker_avg, broker_name)
```

- **CONFIRMED corporate action** — the ratio `broker_qty/db_qty` matches a known split/reverse
  signature **AND** value is preserved (`db_qty*db_avg ≈ broker_qty*broker_avg`, the existing
  `value_preserved` tolerance). The reconciler still performs the **single** DB write to broker
  values (it remains the sole writer), but the event is recorded as a corporate action: a
  `corporate_actions` row, a `corporate_action_history` row, and an `AuditLog` `EVENT_APPLIED`
  (labeled `reconcile_corporate_action`, not `reconcile_fix_qty`). The symbol's CA gate is cleared.
- **UNKNOWN / anomaly** — ratio matches no known signature **OR** value is **not** preserved. This
  is **fail-closed**: the reconciler does **not** silently absorb it. It raises a CRITICAL gap, sets
  the symbol's CA gate to **block**, writes `EVENT_BLOCKED`, and alerts the operator. (This is the
  one behavioral upgrade over today's silent absorb — and the safety-critical decision in §6.)

**Interaction with the existing `_has_pending_order` defer (`reconciler.py:211-214`):** unchanged —
if a pending order exists for the symbol, classification is **deferred** (a fill in flight must not
be misread as a corporate action). **`dry_run`:** classify + log only, no write (matches the
existing `dry_run` contract).

This seam satisfies *no double-adjust*: the reconciler is still the only writer of position value;
the service only labels, records, and gates.

### 2.2 PositionTracker — the gate consult (not an adjuster)

**Where:** the pre-order-submission check, next to `SAFE_MODE.can_trade`
(`strategy/base.py`), and any other order entry point (worker order path; admin/manual order API).

**Designed behavior:** before placing an order, call `service.assert_tradeable(symbol)`. If the
symbol has a pending / CONFIRMED-unapplied / UNKNOWN corporate action, it raises
`CorporateActionPendingError` → the order is blocked (fail-closed). This complements, not replaces,
the tracker's existing `can_place_order()` duplicate-order lock.

`PositionTracker` itself is **not modified to adjust**. On a CONFIRMED CA at reconcile, the tracker
is refreshed from the broker-adjusted DB through the existing restore path — never independently
ratio-scaled (R-1).

### 2.3 Portfolio recovery path — restart durability

**Where:** `StartupRecovery.run()` (`recovery.py:83-108`), a new sub-step **after** step-6
`_step_reconcile()`.

**Designed behavior:** `service.restore_pending()` and `service.restore_history()` load the
`corporate_actions` (non-terminal status) and `corporate_action_history` rows from the DB into the
service's in-memory state. Because step-6 reconcile already converged positions to broker truth,
position *values* are correct on restart; these tables preserve the **gate state and audit
continuity** — so a pending/UNKNOWN CA still blocks trading after a restart (closes restart
fail-open, P2-02A finding 6 / F-7).

---

## 3. Pending-action lifecycle (state machine)

```
            detect (heuristic / reconcile signature / external feed)
                                   │
                                   ▼
   ┌────────── DETECTED (pending, BLOCKS) ──────────┐
   │                                                │ value-not-preserved
   │ confirm (value-preserving known ratio,         │ or unknown ratio
   │ or external announcement)                      ▼
   ▼                                          UNKNOWN (BLOCKS, operator-only)
CONFIRMED (BLOCKS) ── apply (broker already adjusted) ──► APPLIED
                                                          (recorded, pending
                                                           cleared, UNBLOCKS)
   │
   └── operator dismiss ──► DISMISSED (audited)
```

- **BLOCKS** = `assert_tradeable()` raises for that symbol.
- **APPLIED** does not move qty/avg (the broker/reconciler already did); it records the
  `corporate_action_history` row and clears the pending block.
- **UNKNOWN** is terminal-until-operator: requires explicit human resolution (it represents an
  anomaly the system refuses to absorb).
- Every transition writes an **append-only** `AuditLog` row via the existing `AdjustmentAuditLog`
  events `EVENT_DETECTED / EVENT_REGISTERED / EVENT_APPLIED / EVENT_BLOCKED`.

---

## 4. Duplicate-action prevention

- **Idempotency key** = `(broker, symbol, effective_date, action_type)`, enforced by a
  `UniqueConstraint` on the `corporate_actions` table.
- The reconciler runs every ~30 min (`runner.py:341`) and at startup; the unique key guarantees the
  same split is **recorded/applied at most once**. After APPLIED, the next reconcile sees
  `db_qty == broker_qty` (no diff) → no re-detection — a second, independent guard.
- `CorporateActionService.apply()` already removes the action from `_pending` (P2-01B); combined
  with the DB unique key, a detect-before-write race cannot double-record.

---

## 5. Provider-adjusted vs broker-adjusted handling

| Plane | Already adjusted by | Service action in live path |
|---|---|---|
| Positions | broker (Copy0), surfaced via reconciler | **record only** — never adjust (would double-count, F-5) |
| Prices US | provider (`auto_adjust=True`) | **never re-scale** live bars (would double-adjust, F-6) |
| Prices KR | pykrx — **unverified**; enforce adjustment **at the loader** (prerequisite) | never re-scale in the position layer |

The service's bar/position adjusters are explicitly **scoped to non-live (backtest/what-if) inputs**
where raw bars are provided. In the live integration they are dormant — only detection, recording,
gating, and persistence are active.

---

## 6. Reconciliation interaction rules

1. The reconciler is the **single writer** of DB position value from broker.
2. **Classify before absorb** (§2.1).
3. **Value-preserving, known ratio** → converge to broker value + record as corporate action +
   clear the gate.
4. **Non-value-preserving / unknown ratio** → **HOLD + block + alert**; do **not** auto-write.
   *(This is the safety-critical decision: today the reconciler always converges to broker truth;
   this design holds on an unexplained jump instead. It must be paper-validated — see §8.)*
5. `_has_pending_order` **defers** classification (avoid misreading an in-flight fill as a CA).
6. `dry_run` → classify + log, **no write**.

---

## 7. New persistence (schema design only — no migration written here)

**`corporate_actions`**

| column | type | notes |
|---|---|---|
| id | int PK | |
| broker | str | "kis" / "kiwoom" |
| symbol | str | |
| action_type | str | split / reverse_split / cash_dividend / ticker_change / unknown |
| effective_date | date | |
| status | str | pending / confirmed / applied / unknown / dismissed |
| ratio | float? | split/reverse |
| cash_amount | float? | dividend |
| new_symbol | str? | ticker change |
| source | str | price_jump_heuristic / reconcile_signature / external / manual |
| detail | text | JSON |
| detected_at / applied_at | datetime | |

`UniqueConstraint(broker, symbol, effective_date, action_type)` — duplicate prevention (§4).

**`corporate_action_history`** (append-only)

| column | type | notes |
|---|---|---|
| id | int PK | |
| corporate_action_id | int FK | |
| symbol | str | original (pre-ticker-change) symbol |
| qty_before / avg_before / qty_after / avg_after | float | |
| cash_delta | float | dividends |
| value_preserved | bool | |
| applied_at | datetime | |
| actor | str | "reconciler:kis" etc. |

- **Append-only auditability** is preserved: history rows are never updated/deleted, and the
  lifecycle events continue to write to the existing append-only `AuditLog`.
- **Alembic migration required** (the repo runs an alembic round-trip CI check) — to be authored in
  the implementation slice, not here.
- **`positions.qty` is `Integer`** (`models.py:85`): a reverse split can imply fractional residue.
  Since the **broker is authority**, the DB simply takes the broker's integer qty; the *ratio* and
  any residue are recorded in `corporate_action_history` for audit — the service never invents a
  fractional position.

---

## 8. Requirements coverage matrix

| Required guarantee | Satisfied by |
|---|---|
| Exactly one adjustment authority | §0 / §1 (broker for positions, provider for prices; reconciler sole position writer) |
| No double-adjustment | §1 R-2/R-3, §5 (service never adjusts live values) |
| Pending-action persistence | §7 `corporate_actions` + §2.3 restore |
| History persistence | §7 `corporate_action_history` |
| Restart recovery | §2.3 (restore pending/history after step-6 reconcile) |
| Fail closed on UNKNOWN | §2.1 (anomaly → block), §3 (UNKNOWN blocks, operator-only), P2-01B `apply()` guard |
| Append-only auditability | §3 + §7 (AuditLog events + immutable history rows) |

---

## 9. Affected modules (for the future implementation slice)

`backend/execution/reconciler.py` (classify-before-absorb), `backend/data/corporate_actions.py`
(add `classify_broker_jump` / `restore_pending` / `restore_history`),
`backend/worker/recovery.py` + `runner.py` (gate consult + restore sub-step),
`backend/strategy/base.py` (gate at order entry), `backend/database/models.py` + a new alembic
migration (two tables), `backend/brokers/capabilities.py` (add an `adjusts_corporate_actions` flag
so ownership is a queryable contract).

---

## 10. Open risks / decisions to validate before implementing

1. **§6.4 hold-vs-converge** on a non-value-preserving broker jump is the safety-critical call —
   holding blocks trading on an unexplained discrepancy (fail-closed) but could over-block on a
   legitimate-but-unmodeled event (merger/spinoff). **Must be paper-validated.**
2. **KR loader adjustment** (pykrx) is a **prerequisite**, not part of this slice — until verified,
   KR price-plane authority is ambiguous.
3. **Gate placement** must cover **every** order entry point — the strategy path *and* any
   admin/manual order API — or the gate is bypassable.
4. Mergers / spin-offs / rights issues remain **unmodeled** → they fail closed as `UNKNOWN` (safe
   but unhandled); modeling them is later work.

> **Do not implement yet.** Each integration point above is a separate, paper-validated,
> safety-critical change to be approved on its own.
