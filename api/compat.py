"""Frontend/backend request compatibility adapter (P5-02A/B/C, unified P5-02E,
quick-trade coverage added P5-03B).

These models are attribute-compatible with the existing backend schemas
(``LoginRequest``, ``CredentialCreate`` in ``api/schemas.py``) — same field
names an existing route handler reads (``body.email``, ``body.app_key``,
etc.) — but also accept the frontend's actual wire field names via pydantic
``validation_alias``. Swapping a route's body parameter type to one of these
models therefore requires no change to the route handler itself: only the
type annotation changes.

``CompatMiddleware`` covers strategy sub-resource, watchlist/symbol-search,
and quick-trade (Orders) endpoints that take their identifying/query
parameters as bare ``Query(...)`` arguments (or, for quick-trade's
``place-order``, a POST body whose field names differ from the frontend's)
rather than a Pydantic body model already shaped to match — there's often no
type annotation to swap, so translation happens as HTTP middleware instead:
it rewrites the query string and/or JSON request body before FastAPI/Pydantic
resolves route parameters, and reshapes the JSON response body afterwards,
driven by two path-keyed tables (``_PATH_CONFIG`` for strategy/watchlist,
``_ORDERS_PATH_CONFIG`` for quick-trade — kept separate so each family's own
regression/drift-guard tests stay independent). ``api/routers/strategies.py``,
``api/routers/watchlist.py``, and ``api/routers/quick_trade.py`` are not
modified at all.

Scope: request/response translation only. No authentication or business
logic lives here.
"""
import json
from typing import Awaitable, Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import parse_qsl, urlencode

from pydantic import AliasChoices, BaseModel, EmailStr, Field, model_validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


def _rebuild_response(response: Response, body: bytes) -> Response:
    """Construct a fresh Response carrying `body` — unavoidable once
    `response.body_iterator` has been consumed to inspect it — while
    preserving every original header via `raw_headers` (a list, so
    duplicate headers like multiple Set-Cookie survive) rather than
    collapsing them through a `dict(...)`, which would silently drop
    anything but the last value for a repeated header name.

    `response.media_type` is unusable here: `BaseHTTPMiddleware`'s
    `call_next()` hands back a streaming wrapper with `media_type=None`
    regardless of what the underlying route actually returned, so passing
    it to `Response(...)` would silently drop Content-Type instead of
    preserving it. The original Content-Type header is carried over
    verbatim via `raw_headers` instead; only Content-Length is recomputed,
    since `body` may be a different length than the original.

    Used by `CompatMiddleware` for every path in `_PATH_CONFIG` — the
    response-rebuild mechanics are identical regardless of which path is
    being reshaped.
    """
    new_response = Response(content=body, status_code=response.status_code)
    preserved = [(k, v) for k, v in response.raw_headers if k.lower() != b"content-length"]
    new_response.raw_headers = new_response.raw_headers + preserved
    return new_response


def _dumps_like_fastapi(payload: object) -> bytes:
    """Match FastAPI's own JSONResponse.render() formatting exactly
    (ensure_ascii=False, allow_nan=False, compact separators) — otherwise
    Korean/non-ASCII text would round-trip through a compat middleware
    re-encoded as \\uXXXX escapes instead of raw UTF-8, and gain extra
    whitespace compared to the rest of the API."""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _read_query_params(request: Request) -> List[Tuple[str, str]]:
    """Parse the request's query string into a list of (key, value) tuples.
    Kept as a list rather than a dict — collapsing repeated keys into a dict
    would silently drop all but the last value for any repeated param name,
    whether that's the key a caller is about to alias or an unrelated
    sibling sharing the same query string (e.g. `?tag=a&tag=b&id=5`).
    `keep_blank_values=True` matches Starlette's own QueryParams parsing
    (see starlette.datastructures.QueryParams) — without it, a blank-valued
    param (e.g. `?id=1&level=`) would be silently dropped by a rewrite,
    not just left blank.

    Used by `CompatMiddleware` for the same reason `_rebuild_response` is:
    the parse/rewrite mechanics are identical regardless of which params
    a given path aliases.
    """
    return parse_qsl(request.scope["query_string"].decode("latin-1"), keep_blank_values=True)


