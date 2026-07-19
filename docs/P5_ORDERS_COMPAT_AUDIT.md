# P5-03A: Orders Compatibility Audit

> **Analysis only. No code was changed to produce this document.** No `api/` or `frontend/` files were modified. This is the mandatory pre-implementation audit for P5-03 (Orders/quick-trade compatibility), following the same architecture and conventions as `docs/P5_COMPAT_REFACTOR_PLAN.md`/`docs/P5_COMPAT_REFACTOR.md`.

**Date:** 2026-07-18

---

## Context

P5-02E (PR #131, merged) closed out the strategy/watchlist compat-middleware unification and reported **READY for P5-03** on `GET`-shaped quick-trade endpoints, with `getPosition`'s response-shape mismatch flagged as a known gap needing a new adapter primitive. This document is the mandatory audit that precedes any P5-03 implementation: inventory the frontend's actual "Orders" surface, compare it against the backend's actual current behavior, and classify every endpoint before any adapter code is written. Per the task brief: analysis first, TDD-based implementation later (a separate, not-yet-started task), no business-logic changes, no backend API changes, no architecture redesign.

**Scope-defining finding** (from a fresh audit against `frontend/src`, confirmed canonical via `docker-compose.yml`'s `frontend:` service — not the near-duplicate `mobile/` reference copy): this app has **no traditional order-management screen**. There is no order list, no order detail, no cancel-order action, and no order-status polling anywhere in the frontend — confirmed by a repo-wide case-insensitive grep for "cancel" (only Vant UI-dismiss handlers, no cancel-order API call) and by reading every candidate view file. The "Orders screen" the task brief refers to is `frontend/src/views/quick-trade/index.vue` (component `QuickTrade`), which covers: balance, position, place order (buy/sell), close position, and a flat "history" list. `dashboardApi.getPendingOrders` (`GET /api/dashboard/pendingOrders`) exists as a real, working backend endpoint but has **zero frontend callers** — confirmed dead code on the frontend side.

---

## 1. Frontend Orders API inventory

All 5 functions live in `quickTradeApi` (`frontend/src/api/index.js:537-572`), consumed exclusively by `frontend/src/views/quick-trade/index.vue` via `useQuickTradeStore` (Pinia, `frontend/src/stores/index.js:296+`).

| FE function | URL | Method | Request shape (as actually sent) | Response fields UI reads |
|---|---|---|---|---|
| `getBalance(credentialId, marketType='spot')` | `/api/quick-trade/balance` | GET | query `{credential_id, market_type}` | `balance.available`, `balance.total`, `balance.currency` |
| `getPosition({credentialId, symbol, marketType})` | `/api/quick-trade/position` | GET | query `{credential_id, symbol, market_type}` | `unwrapItems(res.data, 'positions')` (expects plural array) → `symbol`, `side`, `size`, `unrealized_pnl`/`pnl` |
| `placeOrder(payload)` | `/api/quick-trade/place-order` | POST | body `{credential_id, symbol, side, order_type, amount, price, leverage, market_type, source:'manual'}` | order confirmation toast only |
| `closePosition(payload)` | `/api/quick-trade/close-position` | POST | body `{credential_id, symbol, market_type, position_side, source:'manual'}` — **no qty/price sent** | confirmation toast only |
| `getHistory(params={})` | `/api/quick-trade/history` | GET | no params sent | `unwrapItems(res.data, 'trades')` → per item `id, symbol, side, amount, created_at, status` |
| `dashboardApi.getPendingOrders` | `/api/dashboard/pendingOrders` | GET | query `params` (default `{}`) | **N/A — zero frontend callers, dead code** |

**Call timing:** on mount (`bootstrap()`: `Promise.allSettled([credentials.list, getHistory, watchlist.getList])`); on `activated()` (watchlist only); on watch of `selectedCredentialId`/`marketType` (`refreshTradeData()`: balance+history+conditionally position); on "Refresh Balance" button; on buy/sell submit; on close-position confirm. **No polling, no websocket subscription** — a backend `order:update` pub/sub event exists per prior docs but nothing in `frontend/src` subscribes to it.

**Store:** `useQuickTradeStore` (Pinia, id `'quickTrade'`). State: `selectedCredentialId, marketType, balance, positions, history, loading`. Actions: `setSelectedCredential, setMarketType, setBalance, setPositions, setHistory`. A thin cache with no polling logic of its own — all refresh is view-driven.

---

## 2. Endpoint mapping table

| Frontend surface | FE method+path | BE method+path | Class |
|---|---|---|---|
| `quickTradeApi.getBalance` | GET `/api/quick-trade/balance` | GET `/api/quick-trade/balance` | DTO differs |
| `quickTradeApi.getPosition` | GET `/api/quick-trade/position` | GET `/api/quick-trade/position` | DTO differs |
| `quickTradeApi.placeOrder` | POST `/api/quick-trade/place-order` | POST `/api/quick-trade/place-order` | DTO differs (guaranteed 422 today) |
| `quickTradeApi.closePosition` | POST `/api/quick-trade/close-position` | POST `/api/quick-trade/close-position` | Partially compatible (missing required fields, not a pure rename) |
| `quickTradeApi.getHistory` | GET `/api/quick-trade/history` | GET `/api/quick-trade/history` | DTO differs + functionally empty (backend never persists quick-trade fills) |
| `dashboardApi.getPendingOrders` | GET `/api/dashboard/pendingOrders` | GET `/api/dashboard/pendingOrders` | Already exists / already compatible shape — but frontend has no caller, out of scope for adapter work |
| order cancel | — (no FE function) | — (no BE route) | Missing on both sides — no UI need, out of scope |
| order detail (by ID) | — (no FE function) | — (no BE route) | Missing on both sides — no UI need, out of scope |
| order status (queryable) | only an implicit static field in place/close responses | not a real queryable status — `"status": "submitted"` is hardcoded | Missing / not real |
| fills | `getHistory` is the closest analog | `Trade` table exists but is strategy-scoped (`strategy_id` NOT NULL) and never written by quick-trade | Missing (functionally) |

---

## 3. DTO difference table

| Endpoint | Field | Frontend | Backend | Fix class |
|---|---|---|---|---|
| getBalance | query | `market_type` | `market` | pure rename — adapter-only |
| getBalance | response | `balance.available`, `balance.total` | `total_eval`, `cash` (no such keys) | key **mismatch**, not a rename — needs a confirmed value mapping, not a guess |
| getPosition | query | `market_type` | `market` | pure rename — adapter-only |
| getPosition | response | expects plural array (`unwrapItems(...,'positions')`) | returns `{symbol, position: dict\|null}` (singular) | shape mismatch — needs a **new** adapter primitive (existing `response_key_add`/`response_unwrap_key` don't fit "wrap singular in array") |
| placeOrder | body | `amount` (qty never named `qty`) | requires `qty` (no default) | pure rename — adapter-only, and the single highest-impact fix (currently guarantees 422 on every order) |
| placeOrder | body | `market_type` | `market` | pure rename — adapter-only |
| placeOrder | body | `order_type`, `leverage`, `source` sent | not accepted (silently dropped by Pydantic, not fatal) | informational only — user-facing order type/leverage choices are silently ignored today, not a 422 cause |
| closePosition | body | no `qty`/`price` sent | both required, no default | **cannot be synthesized statelessly** — needs either a frontend change (send the already-displayed position's qty/price) or a stateful gateway; not a pure adapter fix |
| closePosition | body | `market_type`, `position_side`, `source` sent | `market`, `exchange` expected; no `position_side` concept | rename for `market_type`→`market` is adapter-only; `position_side` has no backend equivalent, informational |
| getHistory | response | expects `data.trades` (`unwrapItems`) | returns `data.items` | pure rename — adapter-only, reuses the exact `response_unwrap_key` primitive already in `_PATH_CONFIG` |
| getHistory | data | expects real fill history | `Trade` never populated by quick-trade (no `Trade(`/`db.add` call anywhere in `quick_trade.py`) | **not adapter-fixable** — backend persistence gap, separate task |

---

## 4. Missing backend endpoints

- **True cancel-order endpoint**: doesn't exist. No UI element calls for one either — flag as "missing, no current UI need," not a blocker.
- **Order detail (single order by ID)**: doesn't exist, no UI need.
- **Real order-status mechanism**: doesn't exist as a queryable thing — `"status": "submitted"` is a hardcoded literal in the place/close response, not derived from any actual broker/order state.
- **Quick-trade fill persistence**: not a missing *route* — `/api/quick-trade/history` exists and returns a real (if key-mismatched) shape — but it is permanently empty for quick-trade orders since nothing is ever written to `Trade`.

---

## 5. Adapter-only fix candidates

Pure reshaping, matches the existing `CompatMiddleware` pattern, no backend logic change:

1. **`getBalance`** — query alias `market_type`→`market` (reuse `_alias()` exactly as-is).
2. **`getPosition`** — query alias `market_type`→`market` (reuse `_alias()`).
3. **`getHistory`** — response key rename `items`→`trades` (reuse `response_unwrap_key="items"`, the exact primitive already used by 5 existing `_PATH_CONFIG` entries).
4. **`placeOrder`** — body field renames `amount`→`qty`, `market_type`→`market`. **Note:** `CompatMiddleware` today only rewrites query strings and response bodies, not POST request bodies — this needs a new capability (a `request_body_remap` hook, analogous to `query_remap` but applied before the route parses the JSON body), not just a new table entry. Still architecturally an adapter (pure reshaping, no business logic), but is new middleware surface, not a drop-in table row.
5. **`getPosition`** — response reshape singular→array. Needs a new `_PathConfig` primitive (e.g. a `response_transform: Optional[Callable]` escape hatch) since neither `response_key_add` nor `response_unwrap_key` can express "wrap this object in a list" — this is exactly the gap P5-02E's final report already flagged.
6. **`getBalance`** — response field reshape `total_eval`/`cash`→`total`/`available`. **Do not implement on a guess** — "available" (freely tradeable cash) and "total" (portfolio evaluation) are different financial concepts; the mapping must be confirmed (likely `available:cash, total:total_eval`, but ask before shipping) rather than assumed.

---

## 6. Frontend-change-required candidates

A stateless adapter cannot fix these:

- **`closePosition`'s missing `qty`/`price`**: structurally absent from the frontend payload. Either the frontend must be changed to send the already-displayed position's qty/price, or a stateful gateway that tracks open positions server-side would be needed (materially bigger scope, arguably backend business logic — out of bounds for an adapter-only phase).
- **`order_type`/`leverage` silently ignored**: not a crash, but a silent UX gap — the frontend lets users pick a limit order or leverage and the backend never sees it. Fixing this for real requires backend field support (business logic, out of scope here); flag as known, non-urgent.
- **Cancel-order / order-detail / order-status screens**: don't exist at all. Building them is net-new frontend (and backend) feature work, not compatibility work — explicitly excluded by "do not add new business logic."
- **`getHistory` permanently empty for quick-trade**: not adapter-fixable — the adapter can only reshape existing data, not invent persisted fills. Needs backend `Trade`-equivalent persistence for quick-trade orders, a separate backend task.

---

## 7. Recommended implementation order

For the future P5-03B implementation task — **not started here**:

1. `getBalance` query alias (`market_type`→`market`) — trivial, unblocks balance display.
2. `getPosition` query alias (`market_type`→`market`) — trivial.
3. `getHistory` response key rename (`items`→`trades`) — trivial, reuses existing primitive (data stays empty until backend persistence lands separately).
4. `placeOrder` body field renames (`amount`→`qty`, `market_type`→`market`) — highest impact: nothing currently works at all (guaranteed 422); requires the new request-body-remap middleware capability from §5 item 4.
5. `getPosition` response reshape (singular→array) — requires the new adapter primitive from §5 item 5; do after 1-4 are proven so the new capability lands on its own, reviewable diff.
6. `getBalance` response field reshape — **blocked on confirming the available/total mapping** before implementing.
7. `closePosition` — **blocked on a product/architecture decision** (frontend change vs. stateful gateway); do not attempt as a stateless adapter fix.
8. Quick-trade fill persistence (unblocks real `getHistory` data) — separate backend-logic task, not adapter work.
9. Cancel-order / order-detail / order-status screens — net-new feature work, a distinct future phase, not compatibility work.

---

## Sources

Every field name, endpoint path, and behavior claim above is drawn directly from a fresh read of current `frontend/src` (`api/index.js`, `views/quick-trade/index.vue`, `views/assets/*`, `views/trading/TradeRecords.vue`, `views/home/index.vue`, `stores/index.js`) and `api/` (`routers/quick_trade.py`, `routers/dashboard.py`, `schemas.py`, `models.py`, `compat.py`) — not inferred from the 4-day-old `docs/P5_API_MAPPING_AUDIT.md`, which was used only as a cross-check and found to still agree on every point checked. `_PATH_CONFIG` was confirmed (grep) to have zero existing coverage for any `/api/quick-trade/*` or `/api/dashboard/pendingOrders` path. `backend/execution/` (order state machine, poller, reconciler) was confirmed unreachable from the HTTP API layer — `quick_trade.py` talks directly to the KIS broker adapter, bypassing it entirely.
