"""TDD suite for P5-03B: Orders (quick-trade) compatibility.

Covers, per docs/P5_ORDERS_COMPAT_AUDIT.md's approved mapping:
  1. TestBodyRemapIsolated        - new `body_remap` _PathConfig field, in
                                     isolation (DB-free stub app).
  2. TestResponseTransformIsolated - new `response_transform` _PathConfig
                                     field, in isolation.
  3. TestMethodGate                - the per-entry `methods` field, proving
                                     existing GET-only _PATH_CONFIG entries
                                     stay byte-identical now that a second
                                     table (_ORDERS_PATH_CONFIG) and POST
                                     support exist.
  4. TestOrdersTransformGolden     - hand-derived golden values for all 4
                                     _ORDERS_PATH_CONFIG entries, same
                                     philosophy as test_compat_unified.py's
                                     TestTransformGolden (never imports the
                                     old middleware, algorithmic not
                                     captured-output).
  5. Full-stack end-to-end tests (real api.main.app, fake KIS broker via
     monkeypatch -- no test anywhere touches quick_trade.py's real broker
     calls) for getBalance/getPosition/placeOrder/getHistory, plus a
     regression pin proving closePosition is deliberately left unfixed
     (see docs/P5_ORDERS_COMPAT.md's "remaining gaps").

Written before `body_remap`/`response_transform`/`methods`/
`_ORDERS_PATH_CONFIG` exist in api/compat.py (TDD red step) and expected
to pass once they land (green step). api/tests/test_compat_unified.py and
every other pre-existing test file are NOT modified by this task.
"""
import copy
import json
from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request


# ── Shared fixtures / helpers for full-stack (Section: end-to-end) tests ────

class _FakeOrders:
    """Records every call so tests can assert the mapped qty/market/side
    actually reached the broker call, not just that Pydantic validation
    passed."""

    def __init__(self):
        self.calls = []

    def buy_us(self, symbol, excd, qty, price):
        self.calls.append(("buy_us", symbol, excd, qty, price))
        return {"output": {"ODNO": "ORDER-BUY-US"}}

    def sell_us(self, symbol, excd, qty, price):
        self.calls.append(("sell_us", symbol, excd, qty, price))
        return {"output": {"ODNO": "ORDER-SELL-US"}}

    def buy_kr(self, symbol, qty, price):
        self.calls.append(("buy_kr", symbol, qty, price))
        return {"output": {"ODNO": "ORDER-BUY-KR"}}

    def sell_kr(self, symbol, qty, price):
        self.calls.append(("sell_kr", symbol, qty, price))
        return {"output": {"ODNO": "ORDER-SELL-KR"}}


