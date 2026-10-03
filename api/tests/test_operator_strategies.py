"""B1 — operators start, stop and list the worker's house strategy from the app
(api/routers/operator.py), through kis-api, never touching worker tables.

Every call to kis-api goes through ``operator._admin_call``, replaced here, so no
test can reach a real ops API.
"""
from datetime import datetime, timedelta

import pytest

from api.routers import operator
from api.tests.test_quick_trade_close_position import (  # reuse the proven harness
    db,          # noqa: F401 - pytest fixtures
    engine,      # noqa: F401
    user,        # noqa: F401
)


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _Upstream:
    """Records calls; answers GET /api/strategies with ``runs`` and the start
    or stop POST with ``post``."""

    def __init__(self, runs=None, post=None, exc=None):
        self.calls = []
        self.runs = runs if runs is not None else []
        self.post = post or _Resp(201, {"run_id": 9, "status": "starting"})
        self.exc = exc

    def __call__(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers})
        if self.exc:
            raise self.exc
        if method == "GET":
            return _Resp(200, self.runs)
        return self.post

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


def _wire(monkeypatch, user, upstream, operators=None, api_key="ops-key"):
    monkeypatch.setattr(operator, "_admin_call", upstream)
    monkeypatch.setenv("KIS_API_KEY", api_key)
    monkeypatch.delenv("KIS_ADMIN_API_BASE", raising=False)
    monkeypatch.setenv("OPERATOR_USER_IDS", str(user.id) if operators is None else operators)
    return upstream


def _start_body(**over):
    body = {"name": "house", "universe": ["SPY", "QQQ"], "position_size_pct": 0.05}
    body.update(over)
    return body


def _iso(dt):
    return dt.isoformat()


# ── authorization comes first ─────────────────────────────────────────────

@pytest.mark.parametrize("call", ["list", "start", "stop"])
def test_non_operators_are_refused_without_reaching_kis_api(monkeypatch, db, user, call):
    up = _wire(monkeypatch, user, _Upstream(), operators="")
    if call == "list":
        resp = operator.list_runs(user)
    elif call == "start":
        resp = operator.start_run(_start_body(), user)
    else:
        resp = operator.stop_run({"run_id": 1}, user)
    assert resp.code == -1 and resp.msg == "Not authorized"
    assert up.calls == []


def test_a_non_operator_learns_nothing_about_the_input_shape(monkeypatch, db, user):
    """Refused before validation: garbage gets the same answer as a valid body."""
    _wire(monkeypatch, user, _Upstream(), operators=str(user.id + 1000))
    assert operator.start_run({"nonsense": 1}, user).msg == "Not authorized"


# ── start: only the house strategy, within bounds ─────────────────────────

def test_a_valid_start_is_forwarded_as_the_indicator_strategy(monkeypatch, db, user):
    up = _wire(monkeypatch, user, _Upstream())
    resp = operator.start_run(_start_body(universe=["SPY", "SPY", "069500"]), user)

    assert resp.code == 1 and resp.data["run_id"] == 9
    (post,) = up.posts()
    assert post["url"] == "http://kis-api:5001/api/strategies/start"
    assert post["headers"] == {"X-API-Key": "ops-key"}
    assert post["json"] == {
        "name": "house", "strategy_type": "indicator", "broker": "kis",
        "config": {"universe": ["SPY", "069500"], "position_size_pct": 0.05,
                   "stop_loss_pct": 0.07},
    }


@pytest.mark.parametrize("body", [
    _start_body(strategy_type="script"),            # unknown key: never passed through
    _start_body(script="print(1)"),
    _start_body(universe=["DOGE"]),                 # outside the trading universe
    _start_body(universe=[]),
    _start_body(position_size_pct=0.06),            # above 5% per position
    _start_body(position_size_pct=0),
    _start_body(stop_loss_pct=0.5),
    _start_body(name="   "),
    _start_body(name="x" * 101),
    {"universe": ["SPY"], "position_size_pct": 0.01},   # no name
])
def test_out_of_bounds_starts_never_reach_kis_api(monkeypatch, db, user, body):
    up = _wire(monkeypatch, user, _Upstream())
    resp = operator.start_run(body, user)
    assert resp.code == -1 and resp.msg.startswith("입력 오류")
    assert up.calls == []


def test_a_start_is_refused_early_while_a_run_occupies_the_slot(monkeypatch, db, user):
    now = datetime.utcnow()
    runs = [{"id": 4, "name": "old", "type": "indicator", "is_active": False,
             "started_at": _iso(now - timedelta(days=2)), "stopped_at": None}]
    up = _wire(monkeypatch, user, _Upstream(runs=runs))
    resp = operator.start_run(_start_body(), user)
    assert resp.code == -1 and "run_id=4" in resp.msg
    assert up.posts() == [], "stop requested but not recorded still holds the slot"


