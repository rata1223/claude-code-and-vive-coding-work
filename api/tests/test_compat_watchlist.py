"""TDD suite for P5-02C watchlist compatibility.

Covers:
  1. TestWatchlistRequestMapping   - keyword->q alias, watchlist(JSON)->symbols alias
  2. TestWatchlistResponseMapping  - get/search/hot/prices all unwrap to a bare array
  3. TestRegressionExistingEndpoints - add/remove (already FC) + non-watchlist routes untouched

Written before the WatchlistCompatMiddleware exists (TDD red step) and expected
to pass once it + the api/main.py registration land (green step).
"""
from api.models import WatchlistItem


def _add_watchlist_item(db_session, user, symbol="AAPL", market="NASD", name=None):
    item = WatchlistItem(user_id=user.id, market=market, symbol=symbol, name=name or symbol)
    db_session.add(item)
    db_session.commit()
    return item


# ── 1. Request mapping ───────────────────────────────────────────────────────
class TestWatchlistRequestMapping:
    def test_search_keyword_query_alias_resolves(self, client, auth_headers):
        # Backend wants `q`, frontend sends `keyword`. Without the alias this
        # query is dropped entirely and search silently returns the full
        # unfiltered catalogue instead of an AAPL-only match.
        res = client.get("/api/market/symbols/search?keyword=AAPL", headers=auth_headers)

        assert res.status_code == 200
        symbols = [row["symbol"] for row in res.json()["data"]]
        assert symbols == ["AAPL"]

    def test_search_q_alone_still_works_without_alias(self, client, auth_headers):
        res = client.get("/api/market/symbols/search?q=AAPL", headers=auth_headers)

        assert res.status_code == 200
        symbols = [row["symbol"] for row in res.json()["data"]]
        assert symbols == ["AAPL"]

    def test_search_q_takes_precedence_over_keyword_when_both_present(self, client, auth_headers):
        res = client.get(
            "/api/market/symbols/search?q=AAPL&keyword=MSFT", headers=auth_headers
        )

        assert res.status_code == 200
        symbols = [row["symbol"] for row in res.json()["data"]]
        assert symbols == ["AAPL"]

    def test_search_duplicate_keyword_resolves_to_last_value(self, client, auth_headers):
        # Regression guard: found during /code-review, reproduced live.
        # _alias_query_param must pick the *last* occurrence of a repeated
        # key, matching Starlette's own QueryParams.get() semantics for
        # duplicate params -- otherwise the aliased path (keyword) and the
        # native path (q) would silently disagree on the same input.
        res = client.get(
            "/api/market/symbols/search?keyword=MSFT&keyword=AAPL", headers=auth_headers
        )

        assert res.status_code == 200
        symbols = [row["symbol"] for row in res.json()["data"]]
        assert symbols == ["AAPL"]

    def test_prices_watchlist_json_query_resolves_to_symbols(self, client, auth_headers):
        # Backend wants `symbols` (CSV, required, no default -> 422 without
        # it). Frontend sends a single JSON-encoded `watchlist` param.
        res = client.get(
            '/api/market/watchlist/prices?watchlist=[{"symbol":"AAPL","market":"NASD"}]',
            headers=auth_headers,
        )

        assert res.status_code == 200
        symbols = [row["symbol"] for row in res.json()["data"]]
        assert symbols == ["AAPL"]

    def test_prices_symbols_alone_still_works_without_alias(self, client, auth_headers):
        res = client.get(
            "/api/market/watchlist/prices?symbols=AAPL", headers=auth_headers
        )

        assert res.status_code == 200
        symbols = [row["symbol"] for row in res.json()["data"]]
        assert symbols == ["AAPL"]

    def test_prices_symbols_takes_precedence_over_watchlist(self, client, auth_headers):
        # Matches the equivalent q/keyword precedence coverage: the native
        # param wins, the remap only fires when "symbols" is absent.
        res = client.get(
            '/api/market/watchlist/prices?symbols=AAPL&watchlist=[{"symbol":"MSFT"}]',
            headers=auth_headers,
        )

        assert res.status_code == 200
        symbols = [row["symbol"] for row in res.json()["data"]]
        assert symbols == ["AAPL"]

    def test_prices_multiple_symbols_in_watchlist_json_all_resolve(self, client, auth_headers):
        res = client.get(
            '/api/market/watchlist/prices?watchlist=[{"symbol":"AAPL","market":"NASD"},'
            '{"symbol":"MSFT","market":"NASD"}]',
            headers=auth_headers,
        )

        assert res.status_code == 200
        symbols = [row["symbol"] for row in res.json()["data"]]
        assert symbols == ["AAPL", "MSFT"]

    def test_prices_duplicate_watchlist_param_resolves_to_last_value(self, client, auth_headers):
        # Same regression guard as the search-keyword case, for the
        # watchlist -> symbols alias.
        res = client.get(
            '/api/market/watchlist/prices?watchlist=[{"symbol":"MSFT"}]'
            '&watchlist=[{"symbol":"AAPL"}]',
            headers=auth_headers,
        )

        assert res.status_code == 200
        symbols = [row["symbol"] for row in res.json()["data"]]
        assert symbols == ["AAPL"]

    def test_prices_missing_identifier_still_rejected(self, client, auth_headers):
        res = client.get("/api/market/watchlist/prices", headers=auth_headers)

        assert res.status_code == 422

    def test_prices_malformed_watchlist_json_degrades_to_existing_422(self, client, auth_headers):
        # Not valid JSON -> the alias can't be derived. Must fail the same
        # way this call already fails today (422), not throw a 500 inside
        # the middleware itself.
        res = client.get(
            "/api/market/watchlist/prices?watchlist=not-json", headers=auth_headers
        )

        assert res.status_code == 422


