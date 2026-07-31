# P0-07C: closePosition Minimal Runtime Implementation — Completion Report

**Date:** 2026-07-27
**Commit:** `feaa8c4`
**Baseline:** `main` @ `738976d`
**Governing documents:** `docs/P0_06_SCOPE_AUDIT.md`, `docs/P0_07_CLOSE_POSITION_AUDIT.md`, `docs/P0_07_CLOSE_POSITION_PLAN.md`
**Method:** TDD — 23 tests written and confirmed failing before any implementation; a 24th was added when self-review found a KR price-truncation edge case.

---

## 1. Modified files

| File | Change | Lines |
|---|---|---|
| `api/routers/quick_trade.py` | `close_position` handler rewritten; three module-level helpers added (`_load_market_data`, `_live_held_qty`, `_live_close_price`) | +151 / −24 |
| `api/schemas.py` | `ClosePositionRequest`: `price` removed, `qty` → `Optional[float] = None` | +7 / −1 |
| `api/tests/test_quick_trade_close_position.py` | **New** — 24 close-position cases | +new |
| `api/tests/test_compat_orders.py` | Obsolete 422 regression pin replaced with a gap-closed assertion + a compat-still-excluded assertion | +45 / −24 |
| `docs/P0_07_IMPLEMENTATION.md` | This document | +new |

**Untouched, as mandated:** `reserve_and_submit()` (`api/services/quick_trade_service.py`), `RiskManager` (`strategy/risk.py`), execution layer (`backend/execution/*`), reconciliation (`api/services/quick_trade_recovery.py`), `PositionTracker`, `OrderStateMachine`, DB schema/migrations, compat middleware (`api/compat.py`), pagination, `EmergencyFlattenManager`, all frontend code (`mobile/`, `frontend/`).

---

## 2. Runtime flow

```text
POST /api/quick-trade/close-position
  body: {credential_id, symbol, market, exchange, qty?}   qty omitted = close all
        ↓
[1] auth + tenant scoping      _get_cred(credential_id, user.id)          unchanged
[2] request-scoped clients     _load_kis(cred) → (client, orders, portfolio)  unchanged
        ↓
[3] LIVE POSITION              _live_held_qty(portfolio, symbol, market)
                               KR hldg_qty | US ovrs_cblc_qty, symbol matched on
                               pdno/ovrs_pdno
                               lookup error → reject · held_qty <= 0 → reject
        ↓
[4] QUANTITY                   qty omitted     → close_qty = held_qty
                               qty <= 0        → reject
                               qty > held_qty  → REJECT (never clamped)
                               else            → close_qty = qty
        ↓
[5] LIVE PRICE                 _live_close_price(market_data, ...)
                               get_price_kr | get_price_us
                               exception / non-numeric / <= 0 → reject
                               NO average-cost fallback, NEVER price 0
                               KR: truncated to int (KIS domestic tick)
        ↓
[6] reserve_and_submit(db, user_id, credential_id,
        request={side:"sell", qty:close_qty, price, order_type:"limit", ...},
        idempotency_key=header or derive_idempotency_key(...),
        request_hash=request_fingerprint(...),
        risk_gate=Depends(get_risk_gate),          ← P0-05 gate, fail-closed
        broker_submit=<sell_kr|sell_us closure>,   ← the only broker call site
        extract_order_id=mapper.extract_broker_order_id)
        │
        ├ RESERVED row COMMITted before any broker contact
        ├ duplicate key → existing row returned, broker not called
        ├ risk denied/errored → QT_BLOCKED, broker not called
        ├ broker rt_cd≠0 → QT_REJECTED · network error → stays QT_RESERVED
        └ success → QT_SUBMITTED + broker_order_id
        ↓
[7] response                   real order.status (submitted / blocked / rejected /
                               reserved / failed) — never a hardcoded "submitted"
        ↓
[8] reconciliation             automatic: the startup sweep claims RESERVED rows
                               with no side filter — zero changes required
```

Steps 1, 2, 6, 8 reuse existing code verbatim. Only steps 3, 4, 5, 7 are new, and all of it lives inside the one handler plus three small helpers.

---

## 3. Added tests

**`api/tests/test_quick_trade_close_position.py` — 24 cases** (all seven mandated cases covered):

| Mandated case | Tests |
|---|---|
| Close full position successfully | `test_close_full_position_uses_live_holdings_and_live_price` (asserts qty from `ovrs_cblc_qty`, price from live quote **not** `pchs_avg_pric`, DB row `sell/10/175.5/submitted`), `test_close_kr_position_resolves_hldg_qty`, `test_partial_close_accepts_qty_below_holdings` |
| Requested qty > holdings | `test_over_close_is_rejected_and_never_clamped` (error mentions "exceed", zero broker calls, zero DB rows), `test_non_positive_requested_qty_is_rejected` |
| Missing live position | `test_missing_live_position_is_rejected`, `test_zero_quantity_holding_is_rejected`, `test_position_lookup_failure_is_rejected` |
| Missing live price | `test_price_lookup_failure_is_rejected_without_cost_basis_fallback`, `test_non_positive_price_is_rejected[0 / 0.0 / -3.5 / None]` |
| Duplicate request idempotency | `test_duplicate_close_request_submits_to_broker_once` (two calls → 1 broker call, 1 DB row, same order id), `test_explicit_idempotency_key_is_honoured` |
| `reserve_and_submit` called exactly once | `test_reserve_and_submit_called_exactly_once_with_sell_and_risk_gate` (spy asserts one call with `side="sell"`, resolved qty/price, `order_type="limit"`, and the **injected** risk gate object) |
| Broker never invoked on validation failure | `test_no_reservation_and_no_broker_on_validation_failure[no position / no price / price error]`, `test_over_close_never_reaches_reserve_and_submit` |

