"""TDD suite for P5-02E: unified CompatMiddleware refactor.

Covers the 8 mandated categories not already exercised by the pre-existing
68 tests (test_compat_login.py / test_compat_credentials.py /
test_compat_strategy.py / test_compat_watchlist.py — all left unmodified,
and remain the full-stack DB/auth/CORS/router regression oracle straight
through this refactor):

  1. TestTransformGolden       - response-shaping algorithm, all 11
                                  _PATH_CONFIG entries, DB-free stub app
                                  with ONLY CompatMiddleware registered.
                                  Expected values are hand-derived from the
                                  documented _PathConfig algorithm (additive
                                  copy / unwrap-replace), not captured from
                                  the old middleware's live output — so this
                                  file keeps compiling and gating CI in the
                                  same commit that deletes
                                  StrategyCompatMiddleware/
                                  WatchlistCompatMiddleware.
  2. TestTransformEdgeCases    - null-data passthrough, non-200 passthrough,
                                  duplicate-header preservation, POST bypass.
  3. TestRegressionCategories  - named traceability pointer for "credential
                                  compatibility": proves the credential body-
                                  model code path (a different mechanism,
                                  CompatCredentialCreate, untouched by this
                                  refactor) is unaffected by CompatMiddleware
                                  joining the ASGI stack.

Written before CompatMiddleware/_PathConfig/_PATH_CONFIG exist in
api/compat.py (TDD red step) and expected to pass once they land and the
two old classes are removed (green step).
"""
import copy
from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.responses import Response


# ── Canned pre-transform payloads, one per _PATH_CONFIG entry ───────────────
# Shapes mirror what api/routers/strategies.py and api/routers/watchlist.py
# actually return today (confirmed against test_compat_strategy.py /
# test_compat_watchlist.py's own assertions) — not invented shapes.
_CANNED: Dict[str, Dict[str, Any]] = {
    "/api/strategies": {"code": 1, "data": {"items": [{"id": 1, "name": "A"}], "total": 1}, "msg": "ok"},
    "/api/strategies/trades": {"code": 1, "data": {"items": [{"id": 1, "symbol": "AAPL"}], "total": 1}, "msg": "ok"},
    "/api/strategies/positions": {"code": 1, "data": {"items": [{"symbol": "AAPL", "qty": 10}], "total": 1}, "msg": "ok"},
    "/api/strategies/equityCurve": {"code": 1, "data": {"items": [{"date": "2026-01-01", "equity": 10000.0}]}, "msg": "ok"},
    "/api/strategies/performance": {"code": 1, "data": {"sharpe": 1.2, "mdd": 0.1}, "msg": "ok"},
    "/api/strategies/logs": {"code": 1, "data": {"items": [{"level": "info", "message": "started"}], "total": 1}, "msg": "ok"},
    "/api/strategies/notifications/unread-count": {"code": 1, "data": {"count": 3}, "msg": "ok"},
    "/api/market/watchlist/get": {"code": 1, "data": {"items": [{"symbol": "AAPL"}]}, "msg": "ok"},
    "/api/market/symbols/search": {"code": 1, "data": {"items": [{"symbol": "AAPL"}]}, "msg": "ok"},
    "/api/market/symbols/hot": {"code": 1, "data": {"items": [{"symbol": "AAPL"}]}, "msg": "ok"},
    "/api/market/watchlist/prices": {"code": 1, "data": {"items": [{"symbol": "AAPL", "price": 100.0}]}, "msg": "ok"},
}


def _expected(path: str, canned: Dict[str, Any]) -> Dict[str, Any]:
    """Hand-derive expected output by applying the _PathConfig algorithm
    documented in docs/P5_COMPAT_REFACTOR_PLAN.md to `canned`:
    response_key_add copies data[old_key] to data[new_key] (additive,
    old_key survives); response_unwrap_key replaces data with
    data[unwrap_key] outright. Derived independently of _PATH_CONFIG's
    actual contents, so a real config entry diverging from this table is
    exactly what these assertions are meant to catch.
    """
    expected = copy.deepcopy(canned)
    data = expected["data"]
    if path == "/api/strategies":
        data["strategies"] = data["items"]
    elif path == "/api/strategies/trades":
        data["trades"] = data["items"]
    elif path == "/api/strategies/positions":
        data["positions"] = data["items"]
    elif path == "/api/strategies/equityCurve":
        expected["data"] = data["items"]
    elif path == "/api/strategies/performance":
        pass  # query_remap only -- no response transform for this path
    elif path == "/api/strategies/logs":
        data["logs"] = data["items"]
    elif path == "/api/strategies/notifications/unread-count":
        data["unread"] = data["count"]
    elif path in (
        "/api/market/watchlist/get",
        "/api/market/symbols/search",
        "/api/market/symbols/hot",
        "/api/market/watchlist/prices",
    ):
        expected["data"] = data["items"]
    else:
        raise AssertionError(f"no golden-value rule for {path!r} -- add one")
    return expected


