"""Frontend/backend request compatibility adapter (P5-02A, Phase 1).

These models are attribute-compatible with the existing backend schemas
(``LoginRequest``, ``CredentialCreate`` in ``api/schemas.py``) — same field
names an existing route handler reads (``body.email``, ``body.app_key``,
etc.) — but also accept the frontend's actual wire field names via pydantic
``validation_alias``. Swapping a route's body parameter type to one of these
models therefore requires no change to the route handler itself: only the
type annotation changes.

Scope: request/response translation only. No authentication or business
logic lives here.
"""
from typing import Optional

from pydantic import AliasChoices, BaseModel, EmailStr, Field, model_validator


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