def test_kis_api_conflict_is_passed_through(monkeypatch, db, user):
    up = _wire(monkeypatch, user, _Upstream(
        post=_Resp(409, {"error": "이미 점유 중인 실행이 있다", "run_id": 3})))
    resp = operator.start_run(_start_body(), user)
    assert resp.code == -1 and "점유" in resp.msg
    assert len(up.posts()) == 1, "sent once, never retried"


def test_a_transport_failure_hides_the_key_and_is_not_retried(monkeypatch, db, user):
    calls = {"n": 0}

    def flaky(method, url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        if method == "GET":
            return _Resp(200, [])
        raise RuntimeError("auth failed for super-secret")

    _wire(monkeypatch, user, flaky, api_key="super-secret")
    resp = operator.start_run(_start_body(), user)
    assert resp.code == -1 and "super-secret" not in resp.msg and "***" in resp.msg
    assert calls["n"] == 2  # one GET, one POST


def test_an_explicit_admin_base_is_used(monkeypatch, db, user):
    up = _wire(monkeypatch, user, _Upstream())
    monkeypatch.setenv("KIS_ADMIN_API_BASE", "https://ops.internal/")
    operator.start_run(_start_body(), user)
    assert up.posts()[0]["url"] == "https://ops.internal/api/strategies/start"


# ── stop ──────────────────────────────────────────────────────────────────

def test_stop_forwards_the_run_id(monkeypatch, db, user):
    up = _wire(monkeypatch, user, _Upstream(post=_Resp(200, {"run_id": 5, "status": "stopping"})))
    resp = operator.stop_run({"run_id": 5}, user)
    assert resp.code == 1
    assert up.posts()[0]["url"] == "http://kis-api:5001/api/strategies/5/stop"


@pytest.mark.parametrize("body", [{}, {"run_id": 0}, {"run_id": "5; drop"}, {"run_id": 5, "x": 1}])
def test_stop_needs_a_plain_positive_id(monkeypatch, db, user, body):
    up = _wire(monkeypatch, user, _Upstream())
    assert operator.stop_run(body, user).code == -1
    assert up.calls == []


# ── list: slot and paper-gate status ──────────────────────────────────────

def test_the_list_marks_the_occupying_run_and_the_paper_gate(monkeypatch, db, user):
    now = datetime.utcnow()
    runs = [
        {"id": 2, "name": "a", "type": "indicator", "is_active": True,
         "started_at": _iso(now - timedelta(days=30)), "stopped_at": None},
        {"id": 1, "name": "b", "type": "indicator", "is_active": False,
         "started_at": _iso(now - timedelta(days=60)),
         "stopped_at": _iso(now - timedelta(days=59))},
    ]
    _wire(monkeypatch, user, _Upstream(runs=runs))
    resp = operator.list_runs(user)
    assert resp.code == 1
    a, b = resp.data["runs"]
    assert a["occupying"] is True and a["paper_gate_met"] is True and a["run_days"] >= 30
    assert b["occupying"] is False and b["paper_gate_met"] is False and b["run_days"] == 1.0


def test_an_unreachable_kis_api_is_an_error_not_an_empty_list(monkeypatch, db, user):
    _wire(monkeypatch, user, _Upstream(exc=ConnectionError("down")))
    assert operator.list_runs(user).code == -1


# ── over HTTP, with the real JWT dependency ──────────────────────────────

def test_routes_require_a_token(client, monkeypatch):
    monkeypatch.setattr(operator, "_admin_call", lambda *a, **k: pytest.fail("no call"))
    for method, path in (("get", "/api/operator/strategies"),
                         ("post", "/api/operator/strategies/start"),
                         ("post", "/api/operator/strategies/stop")):
        kwargs = {} if method == "get" else {"json": {}}
        assert getattr(client, method)(path, **kwargs).status_code in (401, 403)


def test_an_operator_starts_over_http(client, seed_user, auth_headers, monkeypatch):
    up = _Upstream()
    monkeypatch.setattr(operator, "_admin_call", up)
    monkeypatch.setenv("KIS_API_KEY", "k")
    monkeypatch.setenv("OPERATOR_USER_IDS", str(seed_user[0].id))
    res = client.post("/api/operator/strategies/start", json=_start_body(), headers=auth_headers)
    assert res.status_code == 200 and res.json()["code"] == 1
    assert len(up.posts()) == 1


# ── the menu flag ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("method, path", [
    ("get", "/api/auth/info"),
    ("get", "/api/users/profile"),
    ("put", "/api/users/profile/update"),
])
def test_user_info_responses_carry_is_operator(client, seed_user, auth_headers, monkeypatch,
                                              method, path):
    kwargs = {"json": {"nickname": "rider"}} if method == "put" else {}
    monkeypatch.setenv("OPERATOR_USER_IDS", str(seed_user[0].id))
    data = getattr(client, method)(path, headers=auth_headers, **kwargs).json()["data"]
    assert data["is_operator"] is True
    monkeypatch.setenv("OPERATOR_USER_IDS", "")
    data = getattr(client, method)(path, headers=auth_headers, **kwargs).json()["data"]
    assert data["is_operator"] is False