def _write_query_params(request: Request, params: List[Tuple[str, str]]) -> None:
    request.scope["query_string"] = urlencode(params).encode("latin-1")


def _alias_query_param(request: Request, old_key: str, new_key: str) -> None:
    """If `new_key` is absent and `old_key` is present, inject
    `new_key=<value>` alongside the existing params — additive, `old_key`
    stays untouched (the backend simply ignores whatever query key it
    doesn't recognize). Uses the *last* occurrence of `old_key` when it's
    repeated, matching Starlette's `QueryParams.get()` semantics for
    duplicate keys — the native path (a client sending `new_key` directly)
    and this aliased path must resolve a repeated param to the same value,
    not silently disagree with each other.
    """
    params = _read_query_params(request)
    keys = {key for key, _ in params}
    if new_key not in keys and old_key in keys:
        value = next(v for k, v in reversed(params) if k == old_key)
        _write_query_params(request, params + [(new_key, value)])


async def _read_json_body(response: Response) -> Tuple[bytes, Optional[dict]]:
    """Consume `response.body_iterator` once — unavoidable to inspect the
    body — and JSON-decode it. Returns `(body, None)` on decode failure or
    when the payload isn't a dict; callers should fall back to
    `_rebuild_response(response, body)` unchanged in that case, exactly as
    if no transform applied. Shared so each compat middleware's response
    transform only has to express what it does differently, not how to
    safely get there.
    """
    body = b"".join([chunk async for chunk in response.body_iterator])
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, None
    return body, payload if isinstance(payload, dict) else None


class CompatLoginRequest(BaseModel):
    """POST /api/auth/login — accepts either `email` (existing/canonical) or
    `username` (the frontend's actual field name, which holds an email
    address in practice)."""

    email: EmailStr = Field(validation_alias=AliasChoices("email", "username"))
    password: str
    # Accepted for wire compatibility with the frontend payload; not validated
    # anywhere in the backend today (see docs/P5_API_MAPPING_AUDIT.md).
    turnstile_token: Optional[str] = None


class CompatCredentialCreate(BaseModel):
    """POST /api/credentials/create — accepts the frontend's field names
    (`api_key`, `secret_key`, `enable_demo_trading`) and maps them onto the
    backend's existing columns (`app_key`, `app_secret`, `env`)."""

    name: str
    exchange_id: str
    app_key: Optional[str] = Field(default=None, validation_alias=AliasChoices("app_key", "api_key"))
    app_secret: Optional[str] = Field(default=None, validation_alias=AliasChoices("app_secret", "secret_key"))
    account_no: Optional[str] = None
    hts_id: Optional[str] = None
    env: str = "paper"
    # Frontend-only toggle used solely to derive `env` below; not a real column.
    enable_demo_trading: Optional[bool] = None

    @property
    def api_key(self) -> None:
        """Distinct legacy backend field, unrelated to the frontend's
        "api_key" (that value is routed to `app_key` above). A read-only
        property rather than a declared field: a declared field would bind
        from its own name by default, silently re-absorbing the incoming
        "api_key" value and duplicating the app key into api_key_enc
        alongside app_key_enc. A property can't be bound from input at all,
        so the collision is structurally impossible rather than defended
        against — and it no longer appears as a phantom parameter in the
        generated OpenAPI schema."""
        return None

    @model_validator(mode="after")
    def _derive_env(self) -> "CompatCredentialCreate":
        if self.enable_demo_trading is not None:
            self.env = "paper" if self.enable_demo_trading else "real"
        return self


# ── P5-02E: query-alias helpers referenced from _PATH_CONFIG below ──────────


