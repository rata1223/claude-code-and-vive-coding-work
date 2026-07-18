# P5-02B: Compatibility Adapter Phase 1B (Strategy)

> **Superseded by `docs/P5_COMPAT_REFACTOR_PLAN.md` / `docs/P5_COMPAT_REFACTOR.md` (P5-02E):** `StrategyCompatMiddleware` described below was consolidated into a single unified `CompatMiddleware` alongside Phase 1C's `WatchlistCompatMiddleware`. This document remains the correct historical record of *why* Phase 1B's `_StrategyPathConfig` shape and query-remap approach were designed the way they were.

> Implements the strategy portion of the P1 slice of `docs/P5_ADAPTER_PLAN.md` (migration order items 3-4). TDD: failing tests written first, then the minimum code to pass. `api/routers/strategies.py` was not modified — all translation logic lives in `api/compat.py`, activated via a single middleware-registration line in `api/main.py`.

**Date:** 2026-07-15

---

## Why this phase uses a different mechanism than Phase 1A

Phase 1A (`docs/P5_COMPAT_PHASE1A.md`) swapped a route's **body parameter type** (`LoginRequest`→`CompatLoginRequest`) — a one-line change per router file, because those endpoints take a Pydantic body model. Every strategy sub-resource endpoint instead takes its identifying parameter as a bare `Query(...)` argument (`strategy_id: int = Query(...)`) — there's no body model to swap, and this phase's constraint ("do not modify backend services") rules out even the minimal signature edits Phase 1A made.

So Phase 1B uses **HTTP middleware**, entirely defined in `api/compat.py` as `StrategyCompatMiddleware`, registered with one `app.add_middleware(StrategyCompatMiddleware)` line in `api/main.py`. It rewrites the query string before FastAPI resolves route parameters, and reshapes the JSON response body afterward — for a fixed, explicit set of paths only. `api/routers/strategies.py` has zero lines changed.

**Design tradeoff, stated plainly:** this promotes a fix for 6 specific GET endpoints into app-wide ASGI middleware — `dispatch()` runs a cheap `dict` lookup on every request the app serves, including the ~60 other unrelated routes, and `BaseHTTPMiddleware` itself carries a small per-request overhead (an extra `anyio` stream handoff) independent of that lookup. A `Query(alias=...)`/`Depends()` change confined to `api/routers/strategies.py` would avoid both costs, at the price of touching that file — which this phase's constraint explicitly rules out. Under that constraint, middleware is the least-bad option, not a free one; worth revisiting if the "never touch route files" rule turns out to be a self-imposed convention rather than a hard external requirement.

**Self-review + code-review catches, all fixed before this doc was finalized** (two independent review passes, one live-reproducing against the real app):
1. **Blank query values dropped on remap.** `_remap_query_string`'s `parse_qsl` call omitted `keep_blank_values=True` (unlike Starlette's own `QueryParams` parsing), so a blank-valued sibling param (e.g. `?id=1&level=`) would be silently dropped — not preserved as blank — whenever the `id`→`strategy_id` remap fired. Currently inert (no affected route distinguishes "blank" from "absent" today) but a real latent bug for any future route that does. Fixed, locked in by `test_blank_sibling_query_value_survives_the_id_remap`.
2. **Header multidict collapsed to a plain dict.** Response headers were rebuilt via `dict(response.headers.items())`, which silently drops all but the last value of any repeated header name (e.g. multiple `Set-Cookie`). None of these routes currently emit duplicate headers, but the fix (preserving `raw_headers` as a list) removes the risk structurally rather than relying on that staying true. Locked in by `test_cors_headers_survive_response_rebuild`.
3. **Unnecessary work on no-op responses.** Every 200 response on the affected paths — including business errors like "Strategy not found," where `data` is `null` — was unconditionally JSON-decoded and re-serialized even though nothing was actually being added. Now short-circuits (still reconstructs a `Response`, since `body_iterator` must be consumed either way, but skips the re-serialization work when no key was actually added).
4. **`ensure_ascii`/separator mismatch with FastAPI's own JSON rendering.** `json.dumps(payload)` used Python's defaults (`ensure_ascii=True`, spaced separators) instead of matching `fastapi.responses.JSONResponse.render()`'s actual settings (`ensure_ascii=False, allow_nan=False, separators=(",", ":")`, confirmed by reading FastAPI's source directly). A Korean-named strategy or Korean log message — plausible on a KIS platform — would have round-tripped through this middleware re-encoded as `\uXXXX` escapes with extra whitespace, inconsistent with every other endpoint's output. Not data loss (still valid, round-trippable JSON), but a real formatting regression. Fixed, locked in by `test_non_ascii_strategy_name_round_trips_without_unicode_escaping`.
5. **Three separate path tables consolidated into one.** `_STRATEGY_QUERY_REMAP_PATHS`, `_STRATEGY_RESPONSE_KEY_ADD`, and `_STRATEGY_RESPONSE_UNWRAP_PATHS` had no shared key set and an implicit, undocumented `if`/`elif` precedence rule between the response-side two. Replaced with a single `_STRATEGY_PATH_CONFIG: Dict[str, _StrategyPathConfig]` — one entry per path, so there's no drift risk between "what this middleware touches" and "how it touches it."

---

## Implemented endpoints

