# P5-03B: Orders Compatibility Implementation

> Implements the approved `docs/P5_ORDERS_COMPAT_AUDIT.md` mapping. Adapter layer only — no `api/routers/*.py`, business-logic, DB-schema, or frontend changes. TDD: failing tests written first (`api/tests/test_compat_orders.py`), then the minimum code to pass.

**Date:** 2026-07-19

---

## Implemented endpoints (4 of 5)

| Endpoint | Method | Adapter coverage |
|---|---|---|
| `/api/quick-trade/balance` | GET | query alias + response field reshape |
| `/api/quick-trade/position` | GET | query alias + response shape reshape |
| `/api/quick-trade/place-order` | POST | request body field renames |
| `/api/quick-trade/history` | GET | response key rename |
| `/api/quick-trade/close-position` | POST | **not implemented — see Remaining gaps** |

All four implemented endpoints route through `CompatMiddleware` (`api/compat.py`) via a new, separate table, `_ORDERS_PATH_CONFIG` — kept independent of the existing `_PATH_CONFIG` (strategy/watchlist) so that table's own drift-guard test stays untouched. `api/routers/quick_trade.py` has zero lines changed.

## Request mapping

| Endpoint | Frontend sends | Backend expects | Mechanism |
|---|---|---|---|
| `balance` | query `market_type` | query `market` | `query_remap=_alias("market_type", "market")` |
| `position` | query `market_type` | query `market` | `query_remap=_alias("market_type", "market")` |
| `place-order` | body `amount`, `market_type` | body `qty`, `market` | new `body_remap=_alias_body(("amount","qty"), ("market_type","market"))` — rewrites the JSON request body before Pydantic parses it |

`place-order`'s extra frontend-only fields (`order_type`, `leverage`, `source`) need no adapter handling — `PlaceOrderRequest` has no `extra="forbid"`, so Pydantic v2's default `extra="ignore"` already drops them silently. Both renames are additive and last-writer-safe: if the frontend ever sends the native key directly (`qty`, `market`), that value wins and the alias is a no-op — proven by `TestPlaceOrderEndToEnd::test_native_qty_takes_precedence_when_both_amount_and_qty_sent`.

## Response mapping

| Endpoint | Backend returns | Frontend expects | Mechanism |
|---|---|---|---|
| `balance` | `cash`, `total_eval` | `available`, `total` | new `response_transform=_quick_trade_balance_reshape` — `available := cash` (cash on hand), `total := total_eval` (full portfolio evaluation); additive, original keys untouched |
| `position` | `{"symbol", "position": dict\|None}` (singular) | plural array at `data.positions` | new `response_transform=_quick_trade_position_to_array` — wraps `position` into a 0-or-1-element list under a new `positions` key; additive, `position` untouched |
| `history` | `data.items` | `data.trades` | existing `response_key_add=("items", "trades")` primitive, reused unchanged |

## New adapter primitives added to `_PathConfig`

Two new fields, both additive to the existing `NamedTuple` (no existing field removed or renamed):

- **`body_remap: Optional[Callable[[Request], Awaitable[None]]]`** — the request-body counterpart to the existing `query_remap`. Runs before `call_next`, rewriting `request._body` (Starlette's cached-body attribute, re-read fresh by `BaseHTTPMiddleware`'s wrapper on every `call_next` call, not snapshotted earlier). Fails safe: malformed JSON or a non-dict body is left byte-identical, so the route's own validation error surfaces exactly as it does today.
- **`response_transform: Optional[Callable[[dict], Optional[dict]]]`** — a generic escape hatch for response reshapes that aren't a rename, checked before `response_unwrap_key`/`response_key_add` and mutually exclusive with both. Returns `None` for "no-op" (mirrors `response_key_add`'s existing fail-safe contract) or a replacement dict.
- **`methods: Tuple[str, ...] = ("GET",)`** — every pre-P5-03B `_PATH_CONFIG` entry defaults to this, so `dispatch()`'s per-entry method gate reproduces the old middleware-wide `if request.method == "GET"` check byte-for-byte for every existing path. `place-order` overrides it to `("POST",)`.

`dispatch()` now looks up `_PATH_CONFIG.get(path) or _ORDERS_PATH_CONFIG.get(path)` — the two tables' paths never collide, so this is an unambiguous fallback, not a merge.

## Remaining gaps

- **`close-position` — not implemented.** The audit initially classified this as blocked pending a human decision on how to source the missing `qty`/`price` (the frontend sends neither). Investigated further during planning: the backend's `/position` endpoint returns raw KIS broker fields with no live market price — the only price-like field is `pchs_avg_pric` (average *purchase* price), and `kis_adapter/orders.py`'s `sell_us`/`sell_kr` submit whatever price is given as an actual **limit-order price** (`ORD_DVSN: "00"`), not a market order. Auto-injecting a stale average-purchase price as a live limit-sell price is a trading-pricing decision — genuine business logic, out of an adapter's scope, with real financial-risk implications on a live-money account (`KIS_ENV=real`). **Decision: leave unimplemented**, pinned by a regression test (`TestClosePositionRemainingGap`) so the still-422 state is visible and any future fix's diff is easy to see.
- **Order cancel / order detail / order status screens**: confirmed by the audit not to exist anywhere in the frontend. No adapter work applicable — would be net-new feature work on both sides.
- **Quick-trade fill persistence**: `getHistory`'s `items`→`trades` rename is implemented, but the underlying data will remain empty for quick-trade orders regardless, since `place-order`/`close-position` never write to the `Trade` table (a pre-existing backend gap, confirmed in the audit, unrelated to and unfixable by this adapter).
- **`dashboardApi.getPendingOrders`**: confirmed dead code (zero frontend callers) — no adapter coverage needed.

## Risks

- **The same live-pricing risk that ruled out `close-position` is the main risk surface of this whole endpoint family.** `place-order` (now working) still submits the frontend's raw `price` field as a real limit-order price with no adapter-side sanity checking — this was already true before P5-03B and is unchanged by it, but is worth restating: the adapter fixes *shape* mismatches, it does not and should not validate that a price is reasonable.
- **`body_remap` mutates `request._body` directly** — an implementation detail of Starlette's current `BaseHTTPMiddleware`/`Request` internals rather than a public API. Verified against the installed Starlette version (0.37.2) that `_CachedRequest`'s receive wrapper re-checks this attribute fresh on each call; a future Starlette upgrade that changes this caching mechanism could silently break `body_remap` without any error, only a return to pre-fix behavior (422s on `place-order` again). Covered by `TestBodyRemapIsolated` and `TestPlaceOrderEndToEnd`, so a regression here fails loudly in CI rather than silently in production.
- **Two path-config tables (`_PATH_CONFIG`, `_ORDERS_PATH_CONFIG`) instead of one.** Deliberate (see `docs/P5_COMPAT_REFACTOR_PLAN.md`'s own precedent for why merging would be the wrong call here), but a future maintainer adding a new compat path needs to know which table to extend — both now carry a comment pointing at this document.

---

## Sources

Every field name and behavior claim above is drawn from a fresh read of `api/routers/quick_trade.py`, `api/schemas.py` (`PlaceOrderRequest`/`ClosePositionRequest`), `kis_adapter/portfolio.py`, `kis_adapter/orders.py`, and `docs/P5_ORDERS_COMPAT_AUDIT.md` (the approved audit this task implements). The `getBalance` field mapping (`available := cash`, `total := total_eval`) and the decision to leave `close-position` unimplemented were both explicitly confirmed before implementation, not inferred.
