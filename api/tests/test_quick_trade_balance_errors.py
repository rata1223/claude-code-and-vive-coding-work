"""
P1 — a balance lookup failure must not be reported as an empty account.

``get_balance`` caught every exception and returned ``Resp.ok`` with zeros. A
broker outage, an expired token and a genuinely empty account were therefore
indistinguishable on the wire: the screen showed 0 either way. On a trading
surface that is the worst kind of silent failure — the operator reads "no
position" and acts on it.

The fix reports the failure. Nothing is fabricated in its place.
"""
import pytest

from api.routers import quick_trade
from api.tests.test_quick_trade_close_position import (
    FakePortfolio,
    db,          # noqa: F401 - pytest fixtures
    engine,      # noqa: F401
    user,        # noqa: F401
)


def _wire_portfolio(monkeypatch, portfolio):
    monkeypatch.setattr(quick_trade, "_load_kis",
                        lambda cred: (object(), object(), portfolio))


def test_balance_failure_is_reported_not_zeroed(monkeypatch, db, user):
    _wire_portfolio(monkeypatch, FakePortfolio(exc=RuntimeError("broker down")))

    resp = quick_trade.get_balance(1, "us", user, db)

    assert resp.code == -1
    assert "broker down" in resp.msg


def test_a_genuinely_empty_account_still_reports_ok(monkeypatch, db, user):
    """The distinction the old code destroyed: empty is not the same as broken."""
    _wire_portfolio(monkeypatch, FakePortfolio(us=[]))

    resp = quick_trade.get_balance(1, "us", user, db)

    assert resp.code == 1, resp.msg
    assert resp.data["positions"] == []
    assert resp.data["total_eval"] == 0.0


def test_kr_balance_failure_is_also_reported(monkeypatch, db, user):
    _wire_portfolio(monkeypatch, FakePortfolio(exc=RuntimeError("KR down")))

    resp = quick_trade.get_balance(1, "kr", user, db)

    assert resp.code == -1
    assert "KR down" in resp.msg