# ── 2. Response mapping ──────────────────────────────────────────────────────
class TestWatchlistResponseMapping:
    def test_get_returns_bare_array(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        _add_watchlist_item(db_session, user, symbol="AAPL")

        res = client.get("/api/market/watchlist/get", headers=auth_headers)

        data = res.json()["data"]
        assert isinstance(data, list)
        assert data[0]["symbol"] == "AAPL"

    def test_get_empty_watchlist_returns_bare_empty_array(self, client, auth_headers):
        res = client.get("/api/market/watchlist/get", headers=auth_headers)

        assert res.json()["data"] == []

    def test_search_returns_bare_array(self, client, auth_headers):
        res = client.get("/api/market/symbols/search?q=AAPL", headers=auth_headers)

        data = res.json()["data"]
        assert isinstance(data, list)
        assert data[0]["symbol"] == "AAPL"

    def test_hot_returns_bare_array(self, client, auth_headers):
        res = client.get("/api/market/symbols/hot?market=NASD", headers=auth_headers)

        data = res.json()["data"]
        assert isinstance(data, list)
        assert len(data) > 0

    def test_prices_returns_bare_array(self, client, auth_headers):
        res = client.get(
            "/api/market/watchlist/prices?symbols=AAPL", headers=auth_headers
        )

        data = res.json()["data"]
        assert isinstance(data, list)
        assert data[0]["symbol"] == "AAPL"


# ── 3. Regression: untouched endpoints ───────────────────────────────────────
class TestRegressionExistingEndpoints:
    def test_add_unaffected(self, client, auth_headers):
        res = client.post(
            "/api/market/watchlist/add",
            headers=auth_headers,
            json={"market": "NASD", "symbol": "AAPL", "name": "Apple Inc."},
        )

        assert res.status_code == 200
        assert res.json()["code"] == 1
        assert res.json()["data"]["symbol"] == "AAPL"

    def test_remove_unaffected(self, client, auth_headers, db_session, seed_user):
        user, _ = seed_user
        _add_watchlist_item(db_session, user, symbol="AAPL")

        res = client.post(
            "/api/market/watchlist/remove", headers=auth_headers, json={"symbol": "AAPL"}
        )

        assert res.status_code == 200
        assert res.json()["code"] == 1

    def test_unrelated_endpoint_strategies_list_unaffected(self, client, auth_headers):
        # Proves the new middleware doesn't leak into Phase 1B's paths.
        res = client.get("/api/strategies", headers=auth_headers)

        assert res.status_code == 200
        assert "strategies" in res.json()["data"]

    def test_unrelated_endpoint_login_unaffected(self, client, seed_user):
        user, password = seed_user

        res = client.post("/api/auth/login", json={"username": user.email, "password": password})

        assert res.status_code == 200
        assert res.json()["data"]["token"]

    def test_content_type_survives_response_rebuild(self, client, auth_headers):
        res = client.get("/api/market/symbols/hot?market=NASD", headers=auth_headers)

        assert res.status_code == 200
        assert res.headers["content-type"] == "application/json"

    def test_cors_headers_survive_response_rebuild(self, client, auth_headers):
        res = client.get(
            "/api/market/watchlist/get",
            headers={**auth_headers, "Origin": "http://localhost:5173"},
        )

        assert res.status_code == 200
        assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"
