import math
import os
import time
import json
import hashlib
import logging
import requests
import redis
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock

logger = logging.getLogger(__name__)

# Explicit deadline for every KIS HTTP call — token issuance included, since a
# hung token request blocks the inquiry that triggered it. Without a bound, a
# stalled broker holds the reconciliation sweep past shutdown. Tunable per
# environment; a bad value falls back rather than removing the deadline.
HTTP_TIMEOUT_ENV = "KIS_HTTP_TIMEOUT_SECONDS"
HTTP_TIMEOUT_SECONDS = 10.0


def _http_timeout() -> float:
    """Resolve the HTTP deadline, falling back on any value that would remove it."""
    raw = os.environ.get(HTTP_TIMEOUT_ENV)
    if raw is None:
        return HTTP_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("invalid %s=%r, using %ss", HTTP_TIMEOUT_ENV, raw, HTTP_TIMEOUT_SECONDS)
        return HTTP_TIMEOUT_SECONDS
    # nan/inf parse fine but are not deadlines: inf removes the bound entirely
    # and nan raises inside urllib3 — both defeat the point of setting one.
    if not math.isfinite(value) or value <= 0:
        logger.warning(
            "%s must be a finite value > 0 (got %r), using %ss",
            HTTP_TIMEOUT_ENV, raw, HTTP_TIMEOUT_SECONDS,
        )
        return HTTP_TIMEOUT_SECONDS
    return value

PAPER_BASE = "https://openapivts.koreainvestment.com:9443"
REAL_BASE = "https://openapi.koreainvestment.com:9443"

TOKEN_CACHE_KEY = "kis:access_token"
TOKEN_EXPIRY_KEY = "kis:token_expiry"

# Process-level in-memory token cache fallback when Redis is unavailable.
# Keyed by a per-credential fingerprint so distinct app-keys never share a token.
_MEM_TOKENS: dict[str, tuple[str, float]] = {}
_MEM_TOKENS_LOCK = Lock()
_TOKEN_REFRESH_BUFFER = 900  # 15 minutes before expiry

# Per-credential issuance locks — serialize concurrent token issuance for the
# same credential so cold-cache callers don't each hit KIS's 1-token/minute
# limit (EGW00133). Distinct credentials still issue in parallel.
_ISSUE_LOCKS: dict[str, Lock] = {}
_ISSUE_LOCKS_META = Lock()


def _issuance_lock(fp: str) -> Lock:
    with _ISSUE_LOCKS_META:
        lock = _ISSUE_LOCKS.get(fp)
        if lock is None:
            lock = Lock()
            _ISSUE_LOCKS[fp] = lock
        return lock


@dataclass(frozen=True)
class KISCredentials:
    """Explicit, per-scope KIS credentials injected into a client instance.

    Replaces the previous pattern of mutating process-wide ``os.environ`` to
    smuggle per-user credentials into ``KISAuth`` — a cross-request credential
    bleed under concurrency. ``from_env()`` preserves the Execution Layer's
    single static-account behaviour unchanged (same ``KeyError`` on a missing
    ``KIS_APP_KEY`` as before).
    """

    app_key: str
    app_secret: str
    account_no: str = ""
    hts_id: str = ""
    env: str = "paper"

    @classmethod
    def from_env(cls) -> "KISCredentials":
        return cls(
            app_key=os.environ["KIS_APP_KEY"],
            app_secret=os.environ["KIS_APP_SECRET"],
            account_no=os.environ.get("KIS_ACCOUNT_NO", ""),
            hts_id=os.environ.get("KIS_HTS_ID", ""),
            env=os.environ.get("KIS_ENV", "paper"),
        )