def _stub_app() -> FastAPI:
    """DB-free FastAPI app with only CompatMiddleware registered -- proves
    the unified transform logic in isolation, independent of auth/DB/CORS
    (those remain covered by the 68 pre-existing full-stack tests)."""
    from api.compat import CompatMiddleware

    app = FastAPI()
    for path, payload in _CANNED.items():
        app.add_api_route(path, _make_handler(payload), methods=["GET", "POST"])
    app.add_middleware(CompatMiddleware)
    return app


def _make_handler(payload: Dict[str, Any]):
    async def handler():
        return JSONResponse(payload)

    return handler


@pytest.fixture(scope="module")
def stub_client():
    return TestClient(_stub_app())


# ── 1. Golden-value response transform, all 11 _PATH_CONFIG entries ─────────
class TestTransformGolden:
    @pytest.mark.parametrize("path", sorted(_CANNED))
    def test_response_matches_hand_derived_expected_shape(self, stub_client, path):
        res = stub_client.get(path)

        assert res.status_code == 200
        assert res.json() == _expected(path, _CANNED[path])

    def test_all_11_path_config_entries_are_covered(self):
        # Guards the golden-value table against drifting from the real
        # _PATH_CONFIG -- comparing the key sets (not just counts) catches
        # a path being swapped for another while the total stays 11, which
        # a bare length check would silently miss.
        from api.compat import _PATH_CONFIG

        assert set(_CANNED) == set(_PATH_CONFIG)


# ── 2. Edge cases ─────────────────────────────────────────────────────────
class TestTransformEdgeCases:
    def test_null_data_business_error_passes_through_unchanged(self):
        from api.compat import CompatMiddleware

        app = FastAPI()

        async def handler():
            return JSONResponse({"code": 0, "data": None, "msg": "Strategy not found"})

        app.add_api_route("/api/strategies", handler, methods=["GET"])
        app.add_middleware(CompatMiddleware)
        client = TestClient(app)

        res = client.get("/api/strategies")

        assert res.status_code == 200
        assert res.json() == {"code": 0, "data": None, "msg": "Strategy not found"}

    def test_non_200_status_passes_through_untransformed(self):
        from api.compat import CompatMiddleware

        app = FastAPI()

        async def handler():
            return JSONResponse(
                {"code": 1, "data": {"items": [{"id": 1}]}, "msg": "ok"}, status_code=404
            )

        app.add_api_route("/api/strategies", handler, methods=["GET"])
        app.add_middleware(CompatMiddleware)
        client = TestClient(app)

        res = client.get("/api/strategies")

        assert res.status_code == 404
        # Untransformed: "strategies" key must NOT have been added, since
        # dispatch() only transforms on status_code == 200.
        assert "strategies" not in res.json()["data"]

    def test_duplicate_headers_survive_as_order_insensitive_multiset(self):
        from api.compat import CompatMiddleware

        app = FastAPI()

        async def handler():
            response = Response(
                content='{"code": 1, "data": {"items": [{"id": 1}], "total": 1}, "msg": "ok"}',
                media_type="application/json",
            )
            response.raw_headers.append((b"set-cookie", b"a=1"))
            response.raw_headers.append((b"set-cookie", b"b=2"))
            return response

        app.add_api_route("/api/strategies", handler, methods=["GET"])
        app.add_middleware(CompatMiddleware)
        client = TestClient(app)

        res = client.get("/api/strategies")

        assert res.status_code == 200
        cookies = [v for k, v in res.headers.raw if k.lower() == b"set-cookie"]
        assert set(cookies) == {b"a=1", b"b=2"}
        assert len(cookies) == 2

    def test_post_to_a_get_configured_path_bypasses_transform_entirely(self, stub_client):
        res = stub_client.post("/api/strategies")

        assert res.status_code == 200
        # dispatch() only looks up _PATH_CONFIG when request.method == "GET"
        # -- a POST to the same path must come back exactly as the route
        # handler returned it, additive key absent.
        assert res.json() == _CANNED["/api/strategies"]
        assert "strategies" not in res.json()["data"]


# ── 3. Regression traceability: credential compatibility ────────────────────
class TestRegressionCategories:
    def test_credential_create_unaffected_by_unified_middleware(self, client, auth_headers, db_session):
        # CompatCredentialCreate (api/compat.py) is a pydantic body-model
        # swap on api/routers/credentials.py -- a different mechanism from
        # the two BaseHTTPMiddleware classes being consolidated here, and
        # structurally untouched by this refactor. Full CRUD coverage
        # already exists (unedited) in test_compat_credentials.py; this is
        # a named pointer against the 8-category checklist, not new
        # protection.
        res = client.post(
            "/api/credentials/create",
            headers=auth_headers,
            json={
                "name": "Unified Refactor Check",
                "exchange_id": "kis",
                "api_key": "k",
                "secret_key": "s",
                "enable_demo_trading": True,
            },
        )

        assert res.status_code == 200
        assert res.json()["code"] == 1
        assert res.json()["data"]["app_key"] == "****"
