"""Quick Trade must refuse a credential it cannot actually use.

Every Quick Trade handler resolves its credential through ``_get_cred`` and then
hands it to ``_load_kis``, which unconditionally builds ``KISClient``,
``KISOrders`` and ``KISPortfolio``. ``_get_cred`` checked ownership only, never
``exchange_id`` — and ``Credential.exchange_id`` is ``'kis'`` or ``'kiwoom'``
(``api/models.py``).

So a Kiwoom credential could be selected and every balance, position and order
request would go out with Kiwoom keys on a KIS client. The user sees an
authentication failure from the broker rather than "this credential is for the
wrong brokerage".

The client-side picker filter is the other half of this and cannot be the whole
of it: it narrows what is offered, not what is accepted.

All broker access is faked. No network, no KIS, no orders.
"""
import pytest

from api.models import Credential
from api.routers import quick_trade
from api.schemas import CancelOrderRequest, ClosePositionRequest, PlaceOrderRequest
from api.tests.test_quick_trade_close_position import (  # reuse the proven harness
    FakeOrders,
    FakePortfolio,
    _allow,
    _wire,
    db,          # noqa: F401 - pytest fixtures, imported for this module's use
    engine,      # noqa: F401
    user,        # noqa: F401
)


KIWOOM_ID = 2


@pytest.fixture()
def kiwoom_cred(db):
    """A second, Kiwoom credential owned by the same user."""
    db.add(Credential(id=KIWOOM_ID, user_id=1, name="kiwoom",
                      exchange_id="kiwoom", env="paper"))
    db.commit()
    return db.query(Credential).filter(Credential.id == KIWOOM_ID).one()


def _order(**kw):
    payload = {"credential_id": KIWOOM_ID, "symbol": "AAPL", "side": "buy",
               "qty": 1, "price": 100.0}
    payload.update(kw)
    return PlaceOrderRequest(**payload)


# ── a non-KIS credential is refused on every entry point ─────────────────────

def test_place_order_refuses_a_kiwoom_credential(monkeypatch, db, user, kiwoom_cred):
    orders, _, _ = _wire(monkeypatch)

    resp = quick_trade.place_order(_order(), None, user, db, _allow())

    assert resp.code == -1
    assert "kiwoom" in resp.msg.lower() or "kis" in resp.msg.lower()
    assert orders.calls == [], "the broker must never be contacted"


def test_close_position_refuses_a_kiwoom_credential(monkeypatch, db, user, kiwoom_cred):
    orders, _, _ = _wire(monkeypatch)

    resp = quick_trade.close_position(
        ClosePositionRequest(credential_id=KIWOOM_ID, symbol="AAPL"),
        None, user, db, _allow(),
    )

    assert resp.code == -1
    assert orders.calls == []


def test_balance_refuses_a_kiwoom_credential(monkeypatch, db, user, kiwoom_cred):
    _wire(monkeypatch)

    resp = quick_trade.get_balance(KIWOOM_ID, "us", user, db)

    assert resp.code == -1


def test_cancel_refuses_a_kiwoom_credential(monkeypatch, db, user, kiwoom_cred):
    orders, _, _ = _wire(monkeypatch)

    resp = quick_trade.cancel_order(
        CancelOrderRequest(credential_id=KIWOOM_ID, order_id=1), user, db)

    assert resp.code == -1
    assert orders.calls == []


# ── the KIS credential path is unchanged ─────────────────────────────────────

def test_a_kis_credential_still_places_an_order(monkeypatch, db, user, kiwoom_cred):
    """The guard must reject the wrong provider, not the right one. The Kiwoom
    row exists here too, so this also proves the guard reads the *selected*
    credential rather than the account's first."""
    class _Buys(FakeOrders):
        def buy_us(self, symbol, excd, qty, price):
            self.calls.append(("buy_us", symbol, excd, qty, price))
            return self.result

    orders, _, _ = _wire(monkeypatch, orders=_Buys())

    resp = quick_trade.place_order(
        _order(credential_id=1), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("buy_us", "AAPL", "NASD", 1, 100.0)]
