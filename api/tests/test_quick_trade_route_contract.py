"""P0 — every quick-trade endpoint the UI calls must actually exist.

The audit found 19 frontend endpoints with no backend route behind them (the
whole ``fast-analysis``, ``community`` and ``billing`` surfaces). Those are out
of scope to fix, but the *trading* surface must never join them: a Cancel button
posting to a 404 would tell the user, through the interceptor's generic
fallback, only that the "request failed" — while their order stays in the market.

This walks the real API client of both apps and asserts each ``quick-trade``
path is registered on the FastAPI app.
"""
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_APPS = ("frontend", "mobile")

_CALL = re.compile(r"http\.(get|post|put|delete)\(\s*['\"`](/api/quick-trade/[^'\"`]+)")


def _registered_paths():
    from api.main import app

    return {
        (method.upper(), route.path)
        for route in app.routes
        for method in getattr(route, "methods", set()) or set()
    }


def _client_calls(app_dir):
    source = (_REPO / app_dir / "src" / "api" / "index.js").read_text()
    return {(m.group(1).upper(), m.group(2)) for m in _CALL.finditer(source)}


@pytest.mark.parametrize("app_dir", _APPS)
def test_every_quick_trade_call_hits_a_real_route(app_dir):
    calls = _client_calls(app_dir)
    assert calls, f"{app_dir}: found no quick-trade calls — the regex has rotted"

    missing = sorted(calls - _registered_paths())
    assert not missing, (
        f"{app_dir}/src/api/index.js calls quick-trade endpoints that are not "
        f"registered on the FastAPI app: {missing}"
    )


@pytest.mark.parametrize("app_dir", _APPS)
def test_both_apps_call_the_same_quick_trade_surface(app_dir):
    """Parity at the contract level, not just the view file."""
    assert _client_calls(app_dir) == _client_calls("frontend")


def test_the_new_p0_routes_are_reachable():
    """Named explicitly so deleting one fails here rather than in the browser."""
    registered = _registered_paths()

    assert ("GET", "/api/quick-trade/open-orders") in registered
    assert ("POST", "/api/quick-trade/cancel-order") in registered
    assert ("POST", "/api/quick-trade/emergency-flatten") in registered