def _watchlist_remap_prices_symbols(request: Request) -> None:
    """`GET /api/market/watchlist/prices` — the frontend sends one
    JSON-encoded `watchlist` param (`[{symbol, market}, ...]`), the backend
    requires `symbols` as a comma-separated string (no default — a 422 on
    every call without this alias). Only `symbols` is derived: `market` is
    accepted by the backend but never actually read inside
    `get_watchlist_prices` (yfinance resolves purely by symbol) and already
    defaults to `"us"` on its own, so forwarding a synthesized value here
    would be unused complexity dressed up as forward-compatibility (found
    during /code-review — an earlier version of this function did forward
    the first item's `market`). If the backend ever starts using `market`
    per symbol, a single collapsed value wouldn't be the right fix anyway —
    see the remaining-gaps section of docs/P5_COMPAT_PHASE1C.md.

    Fails open: if `watchlist` is missing, isn't valid JSON, isn't a list,
    or yields no symbols, the query string is left untouched and the
    request 422s exactly as it already does today — this alias must not
    turn a 422 into a 500 inside the middleware itself.
    """
    params = _read_query_params(request)
    keys = {key for key, _ in params}
    if "symbols" in keys or "watchlist" not in keys:
        return

    raw = next(value for key, value in reversed(params) if key == "watchlist")
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return
    if not isinstance(items, list):
        return

    symbols = [str(it["symbol"]) for it in items if isinstance(it, dict) and it.get("symbol")]
    if not symbols:
        return

    _write_query_params(request, params + [("symbols", ",".join(symbols))])


# ── P5-02E: unified compat middleware, covering both strategy sub-resource
# and watchlist/symbol-search paths through one config table and one
# dispatch/response-transform control flow. See
# docs/P5_COMPAT_REFACTOR_PLAN.md for the full design rationale.


class _PathConfig(NamedTuple):
    """Per-path translation config, shared by every path this middleware
    covers regardless of resource family. `query_remap`, when set, is
    called to rewrite the request's query string before the route resolves
    params — a plain rename is expressed via `_alias()` below, a genuinely
    custom transform (e.g. `_watchlist_remap_prices_symbols`) is passed
    directly, unchanged.

    `response_key_add` is `(old_key, new_key)`: copies `data[old_key]` to
    `data[new_key]`, additive — `old_key` stays, so nothing reading the old
    shape today breaks. `response_unwrap_key`, when set, replaces `data`
    with `data[response_unwrap_key]` instead — the one non-additive case,
    needed because the frontend does `ensureArray(res.data)` for paths like
    equityCurve and every watchlist path: `data` itself must *be* the
    array, which can't coexist with also being a dict with a keyed array
    inside it. `response_transform`, when set, takes exclusive precedence
    over both — a generic escape hatch for response reshapes that aren't a
    rename (e.g. wrapping a singular value into a list; see
    `_quick_trade_position_to_array` in `_ORDERS_PATH_CONFIG` below). A
    path never sets more than one of `response_key_add`,
    `response_unwrap_key`, `response_transform`.

    `body_remap`, when set, is an async callable that rewrites the JSON
    request body before the route's Pydantic model parses it — the
    request-body counterpart to `query_remap` (see `_alias_body()` below).

    `methods` gates which HTTP method(s) this config applies to; defaults
    to `("GET",)` so every pre-P5-03B entry keeps its exact original
    behavior unchanged. POST-only entries (e.g. `place-order`) override
    this explicitly.
    """

    query_remap: Optional[Callable[[Request], None]] = None
    response_key_add: Optional[Tuple[str, str]] = None
    response_unwrap_key: Optional[str] = None
    body_remap: Optional[Callable[[Request], Awaitable[None]]] = None
    response_transform: Optional[Callable[[dict], Optional[dict]]] = None
    methods: Tuple[str, ...] = ("GET",)


def _alias(old_key: str, new_key: str) -> Callable[[Request], None]:
    """Factory for the common case — a `query_remap` that's a pure rename.
    A plain rename is expressed inline via this factory; a genuinely custom
    transform (like the JSON-parsing prices alias) is passed directly."""
    return lambda request: _alias_query_param(request, old_key, new_key)


