"""P0-08 — Quick Trade runtime hardening validation.

Regression coverage for the runtime paths that the P0-04/05/07 suites left
unverified, plus the defects this validation found:

* **D-1 (defect)** ``place-order`` performs no positivity validation at all.
  ``qty=0``/negative and ``price=0``/negative are reserved and submitted to the
  broker, and on the KR path ``int(body.price)`` truncates a sub-1 KRW quote to
  a **price-0 order** — the exact failure mode the close path was hardened
  against in P0-07C (`docs/P0_07_CLOSE_POSITION_PLAN.md`: "NEVER submit price =
  0"). Buy and direct-sell orders bypassed that rule.
* **Scenario 2 / 5 (coverage gap)** the ``sell_kr``/``sell_us`` branches of
  ``place_order``'s ``broker_submit`` closure had no test — every existing sell
  assertion went through ``close-position``.
* **Scenario 7 (coverage gap)** a retry arriving while the winning reservation
  is still ``RESERVED`` returns the existing row; no test pinned that the broker
  is not called a second time in that window.

Invariants asserted throughout: a rejected request creates **no reservation row**
and makes **no broker call**; a duplicate collapses to exactly one reservation and
one broker call; the response never reports a status the runtime did not reach.
"""
import pytest
from sqlalchemy.orm import sessionmaker

from backend.database.testing import make_test_engine, StaticPool

from api.database import Base
from api import models  # noqa: F401 - register ORM models
from api.models import QuickTradeOrder, QT_RESERVED, QT_SUBMITTED, User, Credential
from api.routers import quick_trade
from api.schemas import PlaceOrderRequest


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def engine():
    eng = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)
    eng.dispose()


@pytest.fixture()
def db(engine):
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = SessionLocal()
    s.add_all([
        User(id=1, email="a@example.com", password_hash="x"),
        Credential(id=1, user_id=1, name="kis", exchange_id="kis", env="paper"),
    ])
    s.commit()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def user(db):
    return db.query(User).filter(User.id == 1).one()


class FakeOrders:
    """Records every broker order call; must stay empty on a rejected request."""

    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def buy_kr(self, symbol, qty, price):
        self.calls.append(("buy_kr", symbol, qty, price))
        if self.exc:
            raise self.exc
        return {"output": {"ODNO": "BRK-BUY-KR"}}

    def sell_kr(self, symbol, qty, price):
        self.calls.append(("sell_kr", symbol, qty, price))
        if self.exc:
            raise self.exc
        return {"output": {"ODNO": "BRK-SELL-KR"}}

    def buy_us(self, symbol, excd, qty, price):
        self.calls.append(("buy_us", symbol, excd, qty, price))
        if self.exc:
            raise self.exc
        return {"output": {"ODNO": "BRK-BUY-US"}}

    def sell_us(self, symbol, excd, qty, price):
        self.calls.append(("sell_us", symbol, excd, qty, price))
        if self.exc:
            raise self.exc
        return {"output": {"ODNO": "BRK-SELL-US"}}


@pytest.fixture()
def orders(monkeypatch):
    fake = FakeOrders()
    monkeypatch.setattr(quick_trade, "_load_kis", lambda cred: (object(), fake, object()))
    return fake


def _allow():
    return lambda: None


def _order(**kw):
    payload = {
        "credential_id": 1, "symbol": "AAPL", "side": "buy",
        "qty": 10, "price": 100.0, "market": "us", "exchange": "NASD",
    }
    payload.update(kw)
    return PlaceOrderRequest(**payload)


def _place(body, db, user, key=None):
    return quick_trade.place_order(body, key, user, db, _allow())


# ── D-1: positivity validation on the submit path ────────────────────────────

@pytest.mark.parametrize("qty", [0, -5])
def test_non_positive_qty_is_rejected_before_reservation(qty, db, user, orders):
    resp = _place(_order(qty=qty), db, user)

    assert resp.code == -1, "a non-positive qty must never be submitted"
    assert orders.calls == []                          # broker never contacted
    assert db.query(QuickTradeOrder).count() == 0      # no reservation written


