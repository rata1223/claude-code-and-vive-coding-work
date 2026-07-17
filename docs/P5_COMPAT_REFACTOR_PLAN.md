# P5-02D: Compat Middleware Consolidation — Design

> **Analysis and design only. No code was changed to produce this document.** It designs collapsing the two existing compatibility middleware classes into one, with the explicit goal of zero runtime behavior change — verification of that claim is left to the implementation phase this document scopes, not performed here.

**Date:** 2026-07-16

---

## Context

Phase 1 of the P5 compatibility-adapter workstream is complete and merged: Phase 1A (auth/credentials, PR #126 — pydantic alias models, not middleware), Phase 1B (strategy, PR #128), Phase 1C (watchlist, PR #129). Phase 1B and 1C each independently introduced a `BaseHTTPMiddleware` subclass in `api/compat.py`, because their endpoints take bare `Query(...)` params with no body-model type annotation to swap (Phase 1A's mechanism doesn't apply to them). `docs/P5_COMPAT_PHASE1C.md`'s own "Recommended Phase 2" section — written during Phase 1C, before this task was requested — already names this exact problem: this is now the *second* near-identical middleware class, `klineApi`/`quickTradeApi` are named as likely next candidates, and it recommends consolidating into one `CompatMiddleware` before a third copy gets added. This document is that consolidation design.

**Premise correction, verified before designing anything:** the task brief that requested this document names a `CredentialCompatMiddleware` as if it exists. It does not — confirmed via a full-repo grep (`CredentialCompat` returns zero hits anywhere in code, tests, or docs). Credential translation (Phase 1A) is `CompatCredentialCreate`, a plain pydantic `BaseModel` swapped in as `api/routers/credentials.py`'s body-parameter type — a different mechanism entirely, out of scope for this middleware consolidation. **Only two `*CompatMiddleware` classes exist in the entire repository**: `StrategyCompatMiddleware` and `WatchlistCompatMiddleware`, both in `api/compat.py`. This is stated explicitly here so it isn't rediscovered confused later.

---

## Current middleware graph

```text
Registration order (api/main.py):
  1. CORSMiddleware             (line 63)
  2. StrategyCompatMiddleware   (line 74)
  3. WatchlistCompatMiddleware  (line 79)

Starlette applies middleware in REVERSE registration order (last-added = outermost).
Actual per-request execution order:

  Request  ──▶ WatchlistCompatMiddleware
           ──▶ StrategyCompatMiddleware
           ──▶ CORSMiddleware
           ──▶ ExceptionMiddleware
           ──▶ Router (api/routers/*.py — untouched by any compat layer)
  Response ◀── (unwinds in reverse)
```

**Path coverage is completely disjoint today:**

| Middleware | Paths covered | Count |
|---|---|---|
| `StrategyCompatMiddleware` | `/api/strategies*` | 7 |
| `WatchlistCompatMiddleware` | `/api/market/*` | 4 |

Neither middleware touches the other's paths, or any of the ~55+ other routes in the app. Each middleware's `dispatch()` does a cheap `dict.get(path)` no-op for everything it doesn't cover — but the `BaseHTTPMiddleware` layer itself (Starlette's anyio stream handoff to bridge the downstream ASGI call into a `Request`/`Response`) still runs for **every request regardless of path**, and this overhead compounds linearly with each additional middleware layer registered. This is the concrete cost the consolidation removes.

---

## Duplicate responsibilities