Additional safety coverage: `test_kr_sub_unit_price_does_not_truncate_to_zero` (KR int truncation can never yield a price-0 order), `test_risk_denied_blocks_broker_and_reports_status` (`QT_BLOCKED` audited, broker untouched), `test_broker_rejection_is_reported_not_masked` (`QT_REJECTED`, error response), `test_credential_scope_is_enforced`. `FakeOrders.buy_kr/buy_us` raise on contact, pinning that a close can never buy.

**`api/tests/test_compat_orders.py`** — the pin that documented the old 422 now asserts the gap is closed: the exact shipped frontend payload (no `qty`, no `price`, with `market_type`/`position_side`/`source`) returns 200 with server-resolved qty 5 and the live price, plus a second test pinning that `close-position` is still excluded from `_ORDERS_PATH_CONFIG`.

**Results:** `python -m pytest api/tests -q` → **218 passed, 3 skipped, 0 failed** (204 at the time of the P0-07C commit; the count rose with the P0-08 validation tests added later on this branch) (3 Postgres-only concurrency tests skipped on SQLite, as before). No pre-existing test was weakened or deleted.

---

## 4. Behavior changes

| Aspect | Before | After |
|---|---|---|
| Quantity | `int(body.qty)` from the client, unvalidated — oversell reached the broker | Resolved from the live holding; client value is an upper bound only; over-close rejected |
| Price | `body.price` from the client, no guard (price 0 was submittable as a limit) | Live quote only; `<= 0`, non-numeric, and lookup failure all rejected; cost-basis fallback explicitly forbidden |
| Reservation | None | Durable RESERVED row committed before the broker call |
| Idempotency | None — every click was a new order | `(user_id, idempotency_key)` unique; header honoured, else a 10s double-click bucket |
| Risk gate | None | P0-05 `RiskManager` gate, fail-closed to `QT_BLOCKED` |
| Persistence / audit | None | `quick_trade_orders` row per close, incl. blocked/rejected/failed outcomes |
| Reconciliation | Invisible (no row to reconcile) | Covered automatically by the startup RESERVED sweep |
| Response status | Hardcoded `"submitted"` regardless of broker outcome | Real order status; non-submitted returns an error envelope |
| Request contract | `qty` **and** `price` required → shipped UI 422'd on every click | Neither required → the existing frontend payload works with **no frontend change** |
| Order type | Limit (`ORD_DVSN "00"`) | Unchanged — limit; KIS exposes no market/IOC sell |

**Wire-compat note:** the schema became strictly more permissive (a required field removed, another made optional), so any existing caller that still sends `qty`/`price` continues to work — `price` is now ignored in favour of the live quote, and `qty` is validated against the holding.

---

## 5. Remaining risks

| # | Risk | Status |
|---|---|---|
| R1 | A limit close is not guaranteed to fill; KIS exposes no market/IOC sell (audit S3). `submitted` ≠ filled | Accepted and documented; unchanged by this task |
| R2 | No "sellable quantity" concept exists anywhere in the repo (audit S2) — shares locked by unsettled trades or resting orders are inside `hldg_qty`, so the broker may still reject | Converges safely on `QT_REJECTED` with the broker's reason persisted |
| R3 | Two extra KIS calls per close (balance + quote) add latency and rate-limit load | Same pattern as the existing `/position` and `/balance` routes; negligible against 15 req/s |
| R4 | Price moves between quote and submit (TOCTOU) | Limit pricing means no adverse fill — it converts to non-fill risk (R1) |
| R5 | A risk halt blocks closes too (audit S1) | Intended fail-closed behavior; `RiskManager` untouched. Emergency liquidation remains `POST /api/admin/flatten` |
| R6 | Partial fills are not tracked in the QT domain | Out of scope by design; the residual is closed naturally by the next request, since qty is re-derived from live holdings each time |

---

## 6. Rollback procedure

1. **Code:** `git revert feaa8c4` — the change is confined to one handler, one schema class, and tests, so the revert restores the previous direct-broker behavior with no coupled side effects.
2. **Database:** nothing to roll back. No schema change, no migration; `quick_trade_orders` rows already written by closes remain valid audit records after a revert.
3. **Runtime mitigation without deploying:** set the `RiskManager` halt flag (Redis) — closes fail closed to `QT_BLOCKED` immediately. Note this blocks exits as well (R5); for emergency liquidation use `POST /api/admin/flatten`, which deliberately bypasses SAFE_MODE.
4. **Partial-deployment safety:** the schema only relaxed constraints, so an old frontend against the new backend works (that is the fix), and a new-style request against an old backend cannot occur since no frontend changed.
5. **Pre-production gate:** exercise a paper-account (`KIS_ENV=paper`) close plus the three rejection paths before enabling on a live account.

---

## 7. Validation performed

- `python -m pytest api/tests -q` → **218 passed, 3 skipped**, including all 24 new close-position cases and the compat-orders tests.
- Constraint audit: `git diff --stat` confirms the change set touches only `api/routers/quick_trade.py`, `api/schemas.py`, and two test files. No forbidden module appears in the diff.
- TDD discipline: the 23 tests were committed in a failing state first (22 failures against the old handler) and only then made green by the implementation.
