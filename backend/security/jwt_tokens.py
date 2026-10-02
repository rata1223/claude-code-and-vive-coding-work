"""Access-token verification shared by the app API and the WS server (#189).

``api/auth.py`` issues HS256 tokens signed with ``JWT_SECRET_KEY``. The WS
server (``kis-ws``, built from ``Dockerfile.kis-bot``) must verify the same
tokens but ships neither ``api/`` nor its requirements, so the check lives here
in ``backend/`` — which both images contain — and ``api.auth`` delegates to it.
"""
import os
from typing import Optional

import jwt

ALGORITHM = "HS256"


def jwt_secret() -> str:
    """``JWT_SECRET_KEY``; a missing value is a configuration error, not a bad token."""
    secret = os.environ.get("JWT_SECRET_KEY", "")
    if not secret:
        raise RuntimeError(
            "JWT_SECRET_KEY environment variable is not set. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return secret


def decode_access_token(token: str, secret: Optional[str] = None) -> Optional[dict]:
    """The token's payload, or ``None`` for any invalid token.

    Only HS256 is accepted, and ``exp`` is required: a bad signature, an expired
    token, a token with no expiry, a malformed token and any other algorithm
    (``none``, HS512, RS256, …) all return ``None``.
    """
    try:
        return jwt.decode(token, secret or jwt_secret(), algorithms=[ALGORITHM],
                          options={"require": ["exp"]})
    except jwt.PyJWTError:
        return None