class KISAuth:
    def __init__(self, credentials: "KISCredentials | None" = None):
        creds = credentials or KISCredentials.from_env()
        # Only the env-sourced path may fall back to the process env for a
        # missing account (see require_account) — an explicitly-injected
        # credential must never read process-wide state.
        self._env_sourced = credentials is None
        self.app_key = creds.app_key
        self.app_secret = creds.app_secret
        self.account_no = creds.account_no
        self.hts_id = creds.hts_id
        self.env = creds.env
        self.base_url = PAPER_BASE if self.env == "paper" else REAL_BASE
        self._redis = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        # Per-credential cache keys — a distinct app-key/env never shares a token.
        fp = hashlib.sha256(f"{self.app_key}:{self.env}".encode()).hexdigest()[:16]
        self._fp = fp
        self._token_cache_key = f"{TOKEN_CACHE_KEY}:{fp}"
        self._token_expiry_key = f"{TOKEN_EXPIRY_KEY}:{fp}"

    def get_token(self) -> str:
        # 1. Try Redis cache (per-credential key)
        try:
            cached = self._redis.get(self._token_cache_key)
            if cached:
                expiry = self._redis.get(self._token_expiry_key)
                if expiry and float(expiry) - time.time() > _TOKEN_REFRESH_BUFFER:
                    token = cached.decode()
                    with _MEM_TOKENS_LOCK:
                        _MEM_TOKENS[self._fp] = (token, float(expiry))
                    return token
        except Exception as e:
            logger.warning("Redis token cache read 실패 (in-memory 폴백): %s", e)
            # Fall through to in-memory cache check

        # 2. In-memory fallback (valid when Redis is down), scoped per credential
        with _MEM_TOKENS_LOCK:
            entry = _MEM_TOKENS.get(self._fp)
        if entry and entry[1] - time.time() > _TOKEN_REFRESH_BUFFER:
            logger.debug("Redis 불가 — in-memory token 사용")
            return entry[0]

        # 3. Issue a new token — serialized per credential so concurrent
        # cold-cache callers issue exactly one token (KIS caps issuance at
        # 1/minute and returns EGW00133 on excess).
        with _issuance_lock(self._fp):
            # Double-check: another thread may have issued while we waited.
            with _MEM_TOKENS_LOCK:
                entry = _MEM_TOKENS.get(self._fp)
            if entry and entry[1] - time.time() > _TOKEN_REFRESH_BUFFER:
                return entry[0]
            token, expires_in = self._issue_token()
            expiry_ts = time.time() + expires_in
            with _MEM_TOKENS_LOCK:
                _MEM_TOKENS[self._fp] = (token, expiry_ts)
            try:
                self._redis.set(self._token_cache_key, token)
                self._redis.set(self._token_expiry_key, expiry_ts)
            except Exception as e:
                logger.warning("Redis token cache write 실패 (in-memory のみ): %s", e)
            return token

    def _issue_token(self) -> tuple[str, int]:
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        resp = requests.post(url, json=body, timeout=_http_timeout())
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"], int(data.get("expires_in", 86400))

    def revoke_token(self):
        try:
            token = self._redis.get(self._token_cache_key)
        except Exception as e:
            logger.warning("Redis token cache read 실패 during revoke: %s", e)
            token = None
        # Always clear the in-memory cache — even on a Redis outage or a
        # token that was only ever cached in-memory — so a revoked credential
        # cannot keep authenticating via the fallback.
        with _MEM_TOKENS_LOCK:
            _MEM_TOKENS.pop(self._fp, None)
        if not token:
            return
        url = f"{self.base_url}/oauth2/revokeP"
        body = {
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "token": token.decode(),
        }
        requests.post(url, json=body, timeout=_http_timeout())
        try:
            self._redis.delete(self._token_cache_key, self._token_expiry_key)
        except Exception as e:
            logger.warning("Redis token cache delete 실패 during revoke: %s", e)

    def require_account(self) -> str:
        """Resolve the trading account for this credential scope.

        An explicitly-injected (request-scoped) credential must never fall back
        to ``os.environ`` — doing so could place an order on a different static
        account under concurrency. Only the env-sourced path reads
        ``KIS_ACCOUNT_NO``, and it fails fast (``KeyError``) when unset, matching
        the prior behaviour.
        """
        if self.account_no:
            return self.account_no
        if self._env_sourced:
            return os.environ["KIS_ACCOUNT_NO"]
        raise ValueError("KIS credential has no account_no (request-scoped account required)")

    def get_hashkey(self, body: dict) -> str:
        url = f"{self.base_url}/uapi/hashkey"
        headers = {
            "content-type": "application/json",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        resp = requests.post(url, json=body, headers=headers, timeout=_http_timeout())
        resp.raise_for_status()
        return resp.json()["HASH"]

    def get_headers(self, tr_id: str, include_token: bool = True) -> dict:
        headers = {
            "content-type": "application/json",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
        }
        if include_token:
            headers["authorization"] = f"Bearer {self.get_token()}"
        return headers