async def _alias_body_fields(request: Request, pairs: Tuple[Tuple[str, str], ...]) -> None:
    """Fail-safe body field rename(s), applied before the route's Pydantic
    model parses/validates the body. Starlette's `Request.body()` caches
    the bytes on `request._body`; `BaseHTTPMiddleware`'s cached-request
    wrapper re-reads that attribute fresh on every `call_next` call rather
    than a snapshot taken earlier, so overwriting `request._body` here is
    what actually gets relayed to the route.

    If the body isn't valid JSON or isn't a JSON object, left byte-
    identical — the route's own validation error (422) surfaces exactly as
    it does today, same fail-open philosophy as
    `_watchlist_remap_prices_symbols`. Per `(old_key, new_key)` pair:
    additive — only injects `new_key` when it's absent and `old_key` is
    present; `old_key` stays, and a caller already sending `new_key`
    directly (native shape) is left alone.
    """
    body = await request.body()
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return

    changed = False
    for old_key, new_key in pairs:
        if new_key not in payload and old_key in payload:
            payload[new_key] = payload[old_key]
            changed = True

    if not changed:
        return
    try:
        request._body = _dumps_like_fastapi(payload)
    except ValueError:
        # `json.loads` accepts NaN/Infinity/-Infinity as a non-standard
        # Python extension, but `_dumps_like_fastapi`'s `allow_nan=False`
        # rejects them on re-encode. Leave the body untouched rather than
        # let this raise mid-middleware -- same fail-safe contract as an
        # undecodable body, just caught one step later.
        return


def _alias_body(*pairs: Tuple[str, str]) -> Callable[[Request], Awaitable[None]]:
    """Factory for the common case — a `body_remap` that's one or more pure
    field renames on the same JSON body, mirroring `_alias()` for query
    params."""

    async def remap(request: Request) -> None:
        await _alias_body_fields(request, pairs)

    return remap


# Single source of truth across both resource families — one entry per
# path, so there's no risk of the request-side and response-side tables
# drifting out of sync with each other, and no implicit precedence rule to
# trip over.
_PATH_CONFIG: Dict[str, _PathConfig] = {
    # Strategy (originally Phase 1B)
    "/api/strategies": _PathConfig(response_key_add=("items", "strategies")),
    "/api/strategies/trades": _PathConfig(query_remap=_alias("id", "strategy_id"), response_key_add=("items", "trades")),
    "/api/strategies/positions": _PathConfig(query_remap=_alias("id", "strategy_id"), response_key_add=("items", "positions")),
    "/api/strategies/equityCurve": _PathConfig(query_remap=_alias("id", "strategy_id"), response_unwrap_key="items"),
    "/api/strategies/performance": _PathConfig(query_remap=_alias("id", "strategy_id")),
    "/api/strategies/logs": _PathConfig(query_remap=_alias("id", "strategy_id"), response_key_add=("items", "logs")),
    "/api/strategies/notifications/unread-count": _PathConfig(response_key_add=("count", "unread")),
    # Watchlist (originally Phase 1C) — every entry uses
    # response_unwrap_key="items", the same primitive Strategy's
    # equityCurve already exercises, not a new one.
    "/api/market/watchlist/get": _PathConfig(response_unwrap_key="items"),
    "/api/market/symbols/search": _PathConfig(query_remap=_alias("keyword", "q"), response_unwrap_key="items"),
    "/api/market/symbols/hot": _PathConfig(response_unwrap_key="items"),
    "/api/market/watchlist/prices": _PathConfig(query_remap=_watchlist_remap_prices_symbols, response_unwrap_key="items"),
}


def _quick_trade_position_to_array(data: dict) -> Optional[dict]:
    """`GET /api/quick-trade/position` — backend returns
    `{"symbol", "position": dict | None}` (singular); the frontend does
    `unwrapItems(res.data, 'positions')`, expecting a plural array. Wraps
    `position` into a 0-or-1-element list under a NEW `positions` key —
    additive, `position` stays untouched. Returns `None` (no-op) if
    `position` isn't present at all (unexpected/future shape), matching
    `response_key_add`'s fail-safe contract when `old_key` is absent.
    """
    if "position" not in data:
        return None
    position = data["position"]
    data["positions"] = [] if position is None else [position]
    return data


