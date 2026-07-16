# P5-02C: Compatibility Adapter Phase 1C (Watchlist)

> Implements the watchlist portion of the P1 slice of `docs/P5_ADAPTER_PLAN.md`. TDD: failing tests written first, then the minimum code to pass. `api/routers/watchlist.py` was not modified — all translation logic lives in `api/compat.py`, activated via a middleware-registration line in `api/main.py`, the same mechanism as Phase 1B.

**Date:** 2026-07-16

---

## Scope

`docs/P5_API_MAPPING_AUDIT.md` §2/§3 classifies `watchlistApi` as 2 FC (fully compatible) + 4 DTO (mismatched). The 2 FC endpoints — `add`, `remove` — were independently re-verified against `WatchlistAdd`/`WatchlistRemove` in `api/schemas.py` and confirmed to need no adapter work; they are not touched by the new middleware. This phase covers the 4 DTO endpoints: `getList`, `search`, `getHot`, `getPrices`.

**One finding beyond the original audit:** the audit's row for `watchlistApi.search` only flagged the response shape (`items` dict vs. bare array). Re-deriving the request side from `mobile/src/components/SymbolPicker.vue` found that the frontend actually sends `keyword`, while `search_symbols` reads `q` (`Query("", min_length=0)`). Since FastAPI silently drops unrecognized query keys, `keyword` is discarded entirely and `q` always falls back to `""` — **every search call today returns the full unfiltered catalogue, regardless of what the user typed.** This is a more severe break than the audit described (matching the precedent set by Phase 1A, which found the `email`-vs-`username` mismatch was worse than its own audit row suggested).

**One thing the audit worried about that turned out not to matter:** `docs/P5_API_MAPPING_AUDIT.md` §6 flagged `getPrices`'s mixed-market watchlist as needing "an adapter that batches by market or a backend enhancement," since the frontend can send symbols from several markets in one call but the backend only accepts one `market` value per request. Reading `get_watchlist_prices` shows `market` is accepted as a parameter but **never referenced anywhere in the function body** — price lookups go straight through `yf.Ticker(symbol)`, which resolves purely by symbol string. Collapsing a mixed-market watchlist to a single `market` value is therefore inert on the current backend, not a real limitation. An earlier version of this adapter still forwarded a best-effort `market` (the first item's) "for forward-compatibility" — `/code-review` flagged that as speculative complexity for a parameter nothing reads, and a wrong-shaped fix even if the backend ever does start using it (see Remaining compatibility gaps below). It was removed; only `symbols` is derived.

---

## Mechanism

Same reasoning as Phase 1B: every affected endpoint takes its parameters as bare `Query(...)` arguments, not a Pydantic body model, so there's no type annotation to swap. `WatchlistCompatMiddleware` (new, in `api/compat.py`) rewrites the query string before FastAPI resolves route parameters and reshapes the JSON response body afterward, for a fixed, explicit set of paths only. `api/routers/watchlist.py` has zero lines changed.

**Refactor alongside this phase (no behavior change):** `StrategyCompatMiddleware`'s response-rebuild and JSON-formatting logic was duplicated verbatim in the new middleware, so both were extracted to shared module-level functions (`_rebuild_response`, `_dumps_like_fastapi`) that `StrategyCompatMiddleware` now calls instead of its own former copies. A second round of this same extraction happened during `/code-review` (see below): query-string parsing/rewriting and JSON-body-decode-with-fallback were *also* duplicated between the two middlewares, so `_read_query_params`/`_write_query_params`/`_alias_query_param`/`_read_json_body` were extracted too. All of these are pure extractions — same logic, same behavior — verified by re-running the full suite after each round (46/46 after the first, 67/67 after the second).

---

## Self-review + code-review catches, all fixed before this doc was finalized

High-effort `/code-review` (8 independent finder angles + 1-vote verify) ran against the full diff. One angle live-reproduced a real bug against the running app; three others independently converged on the same duplication concern.