@pytest.mark.parametrize("price", [0, 0.0, -3.5])
def test_non_positive_price_is_rejected_before_reservation(price, db, user, orders):
    resp = _place(_order(price=price), db, user)

    assert resp.code == -1, "a non-positive price must never be submitted"
    assert orders.calls == []
    assert db.query(QuickTradeOrder).count() == 0


def test_kr_sub_unit_price_does_not_truncate_to_a_zero_price_order(db, user, orders):
    """KR orders are cast with int(); 0.4 KRW must not become a price-0 order.

    This is the same truncation guard close-position received in P0-07C — the
    buy/direct-sell path never had it.
    """
    resp = _place(_order(symbol="069500", market="kr", price=0.4), db, user)

    assert resp.code == -1
    assert orders.calls == []
    assert db.query(QuickTradeOrder).count() == 0


def test_kr_valid_price_still_submits(db, user, orders):
    """The guard must not reject legitimate KR prices."""
    resp = _place(_order(symbol="069500", market="kr", price=9500.0), db, user)

    assert resp.code == 1, resp.msg
    assert orders.calls == [("buy_kr", "069500", 10, 9500)]


# ── Scenario 2: Normal Sell through place-order ──────────────────────────────

def test_normal_sell_us_reserves_and_submits(db, user, orders):
    resp = _place(_order(side="sell", price=190.5), db, user)

    assert resp.code == 1, resp.msg
    assert resp.data["status"] == QT_SUBMITTED
    assert orders.calls == [("sell_us", "AAPL", "NASD", 10, 190.5)]

    row = db.query(QuickTradeOrder).one()
    assert (row.side, row.qty, row.price, row.status) == ("sell", 10.0, 190.5, QT_SUBMITTED)
    assert row.broker_order_id == "BRK-SELL-US"


def test_normal_sell_kr_reserves_and_submits(db, user, orders):
    resp = _place(_order(symbol="069500", side="sell", market="kr", price=9500.0), db, user)

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_kr", "069500", 10, 9500)]
    assert db.query(QuickTradeOrder).one().side == "sell"


# ── Scenario 5: Duplicate Sell through place-order ───────────────────────────

def test_duplicate_sell_collapses_to_one_reservation_and_one_broker_call(db, user, orders):
    body = _order(side="sell", price=190.5)

    first = _place(body, db, user, key="dup-sell-1")
    second = _place(body, db, user, key="dup-sell-1")

    assert first.code == 1 and second.code == 1
    assert len(orders.calls) == 1                      # broker called exactly once
    assert db.query(QuickTradeOrder).count() == 1      # one reservation
    assert first.data["order_id"] == second.data["order_id"]


# ── Scenario 7: retry while the reservation is still RESERVED ────────────────

def test_retry_while_reservation_still_reserved_does_not_resubmit(db, user, orders):
    """An indeterminate submit leaves the row RESERVED; a retry must not re-submit.

    The row stays recoverable (the startup sweep resolves it) and the retry gets
    the existing reservation back rather than a second broker call.
    """
    orders.exc = TimeoutError("broker timeout")
    body = _order(side="sell", price=190.5)

    first = _place(body, db, user, key="retry-1")
    assert first.code == -1                             # indeterminate → error envelope
    row = db.query(QuickTradeOrder).one()
    assert row.status == QT_RESERVED                    # recoverable, not terminal

    orders.exc = None                                   # broker healthy again
    second = _place(body, db, user, key="retry-1")

    assert len(orders.calls) == 1, "retry must not re-submit a live reservation"
    assert db.query(QuickTradeOrder).count() == 1
    assert second.data is None or second.code == -1     # still reports the real state
    assert db.query(QuickTradeOrder).one().status == QT_RESERVED
