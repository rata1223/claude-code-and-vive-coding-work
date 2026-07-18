# P5-02E: Unified Compat Middleware Refactor

> Executes the design in `docs/P5_COMPAT_REFACTOR_PLAN.md` (P5-02D). Refactoring only — no new functionality, no behavior change, no edits to `api/routers/*.py`. TDD: failing regression tests written first (`api/tests/test_compat_unified.py`), then the minimum code to pass.

**Date:** 2026-07-18

---

## Before / after architecture

**Before (Phase 1B + 1C, two classes):**

```
Registration order (api/main.py):
  1. CORSMiddleware
  2. StrategyCompatMiddleware   (7 paths under /api/strategies/*)
  3. WatchlistCompatMiddleware  (4 paths under /api/market/*)

Per-request order (Starlette applies middleware in REVERSE registration order):
  Request  → WatchlistCompatMiddleware → StrategyCompatMiddleware → CORSMiddleware → routes
  Response ← (unwinds in reverse)

Two NamedTuples (_StrategyPathConfig, _WatchlistPathConfig), two config
tables (_STRATEGY_PATH_CONFIG: 7 entries, _WATCHLIST_PATH_CONFIG: 4
entries), two dispatch()/response-transform method pairs with the same
control-flow shape but different config types and mutation logic.
```

**After (P5-02E, one class):**

```
Registration order (api/main.py):
  1. CORSMiddleware
  2. CompatMiddleware   (11 paths, both resource families)

Per-request order:
  Request  → CompatMiddleware → CORSMiddleware → routes
  Response ← (unwinds in reverse)

One NamedTuple (_PathConfig), one config table (_PATH_CONFIG: 11
entries), one dispatch()/_transform_response() pair. dispatch() and
_transform_response() are StrategyCompatMiddleware's former bodies,
copied verbatim (only the config type hint changed:
"_StrategyPathConfig" -> "_PathConfig").
```

One fewer `app.add_middleware()` registration means one fewer ASGI/anyio
stream-handoff layer per request, app-wide — not just for the 11 configured
paths, since `BaseHTTPMiddleware` runs its dispatch machinery for every
request regardless of whether a given path is in its config table.

## Removed duplication

