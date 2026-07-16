"""TDD suite for P5-02B strategy compatibility.

Covers:
  1. TestStrategyRequestMapping        - id query alias resolves to strategy_id
  2. TestStrategyResponseMapping        - trades/positions/logs/strategies/unread keys present
  3. TestStrategyBackwardCompatibility  - old items/count keys still present (additive, not replaced)
  4. TestRegressionExistingEndpoints    - untouched strategy + non-strategy endpoints keep working

Written before the api/compat.py middleware exists (TDD red step) and expected
to pass once the middleware + api/main.py registration land (green step).
"""
from datetime import date, datetime

from api.models import Notification, Strategy, StrategyLog, Trade


def _create_strategy(db_session, user, **overrides):
    defaults = dict(
        user_id=user.id,
        name="Momentum Bot",
        type="indicator",
        status="stopped",
        symbol="AAPL",
        timeframe="1h",
        market_type="spot",
        direction="long",
        initial_capital=10000.0,
        config={},
    )
    defaults.update(overrides)
    s = Strategy(**defaults)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _add_trade(db_session, strategy, **overrides):
    defaults = dict(
        strategy_id=strategy.id,
        symbol="AAPL",
        side="buy",
        qty=10,
        price=150.0,
        filled_at=datetime(2026, 1, 5),
        pnl=25.0,
        fee=1.0,
    )
    defaults.update(overrides)
    t = Trade(**defaults)
    db_session.add(t)
    db_session.commit()
    return t


def _add_log(db_session, strategy):
    lg = StrategyLog(strategy_id=strategy.id, message="Strategy started", level="INFO")
    db_session.add(lg)
    db_session.commit()
    return lg


def _add_notification(db_session, user, is_read=False):
    n = Notification(user_id=user.id, title="Fill", message="Order filled", is_read=is_read)
    db_session.add(n)
    db_session.commit()
    return n


