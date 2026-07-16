"""Frontend/backend request compatibility adapter (P5-02A/B, Phase 1).

These models are attribute-compatible with the existing backend schemas
(``LoginRequest``, ``CredentialCreate`` in ``api/schemas.py``) — same field
names an existing route handler reads (``body.email``, ``body.app_key``,
etc.) — but also accept the frontend's actual wire field names via pydantic
``validation_alias``. Swapping a route's body parameter type to one of these
models therefore requires no change to the route handler itself: only the
type annotation changes.

``StrategyCompatMiddleware`` (Phase 1B) covers the strategy sub-resource
endpoints, which take their identifying parameter as a bare ``Query(...)``
rather than a Pydantic body model — there's no type annotation to swap, so
translation happens as HTTP middleware instead: it rewrites the query string
before FastAPI resolves route parameters, and reshapes the JSON response
body afterwards. ``api/routers/strategies.py`` is not modified at all.

Scope: request/response translation only. No authentication or business
logic lives here.
"""
import json
from typing import Dict, NamedTuple, Optional, Tuple
from urllib.parse import parse_qsl, urlencode

from pydantic import AliasChoices, BaseModel, EmailStr, Field, model_validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


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
        # keep_blank_values=True matches Starlette's own QueryParams parsing
        # (see starlette.datastructures.QueryParams) — without it, any other
        # blank-valued param sharing the query string (e.g. `?id=1&level=`)
        # would be silently dropped by the rewrite below, not just left blank.
        #
        # Kept as a list of tuples rather than a dict — parse_qsl already
        # returns one, and collapsing it into a dict would silently drop any
        # other repeated sibling parameter sharing the query string (e.g.
        # `?tag=a&tag=b&id=5`) down to its last value.
        params = parse_qsl(request.scope["query_string"].decode("latin-1"), keep_blank_values=True)
        keys = {key for key, _ in params}
        if "strategy_id" not in keys and "id" in keys:
            id_value = next(value for key, value in reversed(params) if key == "id")
            params = params + [("strategy_id", id_value)]
            request.scope["query_string"] = urlencode(params).encode("latin-1")

    @staticmethod
    async def _transform_response(config: "_StrategyPathConfig", response: Response) -> Response:
        body = b"".join([chunk async for chunk in response.body_iterator])
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return StrategyCompatMiddleware._rebuild(response, body)

        data = payload.get("data") if isinstance(payload, dict) else None
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
            return StrategyCompatMiddleware._rebuild(response, body)

        # Match FastAPI's own JSONResponse.render() formatting exactly
        # (ensure_ascii=False, allow_nan=False, compact separators) —
        # otherwise Korean/non-ASCII text (strategy names, log messages)
        # would round-trip through this middleware re-encoded as \uXXXX
        # escapes instead of raw UTF-8, and every response on these paths
        # would gain extra whitespace compared to the rest of the API.
        new_body = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        return StrategyCompatMiddleware._rebuild(response, new_body)

    @staticmethod
    def _rebuild(response: Response, body: bytes) -> Response:
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
        """
        new_response = Response(content=body, status_code=response.status_code)
        preserved = [
            (k, v) for k, v in response.raw_headers if k.lower() != b"content-length"
        ]
        new_response.raw_headers = new_response.raw_headers + preserved
        return new_response