The task named 6 mandatory categories to check. Three are already fully solved (from Phase 1C's own code-review pass); three still have real duplication.

### 1. Duplicated request handling — partially fixed
`_read_query_params`/`_write_query_params`/`_alias_query_param` (module-level, `api/compat.py:68-104`) are already shared by both classes. What remains duplicated is at the **control-flow level**, not the primitive level: both classes' `dispatch()` methods are near-identical (path lookup → conditional query remap → `call_next` → conditional response transform), just operating on two different tables and calling two differently-shaped per-class transform methods.

### 2. Duplicated response handling — partially fixed
`_read_json_body` (decode + fallback, `api/compat.py:107-121`) and `_rebuild_response`/`_dumps_like_fastapi` (rebuild + serialize, `api/compat.py:33-65`) are already shared. What remains duplicated: `StrategyCompatMiddleware._transform_response` (`api/compat.py:230`) and `WatchlistCompatMiddleware._unwrap_items` (`api/compat.py:351`) are still two separate methods with the same overall shape (decode → check dict → mutate → track changed → rebuild-or-passthrough) but different mutation logic.

### 3. Duplicated query alias logic — partially fixed
`_alias_query_param` is shared, but each class still carries its own thin one-line wrapper around it purely to satisfy that class's own config-table shape: `StrategyCompatMiddleware._remap_query_string` (`api/compat.py:226-228`, hardcoded to `id`→`strategy_id`) and `_watchlist_remap_search_keyword` (`api/compat.py:260-269`, hardcoded to `keyword`→`q`). The wrapper-function *pattern* — one hand-written function per simple rename — is itself the remaining duplication.

### 4. Duplicated header preservation — **fully solved already**
`_rebuild_response` is the single shared implementation, extracted during Phase 1C. No further work needed. Included here only because the task's checklist asks for it explicitly.

### 5. Duplicated content-type handling — **fully solved already**
Same helper: `_rebuild_response` deliberately omits `media_type` on the rebuilt `Response` (working around `BaseHTTPMiddleware.call_next()` always returning `media_type=None` regardless of the real response) and carries the original `Content-Type` header over verbatim via `raw_headers`. No further work needed.

### 6. Duplicated array-unwrap logic — **the most direct hit, genuinely duplicated, not yet fixed**
`_StrategyPathConfig.response_unwrap_key` (a configurable per-path field, used today only by `equityCurve`) and `WatchlistCompatMiddleware._unwrap_items` (the identical operation — `data = data[key]` — hardcoded in the middleware body, applied unconditionally to every one of its 4 paths) are **the same primitive implemented twice**: once as generic per-path config, once as a fixed method. Concretely — and this is the strongest evidence the unification below is lossless rather than merely plausible — all 4 Watchlist entries would populate `response_unwrap_key="items"` if expressed in Strategy's existing config shape.

---

## Proposed architecture

**One `CompatMiddleware(BaseHTTPMiddleware)` class, one `_PathConfig` NamedTuple, one `_PATH_CONFIG: Dict[str, _PathConfig]` table spanning all 11 current entries, registered once.**

### Alternatives considered and rejected

| Alternative | Why rejected |
|---|---|
| `Depends()`-based FastAPI dependency injection | Can resolve query aliasing *if* dependency resolution reliably runs before `Query(...)` extraction (an implementation detail, not a contract) — but **cannot** rewrite the response body after the handler returns without a custom `route_class`, which must be set at router-definition time. That means editing `api/routers/*.py`, the one thing this whole workstream is forbidden from doing. Dead end for the response half. |
| Decorator-based (`@compat_route`) | A decorator has to sit directly above `@router.get(...)` to intercept anything — same router-file-editing problem. Retrofitting via post-hoc monkeypatching could handle the response side, but still needs *some* ASGI-level piece for the query half (by the time a wrapped endpoint function runs, `Query(...)` has already resolved its default). Two mechanisms instead of one — not a reduction. |
| Shared base class, two subclasses still registered separately | Deduplicates *code* but not the *middleware layer* — `app.add_middleware()` is still called twice, so the exact per-request ASGI overhead this consolidation exists to remove stays exactly where it was. Doesn't achieve the stated goal. |

None of these achieve layer-reduction while respecting the "never edit router files" constraint that has governed every phase of this workstream so far. Only the single-class merge does both.

### Pipeline framing: reconciling the requested 4-stage diagram against the actual code

The requested framing is `Route → Request Mapping → Query Mapping → Response Mapping → Return`. The existing code — and the existing test-class names, `TestStrategyRequestMapping`/`TestWatchlistRequestMapping` in `api/tests/` — already use **"Request Mapping" to mean the query-string rewrite**. There is no second, distinct request-side transform in this middleware to justify a separate "Query Mapping" stage; a genuine body-mapping stage exists (Phase 1A's pydantic alias models), but it's a different mechanism, applied at route-definition time, not something this middleware does or could do.

This document presents **3 real stages**, not 4:

```text
Route
  ↓
Request Mapping   (query-string rewrite — what the code and existing
  ↓                tests already call this; "Query Mapping" would be
Response Mapping   the same thing under a second name)
  ↓
Return
```

If a future phase introduces a genuinely distinct request-side transform (not query-string rewriting — e.g. something that needs to inspect and rewrite a request body before a `Query(...)`/body-model resolves it), split this into two stages *then*, with a real second thing for "Query Mapping" to mean. Not speculatively now.

### `_PathConfig` design

```python
class _PathConfig(NamedTuple):
    query_remap: Optional[Callable[[Request], None]] = None
    response_key_add: Optional[Tuple[str, str]] = None
    response_unwrap_key: Optional[str] = None


def _alias(old_key: str, new_key: str) -> Callable[[Request], None]:
    """Factory for the common case: a query_remap that's a pure rename.
    Answers docs/P5_COMPAT_PHASE1C.md's open question ("does a custom
    transform deserve a distinct config slot from simple key-renames?"):
    no separate slot. Every query_remap is Callable[[Request], None],
    adopting Watchlist's existing (more general) shape universally. A
    plain rename is expressed inline via this factory; a genuinely
    custom transform (the JSON-parsing prices alias) is passed directly,
    unchanged.
    """
    return lambda request: _alias_query_param(request, old_key, new_key)
```

**Query side fully collapses to one shape** (`Optional[Callable]`) — this is adopting Watchlist's already-more-general shape universally, not inventing a new one. Strategy's `query_remap: bool` + hardcoded `_remap_query_string` staticmethod, and Watchlist's own `_watchlist_remap_search_keyword` one-liner, both collapse into `_alias("id", "strategy_id")` / `_alias("keyword", "q")` written inline in the table — net code reduction.

**Response side is deliberately left as two typed fields**, not also collapsed to a bare callable. Reasoning, stated explicitly since this is the one place a symmetric design was considered and rejected:
- All 11 existing entries fit these two primitives losslessly (table below).
- The table's value as a readable, diffable single source of truth — a property the original Phase 1B docstring calls out explicitly — is worth preserving for the common case.
- The one response shape that doesn't fit today — `quickTradeApi.getPosition`'s singular-object-vs-plural-array mismatch (`docs/P5_API_MAPPING_AUDIT.md` row 19: frontend does `unwrapItems(res.data, 'positions')` expecting an array, backend returns `{"symbol": ..., "position": pos_or_null}`, a singular object-or-null) — belongs to an **unscoped future phase**. Adding a third response primitive (or a callable escape hatch) speculatively, for a row that isn't part of this consolidation, would itself be a feature addition — which this task's "do not add features" instruction rules out. It's named here as a flagged extension point, not designed.

**The `if request.method == "GET"` gate is kept exactly as today**, pre-filtering before the dict lookup rather than keying `_PATH_CONFIG` by `(method, path)`. Currently redundant — no `POST` route shares an exact path string with a configured `GET` one, checked against every route in both `api/routers/strategies.py` and `api/routers/watchlist.py` — but cheap, defensive, and not worth removing as part of a change whose entire mandate is "no behavior change."

### Mapping table design — all 11 entries, lossless translation

```python
_PATH_CONFIG: Dict[str, _PathConfig] = {
    # ── Strategy (Phase 1B) ──
    "/api/strategies": _PathConfig(
        response_key_add=("items", "strategies")),
    "/api/strategies/trades": _PathConfig(
        query_remap=_alias("id", "strategy_id"),
        response_key_add=("items", "trades")),
    "/api/strategies/positions": _PathConfig(
        query_remap=_alias("id", "strategy_id"),
        response_key_add=("items", "positions")),
    "/api/strategies/equityCurve": _PathConfig(
        query_remap=_alias("id", "strategy_id"),
        response_unwrap_key="items"),
    "/api/strategies/performance": _PathConfig(
        query_remap=_alias("id", "strategy_id")),
    "/api/strategies/logs": _PathConfig(
        query_remap=_alias("id", "strategy_id"),
        response_key_add=("items", "logs")),
    "/api/strategies/notifications/unread-count": _PathConfig(
        response_key_add=("count", "unread")),

    # ── Watchlist (Phase 1C) ──
    # Every entry uses response_unwrap_key="items" — the same primitive
    # Strategy's equityCurve already exercises, not a new one (see
    # Duplicate Responsibilities §6 above).
    "/api/market/watchlist/get": _PathConfig(
        response_unwrap_key="items"),
    "/api/market/symbols/search": _PathConfig(
        query_remap=_alias("keyword", "q"),
        response_unwrap_key="items"),
    "/api/market/symbols/hot": _PathConfig(
        response_unwrap_key="items"),
    "/api/market/watchlist/prices": _PathConfig(
        query_remap=_watchlist_remap_prices_symbols,  # kept verbatim — genuinely custom logic, already Callable[[Request], None]
        response_unwrap_key="items"),
}
```

`dispatch()` and the response-transform method become **exactly `StrategyCompatMiddleware`'s existing, already-tested code, verbatim** — not new logic written to also handle Watchlist:

```python
class CompatMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        config = _PATH_CONFIG.get(path) if request.method == "GET" else None
        if config and config.query_remap:
            config.query_remap(request)
        response = await call_next(request)
        if config and response.status_code == 200:
            response = await self._transform_response(config, response)
        return response

    @staticmethod
    async def _transform_response(config: "_PathConfig", response: Response) -> Response:
        # identical body to today's StrategyCompatMiddleware._transform_response
        ...
```

`WatchlistCompatMiddleware._unwrap_items` disappears entirely rather than being ported — its behavior was never a different mechanism, just the same `response_unwrap_key` primitive applied to every one of its paths instead of the one Strategy path that used it.

### Effect on named future phases (klineApi, quickTradeApi)

Stated honestly rather than oversold — the effect is mixed, not uniformly positive:

- `quickTradeApi.getBalance`/`getPosition`/`getHistory` are `GET` endpoints with bare `Query(...)` params (`credential_id`, `market`, `symbol`), structurally identical to today's Strategy/Watchlist shape. The unified table makes these cheaper to add: `market_type`→`market` is just another `_alias(...)` call, `getHistory`'s `items`→`trades` is just another `response_key_add` entry.
- `placeOrder` and `closePosition` are **`POST` endpoints that already take a Pydantic body model** (`PlaceOrderRequest`, `ClosePositionRequest` in `api/routers/quick_trade.py`) — their mismatches (`amount` vs `qty`, missing required fields) are **Phase 1A's mechanism**, not this middleware's, and out of scope for it regardless of whether it's one class or two. `docs/P5_ADAPTER_PLAN.md` independently classifies `placeOrder` as not adapter-fixable this way at all (a genuine frontend/backend paradigm mismatch) and `closePosition` as needing server-side stateful auto-resolution (reading the user's live position from the DB to synthesize missing `qty`/`price`) — something a stateless ASGI middleware doesn't have easy access to and arguably shouldn't try to acquire.
- `getPosition`'s singular-vs-plural response mismatch is the one genuinely new transform shape surfaced by looking ahead — already flagged above as an explicitly deferred extension point, not solved here.

Net: consolidation is a clear win for the straightforward `GET` rows a future phase will actually need, neutral for the two `POST` rows that were never going to fit this middleware in any form, and honestly surfaces (without forcing a premature answer to) the one row that will need the config schema to grow later.

---

## Migration order

No feature-flag system exists in this repository, and none should be built for this. This is a single small FastAPI service behind branch-protection + CI, and "is the new middleware's output identical to the old one's" is a pre-merge correctness question answerable completely by tests — not a partial-rollout observability question that benefits from watching production traffic.

**Two separate PRs, not one PR with an internal delete-in-the-same-commit step** (revised from an earlier draft of this plan after review: deleting the differential test in the same commit that also deletes the old classes means that commit's own CI run never actually executes the comparison it's meant to gate — the proof and the thing being proven can't coexist in the state being merged. Splitting into two PRs makes the differential test's green run a real, independently-merged checkpoint instead of an artifact that vanishes before it can gate anything.)

**PR 1 — additive, proves equivalence, merges on its own:**
1. Add `CompatMiddleware` / `_PathConfig` / `_alias` / `_PATH_CONFIG` to `api/compat.py` *alongside* the two existing classes — not yet wired into `api/main.py`. Reuses all 6 existing shared helpers (`_rebuild_response`, `_dumps_like_fastapi`, `_read_query_params`, `_write_query_params`, `_alias_query_param`, `_read_json_body`) unchanged.
2. Add a differential equivalence test in the same PR. Build two in-process apps: one with the current two-middleware registration, one with the new single-middleware registration. Replay a table-driven battery derived directly from the 11 `_PATH_CONFIG` keys — happy path, the null-`data` business-error path (e.g. "Strategy not found"), a duplicate-query-key path, and at least one path neither middleware ever touched (e.g. `/health`, `/api/auth/login`) — asserting identical `status_code`, response body bytes, and headers as an **order-insensitive multiset of `(lowercase(name), value)` pairs**, preserving duplicate headers (e.g. multiple `Set-Cookie`) as separate pairs rather than collapsing them. Comparing values, not just names, is load-bearing: two responses with identical header *names* but different `Content-Type`/`Content-Length`/CORS *values* must not compare as equal, or the test proves nothing.
3. **This PR's own CI run is the real gate** — merging it to `main` with a green differential test is the actual proof of equivalence, on the actual code that will ship, not on an intermediate state that gets discarded in the same commit. Both the old and new classes stay live in `main` after this merges (the new `CompatMiddleware` unused/unwired); this is intentionally a no-behavior-change PR by itself, verifiable by the existing 68 tests passing unchanged (`api/tests/conftest.py`'s `client` fixture builds `TestClient(app)` from the real `api.main.app`, so those tests are already exercising the shipped code, not a mock).

