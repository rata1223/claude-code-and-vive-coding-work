# P0-07: closePosition Runtime Semantics & Bypass-Path Audit

**Date:** 2026-07-26
**Type:** Strict read-only audit (documentation only — zero code changes)
**Baseline:** `main` @ `738976d` (merge of PR #141 / P0-06 scope audit)
**Precondition:** `docs/P0_06_SCOPE_AUDIT.md` (P0-06, merged) — which recommended this audit's subject as the next task.
**Scope:** Quick Trade, Execution Layer, PositionTracker, RiskManager, OrderStateMachine, BrokerAdapter, Reconciliation, Portfolio.

Every statement below is traced to runtime code with `file:line` evidence. Items not determinable from this repository are marked **UNVERIFIED**.

---

## 1. Current Runtime

### 1.1 What `closePosition` actually is (exact semantics)

`POST /api/quick-trade/close-position` (`api/routers/quick_trade.py:233-266`) is a **synthetic reverse order: an unconditional LIMIT sell at the caller-supplied price**, submitted directly to KIS.

It is **not**: a market liquidation, an IOC/FOK order, a broker-native close, or a position-aware operation of any kind.

Evidence:

- **Always limit.** The handler calls `orders.sell_kr(body.symbol, qty, int(body.price))` (`:248`) or `orders.sell_us(body.symbol, exchange, qty, body.price)` (`:251`). Both adapter methods hardcode `ORD_DVSN: "00"` (지정가/limit) — `kis_adapter/orders.py:85` (KR, TR `TTTC0801U`/`VTTC0801U`) and `:61` (US, TR `TTTT1006U`/`JTTT1006U`). No order-type parameter, no branch, no market (`"01"`) path exists anywhere in the adapter. There is no TIF/IOC/FOK concept in the codebase (no field on `Order`, no `ORD_DVSN` variant).
- **Caller-supplied qty, never validated against the holding.** `qty = int(body.qty)` (`:245`) — a float truncation of whatever the client sent. The handler performs **no position lookup**; an oversell or a sell of a nonexistent position is passed straight to the broker. `BrokerCapabilityValidator._check_short` is an explicit no-op (`backend/brokers/validator.py:109-115`), so nothing pre-empts an oversell anywhere in the stack.
- **Caller-supplied price, no sanity check.** No client-side guard rejects price ≤ 0; a `price=0` request is sent as a *limit order at price 0* (`"ORD_UNPR": "0"` / `"OVRS_ORD_UNPR": "0.00"`). KIS broker-side behavior for that payload is **UNVERIFIED**.
- **Response is asserted, not observed.** The handler returns `"status": "submitted"` **hardcoded** (`:261`) regardless of the broker response, with `order_id` from `extract_broker_order_id` (`backend/brokers/semantic_mapper.py:177-178`, `:232-233`) — which returns `""` on an unexpected response shape, yielding `order_id: ""` + `status: "submitted"`.
- **Schema:** `ClosePositionRequest` (`api/schemas.py:196-202`) requires `credential_id`, `symbol`, `qty`, `price` (no defaults), with `market="us"`, `exchange="NASD"` defaults. No `side` field (implicitly sell), no `order_type`.

### 1.2 The full runtime trace

```text
Frontend Close button        mobile/src/views/quick-trade/index.vue:421-444
  payload: {credential_id, symbol, market_type, position_side, source}
  → NO qty, NO price
        ↓
CompatMiddleware             api/compat.py:422 — close-position NOT in _ORDERS_PATH_CONFIG
  (deliberate exclusion, :387-391: deriving a price from stale position data
   is "a live-trading pricing decision, not a DTO reshape")
  → request passes through UNTRANSLATED
        ↓
FastAPI validation           api/schemas.py:196-202 — qty/price required, market_type unknown
  → 422 Unprocessable Entity ON EVERY CLICK (endpoint unreachable from shipped UI)
        ↓  (only reachable via direct API call with correct fields)
close_position handler       api/routers/quick_trade.py:233-266
  _get_cred (:239)           tenant scoping — the ONLY safety layer present
  _load_kis (:244)           request-scoped KIS credentials (P0-03 preserved)
  sell_kr/:248 | sell_us/:251  direct broker call — LIMIT @ caller price
  → NO reservation, NO idempotency, NO risk gate, NO DB row, NO reconciliation
```

The frontend *cannot* supply qty/price today without a mapping change: the store holds raw KIS `output1` rows (`api/routers/quick_trade.py:139-145` ← `kis_adapter/portfolio.py:41,61`), whose real fields (`hldg_qty`/`ovrs_cblc_qty`, `prpr`/`now_pric2`, `pchs_avg_pric`) are never read by the template — it reads nonexistent keys `position.size` etc. (`index.vue:115-124`). The data exists in the payload; the UI never maps it. (Classification: **Conflicting / needs clarification** — a P0-07 implementation prerequisite.)

### 1.3 All other position-closing flows in scope (comparison baseline)

Every close in this codebase is a synthetic reverse `sell`; **no `close_position`/`flatten`/`reduce_only` primitive exists on `BrokerAdapter`** (`backend/brokers/base.py` — complete ABC: `get_balance`, `get_positions`, `place_order`, `cancel_order`, `get_order_status`, `get_price`). KIS capabilities declare `supports_market_sell=False` (`backend/brokers/capabilities.py:6,38`); a `"market"` request is silently downgraded to a limit at the fetched quote (`backend/brokers/validator.py:124-146`).

| Flow | Entry | Type | Price source | Qty source | Reservation | Idempotency | Pending lock | Risk gate | StateMachine/Poller/Tracker | Reconciled |
|---|---|---|---|---|---|---|---|---|---|---|
| **QT close-position** | `api/routers/quick_trade.py:233` | limit | caller | caller | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
| **QT place-order (sell)** | `quick_trade.py:153` | limit | caller | caller | ✓ (P0-04) | ✓ | n/a (QT domain) | ✓ (P0-05) | n/a (QT domain) | ✓ (startup sweep) |
| **EmergencyFlatten** | `backend/api/server.py:329-361` → `backend/worker/emergency.py:79` | limit | live quote; **on any error → `pos.avg_price` (cost basis)** (`emergency.py:138-147`, verified) | broker positions (`hldg_qty`/`ovrs_cblc_qty`, `backend/brokers/kis.py:83,98`) | ✗ | ✗ | ✗ | ✗ (bypasses SAFE_MODE by design) | ✗ (order id logged & discarded, `emergency.py:157-164`) | indirect (next reconciler run) |
| **IndicatorStrategy exit/stop-loss** | `backend/strategy/indicator/strategy.py:121-127, 251-254` | limit | live quote (`:203`) | in-memory `PositionTracker` (`:190`) | ✗ | ✗ | **✓ `try_mark_pending` (`:194`)** | ✓ SAFE_MODE + ENABLE_LIVE_TRADING (`base.py:23-38`) + freshness gate + failure breaker | ✓ (`machine.register` post-submit `:238`, poller `:242`) | ✓ |
| **LivePipeline sell** | `backend/quant/live/pipeline.py:182-196` | limit | quote/cached | broker positions | ✗ | ✗ | ✗ (own `SignalDeduplicator`, 120 min) | own in-memory kill switch | ✗ (own `PartialFillTracker`) | ✗ — production wiring **UNVERIFIED** (no non-test call site found) |
| **bot/main.py stop-loss / signal sells / rebalance / MDD flatten** | `bot/main.py:71-112, 129-195, 215-243, 274-277` | limit | quote | KIS balance fields | ✗ | ✗ | ✗ | `RiskManager.is_trading_halted` only (session entry) | ✗ (bypasses the entire execution layer) | ✗ |

Two structurally important risk facts (evidence-verified):

- **The worker's MDD kill switch blocks exits instead of liquidating.** `LossTracker` MDD breach → `SAFE_MODE.disable(...)` (`backend/quant/risk/engine.py:267-293` per trace; wiring `backend/worker/runner.py:512-541`), and `_live_trade_allowed` checks `SAFE_MODE.can_trade` for **both buy and sell** (`backend/strategy/base.py:24-30`, verified). The only automatic MDD liquidation in the repo is the legacy bot's `run_rebalance` (`bot/main.py:274-277`) — which is itself suppressed by a same-day trading halt (`:219-221`).
- **The standalone `backend/risk/kill_switch.py` library (unwired) blocks `CLOSE_POSITION` when halted by default** — `allow_close_position_when_halted: bool = False` (`kill_switch.py:230-231`, verified). If ever wired, a halt would block closes unless this flag is deliberately set.

---

## 2. Ownership Map

Who owns each concern **today**, per flow. "—" = nobody (the concern is simply absent from that flow).

| Concern | QT close-position (current) | QT place-order (hardened) | IndicatorStrategy sell | EmergencyFlatten | bot/main.py |
|---|---|---|---|---|---|
| **Quantity calculation** | caller (unvalidated `int(body.qty)`, `quick_trade.py:245`) | caller (`int(body.qty)`, `:166`) | `PositionTracker.get_position().qty` (`strategy.py:190`) | broker `get_positions()` (`emergency.py:112`) | KIS balance fields (`bot/main.py:82-93`) |
| **Order price** | caller (`body.price`) | caller (`body.price`) | `broker.get_price()` (`strategy.py:203`) | `broker.get_price()` → fallback `pos.avg_price` (`emergency.py:138-147`) | quote helpers |
| **Pending lock** | — | — (QT domain uses idempotency instead) | `PositionTracker.try_mark_pending` (`position_tracker.py:67-77`, TTL 1800s `:12`, release `on_fill :111` / `unmark_pending :101`) | — (process-local `threading.Lock` only, `emergency.py:23`) | — |
| **Risk validation** | — | `get_risk_gate` → `RiskManager.is_trading_halted` (`quick_trade.py:29-43`), fail-closed in service (`quick_trade_service.py:185-202`) | SAFE_MODE + ENABLE_LIVE_TRADING (`base.py:13-40`) + `FreshnessGate` + `ConsecutiveFailureBreaker` | — (deliberate bypass) | `RiskManager.is_trading_halted` at session entry |
| **Idempotency** | — | `derive_idempotency_key`/`request_fingerprint` (`quick_trade_service.py:68-116`) + DB unique `(user_id, idempotency_key)` (`api/models.py:180-182`) | — (`backend/execution/idempotency.py` has **zero runtime call sites** — tests only) | — | — |
| **Reservation (pre-submit durable write)** | — | `reserve_and_submit`: RESERVED row committed before broker call (`quick_trade_service.py:146-162`) | — (first DB write is post-submit via `machine.register`, `strategy.py:238`) | — | — |
| **Audit** | error log only (`:265`) | `QuickTradeOrder` row = full audit trail | `machine.register` → `_persist_order` + poller events | audit rows `emergency_flatten_order` (`emergency.py:195-210` region) | Telegram notifications only |

Ownership conclusion: in the two domains, the concerns have clear single owners — **QT domain: `reserve_and_submit` owns reservation+idempotency+risk-gate sequencing; `quick_trade_recovery` owns reconciliation.** **Execution domain: `PositionTracker` owns pending locks and position mutation; `OrderStateMachine` owns lifecycle; `OrderFillPoller`/`PositionReconciler` own fills and repair.** The current `close_position` handler assigns **every concern to the caller or to nobody**.

---

## 3. Reuse Analysis — `reserve_and_submit()`

**Verdict: mechanically reusable for closePosition as-is. Nothing in the service, model, or recovery sweep is buy-specific.** (Verified line-by-line.)

| Requirement of a close flow | Does `reserve_and_submit` satisfy it? | Evidence |
|---|---|---|
| Accept a sell | ✓ — `side` is read verbatim from `request["side"]` (`quick_trade_service.py:152`) and never branched on anywhere in the file | `place_order`'s own `broker_submit` closure already dispatches sells (`quick_trade.py:188-198`) |
| Durable pre-submit reservation | ✓ — RESERVED insert + commit before `broker_submit()` (`:146-162`) | identical for any side |
| Idempotency (double-click, retry) | ✓ — `request_fingerprint` hashes `side.lower()` as an opaque component (`:85`); DB unique constraint side-agnostic | `IdempotencyConflict` on param mismatch (`:163-178`) |
| Risk gate, fail-closed | ✓ — `risk_gate()` after commit, before submit; `RiskDenied`→`QT_BLOCKED`, any error→`QT_BLOCKED` (`:185-202`) | injected callable; no side awareness |
| Broker error mapping | ✓ — `RuntimeError`→`QT_REJECTED`; network/timeout→stays `QT_RESERVED`, never blind-retried (`:205-220`) | |
| Persistence fields for a close | ✓ — `QuickTradeOrder` already has `side/qty/price/market/exchange/order_type/broker_order_id/error/status` (`api/models.py:186-198`). **No schema or migration change needed** | |
| Reconciliation coverage | ✓ automatic — recovery sweep filters only `status == QT_RESERVED and created_at <= cutoff` (`quick_trade_recovery.py:117-128`), no side predicate; classification is side-aware via `extract_side` (`:95-99`, `semantic_mapper.py:180-181,235-236`) and uses the per-order credential (`:164`) | a close routed through the service is recoverable with **zero changes** to the recovery module |

What `reserve_and_submit` does **not** provide (true gaps for a close, not defects of the service):

1. **Quantity truth.** It records the caller's qty; it does not (and should not) know the actual holding. Deciding qty from the live position — including the *sellable* vs *held* distinction, which **no code in the repo makes** (zero hits for `ord_psbl_qty`/sellable; `hldg_qty`/`ovrs_cblc_qty` only) — is the caller's job.
2. **Price decision.** It records the caller's price. KIS supports no market sell (`capabilities.py`), so a close must choose a limit price (live quote, protective offset, or client-supplied). This is exactly the "live-trading pricing decision" the compat layer refused to make (`api/compat.py:387-391`).
3. **Fill confirmation.** `QT_SUBMITTED` ≠ filled (documented DEFER since P0-04); a close can rest unfilled at its limit price with no follow-up. Same limitation as `place-order` — not new.

---

## 4. Remaining Gaps (Bypass Paths)

Every runtime path that bypasses Reservation / RiskManager / Idempotency / Reconciliation, ordered by exposure:

| # | Path | Bypasses | Exposure | Notes |
|---|---|---|---|---|
| G1 | **`POST /api/quick-trade/close-position`** (`quick_trade.py:233-266`) | Reservation ✗ Risk ✗ Idempotency ✗ Reconciliation ✗ (persistence ✗) | **The only route in the entire `api/` surface that can submit a live broker order while skipping all four** (exhaustive enumeration: exactly two broker-submit sites exist in `api/`, and `place-order` is fully hardened). Mitigated today only by the fact that the shipped UI 422s on every click (§1.2) — i.e., protected by a bug. Retains tenant credential scoping (P0-03) only. |
| G2 | **EmergencyFlatten** (`backend/worker/emergency.py`) | Reservation ✗ Idempotency ✗ Risk ✗ (by design) StateMachine/Poller ✗ | Manual-only trigger (`POST /api/admin/flatten`; watchdog never flattens, `backend/worker/watchdog.py:300,318`). Deliberate SAFE_MODE bypass is *correct* for its purpose, but: cost-basis price fallback can post a deeply off-market limit; orders are untracked after submit; duplicate-flatten guard is process-local only (`emergency.py:19-23`). |
| G3 | **`bot/main.py` sells** (stop-loss, signal, rebalance, MDD flatten) | All four ✗ | Legacy standalone process; disabled in `docker-compose` per CLAUDE.md (P1-08/P5-04) — **runtime deployment state UNVERIFIED from code alone**. Contains the repo's only automatic MDD liquidation (`:274-277`), scheduled at 23:50 KST when KR market is closed. |
| G4 | **LivePipeline sells** (`backend/quant/live/pipeline.py:182-196`) | Reservation ✗ Idempotency ✗ Reconciliation ✗ (own dedup + own kill switch) | Production wiring **UNVERIFIED** — no non-test call site found; worker scheduler drives `StrategyWorker`, not `LivePipeline`. |
| G5 | **IndicatorStrategy sells** (`strategy.py:190-242`) | Reservation ✗ Idempotency ✗ (formal store unused) | Least concerning: pending lock + SAFE_MODE + freshness + breaker + state machine + poller + reconciler all engaged. Listed for completeness: the pre-submit durable write and the `IdempotencyStore` from `backend/execution/idempotency.py` remain unwired (ROADMAP P0-02-old territory, not P0-07). |

Cross-cutting structural gaps discovered (recorded, not in P0-07 implementation scope):

- **S1 — Kill switch vs. exits contradiction:** worker MDD breach halts *sells too* (SAFE_MODE gates both sides), while the unwired `kill_switch.py` library would also block `CLOSE_POSITION` by default when halted. The system's only halt-immune close is the manual EmergencyFlatten. Whether "halt blocks exits" is intended policy is **Conflicting / needs clarification**.
- **S2 — No sellable-qty concept anywhere:** all flows sell full `hldg_qty`/`ovrs_cblc_qty`; shares locked by resting orders/unsettled trades cause broker-side rejects. (Whether KIS `INQR_DVSN="02"` nets pending sells: **UNVERIFIED**.)
- **S3 — No market/IOC/FOK capability on KIS adapter:** every "liquidation" in the system is a resting limit order; fill is never guaranteed, including in emergencies.

---

## 5. Implementation Recommendation

**P0-07 implementation = route `close_position` through the existing `reserve_and_submit`, with qty/price resolved server-side.** Smallest safe shape, in order:

1. **Backend (core, one handler):** rebuild `close_position` to (a) look up the live position via the request-scoped `KISPortfolio` (`hldg_qty`/`ovrs_cblc_qty` — the same fields every other flow trusts), rejecting if no position or requested qty > held qty; (b) resolve a limit price from the live quote (`get_price_kr/us`) — **not** `pchs_avg_pric`, repeating EmergencyFlatten's fallback mistake, and **not** caller-blind price 0; (c) build the same `req` dict + `derive_idempotency_key`/`request_fingerprint` + `broker_submit` closure as `place_order` (`quick_trade.py:170-198`) with `side="sell"`, and call `reserve_and_submit` with `risk_gate=Depends(get_risk_gate)`. This yields reservation, idempotency, fail-closed risk gating, persistence, and automatic recovery-sweep coverage with **zero changes** to service, model, or recovery modules (§3).
2. **Contract decision (must precede or accompany 1):** either the frontend sends `qty` (mapped from the real KIS field names — a field-mapping fix, §1.2) and the backend validates it against the holding, or the backend derives qty entirely ("close all of symbol X"). Recommend the latter for a true "close position" semantic: schema becomes `{credential_id, symbol, market, exchange}` + optional qty — this is new-but-minimal business logic and is exactly the pricing/qty decision `api/compat.py:387-391` correctly refused to hide in middleware.
3. **Honest response:** return the actual `order.status` from `reserve_and_submit` (submitted/blocked/rejected/reserved) instead of the hardcoded `"submitted"`, mirroring `place_order:211-223`.
4. **Explicitly out of P0-07 scope** (separate tasks): S1 halt-vs-exit policy, S2 sellable-qty support, S3 market/IOC capability, EmergencyFlatten price hardening (G2), fill confirmation for QT orders, and anything touching `bot/main.py` (G3) or `LivePipeline` (G4).

Interim stopgap if implementation is deferred: hide/feature-flag the frontend Close button — today it can only produce a 422, and if the frontend contract were ever "fixed" naively (sending qty/price without server-side validation), G1 becomes a live unguarded order path.

**Classification summary:** G1 = **Planned / not implemented** (P0-07 core); §3 reuse = **Implemented, reusable as-is**; G2/G3/G4, S1–S3 = **Out of scope for P0-07** (recorded); frontend contract & LivePipeline wiring & KIS price-0/INQR_DVSN behavior = **UNVERIFIED / Conflicting — needs clarification**.
