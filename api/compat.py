"""Frontend/backend request compatibility adapter (P5-02A/B, Phase 1).

These models are attribute-compatible with the existing backend schemas
(``LoginRequest``, ``CredentialCreate`` in ``api/schemas.py``) — same field
names an existing route handler reads (``body.email``, ``body.app_key``,
etc.) — but also accept the frontend's actual wire field names via pydantic
``validation_alias``. Swapping a route's body parameter type to one of these
models therefore requires no change to the route handler itself: only the
type annotation changes.

``StrategyCompatMiddleware`` (Phase 1B) and ``WatchlistCompatMiddleware``
(Phase 1C) cover sub-resource endpoints that take their identifying/query
parameters as bare ``Query(...)`` arguments rather than a Pydantic body
model — there's no type annotation to swap, so translation happens as HTTP
middleware instead: each rewrites the query string before FastAPI resolves
route parameters, and reshapes the JSON response body afterwards.
``api/routers/strategies.py`` and ``api/routers/watchlist.py`` are not
modified at all.

Scope: request/response translation only. No authentication or business
logic lives here.
"""
import json
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple
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

    Shared by every compat middleware in this module (Phase 1B, 1C, ...) —
    the response-rebuild mechanics are identical regardless of which paths
    a given middleware covers or how it reshapes the payload.
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

    Shared by every compat middleware in this module for the same reason
    `_rebuild_response` is: the parse/rewrite mechanics are identical
    regardless of which params a given middleware aliases.
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


# ── Phase 1B: strategy sub-resource compatibility (HTTP middleware) ─────────


class _StrategyPathConfig(NamedTuple):
    """Per-path Phase 1B translation config.

    `response_key_add` is `(old_key, new_key)`: copies `data[old_key]` to
    `data[new_key]`, additive — `old_key` stays, so nothing reading the old
    shape today breaks. `response_unwrap_key`, when set, replaces `data`
    with `data[response_unwrap_key]` instead — the one non-additive case,
    needed because the frontend does `ensureArray(res.data)` for
    equityCurve: `data` itself must *be* the array, which can't coexist
    with also being a dict with a keyed array inside it. A path never sets
    both `response_key_add` and `response_unwrap_key`.
    """

    query_remap: bool = False
    response_key_add: Optional[Tuple[str, str]] = None
    response_unwrap_key: Optional[str] = None


# Single source of truth for which strategy GET paths this middleware
# touches and how — one entry per path, so there's no risk of the
# request-side and response-side tables drifting out of sync with each
# other, and no implicit precedence rule to trip over.
_STRATEGY_PATH_CONFIG: Dict[str, _StrategyPathConfig] = {
    "/api/strategies": _StrategyPathConfig(response_key_add=("items", "strategies")),
    "/api/strategies/trades": _StrategyPathConfig(query_remap=True, response_key_add=("items", "trades")),
    "/api/strategies/positions": _StrategyPathConfig(query_remap=True, response_key_add=("items", "positions")),
    "/api/strategies/equityCurve": _StrategyPathConfig(query_remap=True, response_unwrap_key="items"),
    "/api/strategies/performance": _StrategyPathConfig(query_remap=True),
    "/api/strategies/logs": _StrategyPathConfig(query_remap=True, response_key_add=("items", "logs")),
    "/api/strategies/notifications/unread-count": _StrategyPathConfig(response_key_add=("count", "unread")),
}


class StrategyCompatMiddleware(BaseHTTPMiddleware):
    """Translates strategy sub-resource requests/responses between the
    frontend's expected shape and the backend's actual shape, without any
    change to `api/routers/strategies.py`."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        config = _STRATEGY_PATH_CONFIG.get(path) if request.method == "GET" else None

        if config and config.query_remap:
            self._remap_query_string(request)

        response = await call_next(request)

        if config and response.status_code == 200:
            response = await self._transform_response(config, response)

        return response

    @staticmethod
    def _remap_query_string(request: Request) -> None:
        _alias_query_param(request, "id", "strategy_id")

    @staticmethod
    async def _transform_response(config: "_StrategyPathConfig", response: Response) -> Response:
        body, payload = await _read_json_body(response)
        if payload is None:
            return _rebuild_response(response, body)

        data = payload.get("data")
        changed = False
        if isinstance(data, dict):
            if config.response_unwrap_key is not None and config.response_unwrap_key in data:
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


# ── Phase 1C: watchlist / symbol-search compatibility (HTTP middleware) ─────


def _watchlist_remap_search_keyword(request: Request) -> None:
    """`GET /api/market/symbols/search` — the frontend sends `keyword`, the
    backend reads `q` (`Query("", min_length=0)`). Without this alias
    `keyword` is silently ignored (FastAPI drops unrecognized query keys),
    so `q` always falls back to its `""` default and search always returns
    the full unfiltered catalogue instead of matching anything — this is a
    genuine break `docs/P5_API_MAPPING_AUDIT.md` didn't flag (it only
    covered this endpoint's response shape).
    """
    _alias_query_param(request, "keyword", "q")


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


class _WatchlistPathConfig(NamedTuple):
    """Per-path Phase 1C translation config. Unlike Strategy's mixed
    additive/unwrap response modes, every watchlist path covered here needs
    the identical response transform (`ensureArray(res.data)` on the
    frontend, so `data` is replaced with `data["items"]`) — only the
    request side differs per path, so that's the only per-path knob."""

    query_remap: Optional[Callable[[Request], None]] = None


_WATCHLIST_PATH_CONFIG: Dict[str, _WatchlistPathConfig] = {
    "/api/market/watchlist/get": _WatchlistPathConfig(),
    "/api/market/symbols/search": _WatchlistPathConfig(query_remap=_watchlist_remap_search_keyword),
    "/api/market/symbols/hot": _WatchlistPathConfig(),
    "/api/market/watchlist/prices": _WatchlistPathConfig(query_remap=_watchlist_remap_prices_symbols),
}


class WatchlistCompatMiddleware(BaseHTTPMiddleware):
    """Translates watchlist/symbol-search requests/responses between the
    frontend's expected shape and the backend's actual shape, without any
    change to `api/routers/watchlist.py`. `add`/`remove` are already
    fully compatible (verified against `WatchlistAdd`/`WatchlistRemove` in
    `api/schemas.py`) and are not in `_WATCHLIST_PATH_CONFIG`, so this
    middleware never touches them."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        config = _WATCHLIST_PATH_CONFIG.get(path) if request.method == "GET" else None

        if config and config.query_remap:
            config.query_remap(request)

        response = await call_next(request)

        if config and response.status_code == 200:
            response = await self._unwrap_items(response)

        return response

    @staticmethod
    async def _unwrap_items(response: Response) -> Response:
        body, payload = await _read_json_body(response)
        if payload is None:
            return _rebuild_response(response, body)

        data = payload.get("data")
        if not (isinstance(data, dict) and "items" in data):
            # Nothing to unwrap (e.g. an auth/business error where `data`
            # is null, or an already-unexpected shape) — leave as-is rather
            # than guessing.
            return _rebuild_response(response, body)

        payload["data"] = data["items"]
        return _rebuild_response(response, _dumps_like_fastapi(payload))
