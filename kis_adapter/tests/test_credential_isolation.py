"""P0-03 — request-scoped KIS credential isolation.

These tests pin the isolation contract: credentials are injected per client
instance, never read from (or written to) process-wide ``os.environ`` for the
request-scoped path, and no two credential scopes share a bearer token — even
under concurrent execution. The env-based path (the Execution Layer's single
static account) must keep working unchanged.
"""
import os
import threading
import time

import pytest

from kis_adapter import KISClient, KISCredentials, KISMarketData, KISOrders, KISPortfolio
from kis_adapter.auth import KISAuth

CRED_A = KISCredentials(
    app_key="APPKEY_A", app_secret="SECRET_A",
    account_no="1111111101", hts_id="htsA", env="paper",
)
CRED_B = KISCredentials(
    app_key="APPKEY_B", app_secret="SECRET_B",
    account_no="2222222202", hts_id="htsB", env="real",
)

_KIS_ENV_VARS = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "KIS_HTS_ID", "KIS_ENV")


class _NoRedis:
    """Redis stand-in that is reachable but empty — forces the issue+in-memory path."""

    def get(self, *a, **k):
        return None

    def set(self, *a, **k):
        return None

    def delete(self, *a, **k):
        return None


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No real Redis, deterministic token issuance, clean in-memory store per test."""
    import kis_adapter.auth as authmod

    monkeypatch.setattr("redis.from_url", lambda *a, **k: _NoRedis())
    # Issue a token deterministically derived from the app_key, so a bearer token
    # unambiguously identifies which credential produced it.
    monkeypatch.setattr(KISAuth, "_issue_token", lambda self: (f"tok-{self.app_key}", 3600))
    with authmod._MEM_TOKENS_LOCK:
        authmod._MEM_TOKENS.clear()
    yield
    with authmod._MEM_TOKENS_LOCK:
        authmod._MEM_TOKENS.clear()


# ── 1. Instance-level credential injection ────────────────────────────────────

def test_auth_uses_injected_credentials_a():
    auth = KISAuth(CRED_A)
    assert auth.app_key == "APPKEY_A"
    assert auth.app_secret == "SECRET_A"
    assert auth.env == "paper"
    assert auth.account_no == "1111111101"


def test_auth_uses_injected_credentials_b():
    auth = KISAuth(CRED_B)
    assert auth.app_key == "APPKEY_B"
    assert auth.app_secret == "SECRET_B"
    assert auth.env == "real"
    assert auth.account_no == "2222222202"


def test_client_threads_credentials_to_auth():
    assert KISClient(CRED_A).auth.app_key == "APPKEY_A"
    assert KISClient(CRED_B).auth.app_key == "APPKEY_B"


def test_orders_account_from_injected_client():
    assert KISOrders(KISClient(CRED_A))._account == "1111111101"


def test_portfolio_account_from_injected_client():
    assert KISPortfolio(KISClient(CRED_B))._account == "2222222202"


def test_market_data_accepts_injected_credentials():
    md = KISMarketData(credentials=CRED_A)
    assert md._client.auth.app_key == "APPKEY_A"


# ── 2. No process-wide os.environ mutation on the request-scoped path ──────────

def test_building_clients_from_credentials_does_not_mutate_environ(monkeypatch):
    for k in _KIS_ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    before = dict(os.environ)

    client_a = KISClient(CRED_A)
    KISOrders(client_a)
    KISPortfolio(client_a)
    KISOrders(KISClient(CRED_B))

    assert dict(os.environ) == before
    for k in _KIS_ENV_VARS:
        assert k not in os.environ


# ── 3. Per-credential token cache isolation ───────────────────────────────────

def test_redis_cache_keys_differ_per_credential():
    key_a = KISAuth(CRED_A)._token_cache_key
    key_b = KISAuth(CRED_B)._token_cache_key
    assert key_a.startswith("kis:access_token:")
    assert key_b.startswith("kis:access_token:")
    assert key_a != key_b


def test_token_cache_is_isolated_per_credential():
    tok_a = KISAuth(CRED_A).get_token()
    tok_b = KISAuth(CRED_B).get_token()
    assert tok_a == "tok-APPKEY_A"
    assert tok_b == "tok-APPKEY_B"
    assert tok_a != tok_b
    # A second A-scoped auth reuses A's cached token, never B's.
    assert KISAuth(CRED_A).get_token() == "tok-APPKEY_A"


# ── 4. Concurrent execution: zero cross-talk ──────────────────────────────────

def test_concurrent_execution_zero_credential_crosstalk():
    errors: list[str] = []
    done: list[str] = []

    def worker(name: str, cred: KISCredentials, expected: str):
        try:
            for _ in range(30):
                auth = KISAuth(cred)
                tok = auth.get_token()
                headers = auth.get_headers("SOME_TR")
                assert tok == expected, f"{name}: token {tok!r} != {expected!r}"
                assert headers["appkey"] == cred.app_key, f"{name}: appkey leak"
                assert headers["authorization"] == f"Bearer {expected}", f"{name}: bearer leak"
            done.append(name)
        except AssertionError as exc:  # pragma: no cover - surfaced via `errors`
            errors.append(str(exc))

    threads = []
    for i in range(10):
        cred, expected = (CRED_A, "tok-APPKEY_A") if i % 2 == 0 else (CRED_B, "tok-APPKEY_B")
        threads.append(threading.Thread(target=worker, args=(f"t{i}", cred, expected)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(done) == 10


def test_distinct_instances_do_not_share_credential_state():
    a = KISAuth(CRED_A)
    b = KISAuth(CRED_B)
    a.get_token()
    b.get_token()
    assert a.get_headers("T")["appkey"] == "APPKEY_A"
    assert b.get_headers("T")["appkey"] == "APPKEY_B"


# ── 5. Env-based path (Execution Layer) — no regression ───────────────────────

def test_env_fallback_when_no_credentials(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY", "ENVKEY")
    monkeypatch.setenv("KIS_APP_SECRET", "ENVSECRET")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "9999999909")
    monkeypatch.setenv("KIS_ENV", "paper")

    auth = KISAuth()
    assert auth.app_key == "ENVKEY"
    assert auth.account_no == "9999999909"
    assert auth.env == "paper"
    assert KISOrders(KISClient())._account == "9999999909"


def test_missing_env_key_still_raises(monkeypatch):
    for k in _KIS_ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(KeyError):
        KISAuth()


def test_injected_credential_never_falls_back_to_env_account(monkeypatch):
    """An injected credential with an empty account must NOT use the process
    env's static KIS_ACCOUNT_NO — that would place an order on the wrong account.
    """
    monkeypatch.setenv("KIS_ACCOUNT_NO", "8888888808")  # static process account
    cred_no_account = KISCredentials(app_key="K", app_secret="S", account_no="", env="paper")
    auth = KISAuth(cred_no_account)
    with pytest.raises(ValueError):
        auth.require_account()
    with pytest.raises(ValueError):
        KISOrders(KISClient(cred_no_account))


# ── 6. revoke_token robustness (CodeRabbit Major) ─────────────────────────────

def test_revoke_token_tolerates_redis_failure_and_clears_memory():
    """revoke_token must not raise on a Redis outage and must always clear the
    in-memory cache — otherwise a revoked, in-memory-only token keeps working.
    """
    import kis_adapter.auth as authmod

    class _RaisingRedis:
        def get(self, *a, **k):
            raise ConnectionError("redis down")

        def delete(self, *a, **k):
            raise ConnectionError("redis down")

    auth = KISAuth(CRED_A)
    auth._redis = _RaisingRedis()
    with authmod._MEM_TOKENS_LOCK:
        authmod._MEM_TOKENS[auth._fp] = ("tok-APPKEY_A", 9e18)  # seed in-memory token

    auth.revoke_token()  # must not raise

    with authmod._MEM_TOKENS_LOCK:
        assert auth._fp not in authmod._MEM_TOKENS


# ── 7. token issuance serialized per credential (CodeRabbit Major) ────────────

def test_concurrent_cold_cache_issues_token_once(monkeypatch):
    """Concurrent cold-cache callers for the SAME credential must issue exactly
    one token — KIS caps issuance at 1/minute (EGW00133 on excess).
    """
    calls = {"n": 0}
    calls_lock = threading.Lock()

    def _counting_issue(self):
        # Hold the issuance lock briefly so the other threads pile up on it
        # (proving they wait and then reuse rather than each issuing).
        with calls_lock:
            calls["n"] += 1
        time.sleep(0.05)
        return (f"tok-{self.app_key}", 3600)

    monkeypatch.setattr(KISAuth, "_issue_token", _counting_issue)

    tokens: list[str] = []
    tokens_lock = threading.Lock()

    def worker():
        tok = KISAuth(CRED_A).get_token()
        with tokens_lock:
            tokens.append(tok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1, f"expected exactly one issuance, got {calls['n']}"
    assert tokens == ["tok-APPKEY_A"] * 8
