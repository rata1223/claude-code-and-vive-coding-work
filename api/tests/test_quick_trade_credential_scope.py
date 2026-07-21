"""P0-03 — the two credential injectors must NOT mutate process-wide os.environ.

`quick_trade._load_kis` and `dashboard._build_kis_client_from_cred` both used to
write decrypted per-user credentials into `os.environ` before constructing KIS
clients — a cross-request credential-bleed risk under concurrency. These tests
pin that they now inject credentials explicitly and leave `os.environ` untouched.
"""
import os

import pytest

from api.routers import dashboard, quick_trade

_KIS_ENV_VARS = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO", "KIS_HTS_ID", "KIS_ENV")


class _FakeCred:
    app_key_enc = "AK"
    app_secret_enc = "AS"
    account_no_enc = "1234567890"
    hts_id_enc = "HTS"
    env = "paper"


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    # KISAuth constructs a redis client; keep it offline and non-mutating.
    monkeypatch.setattr("redis.from_url", lambda *a, **k: None)
    yield


def test_load_kis_does_not_mutate_environ(monkeypatch):
    for k in _KIS_ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(quick_trade, "decrypt", lambda v: v)
    before = dict(os.environ)

    client, orders, portfolio = quick_trade._load_kis(_FakeCred())

    assert dict(os.environ) == before
    for k in _KIS_ENV_VARS:
        assert k not in os.environ
    assert client.auth.app_key == "AK"
    assert client.auth.env == "paper"
    assert orders._account == "1234567890"
    assert portfolio._account == "1234567890"


def test_dashboard_build_client_does_not_mutate_environ(monkeypatch):
    for k in _KIS_ENV_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(dashboard, "decrypt", lambda v: v)
    before = dict(os.environ)

    client, portfolio = dashboard._build_kis_client_from_cred(_FakeCred())

    assert dict(os.environ) == before
    for k in _KIS_ENV_VARS:
        assert k not in os.environ
    assert client.auth.app_key == "AK"
    assert portfolio._account == "1234567890"
