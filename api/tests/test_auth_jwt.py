"""api/auth.py on PyJWT (was python-jose).

python-jose pulled in ecdsa (a timing advisory with no fixed release), rsa and
pyasn1 0.4.8 (four DoS advisories) for algorithms this API never uses — it only
signs and verifies HS256. These tests pin that the swap kept the contract:
valid tokens decode, anything else is ``None``, and tokens already issued by
python-jose (plain RFC 7519 HS256) still decode after the upgrade.
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from api import auth


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _hand_signed(payload: dict, key: str, alg_header: str = "HS256") -> str:
    """An HS256 token built from the RFC, as python-jose issued them."""
    header = _b64(json.dumps({"alg": alg_header, "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    sig = hmac.new(key.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64(sig)}"


def _future(minutes=30) -> int:
    return int((datetime.now(timezone.utc) + timedelta(minutes=minutes)).timestamp())


class TestValidTokens:
    def test_round_trip(self):
        token = auth.create_access_token(7, "a@example.com")
        payload = auth.decode_access_token(token)
        assert payload["sub"] == "7" and payload["email"] == "a@example.com"

    def test_a_token_issued_before_the_switch_still_decodes(self):
        token = _hand_signed({"sub": "7", "email": "a@example.com", "exp": _future()},
                             auth.SECRET_KEY)
        assert auth.decode_access_token(token)["sub"] == "7"


class TestEverythingElseIsNone:
    def test_expired(self):
        token = auth.create_access_token(7, "a@example.com",
                                         expires_delta=timedelta(seconds=-5))
        assert auth.decode_access_token(token) is None

    def test_tampered_payload(self):
        head, _body, sig = auth.create_access_token(7, "a@example.com").split(".")
        forged = _b64(json.dumps({"sub": "1", "exp": _future()}).encode())
        assert auth.decode_access_token(f"{head}.{forged}.{sig}") is None

    def test_signed_with_another_key(self):
        token = _hand_signed({"sub": "7", "exp": _future()}, "x" * 40)
        assert auth.decode_access_token(token) is None

    def test_unsigned_alg_none(self):
        header = _b64(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        body = _b64(json.dumps({"sub": "7", "exp": _future()}).encode())
        assert auth.decode_access_token(f"{header}.{body}.") is None

    def test_another_hmac_algorithm_is_refused(self):
        """``algorithms`` is pinned to HS256 — a same-key HS512 token is not accepted."""
        token = jwt.encode({"sub": "7", "exp": _future()}, auth.SECRET_KEY,
                           algorithm="HS512")
        assert auth.decode_access_token(token) is None

    @pytest.mark.parametrize("garbage", ["", "not-a-token", "a.b.c"])
    def test_malformed(self, garbage):
        assert auth.decode_access_token(garbage) is None