| Endpoint | Change |
|---|---|
| `GET /api/strategies` (list) | Response gains a `strategies` key (copy of `items`). |
| `GET /api/strategies/trades` | Accepts `id` as a `strategy_id` alias. Response gains a `trades` key (copy of `items`). |
| `GET /api/strategies/positions` | Accepts `id` as a `strategy_id` alias. Response gains a `positions` key (copy of `items`). |
| `GET /api/strategies/equityCurve` | Accepts `id` as a `strategy_id` alias. Response `data` is replaced with the bare array (see below — the one non-additive case). |
| `GET /api/strategies/performance` | Accepts `id` as a `strategy_id` alias. Response shape unchanged (already a flat dict). |
| `GET /api/strategies/logs` | Accepts `id` as a `strategy_id` alias. Response gains a `logs` key (copy of `items`). |
| `GET /api/strategies/notifications/unread-count` | Response gains an `unread` key (copy of `count`). |

No other endpoint (`create`, `update`, `delete`, `start`, `stop`, `detail`, `test-connection`, `ai-generate`, `backtest`, `batch-create`, notification read/read-all/clear) was touched — verified by regression tests.

---

## Request mapping

For `trades`, `positions`, `equityCurve`, `performance`, `logs`:

```
FE sends query:  { id, limit }             (frontend's actual shape)
BE wants query:  { strategy_id, page, page_size }

Middleware: if "strategy_id" not in query and "id" in query,
            inject strategy_id = id into the query string
            before FastAPI resolves Query(...) parameters.
```

- `strategy_id` always wins if both are present (same precedence pattern as Phase 1A's `email` over `username`) — verified by `test_strategy_id_takes_precedence_over_id_when_both_present`.
- Old-style direct callers sending `strategy_id` alone are completely unaffected — the middleware only acts when `strategy_id` is *absent*.
- `limit` has no backend equivalent (pagination is `page`/`page_size` with its own defaults) and is left alone — FastAPI silently ignores unrecognized query keys, so this is a no-op, not an error.
- `GET /api/strategies` (list) and `GET /api/strategies/notifications/unread-count` take no identifying query parameter, so no request-side change applies to them.

---

## Response mapping

For `list`, `trades`, `positions`, `logs`, `notifications/unread-count`:

```
BE returns:  { "total": n, "items": [...] }     (or {"count": n} for unread-count)
Middleware:  data[new_key] = data[old_key]       -- additive, old_key stays
FE reads:    data.strategies / data.trades / data.positions / data.logs / data.unread
```

This is deliberately **additive, not a rename** — `items`/`count` remain in the response exactly as before. Nothing that currently reads the old key breaks; the frontend's expected key is simply also present.

For `equityCurve` specifically, this additive approach isn't possible: the frontend does `ensureArray(res.data)`, meaning `data` itself must *be* the array, not an object containing one. An object can't simultaneously be a dict with a keyed array and a bare array, so `data` is replaced with `data.items` for this one path only. This is documented as the sole non-additive transform in this phase, matching the precedent already set for `closePosition`'s auto-resolution in `docs/P5_ADAPTER_PLAN.md` §3 (sometimes the frontend's contract genuinely can't be satisfied by addition alone).

`performance` needs no response change — the backend already returns a flat metrics dict, and the frontend already reads `res.data || {}` directly.

---

## Backward compatibility

Verified by `TestStrategyBackwardCompatibility`:
- `trades`/`list` responses still contain `items` (and `total`) alongside the new keys.
- `unread-count` still contains `count` alongside `unread`.
- Direct `strategy_id`-only requests (the backend-native shape) are completely unaffected by the query-remap logic, since it only fires when `strategy_id` is absent.

---

## Remaining compatibility gaps

- **`strategyApi.create`'s nested-payload issue** (`docs/P5_API_MAPPING_AUDIT.md` DTO row 1 in the strategy group, not in this phase's scope): the frontend sometimes sends a nested `trading_config: {symbol, timeframe, broker}` object that `StrategyCreate`'s flat fields don't capture. This was never scheduled into the Phase 1 migration order (`docs/P5_ADAPTER_PLAN.md` §6 items 1-6 cover only what's implemented in Phase 1A + 1B) and remains open. Worth noting: the one frontend screen with this exact nested-payload shape (`trading/CreateStrategy.vue`) was already flagged as unreachable (no registered route, either app) in `docs/P5_FRONTEND_API_INVENTORY.md` — low real-world impact.
- Everything else from `docs/P5_ADAPTER_PLAN.md` Phase 1 (watchlist array-unwrapping, quick-trade balance/position field remap) is still open, along with all Phase 2/3 items (AI Analysis, Quick Trade paradigm rework, WebSocket wiring, Community/Billing).
- The CI-wiring gap flagged in `docs/P5_COMPAT_PHASE1A.md` (`api/tests/` not yet wired into `tests.yml`/`ci-postgres.yml`) still applies to this phase's tests too.

---

## Test results (TDD red → green)

**Before implementation** (middleware did not exist): 11 of 25 initial tests failed exactly as expected — every test depending on the `id`→`strategy_id` alias or the new additive/unwrapped response keys returned 422 or `KeyError`, while tests exercising already-existing behavior (`strategy_id`-only requests, old `items`/`count` keys, untouched endpoints) already passed.

**After implementation, plus 3 regression tests added during the review passes** (non-ASCII round-trip, blank sibling query value, CORS header survival — see the catches above): all 28 strategy tests pass, and all 16 Phase 1A tests continue to pass unchanged (44/44 total in `api/tests/`).

```
api/tests/test_compat_strategy.py — 28 passed
api/tests/test_compat_login.py — 7 passed
api/tests/test_compat_credentials.py — 9 passed

======================= 44 passed in 14.50s =======================
```

## Files changed

- **Modified**: `api/compat.py` (`StrategyCompatMiddleware`, its `_StrategyPathConfig` table, appended after the Phase 1A models), `api/main.py` (+6 lines: import + one `app.add_middleware(...)` registration).
- **New**: `api/tests/test_compat_strategy.py`, this document.
- **Untouched**: `api/routers/strategies.py` — zero lines changed, confirmed via `git diff --stat`.