**PR 2 — small cutover, merges only after PR 1 is green on `main`:**
4. Swap `api/main.py`'s two `add_middleware(...)` calls for one. Delete `StrategyCompatMiddleware`, `WatchlistCompatMiddleware`, `_StrategyPathConfig`, `_WatchlistPathConfig`, `_STRATEGY_PATH_CONFIG`, `_WATCHLIST_PATH_CONFIG`, the now-inlined one-line wrapper functions (`_remap_query_string`, `_watchlist_remap_search_keyword`), and the differential test from PR 1 (it can no longer run once the classes it compares are gone — that's expected, not a gap, since PR 1 already merged with it green). The existing 68 tests need zero edits and are what actually gates *this* PR's merge — by design, they're the oracle that proves the real, final wiring still behaves correctly, which the differential test (by its nature) cannot do once one side of its comparison is deleted.
5. **Keep `api/tests/test_compat_strategy.py` and `api/tests/test_compat_watchlist.py` as two separate files** even after the classes merge. They need different DB fixtures (Strategy needs `Trade`/`StrategyLog`/`Notification` rows; Watchlist doesn't) — merging the test files would be an unrelated, riskier restructure bundled into a change whose entire point is "no behavior change."
6. **`docs/P5_COMPAT_PHASE1B.md` and `docs/P5_COMPAT_PHASE1C.md` get a one-line "superseded by `P5_COMPAT_REFACTOR_PLAN.md`" pointer added, not deleted.** They remain the correct historical record of *why* `query_remap` started life as two differently-shaped fields (`bool` vs `Callable`) across two authors — that context is worth keeping, not erasing.

---

## Rollback strategy

**`git revert` of PR 2 (the small cutover PR, step 4 above).** This is clean and sufficient because:
- PR 2 is self-contained: add + delete + re-register, with nothing in a later commit depending on the new shape. Reverting it restores the two old classes and their registration exactly as they were when PR 1 merged — a state already proven correct by the existing 68 tests (which never stopped running) and, at the time PR 1 merged, by the differential test too.
- There is no persistence, schema, or data-migration angle whatsoever — this is stateless HTTP middleware operating only on the in-flight request/response.
- CI gates each PR's merge independently (branch protection requires a green run before merge to `main`), so a bad PR 2 is caught before it ever reaches `main`, let alone the deployed environment — the existing deploy pipeline (`.github/workflows/deploy.yml`) is itself gated on tests per this repo's standing safety practice.
- If PR 1 itself needs to be rolled back (unlikely, since it changes nothing about what's wired), reverting it is equally clean — it's purely additive, nothing depends on the new unwired classes existing.

No canary or gradual-traffic-split mechanism is proposed, and none is recommended if asked for one later: it doesn't reduce risk for the specific question this change poses ("is the output identical"), which is fully answerable pre-merge by PR 1's differential test plus the existing suite that gates both PRs.

---

## Settled decisions worth calling out explicitly

- **Header-comparison semantics for the differential test are fixed, not open**: an order-insensitive multiset of `(lowercase(name), value)` pairs, preserving duplicates. This is stated in the Migration order section above (PR 1, step 2) as a requirement, not a recommendation to revisit — comparing names alone would treat responses with different header values as equivalent, which would make the test pass on real regressions.

## Open questions / future extension points (flagged, not resolved here)

- **Whether to add a startup-time assertion** that no `_PATH_CONFIG` entry sets both `response_key_add` and `response_unwrap_key` (today this invariant is enforced only by a docstring comment: "a path never sets both"). Cheap, zero per-request cost, and arguably worth doing once the table doubles in size and gains a second author's entries — but it is a small *new* behavior, not purely mechanical consolidation, so it's named as optional/recommended here rather than assumed in scope, consistent with this task's "do not add features" constraint.
- **The `getPosition`-style singular→list response primitive**, surfaced above under "Effect on named future phases." Explicitly deferred to whichever phase actually scopes `quickTradeApi` — not designed here, per the same "do not add features" constraint, and per `docs/P5_COMPAT_PHASE1C.md`'s own request that this kind of question be "settled before that phase, not during it."

---

## Files referenced (read, not modified, in producing this design)

- `api/compat.py` — both existing middleware classes, all 6 shared helpers, both existing config tables.
- `api/main.py` — middleware registration order and imports.
- `api/tests/test_compat_strategy.py`, `api/tests/test_compat_watchlist.py`, `api/tests/conftest.py` — confirmed the existing suite already exercises the real wired app and requires no changes at cutover.
- `api/routers/quick_trade.py`, `docs/P5_API_MAPPING_AUDIT.md`, `docs/P5_ADAPTER_PLAN.md` — confirmed the `placeOrder`/`closePosition`/`getPosition` details cited under "Effect on named future phases."
- `docs/P5_COMPAT_PHASE1B.md`, `docs/P5_COMPAT_PHASE1C.md` — prior-phase context and the pre-existing "Recommended Phase 2" note this task originates from.