| # | Category | Before | After |
|---|---|---|---|
| 1 | Config shape | `_StrategyPathConfig` (bool + fixed staticmethod) and `_WatchlistPathConfig` (Callable) — two shapes for the same "how do I alias a query param" question | One `_PathConfig`, `query_remap: Optional[Callable[[Request], None]]` — Watchlist's already-more-general shape adopted universally |
| 2 | Config table | `_STRATEGY_PATH_CONFIG` (7) + `_WATCHLIST_PATH_CONFIG` (4), no shared key namespace | One `_PATH_CONFIG` (11 entries) |
| 3 | Query-alias wrapper functions | `StrategyCompatMiddleware._remap_query_string` (hardcoded `id`→`strategy_id`) + `_watchlist_remap_search_keyword` (hardcoded `keyword`→`q`) — one hand-written function per simple rename | Both collapse to `_alias("id", "strategy_id")` / `_alias("keyword", "q")`, written inline in the table via one `_alias()` factory — zero wrapper functions |
| 4 | dispatch()/transform control flow | Two near-identical `dispatch()` methods (path lookup → conditional query remap → `call_next` → conditional response transform) + two response-transform methods (`_transform_response` vs `_unwrap_items`) with the same shape but different mutation logic | One `dispatch()`, one `_transform_response()` — `response_unwrap_key` (already used by Strategy's `equityCurve`) is the same primitive every Watchlist path needed; `_unwrap_items` disappears entirely, not ported |
| 5 | Middleware registration | Two `app.add_middleware()` calls, two `from api.compat import ...` names | One of each |

Categories already fully solved before this refactor (confirmed unchanged, not touched here): shared response rebuild (`_rebuild_response`), FastAPI-matching JSON serialization (`_dumps_like_fastapi`), header preservation (`raw_headers`, order-insensitive multiset, duplicates like multiple `Set-Cookie` survive), content-type preservation (verbatim header carry-over, `media_type` never re-derived).

## Helper inventory (`api/compat.py`)

Unchanged, shared by `CompatMiddleware` (all pre-date this refactor):

| Helper | Purpose |
|---|---|
| `_rebuild_response(response, body)` | Rebuild a `Response` with a new body while preserving every original header via `raw_headers` (duplicates survive) and recomputing only `Content-Length` |
| `_dumps_like_fastapi(payload)` | Serialize matching `fastapi.responses.JSONResponse.render()` exactly (`ensure_ascii=False`, `allow_nan=False`, compact separators) |
| `_read_query_params(request)` | Parse the query string into a list of `(key, value)` tuples (not a dict — preserves duplicates) |
| `_write_query_params(request, params)` | Write a param list back onto `request.scope["query_string"]` |
| `_alias_query_param(request, old_key, new_key)` | Inject `new_key=<value>` from the *last* occurrence of `old_key` if `new_key` is absent — additive, last-value-wins to match Starlette's own `QueryParams.get()` |
| `_read_json_body(response)` (async) | Consume `body_iterator` once, JSON-decode with a `(body, None)` fallback on failure/non-dict |

New in this refactor:

| Name | Purpose |
|---|---|
| `_PathConfig` (`NamedTuple`) | Unified per-path config: `query_remap: Optional[Callable[[Request], None]]`, `response_key_add: Optional[Tuple[str, str]]`, `response_unwrap_key: Optional[str]` |
| `_alias(old_key, new_key)` | Factory returning a `query_remap` callable for the common case — a pure rename. A genuinely custom transform (`_watchlist_remap_prices_symbols`) is still passed directly, unchanged |
| `_PATH_CONFIG` | The single 11-entry table, both resource families |
| `CompatMiddleware` | The unified `BaseHTTPMiddleware` subclass |

Kept unchanged, not part of this refactor's scope: `CompatLoginRequest`, `CompatCredentialCreate` (Phase 1A — a different mechanism, pydantic body-model swap, not middleware), `_watchlist_remap_prices_symbols` (still referenced from `_PATH_CONFIG` — genuinely custom, JSON-parsing, not a simple rename).

## Extension guide

To cover a new `GET` + bare-`Query(...)` path (e.g. a future `quickTradeApi.getBalance`):

1. Add one entry to `_PATH_CONFIG` in `api/compat.py`:
   - Simple query rename → `query_remap=_alias("old_name", "new_name")`.
   - Custom query transform → write a small `def _my_remap(request: Request) -> None:` function (see `_watchlist_remap_prices_symbols` for the pattern: read params, fail open on anything unparseable, write back only if you can derive a value) and pass it directly.
   - Response needs an additive key copy → `response_key_add=("old_key", "new_key")`.
   - Response needs `data` itself replaced by a nested array/value → `response_unwrap_key="key"`.
   - Never set both `response_key_add` and `response_unwrap_key` on the same entry.
2. No change to `CompatMiddleware`, `dispatch()`, or `_transform_response()` — the whole point of the unified table is that a new path is purely a data addition.
3. Add tests: a `TestTransformGolden`-style case in `api/tests/test_compat_unified.py` (hand-derive the expected output the same way the existing 11 do) plus a full-stack regression test alongside the resource's existing test file.

**Known limitation, not solved by this refactor:** `_PathConfig`'s response side has exactly two primitives (additive-copy, unwrap-replace). A path needing a genuinely different response reshape — e.g. `quickTradeApi.getPosition`'s singular-object-vs-plural-array mismatch — doesn't fit either and needs a new field (or a `response_transform: Optional[Callable]` escape hatch) added to `_PathConfig` first. Flagged as a future extension point per `docs/P5_COMPAT_REFACTOR_PLAN.md`'s own "Effect on Future Phases" section; deliberately not designed here, consistent with "do not add features."

## Rollback notes

`git revert` of this refactor's commit(s). Safe and sufficient because: the change is self-contained (add unified middleware + config, swap registration, delete old classes — nothing later in the codebase depends on the new shape), there is no persistence/schema/data-migration angle (stateless HTTP middleware only, no DB writes), and the full test suite (`api/tests/` — 68 pre-existing + `test_compat_unified.py`) gates the merge itself, so a bad consolidation would be caught before reaching `main`. No canary or gradual-rollout mechanism — not a fit for a correctness question this small a service can fully answer pre-merge, consistent with `docs/P5_COMPAT_REFACTOR_PLAN.md`'s own rollback rationale.

## Test coverage: 8-category map

| Category | Coverage |
|---|---|
| Credential compatibility | `api/tests/test_compat_credentials.py` (9 tests, unedited — `CompatCredentialCreate` is a different mechanism, untouched by this refactor) + `test_compat_unified.py::TestRegressionCategories::test_credential_create_unaffected_by_unified_middleware` (named traceability pointer) |
| Strategy compatibility | `api/tests/test_compat_strategy.py` (30 tests, unedited) + 7 of `TestTransformGolden`'s 11 golden-value cases |
| Watchlist compatibility | `api/tests/test_compat_watchlist.py` (22 tests, unedited) + 4 of `TestTransformGolden`'s 11 golden-value cases |
| Backward compatibility | `TestStrategyBackwardCompatibility` (3), `TestCredentialBackwardCompatibility` (1), `TestLoginBackwardCompatibility` (4), Watchlist's precedence tests + `TestTransformGolden`'s additive-key assertions (old shape untouched, new key only added) |
| Header preservation | Existing `test_cors_headers_survive_response_rebuild` / `test_content_type_survives_response_rebuild` (both strategy and watchlist files) + new `TestTransformEdgeCases::test_duplicate_headers_survive_as_order_insensitive_multiset` |
| Response equality | `TestTransformGolden`'s 11 hand-derived exact-match assertions |
| Query alias | `TestStrategyRequestMapping` (11) + `TestWatchlistRequestMapping` (11), unedited — `_alias`/`_alias_query_param` behavior is byte-identical to before |
| Array unwrap | `test_equity_curve_returns_bare_array` + `TestWatchlistResponseMapping` (4) + `TestTransformGolden`'s unwrap-key cases |

## Files changed

- `api/compat.py` — modified, net shrink (`git diff --stat`: 223 changed lines, 95 insertions / 144 deletions)
- `api/main.py` — modified, ~16 lines (one import, one `add_middleware` call replacing two)
- `api/tests/test_compat_unified.py` — new (17 tests: 11 golden-value + 1 table-size guard + 4 edge cases + 1 credential traceability)
- `docs/P5_COMPAT_PHASE1B.md`, `docs/P5_COMPAT_PHASE1C.md` — one-line "superseded by" pointers added
- `docs/P5_COMPAT_REFACTOR_PLAN.md` — implementation note added explaining the single-PR/golden-value deviation from its originally-proposed 2-PR plan
- `docs/P5_COMPAT_REFACTOR.md` — this document (new)

**Zero changes** to any `api/routers/*.py` file, any existing test file (`test_compat_login.py`, `test_compat_credentials.py`, `test_compat_strategy.py`, `test_compat_watchlist.py`), business logic, execution, risk, database, API URLs, or frontend.