# ── 1. Request mapping ───────────────────────────────────────────────────────
class TestStrategyRequestMapping:
    def test_trades_id_query_alias_resolves(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)
        _add_trade(db_session, s)

        res = client.get(f"/api/strategies/trades?id={s.id}", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["code"] == 1
        assert res.json()["data"]["total"] == 1

    def test_positions_id_query_alias_resolves(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)
        _add_trade(db_session, s, side="buy", qty=5)

        res = client.get(f"/api/strategies/positions?id={s.id}", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["code"] == 1

    def test_equity_curve_id_query_alias_resolves(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)

        res = client.get(f"/api/strategies/equityCurve?id={s.id}", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["code"] == 1

    def test_performance_id_query_alias_resolves(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)

        res = client.get(f"/api/strategies/performance?id={s.id}", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["code"] == 1

    def test_logs_id_query_alias_resolves(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)
        _add_log(db_session, s)

        res = client.get(f"/api/strategies/logs?id={s.id}", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["data"]["total"] == 1

    def test_strategy_id_alone_still_works_without_alias(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)

        res = client.get(f"/api/strategies/trades?strategy_id={s.id}", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["code"] == 1

    def test_strategy_id_takes_precedence_over_id_when_both_present(
        self, client, auth_headers, db_session, seed_user
    ):
        user, _ = seed_user
        real = _create_strategy(db_session, user, name="Real")
        decoy = _create_strategy(db_session, user, name="Decoy")
        _add_trade(db_session, real)

        # id points at the decoy strategy; strategy_id points at the real one.
        # strategy_id must win.
        res = client.get(
            f"/api/strategies/trades?strategy_id={real.id}&id={decoy.id}", headers=auth_headers
        )

        assert res.status_code == 200
        assert res.json()["data"]["total"] == 1

    def test_missing_identifier_still_rejected(self, client, auth_headers):
        res = client.get("/api/strategies/trades", headers=auth_headers)

        assert res.status_code == 422

    def test_blank_sibling_query_value_survives_the_id_remap(self, client, auth_headers, db_session, seed_user):
        # Regression guard: found during /code-review — parse_qsl without
        # keep_blank_values=True would silently drop a blank-valued sibling
        # param (e.g. level=) when rewriting id -> strategy_id, rather than
        # leaving it blank. level='' should behave identically to level
        # omitted (get_logs's `if level:` check), not error or 422.
        user, _ = seed_user
        s = _create_strategy(db_session, user)
        _add_log(db_session, s)

        res = client.get(f"/api/strategies/logs?id={s.id}&level=", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["data"]["total"] == 1

    def test_repeated_sibling_query_param_survives_the_id_remap(
        self, client, auth_headers, db_session, seed_user
    ):
        # Regression guard: found during CodeRabbit review — parsing the query
        # string into a dict collapsed repeated sibling params (any key other
        # than id/strategy_id sharing the query string) down to their last
        # value whenever the id -> strategy_id remap fired.
        user, _ = seed_user
        s = _create_strategy(db_session, user)
        _add_log(db_session, s)

        res = client.get(f"/api/strategies/logs?tag=a&tag=b&id={s.id}", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["data"]["total"] == 1


# ── 2. Response mapping ──────────────────────────────────────────────────────
class TestStrategyResponseMapping:
    def test_list_adds_strategies_key(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        _create_strategy(db_session, user)

        res = client.get("/api/strategies", headers=auth_headers)

        data = res.json()["data"]
        assert "strategies" in data
        assert data["strategies"] == data["items"]

    def test_trades_adds_trades_key(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)
        _add_trade(db_session, s)

        res = client.get(f"/api/strategies/trades?strategy_id={s.id}", headers=auth_headers)

        data = res.json()["data"]
        assert "trades" in data
        assert data["trades"] == data["items"]
        assert len(data["trades"]) == 1

    def test_positions_adds_positions_key(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)
        _add_trade(db_session, s, side="buy", qty=8)

        res = client.get(f"/api/strategies/positions?strategy_id={s.id}", headers=auth_headers)

        data = res.json()["data"]
        assert "positions" in data
        assert data["positions"] == data["items"]

    def test_logs_adds_logs_key(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)
        _add_log(db_session, s)

        res = client.get(f"/api/strategies/logs?strategy_id={s.id}", headers=auth_headers)

        data = res.json()["data"]
        assert "logs" in data
        assert data["logs"] == data["items"]

    def test_equity_curve_returns_bare_array(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)
        _add_trade(db_session, s)

        res = client.get(f"/api/strategies/equityCurve?strategy_id={s.id}", headers=auth_headers)

        data = res.json()["data"]
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_unread_count_adds_unread_key(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        _add_notification(db_session, user, is_read=False)

        res = client.get("/api/strategies/notifications/unread-count", headers=auth_headers)

        data = res.json()["data"]
        assert data["unread"] == 1
        assert data["unread"] == data["count"]

    def test_non_ascii_strategy_name_round_trips_without_unicode_escaping(
        self, client, auth_headers, db_session, seed_user
    ):
        user, _ = seed_user
        _create_strategy(db_session, user, name="한국주식전략")

        res = client.get("/api/strategies", headers=auth_headers)

        # The re-serialized response must match FastAPI's own JSONResponse
        # formatting (ensure_ascii=False) — raw UTF-8 text on the wire, not
        # \uXXXX escapes, and the parsed value must be unchanged either way.
        assert "한국주식전략" in res.text
        assert "\\u" not in res.text
        assert res.json()["data"]["strategies"][0]["name"] == "한국주식전략"

    def test_performance_response_shape_unchanged(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)
        _add_trade(db_session, s)

        res = client.get(f"/api/strategies/performance?strategy_id={s.id}", headers=auth_headers)

        data = res.json()["data"]
        assert "total_trades" in data
        assert "items" not in data  # never had an items wrapper to begin with


# ── 3. Backward compatibility ────────────────────────────────────────────────
class TestStrategyBackwardCompatibility:
    def test_old_items_key_still_present_on_trades(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)
        _add_trade(db_session, s)

        res = client.get(f"/api/strategies/trades?strategy_id={s.id}", headers=auth_headers)

        data = res.json()["data"]
        assert "items" in data
        assert "total" in data

    def test_old_count_key_still_present_on_unread(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        _add_notification(db_session, user)

        res = client.get("/api/strategies/notifications/unread-count", headers=auth_headers)

        assert "count" in res.json()["data"]

    def test_old_items_key_still_present_on_list(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        _create_strategy(db_session, user)

        res = client.get("/api/strategies", headers=auth_headers)

        assert "items" in res.json()["data"]


# ── 4. Regression: untouched endpoints ───────────────────────────────────────
class TestRegressionExistingEndpoints:
    def test_strategy_create_unaffected(self, client, auth_headers):
        res = client.post(
            "/api/strategies/create",
            headers=auth_headers,
            json={"name": "New Strategy", "type": "indicator", "symbol": "MSFT"},
        )

        assert res.status_code == 200
        assert res.json()["code"] == 1
        assert res.json()["data"]["name"] == "New Strategy"

    def test_strategy_detail_unaffected(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)

        res = client.get(f"/api/strategies/detail?id={s.id}", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["data"]["id"] == s.id
        assert "trades" not in res.json()["data"]  # detail response untouched, no strategy list keys leaking in

    def test_strategy_start_stop_unaffected(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)

        start_res = client.post(f"/api/strategies/start?id={s.id}", headers=auth_headers)
        assert start_res.status_code == 200
        assert start_res.json()["data"]["status"] == "running"

        stop_res = client.post(f"/api/strategies/stop?id={s.id}", headers=auth_headers)
        assert stop_res.status_code == 200
        assert stop_res.json()["data"]["status"] == "stopped"

    def test_strategy_delete_unaffected(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        s = _create_strategy(db_session, user)

        res = client.delete(f"/api/strategies/delete?id={s.id}", headers=auth_headers)

        assert res.status_code == 200
        assert res.json()["code"] == 1

    def test_test_connection_unaffected(self, client, auth_headers):
        res = client.post("/api/strategies/test-connection", headers=auth_headers, json={})

        assert res.status_code == 200
        assert res.json()["data"]["connected"] is True

    def test_unrelated_endpoint_login_unaffected(self, client, seed_user):
        user, password = seed_user

        res = client.post("/api/auth/login", json={"username": user.email, "password": password})

        assert res.status_code == 200
        assert res.json()["data"]["token"]

    def test_unrelated_endpoint_credentials_create_unaffected(self, client, auth_headers):
        res = client.post(
            "/api/credentials/create",
            headers=auth_headers,
            json={"name": "A", "exchange_id": "kis", "api_key": "k", "secret_key": "s"},
        )

        assert res.status_code == 200
        assert res.json()["code"] == 1

    def test_cors_headers_survive_response_rebuild(self, client, auth_headers, db_session, seed_user):
        # Regression guard: _rebuild constructs a fresh Response once the
        # original's body has been consumed — this must not drop headers
        # set by other middleware (CORSMiddleware) further along the chain.
        user, _ = seed_user
        _create_strategy(db_session, user)

        res = client.get(
            "/api/strategies",
            headers={**auth_headers, "Origin": "http://localhost:5173"},
        )

        assert res.status_code == 200
        assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_content_type_survives_response_rebuild(self, client, auth_headers, db_session, seed_user):
        # Regression guard: found during CodeRabbit review — BaseHTTPMiddleware's
        # call_next() hands back a streaming wrapper with media_type=None
        # regardless of the route's actual response, so passing
        # response.media_type into the rebuilt Response silently dropped
        # Content-Type entirely instead of preserving "application/json".
        user, _ = seed_user
        _create_strategy(db_session, user)

        res = client.get("/api/strategies", headers=auth_headers)

        assert res.status_code == 200
        assert res.headers["content-type"] == "application/json"