class _FakePortfolio:
    """Raw-shaped like the real KIS response (`pdno`/`ovrs_pdno`,
    `hldg_qty`/`ovrs_cblc_qty`) since api/routers/quick_trade.py's
    /position handler matches on these exact keys."""

    def get_us_balance(self):
        return {
            "summary": {"tot_evlu_amt": "1000.50", "frcr_dncl_amt_2": "200.25"},
            "positions": [{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "5", "pchs_avg_pric": "150.0"}],
        }

    def get_kr_balance(self):
        return {
            "summary": {"tot_evlu_amt": "500", "dnca_tot_amt": "100"},
            "positions": [{"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "70000"}],
        }


@pytest.fixture()
def fake_kis(monkeypatch):
    fake_orders = _FakeOrders()
    fake_portfolio = _FakePortfolio()
    monkeypatch.setattr(
        "api.routers.quick_trade._load_kis", lambda cred: (None, fake_orders, fake_portfolio)
    )
    return fake_orders, fake_portfolio


def _seed_credential(client, auth_headers) -> int:
    res = client.post(
        "/api/credentials/create",
        headers=auth_headers,
        json={"name": "Orders Test", "exchange_id": "kis", "app_key": "k", "app_secret": "s"},
    )
    return res.json()["data"]["id"]


# ── 1. body_remap, isolated (DB-free) ────────────────────────────────────────

def _orders_echo_stub_app() -> FastAPI:
    """Echoes back the exact raw + parsed body CompatMiddleware forwards --
    proves body_remap's rewrite (or fail-safe no-op) reaches the route."""
    from api.compat import CompatMiddleware

    app = FastAPI()

    @app.post("/api/quick-trade/place-order")
    async def echo(request: Request):
        raw = await request.body()
        try:
            parsed = json.loads(raw)
            # FastAPI's own JSONResponse.render() uses allow_nan=False --
            # reject non-finite floats here too, or this handler's own
            # response would crash trying to echo them back.
            json.dumps(parsed, allow_nan=False)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            parsed = None
        return JSONResponse({"raw": raw.decode("utf-8", errors="replace"), "parsed": parsed})

    app.add_middleware(CompatMiddleware)
    return app


@pytest.fixture(scope="module")
def orders_echo_client():
    return TestClient(_orders_echo_stub_app())


class TestBodyRemapIsolated:
    def test_alias_body_renames_single_field_when_new_key_absent(self, orders_echo_client):
        res = orders_echo_client.post("/api/quick-trade/place-order", json={"amount": 5, "symbol": "AAPL"})

        parsed = res.json()["parsed"]
        assert parsed["qty"] == 5
        assert parsed["amount"] == 5  # old key survives -- additive

    def test_alias_body_renames_multiple_fields_in_one_pass(self, orders_echo_client):
        res = orders_echo_client.post(
            "/api/quick-trade/place-order", json={"amount": 5, "market_type": "kr"}
        )

        parsed = res.json()["parsed"]
        assert parsed["qty"] == 5
        assert parsed["market"] == "kr"

    def test_alias_body_leaves_body_untouched_when_new_key_already_present(self, orders_echo_client):
        res = orders_echo_client.post("/api/quick-trade/place-order", json={"amount": 5, "qty": 3})

        assert res.json()["parsed"]["qty"] == 3  # native key wins, not overwritten

    def test_alias_body_leaves_body_untouched_when_old_key_absent(self, orders_echo_client):
        res = orders_echo_client.post("/api/quick-trade/place-order", json={"symbol": "AAPL"})

        parsed = res.json()["parsed"]
        assert "qty" not in parsed
        assert "market" not in parsed

    def test_alias_body_fails_safe_on_malformed_json(self, orders_echo_client):
        res = orders_echo_client.post(
            "/api/quick-trade/place-order",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )

        assert res.status_code == 200
        assert res.json()["raw"] == "{not json"
        assert res.json()["parsed"] is None

    def test_alias_body_fails_safe_on_non_dict_json(self, orders_echo_client):
        res = orders_echo_client.post(
            "/api/quick-trade/place-order",
            content=b"[1, 2, 3]",
            headers={"Content-Type": "application/json"},
        )

        assert res.status_code == 200
        assert res.json()["parsed"] == [1, 2, 3]

    def test_alias_body_fails_safe_on_non_finite_float_values(self, orders_echo_client):
        # `json.loads` accepts NaN/Infinity as a non-standard Python
        # extension, but `_dumps_like_fastapi`'s `allow_nan=False` would
        # raise on re-encode if the rename fires and tries to re-serialize.
        # Must not crash mid-middleware -- same fail-safe contract as an
        # undecodable body. Asserted against the raw echoed string (not
        # "parsed", which the echo handler's own JSONResponse can't
        # round-trip through `inf` either -- a test-handler artifact,
        # unrelated to what's being proven about the middleware).
        res = orders_echo_client.post(
            "/api/quick-trade/place-order",
            content=b'{"amount": Infinity, "symbol": "AAPL"}',
            headers={"Content-Type": "application/json"},
        )

        assert res.status_code == 200
        assert res.json()["raw"] == '{"amount": Infinity, "symbol": "AAPL"}'  # untouched, not renamed


# ── 2. response_transform, isolated (DB-free) ────────────────────────────────

def _position_stub_app(position_payload: Dict[str, Any]) -> FastAPI:
    from api.compat import CompatMiddleware

    app = FastAPI()

    @app.get("/api/quick-trade/position")
    async def handler():
        return JSONResponse(position_payload)

    app.add_middleware(CompatMiddleware)
    return app


class TestResponseTransformIsolated:
    def test_position_transform_wraps_dict_into_single_element_list(self):
        payload = {
            "code": 1,
            "data": {"symbol": "AAPL", "position": {"pdno": "AAPL", "hldg_qty": "5"}},
            "msg": "ok",
        }
        client = TestClient(_position_stub_app(payload))

        res = client.get("/api/quick-trade/position")

        assert res.json()["data"]["positions"] == [{"pdno": "AAPL", "hldg_qty": "5"}]

    def test_position_transform_wraps_null_into_empty_list(self):
        payload = {"code": 1, "data": {"symbol": "AAPL", "position": None}, "msg": "ok"}
        client = TestClient(_position_stub_app(payload))

        res = client.get("/api/quick-trade/position")

        assert res.json()["data"]["positions"] == []

    def test_position_transform_is_additive_singular_key_survives(self):
        payload = {"code": 1, "data": {"symbol": "AAPL", "position": {"pdno": "AAPL"}}, "msg": "ok"}
        client = TestClient(_position_stub_app(payload))

        res = client.get("/api/quick-trade/position")

        assert res.json()["data"]["position"] == {"pdno": "AAPL"}

    def test_position_transform_noop_when_position_key_absent(self):
        payload = {"code": 1, "data": {"symbol": "AAPL"}, "msg": "ok"}
        client = TestClient(_position_stub_app(payload))

        res = client.get("/api/quick-trade/position")

        assert "positions" not in res.json()["data"]


# ── 3. Method gate ────────────────────────────────────────────────────────

class TestMethodGate:
    def test_get_only_path_config_entry_still_bypasses_on_post(self):
        from api.compat import CompatMiddleware

        app = FastAPI()
        payload = {"code": 1, "data": {"items": [{"id": 1, "name": "A"}], "total": 1}, "msg": "ok"}

        @app.api_route("/api/strategies", methods=["GET", "POST"])
        async def handler():
            return JSONResponse(payload)

        app.add_middleware(CompatMiddleware)
        client = TestClient(app)

        res = client.post("/api/strategies")

        assert res.status_code == 200
        assert res.json() == payload
        assert "strategies" not in res.json()["data"]

    def test_post_only_orders_entry_does_not_fire_on_get(self):
        from api.compat import CompatMiddleware

        app = FastAPI()
        payload = {"amount": 5, "market_type": "us"}

        @app.get("/api/quick-trade/place-order")
        async def handler():
            return JSONResponse(payload)

        app.add_middleware(CompatMiddleware)
        client = TestClient(app)

        res = client.get("/api/quick-trade/place-order")

        assert res.status_code == 200
        assert res.json() == payload  # config.methods=("POST",) excludes GET

    def test_non_200_response_on_configured_get_orders_path_is_not_transformed(self):
        from api.compat import CompatMiddleware

        app = FastAPI()

        @app.get("/api/quick-trade/history")
        async def handler():
            return JSONResponse(
                {"code": -1, "data": {"total": 0, "items": []}, "msg": "error"}, status_code=500
            )

        app.add_middleware(CompatMiddleware)
        client = TestClient(app)

        res = client.get("/api/quick-trade/history")

        assert res.status_code == 500
        assert "trades" not in res.json()["data"]


# ── 4. Golden-value response transform, all 4 _ORDERS_PATH_CONFIG entries ───

_ORDERS_CANNED: Dict[str, Dict[str, Any]] = {
    "/api/quick-trade/balance": {
        "code": 1,
        "data": {"currency": "USD", "total_eval": 1000.5, "cash": 200.25, "positions": []},
        "msg": "ok",
    },
    "/api/quick-trade/position": {
        "code": 1,
        "data": {"symbol": "AAPL", "position": {"pdno": "AAPL", "hldg_qty": "5"}},
        "msg": "ok",
    },
    "/api/quick-trade/place-order": {
        "code": 1,
        "data": {
            "order_id": "ORDER1",
            "symbol": "AAPL",
            "side": "buy",
            "qty": 5,
            "price": 150.0,
            "status": "submitted",
        },
        "msg": "ok",
    },
    "/api/quick-trade/history": {
        "code": 1,
        "data": {"total": 1, "items": [{"id": 1, "symbol": "AAPL"}]},
        "msg": "ok",
    },
}

_ORDERS_METHOD: Dict[str, str] = {
    "/api/quick-trade/balance": "GET",
    "/api/quick-trade/position": "GET",
    "/api/quick-trade/place-order": "POST",
    "/api/quick-trade/history": "GET",
}


def _orders_expected(path: str, canned: Dict[str, Any]) -> Dict[str, Any]:
    """Hand-derived from the _ORDERS_PATH_CONFIG algorithm documented in
    docs/P5_ORDERS_COMPAT.md -- independent of _ORDERS_PATH_CONFIG's actual
    contents, so a real entry diverging from this table is exactly what
    this test is meant to catch."""
    expected = copy.deepcopy(canned)
    data = expected["data"]
    if path == "/api/quick-trade/balance":
        data["available"] = data["cash"]
        data["total"] = data["total_eval"]
    elif path == "/api/quick-trade/position":
        data["positions"] = [data["position"]]
    elif path == "/api/quick-trade/place-order":
        pass  # body_remap only -- no response transform for this path
    elif path == "/api/quick-trade/history":
        data["trades"] = data["items"]
    else:
        raise AssertionError(f"no golden-value rule for {path!r} -- add one")
    return expected


def _orders_stub_app() -> FastAPI:
    from api.compat import CompatMiddleware

    app = FastAPI()
    for path, payload in _ORDERS_CANNED.items():
        app.add_api_route(path, _make_orders_handler(payload), methods=["GET", "POST"])
    app.add_middleware(CompatMiddleware)
    return app


def _make_orders_handler(payload: Dict[str, Any]):
    async def handler():
        return JSONResponse(payload)

    return handler


@pytest.fixture(scope="module")
def orders_stub_client():
    return TestClient(_orders_stub_app())


class TestOrdersTransformGolden:
    @pytest.mark.parametrize("path", sorted(_ORDERS_CANNED))
    def test_response_matches_hand_derived_expected_shape(self, orders_stub_client, path):
        res = orders_stub_client.request(_ORDERS_METHOD[path], path)

        assert res.status_code == 200
        assert res.json() == _orders_expected(path, _ORDERS_CANNED[path])

    def test_all_4_orders_path_config_entries_are_covered(self):
        from api.compat import _ORDERS_PATH_CONFIG

        assert set(_ORDERS_CANNED) == set(_ORDERS_PATH_CONFIG)


# ── 5. Full-stack end-to-end ────────────────────────────────────────────────

class TestGetBalanceEndToEnd:
    def test_market_type_alias_forwards_value_to_market_param(self, client, auth_headers, db_session, fake_kis):
        # "kr" is a convenient test value that hits a distinguishable
        # backend code branch (the KR-vs-US balance lookup) -- it does NOT
        # reflect real frontend traffic. The live UI's `marketType` is a
        # crypto-exchange concept ("spot"/"swap"), never "kr"; see the
        # correction note in docs/P5_ORDERS_COMPAT.md's Request mapping
        # section. This test proves the alias mechanism forwards whatever
        # value it's given, not that the live UI can reach the KR branch.
        cred_id = _seed_credential(client, auth_headers)

        res = client.get(
            f"/api/quick-trade/balance?credential_id={cred_id}&market_type=kr", headers=auth_headers
        )

        assert res.status_code == 200
        assert res.json()["data"]["currency"] == "KRW"

    def test_native_market_param_still_works_unchanged(self, client, auth_headers, db_session, fake_kis):
        cred_id = _seed_credential(client, auth_headers)

        res = client.get(
            f"/api/quick-trade/balance?credential_id={cred_id}&market=us", headers=auth_headers
        )

        assert res.status_code == 200
        assert res.json()["data"]["currency"] == "USD"

    def test_available_and_total_keys_are_added_alongside_cash_and_total_eval(
        self, client, auth_headers, db_session, fake_kis
    ):
        cred_id = _seed_credential(client, auth_headers)

        res = client.get(f"/api/quick-trade/balance?credential_id={cred_id}", headers=auth_headers)

        data = res.json()["data"]
        assert data["available"] == data["cash"]
        assert data["total"] == data["total_eval"]


class TestGetPositionEndToEnd:
    def test_market_type_alias_and_positions_array_added_when_found(
        self, client, auth_headers, db_session, fake_kis
    ):
        cred_id = _seed_credential(client, auth_headers)

        res = client.get(
            f"/api/quick-trade/position?credential_id={cred_id}&symbol=AAPL&market_type=us",
            headers=auth_headers,
        )

        assert res.status_code == 200
        data = res.json()["data"]
        assert data["position"] is not None
        assert data["positions"] == [data["position"]]

    def test_positions_is_empty_list_when_position_not_found(self, client, auth_headers, db_session, fake_kis):
        cred_id = _seed_credential(client, auth_headers)

        res = client.get(
            f"/api/quick-trade/position?credential_id={cred_id}&symbol=NOPE&market=us",
            headers=auth_headers,
        )

        data = res.json()["data"]
        assert data["position"] is None
        assert data["positions"] == []

    def test_singular_position_key_still_present_for_backward_compat(
        self, client, auth_headers, db_session, fake_kis
    ):
        cred_id = _seed_credential(client, auth_headers)

        res = client.get(
            f"/api/quick-trade/position?credential_id={cred_id}&symbol=AAPL&market=us",
            headers=auth_headers,
        )

        assert "position" in res.json()["data"]


class TestPlaceOrderEndToEnd:
    def test_frontend_shape_amount_and_market_type_map_onto_qty_and_market(
        self, client, auth_headers, db_session, fake_kis
    ):
        cred_id = _seed_credential(client, auth_headers)
        fake_orders, _ = fake_kis

        res = client.post(
            "/api/quick-trade/place-order",
            headers=auth_headers,
            json={
                "credential_id": cred_id,
                "symbol": "AAPL",
                "side": "buy",
                "order_type": "market",
                "amount": 5,
                "price": 150.0,
                "leverage": 1,
                "market_type": "us",
                "source": "manual",
            },
        )

        assert res.status_code == 200
        assert res.json()["code"] == 1
        assert fake_orders.calls == [("buy_us", "AAPL", "NASD", 5, 150.0)]

    def test_native_backend_shape_still_works_unchanged(self, client, auth_headers, db_session, fake_kis):
        cred_id = _seed_credential(client, auth_headers)

        res = client.post(
            "/api/quick-trade/place-order",
            headers=auth_headers,
            json={
                "credential_id": cred_id,
                "symbol": "AAPL",
                "side": "buy",
                "qty": 3,
                "price": 150.0,
                "market": "us",
            },
        )

        assert res.status_code == 200
        assert res.json()["data"]["qty"] == 3

    def test_native_qty_takes_precedence_when_both_amount_and_qty_sent(
        self, client, auth_headers, db_session, fake_kis
    ):
        cred_id = _seed_credential(client, auth_headers)

        res = client.post(
            "/api/quick-trade/place-order",
            headers=auth_headers,
            json={
                "credential_id": cred_id,
                "symbol": "AAPL",
                "side": "buy",
                "amount": 5,
                "qty": 3,
                "price": 150.0,
                "market": "us",
            },
        )

        assert res.status_code == 200
        assert res.json()["data"]["qty"] == 3

    def test_missing_required_fields_still_422s_as_before(self, client, auth_headers, db_session, fake_kis):
        cred_id = _seed_credential(client, auth_headers)

        res = client.post(
            "/api/quick-trade/place-order",
            headers=auth_headers,
            json={
                "credential_id": cred_id,
                # "symbol" intentionally omitted
                "side": "buy",
                "amount": 5,
                "price": 150.0,
                "market_type": "us",
            },
        )

        assert res.status_code == 422

    def test_extra_frontend_only_fields_are_ignored_not_fatal(self, client, auth_headers, db_session, fake_kis):
        cred_id = _seed_credential(client, auth_headers)

        res = client.post(
            "/api/quick-trade/place-order",
            headers=auth_headers,
            json={
                "credential_id": cred_id,
                "symbol": "AAPL",
                "side": "buy",
                "order_type": "limit",
                "amount": 5,
                "price": 150.0,
                "leverage": 3,
                "market_type": "us",
                "source": "manual",
            },
        )

        assert res.status_code == 200


class TestGetHistoryEndToEnd:
    def test_items_and_trades_keys_both_present_with_empty_history(self, client, auth_headers, db_session):
        res = client.get("/api/quick-trade/history", headers=auth_headers)

        assert res.status_code == 200
        data = res.json()["data"]
        assert data["items"] == []
        assert data["trades"] == []


class TestClosePositionGapClosed:
    """The former 422 gap, closed by P0-07C -- without touching compat or the UI.

    close-position remains deliberately absent from `_ORDERS_PATH_CONFIG`: the
    adapter still refuses to invent a limit price from stale cost basis. The fix
    moved that decision server-side (live position + live quote), so the shipped
    frontend payload -- which sends neither qty nor price -- now validates and
    executes through the hardened path.
    """

    def test_frontend_payload_without_qty_or_price_now_executes(
        self, client, auth_headers, db_session, fake_kis, monkeypatch
    ):
        cred_id = _seed_credential(client, auth_headers)

        class _FakeMarketData:
            def get_price_us(self, symbol, excd):
                return 175.5

            def get_price_kr(self, symbol):  # pragma: no cover - us path here
                return 70000

        monkeypatch.setattr("api.routers.quick_trade._load_market_data",
                            lambda client: _FakeMarketData())

        from api.main import app
        from api.routers.quick_trade import get_risk_gate
        app.dependency_overrides[get_risk_gate] = lambda: (lambda: None)
        try:
            res = client.post(
                "/api/quick-trade/close-position",
                headers=auth_headers,
                json={
                    "credential_id": cred_id,
                    "symbol": "AAPL",
                    "market_type": "us",      # still untranslated by compat
                    "position_side": "long",  # ignored by the schema
                    "source": "manual",       # ignored by the schema
                },
            )
        finally:
            app.dependency_overrides.pop(get_risk_gate, None)

        assert res.status_code == 200          # no longer a validation failure
        body = res.json()
        assert body["code"] == 1, body["msg"]
        # qty came from the live holding (_FakePortfolio: ovrs_cblc_qty "5"),
        # never from the client, and the price is the live quote.
        assert body["data"]["qty"] == 5
        assert body["data"]["price"] == 175.5
        assert body["data"]["side"] == "sell"

    def test_close_position_is_still_excluded_from_compat(self):
        from api.compat import _ORDERS_PATH_CONFIG

        assert not any("close-position" in p for p in _ORDERS_PATH_CONFIG)