def _quick_trade_balance_reshape(data: dict) -> Optional[dict]:
    """`GET /api/quick-trade/balance` — the frontend reads
    `balance.available`/`balance.total`; the backend returns
    `cash`/`total_eval`. Confirmed mapping (not inferred): `available` =
    cash on hand, `total` = full portfolio evaluation. Additive —
    `cash`/`total_eval` stay untouched.
    """
    if "cash" not in data or "total_eval" not in data:
        return None
    data["available"] = data["cash"]
    data["total"] = data["total_eval"]
    return data


# ── P5-03B: quick-trade (Orders) path config — kept as a separate table
# from _PATH_CONFIG (strategy/watchlist) rather than merged into it, so
# api/tests/test_compat_unified.py's existing drift guard
# (`set(_CANNED) == set(_PATH_CONFIG)`) stays untouched; this table gets
# its own drift guard in api/tests/test_compat_orders.py instead. See
# docs/P5_ORDERS_COMPAT_AUDIT.md for the full per-endpoint mapping
# rationale and docs/P5_ORDERS_COMPAT.md for what shipped.
#
# `close-position` is deliberately NOT covered here: the only "price"
# derivable from the backend's position data is a stale average purchase
# price, which the broker submits as an actual limit-order price — a
# live-trading pricing decision, not a DTO reshape, and out of this
# adapter's scope. See docs/P5_ORDERS_COMPAT.md's "remaining gaps".
_ORDERS_PATH_CONFIG: Dict[str, _PathConfig] = {
    "/api/quick-trade/balance": _PathConfig(
        query_remap=_alias("market_type", "market"),
        response_transform=_quick_trade_balance_reshape,
    ),
    "/api/quick-trade/position": _PathConfig(
        query_remap=_alias("market_type", "market"),
        response_transform=_quick_trade_position_to_array,
    ),
    "/api/quick-trade/place-order": _PathConfig(
        methods=("POST",),
        body_remap=_alias_body(("amount", "qty"), ("market_type", "market")),
    ),
    "/api/quick-trade/history": _PathConfig(response_key_add=("items", "trades")),
}


class CompatMiddleware(BaseHTTPMiddleware):
    """Translates strategy sub-resource, watchlist/symbol-search, and
    quick-trade (Orders) requests/responses between the frontend's expected
    shape and the backend's actual shape, without any change to
    `api/routers/strategies.py`, `api/routers/watchlist.py`, or
    `api/routers/quick_trade.py`. `add`/`remove` (watchlist) are already
    fully compatible (verified against `WatchlistAdd`/`WatchlistRemove` in
    `api/schemas.py`) and are not in `_PATH_CONFIG`, so this middleware
    never touches them; `close-position` (quick-trade) is deliberately not
    in `_ORDERS_PATH_CONFIG` either — see that table's docstring."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        config = _PATH_CONFIG.get(path) or _ORDERS_PATH_CONFIG.get(path)
        if config is not None and request.method not in config.methods:
            config = None

        if config and config.query_remap:
            config.query_remap(request)
        if config and config.body_remap:
            await config.body_remap(request)

        response = await call_next(request)

        if config and response.status_code == 200:
            response = await self._transform_response(config, response)

        return response

    @staticmethod
    async def _transform_response(config: "_PathConfig", response: Response) -> Response:
        body, payload = await _read_json_body(response)
        if payload is None:
            return _rebuild_response(response, body)

        data = payload.get("data")
        changed = False
        if isinstance(data, dict):
            if config.response_transform is not None:
                new_data = config.response_transform(data)
                if new_data is not None:
                    payload["data"] = new_data
                    changed = True
            elif config.response_unwrap_key is not None and config.response_unwrap_key in data:
                payload["data"] = data[config.response_unwrap_key]
                changed = True
            elif config.response_key_add is not None:
                old_key, new_key = config.response_key_add
                if old_key in data:
                    data[new_key] = data[old_key]
                    changed = True

        if not changed:
            # Nothing to add (e.g. a "Strategy not found" business error,
            # where `data` is null) — skip the JSON re-serialization entirely
            # rather than paying for it on every no-op response.
            return _rebuild_response(response, body)

        return _rebuild_response(response, _dumps_like_fastapi(payload))