1. **Duplicate query keys resolved to the wrong value.** The two new query-remap functions (`_watchlist_remap_search_keyword`, `_watchlist_remap_prices_symbols`) picked the *first* occurrence of a repeated key, while the pre-existing `StrategyCompatMiddleware._remap_query_string` deliberately picks the *last* occurrence, matching Starlette's own `QueryParams.get()` semantics for duplicate params — a distinction its own inline comment already called out. Live-reproduced: `GET /api/market/symbols/search?keyword=MSFT&keyword=AAPL` returned `['MSFT']` through the aliased path, while the equivalent native call `?q=MSFT&q=AAPL` returns `['AAPL']` — the aliased and native paths silently disagreed on identical input. Fixed by extracting a single `_alias_query_param(request, old_key, new_key)` helper (last-value-wins, used by both Strategy's and Watchlist's simple aliases) so the semantics can't drift between call sites again. Locked in by `test_search_duplicate_keyword_resolves_to_last_value` and `test_prices_duplicate_watchlist_param_resolves_to_last_value`.
2. **Query-string parse/rewrite boilerplate triplicated.** `_remap_query_string` (Strategy), `_watchlist_remap_search_keyword`, and `_watchlist_remap_prices_symbols` each independently parsed the query string into a list of tuples and re-encoded it back, with the `keep_blank_values=True`/`latin-1` correctness details (already load-bearing per Phase 1B's own findings) copy-pasted rather than shared. Fixed by extracting `_read_query_params`/`_write_query_params`, used by all three.
3. **JSON-body-decode-with-fallback duplicated.** `StrategyCompatMiddleware._transform_response` and the new `WatchlistCompatMiddleware._unwrap_items` both opened with the identical "consume `body_iterator`, `json.loads` with a decode-failure fallback to `_rebuild_response`" sequence. Fixed by extracting `_read_json_body`, used by both — each middleware's `_transform_response`/`_unwrap_items` now only expresses what it does differently (additive-key-or-unwrap vs. unconditional unwrap), not how to safely get there.
4. **Speculative `market`-forwarding removed.** Covered above under Scope — an earlier version forwarded a best-effort `market` value for a backend parameter that's provably inert today; two review angles (simplification, altitude) independently flagged this as complexity that doesn't actually prepare for the future case it claims to (a single collapsed value is the wrong fix if the backend ever does start routing by market — see Remaining compatibility gaps). Removed; `_watchlist_remap_prices_symbols` now only derives `symbols`.

**Not fixed, deliberately, with reasoning:**
- **This is now the second near-identical `BaseHTTPMiddleware` subclass** (Strategy, Watchlist), and the project's own backlog (`docs/P5_ADAPTER_PLAN.md`) names `klineApi`/`indicatorApi`/`quickTradeApi` as future phases — each would add a third, fourth, fifth copy of the same "path-keyed config table + dispatch + query remap + response transform" shape, and each additional `BaseHTTPMiddleware` layer adds its own per-request `anyio` overhead to every route in the app, not just the ones it covers. Generalizing this into one cross-resource dispatcher (a single middleware, one unified path-config table keyed by resource) would need to happen before or during the next phase, not retrofitted into this one — see Recommended Phase 2 below. Not attempted here because it would mean redesigning Phase 1B's already-merged, already-tested middleware shape as a side effect of a task explicitly scoped to "Implement ONLY Watchlist compatibility," which is a decision worth making deliberately with the person driving this workstream, not silently bundled into this diff.
- **`_WatchlistPathConfig` as a one-field `NamedTuple`** rather than a flat `Dict[str, Optional[Callable]]`. Structurally trivial either way; kept for parity with `_StrategyPathConfig` so a future generalization (see above) has one consistent shape to consolidate from, not two.

---

## Implemented endpoints

| Endpoint | Request change | Response change |
|---|---|---|
| `GET /api/market/watchlist/get` | none | `data` replaced with `data["items"]` (bare array) |
| `GET /api/market/symbols/search` | `keyword` → also injects `q` (last value wins if either is repeated) | `data` replaced with `data["items"]` (bare array) |
| `GET /api/market/symbols/hot` | none | `data` replaced with `data["items"]` (bare array) |
| `GET /api/market/watchlist/prices` | JSON `watchlist=[{symbol,market},...]` → also injects `symbols` (CSV) | `data` replaced with `data["items"]` (bare array) |

`add` and `remove` are untouched — verified by regression tests. So is every non-watchlist route (`/api/strategies/*`, `/api/auth/*`, etc.) — same regression discipline as Phase 1B.

---

## Request mapping

**`search`:**
```
FE sends query:  { keyword, market, limit }
BE wants query:  { q, market, limit }

Middleware: if "q" not in query and "keyword" in query,
            inject q = keyword into the query string.
```
`q` always wins if both are present (same precedence pattern as Phase 1A's `email`/`username` and Phase 1B's `strategy_id`/`id`) — verified by `test_search_q_takes_precedence_over_keyword_when_both_present`. Direct callers already sending `q` are unaffected, since the remap only fires when `q` is absent. If `keyword` itself is repeated, the last value is used, matching native `q` semantics — verified by `test_search_duplicate_keyword_resolves_to_last_value`.

**`getPrices`:**
```
FE sends query:  { watchlist: '[{"symbol":"AAPL","market":"NASD"}, ...]' }
BE wants query:  { symbols: "AAPL,MSFT,..." }

Middleware: if "symbols" not in query and "watchlist" in query,
            parse watchlist as JSON, extract each item's symbol,
            join as CSV, inject as symbols.
```
Fails open by design: if `watchlist` is missing, not valid JSON, not a list, or yields no symbols, the query string is left untouched and the request 422s exactly as it already does today (`symbols` has no default) — verified by `test_prices_malformed_watchlist_json_degrades_to_existing_422`. This alias must degrade to the existing broken behavior, not turn a 422 into a 500 inside the middleware. `market` is deliberately not derived or forwarded — see Scope above.

`getList` and `getHot` take no identifying query parameter that needs remapping (their existing params — none, and `market`/`limit` respectively — already match).

---

## Response mapping

For all four paths:
```
BE returns:  { "total"?: n, "items": [...] }
Middleware:  data = data["items"]              -- non-additive, matches equityCurve's precedent
FE reads:    ensureArray(res.data)
```
This mirrors Phase 1B's `equityCurve` case exactly, for the same reason: the frontend does `ensureArray(res.data)` for all four of these calls, meaning `data` itself must *be* the array — an object can't simultaneously be a dict with a keyed array and a bare array, so there's no additive option here (unlike `strategies`/`trades`/etc. in Phase 1B, which added a new key alongside `items`). `search`'s `total` field is dropped in the process; nothing on the frontend reads it (`this.searchResults = res.data || []` is the only consumer).

---

## Remaining compatibility gaps

- **`getPrices` has no real per-symbol market routing**, and none was built here, deliberately (see Scope/self-review above). If `get_watchlist_prices` is ever changed to actually use a `market` parameter (e.g. to route KRX symbols through a Korea-specific price source), a mixed-market watchlist would need real per-symbol routing — either a backend contract change (accept a list of `{symbol, market}` pairs) or an adapter that batches requests by market and merges results. A single collapsed `market` value — what an earlier version of this adapter did — would not be a correct fix for that case, which is exactly why it was removed rather than kept as a placeholder.
- **`search`'s yfinance fallback path** (triggered when no catalogue symbol matches) is untested by this phase's suite — deliberately avoided to keep tests deterministic and offline; all `search`/`getPrices` tests use catalogue symbols (`AAPL`, `MSFT`) so `get_watchlist_prices`'s real network calls fail closed (caught exception → `price: 0.0`) without making the test suite's pass/fail depend on actual internet access. This mirrors how the backend itself already degrades on a lookup failure — not a gap introduced by this phase.
- Everything else from `docs/P5_ADAPTER_PLAN.md` Phase 1/2/3 not covered by Phase 1A (login/credentials), 1B (strategy), or this phase — `strategyApi.create`'s nested-payload issue, `klineApi`/`indicatorApi`, `quickTradeApi`'s several breaks, AI Analysis, Community/Billing (unimplemented backend routers) — remains open.
- The CI-wiring gap flagged in `docs/P5_COMPAT_PHASE1A.md` (`api/tests/` not yet wired into `tests.yml`/`ci-postgres.yml`) still applies to this phase's tests too.

---

## Recommended Phase 2

`/code-review`'s altitude angle raised a point worth acting on deliberately rather than accreting further: **this pattern (one `BaseHTTPMiddleware` subclass per backend resource, each with its own path-config table) is now used twice and the project's own migration order names three more candidates** (`klineApi`/`indicatorApi`, `quickTradeApi`'s several DTO breaks, plus whatever a strategy `create` nested-payload fix needs). Recommendation for whichever phase comes next:

1. **Before or at the start of the next resource phase, consolidate `StrategyCompatMiddleware` and `WatchlistCompatMiddleware` into one `CompatMiddleware`** driven by a single path-keyed config table across all resources, rather than adding a third parallel class. The building blocks this phase and Phase 1B already extracted (`_rebuild_response`, `_dumps_like_fastapi`, `_read_query_params`/`_write_query_params`/`_alias_query_param`, `_read_json_body`) are resource-agnostic and are the right foundation for that single dispatcher — the remaining resource-specific pieces are just each path's config entry (which query params to alias, how to reshape the response), which a unified table can hold as easily as two separate ones.
2. **Decide, once and explicitly, whether custom (non-alias) query transforms like `_watchlist_remap_prices_symbols` deserve a distinct config slot from simple key-renames**, rather than letting them share `Optional[Callable]` and rely on each implementer to notice the difference in complexity. `quickTradeApi`'s breaks (per `docs/P5_API_MAPPING_AUDIT.md` rows 18-22 — `qty`/`amount`, singular-vs-plural position shape, `unwrapItems(res.data, 'trades')`) look likely to need more of this custom-transform shape than either Strategy or Watchlist did, so this is worth settling before that phase rather than during it.
3. **`strategyApi.create`'s nested-payload issue** (deferred since Phase 1B, `docs/P5_API_MAPPING_AUDIT.md` DTO row 1) remains the most isolated, lowest-risk remaining item if a smaller phase is wanted before tackling `quickTradeApi` or the unimplemented routers (`aiAnalysisApi`, `billingApi`, community indicators) — it's a body-model swap (Phase 1A's mechanism, not middleware), and the one frontend screen that needs it is already flagged unreachable, so it's low-urgency but cheap to close out.

---

## Test results (TDD red → green)

**Before implementation** (middleware did not exist): 11 of 19 initial tests failed exactly as expected — every test depending on the `keyword`→`q` alias, the `watchlist`→`symbols` alias, or the response unwrap returned the pre-existing broken shape (unfiltered catalogue, 422, or a dict instead of an array), while tests exercising already-existing behavior (missing-param 422, malformed-JSON 422, `add`/`remove`, unrelated routes) already passed.

**After implementation, plus 2 regression tests added during the `/code-review` pass** (duplicate-query-key last-value-wins, for both `search` and `getPrices` — see the catches above): all 21 watchlist tests pass, and all 46 Phase 1A/1B tests continue to pass unchanged (67/67 total in `api/tests/`).

```
api/tests/test_compat_watchlist.py — 21 passed
api/tests/test_compat_strategy.py  — 30 passed
api/tests/test_compat_login.py     — 7 passed
api/tests/test_compat_credentials.py — 9 passed

======================= 67 passed in 30.21s =======================
```

## Files changed

- **Modified**: `api/compat.py` (`WatchlistCompatMiddleware`, its `_WatchlistPathConfig` table and two query-remap functions, appended after the Phase 1B middleware; `_rebuild_response`/`_dumps_like_fastapi`/`_read_query_params`/`_write_query_params`/`_alias_query_param`/`_read_json_body` extracted as shared module-level helpers, with `StrategyCompatMiddleware` updated to call them — no behavior change, re-verified against the full suite after each extraction), `api/main.py` (+4 lines: import + one `app.add_middleware(...)` registration).
- **New**: `api/tests/test_compat_watchlist.py`, this document.
- **Untouched**: `api/routers/watchlist.py`, `api/routers/strategies.py` — zero lines changed, confirmed via `git diff --stat`.
