"""kis-api (:5001) has no unauthenticated mode and no public port.

With ``KIS_API_KEY`` empty — the compose default — ``_check_api_key`` switched
auth off for every route, and compose published 5001 on every host interface.
Anyone who could reach the host could ``POST /api/admin/flatten
{"confirm": true}`` (sell everything in the operator account), start or stop
strategies, reconcile, read the balance. Now: no key means 503 on every
non-open route and no start under gunicorn; compose binds the port to the host
loopback and refuses to run without the key. The worker-side alert path also
gets the Telegram settings it never had.
"""
import inspect
import re
from pathlib import Path

import pytest

from backend.api import server as srv

ROOT = Path(__file__).resolve().parents[2]
KEY = "test-ops-key-0123456789abcdef"

# endpoint name -> (method, path); handlers are swapped for ones that fail the
# test if the guard ever lets a request through.
_GUARDED = {
    "trigger_flatten": ("post", "/api/admin/flatten"),
    "start_strategy": ("post", "/api/strategies/start"),
    "get_balance": ("get", "/api/balance"),
    "trigger_reconcile": ("post", "/api/admin/reconcile"),
}


@pytest.fixture()
def client(monkeypatch):
    srv.app.config.update(TESTING=True)
    for endpoint in _GUARDED:
        assert endpoint in srv.app.view_functions, endpoint

        def reached(*_a, _e=endpoint, **_k):
            return {"reached": _e}, 299

        monkeypatch.setitem(srv.app.view_functions, endpoint, reached)
    return srv.app.test_client()


def _call(client, method, path, headers=None):
    return getattr(client, method)(path, json={"confirm": True}, headers=headers or {})


class TestNoKey:
    @pytest.fixture(autouse=True)
    def no_key(self, monkeypatch):
        monkeypatch.setattr(srv, "_API_KEY", "")

    @pytest.mark.parametrize("endpoint", sorted(_GUARDED))
    def test_guarded_routes_answer_503_and_never_run(self, client, endpoint):
        method, path = _GUARDED[endpoint]
        res = _call(client, method, path, {"X-API-Key": ""})
        assert res.status_code == 503
        assert "KIS_API_KEY" in res.get_json()["error"]

    def test_the_server_will_not_start(self):
        with pytest.raises(RuntimeError, match="KIS_API_KEY"):
            srv.require_api_key()

    def test_open_routes_stay_open_for_healthchecks(self, client):
        assert client.get("/api/health").status_code == 200
        assert "/api/status" in srv._OPEN_ROUTES and "/api/metrics" in srv._OPEN_ROUTES


class TestWithKey:
    @pytest.fixture(autouse=True)
    def key(self, monkeypatch):
        monkeypatch.setattr(srv, "_API_KEY", KEY)

    @pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
    def test_a_missing_or_wrong_key_is_401(self, client, headers):
        method, path = _GUARDED["trigger_flatten"]
        assert _call(client, method, path, headers).status_code == 401

    def test_the_right_key_passes_the_guard(self, client):
        method, path = _GUARDED["trigger_flatten"]
        res = _call(client, method, path, {"X-API-Key": KEY})
        assert res.status_code == 299 and res.get_json() == {"reached": "trigger_flatten"}

    def test_start_check_passes(self):
        srv.require_api_key()


def test_gunicorn_refuses_to_start_without_a_key():
    from backend.api import gunicorn_conf
    assert "require_api_key()" in inspect.getsource(gunicorn_conf.on_starting)


# ── compose ────────────────────────────────────────────────────────────────

def _service(name: str) -> str:
    text = (ROOT / "docker-compose.yml").read_text()
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z]|^\S|\Z)", text, re.M | re.S)
    assert m, name
    return m.group(1)


def test_kis_api_is_published_on_loopback_only():
    block = _service("kis-api")
    ports = re.findall(r'^\s+-\s+"([^"]+)"\s*$', block.split("ports:", 1)[1].split("environment:", 1)[0], re.M)
    assert ports, "kis-api publishes no port — the deploy probe needs one on loopback"
    for mapping in ports:
        assert mapping.startswith("127.0.0.1:"), mapping


def test_compose_requires_the_key_for_kis_api():
    assert re.search(r"^\s+KIS_API_KEY:\s*\$\{KIS_API_KEY:\?", _service("kis-api"), re.M)


@pytest.mark.parametrize("service", ["kis-api", "kis-worker"])
def test_alerting_services_get_the_telegram_settings(service):
    block = _service(service)
    for var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        assert re.search(rf"^\s+{var}:\s*\$\{{{var}:-\}}\s*$", block, re.M), (service, var)
