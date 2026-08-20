"""P0 — an authenticated path to the emergency liquidation control.

``EmergencyFlattenManager`` is reachable only through ``POST /api/admin/flatten``
on the Flask ops API (:5001), behind ``X-API-Key``. The Vue apps talk to FastAPI
(:8000) with a JWT bearer and must never hold that key. So the one control that
exists for getting flat in a drawdown is, from the product, unreachable — an
operator has to `curl` it.

This is a **thin proxy**, deliberately. The Flask side already enforces
``confirm=true``, rate-limits 3 per 300s, derives ``dry_run`` from
``ENABLE_LIVE_TRADING`` and returns only integer/boolean counters so no
exception text can leak. Re-implementing any of that here would fork the safety
rules; the proxy's whole job is authentication and reachability.

⚠️ Recorded, not fixed here: with ``KIS_API_KEY`` empty — the compose default —
Flask's ``_check_api_key`` returns ``None`` and the admin route is open. The
proxy adds a JWT gate but is not a security boundary while :5001 is directly
reachable. See the plan's §8 blocker.
"""
import pytest

from api.routers import quick_trade
from api.schemas import EmergencyFlattenRequest
from api.tests.test_quick_trade_close_position import (  # reuse the proven harness
    db,          # noqa: F401 - pytest fixtures
    engine,      # noqa: F401
    user,        # noqa: F401
)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "attempted": 2, "success": 2, "submitted": 2,
            "dry_run": True, "failed_count": 0, "status": "ok",
        }

    def json(self):
        return self._payload


class _FakePoster:
    """Captures the outbound call. No network, no Flask, no broker."""

    def __init__(self, response=None, exc=None):
        self.calls = []
        self.response = response or _FakeResponse()
        self.exc = exc

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.exc:
            raise self.exc
        return self.response


def _wire(monkeypatch, poster, api_key="secret-key"):
    monkeypatch.setattr(quick_trade, "_admin_post", poster)
    monkeypatch.setenv("KIS_API_KEY", api_key)
    return poster


def _body(confirm=True):
    return EmergencyFlattenRequest(confirm=confirm)


# ── the control works and reports the truth ───────────────────────────────────

def test_a_confirmed_flatten_is_forwarded(monkeypatch, db, user):
    poster = _wire(monkeypatch, _FakePoster())

    resp = quick_trade.emergency_flatten(_body(), user, db)

    assert resp.code == 1, resp.msg
    assert len(poster.calls) == 1
    assert poster.calls[0]["json"]["confirm"] is True


def test_the_upstream_counters_are_passed_through_verbatim(monkeypatch, db, user):
    """The operator must see what actually happened, not our summary of it."""
    payload = {"attempted": 5, "success": 3, "submitted": 3,
               "dry_run": False, "failed_count": 2, "status": "partial"}
    _wire(monkeypatch, _FakePoster(_FakeResponse(payload=payload)))

    resp = quick_trade.emergency_flatten(_body(), user, db)

    assert resp.data == payload


def test_dry_run_is_surfaced(monkeypatch, db, user):
    """The worst outcome is telling an operator they are flat when nothing was
    sent. ``dry_run`` must survive the hop."""
    _wire(monkeypatch, _FakePoster())

    resp = quick_trade.emergency_flatten(_body(), user, db)

    assert resp.data["dry_run"] is True


# ── guards ────────────────────────────────────────────────────────────────────

def test_an_unconfirmed_request_never_reaches_the_upstream(monkeypatch, db, user):
    poster = _wire(monkeypatch, _FakePoster())

    resp = quick_trade.emergency_flatten(_body(confirm=False), user, db)

    assert resp.code == -1
    assert poster.calls == [], "liquidation must never fire without confirmation"


def test_the_rate_limit_is_passed_through_not_masked(monkeypatch, db, user):
    """429 is the upstream telling the operator to stop. Rewriting it as a
    generic failure would hide that the control is intact but throttled."""
    limited = _FakeResponse(status_code=429,
                            payload={"error": "비상청산 요청 과다 (5분 내 3회 제한)"})
    _wire(monkeypatch, _FakePoster(limited))

    resp = quick_trade.emergency_flatten(_body(), user, db)

    assert resp.code == -1
    assert "3회 제한" in resp.msg


def test_an_upstream_error_is_an_envelope_not_a_500(monkeypatch, db, user):
    _wire(monkeypatch, _FakePoster(exc=RuntimeError("connection refused")))

    resp = quick_trade.emergency_flatten(_body(), user, db)

    assert resp.code == -1
    assert "connection refused" in resp.msg


# ── the key never crosses to the client ───────────────────────────────────────

def test_the_api_key_is_sent_upstream_not_returned(monkeypatch, db, user):
    poster = _wire(monkeypatch, _FakePoster(), api_key="super-secret")

    resp = quick_trade.emergency_flatten(_body(), user, db)

    assert poster.calls[0]["headers"]["X-API-Key"] == "super-secret"
    assert "super-secret" not in str(resp.model_dump())


def test_no_secret_leaks_when_the_upstream_fails(monkeypatch, db, user):
    """An exception string is the classic accidental disclosure channel."""
    _wire(monkeypatch, _FakePoster(exc=RuntimeError("auth failed for super-secret")),
          api_key="super-secret")

    resp = quick_trade.emergency_flatten(_body(), user, db)

    assert resp.code == -1
    assert "super-secret" not in resp.msg
