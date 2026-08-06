"""P0-07C — closePosition routed through the hardened Quick Trade path.

Per ``docs/P0_07_CLOSE_POSITION_PLAN.md``, ``close_position`` is a
server-resolved, position-derived LIMIT sell:

    live position lookup → qty validation → live price lookup
        → reserve_and_submit(side="sell", risk_gate=...) → real status

The backend owns quantity and price outright; the client can never dictate
execution size. The three pre-reservation rejections (no position, over-close,
no live price) must abort *before* any DB row is written and *before* the broker
is contacted — and must never silently clamp qty, submit price 0, or fall back
to average cost.
"""
import pytest
from sqlalchemy.orm import sessionmaker

from backend.database.testing import make_test_engine, StaticPool

from api.database import Base
from api import models  # noqa: F401 - register ORM models
from api.models import (
    QuickTradeOrder,
    QT_SUBMITTED,
    User, Credential,
)
from api.routers import quick_trade
from api.schemas import ClosePositionRequest


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
    """Records every broker order call. Must stay empty on validation failures."""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result if result is not None else {"output": {"ODNO": "BRK-1"}}
        self.exc = exc

    def sell_kr(self, symbol, qty, price):
        self.calls.append(("sell_kr", symbol, qty, price))
        if self.exc:
            raise self.exc
        return self.result

    def sell_us(self, symbol, excd, qty, price):
        self.calls.append(("sell_us", symbol, excd, qty, price))
        if self.exc:
            raise self.exc
        return self.result

    # Buys must never be reachable from a close.
    def buy_kr(self, *a, **k):  # pragma: no cover - guard
        raise AssertionError("close_position must never buy")

    def buy_us(self, *a, **k):  # pragma: no cover - guard
        raise AssertionError("close_position must never buy")


class FakePortfolio:
    """Returns raw KIS-shaped balance rows (the real field names)."""

    def __init__(self, kr=None, us=None, exc=None):
        self._kr = kr if kr is not None else []
        self._us = us if us is not None else []
        self.exc = exc

    def get_kr_balance(self):
        if self.exc:
            raise self.exc
        return {"positions": self._kr, "summary": {}}

    def get_us_balance(self):
        if self.exc:
            raise self.exc
        return {"positions": self._us, "summary": {}}


class FakeMarketData:
    def __init__(self, price=None, exc=None):
        self.price = price
        self.exc = exc
        self.calls = 0

    def get_price_kr(self, symbol):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.price

    def get_price_us(self, symbol, excd):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.price


def _wire(monkeypatch, *, orders=None, portfolio=None, market_data=None):
    """Point the handler's request-scoped loaders at fakes."""
    orders = orders if orders is not None else FakeOrders()
    portfolio = portfolio if portfolio is not None else FakePortfolio()
    market_data = market_data if market_data is not None else FakeMarketData(price=100.0)

    monkeypatch.setattr(quick_trade, "_load_kis",
                        lambda cred: (object(), orders, portfolio))
    monkeypatch.setattr(quick_trade, "_load_market_data", lambda client: market_data)
    return orders, portfolio, market_data


def _allow():
    """A risk gate that always allows."""
    return lambda: None


def _body(**kw):
    payload = {"credential_id": 1, "symbol": "AAPL", "market": "us", "exchange": "NASD"}
    payload.update(kw)
    return ClosePositionRequest(**payload)


# ``ord_psbl_qty`` is KIS's own orderable (매도가능) figure. P0-07 S2 made it the
# authority on close quantity, so these rows state it; a row without it models a
# broker that reports none, which fails closed (see test_close_position_sellable).
US_POS = [{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "10", "ord_psbl_qty": "10",
           "pchs_avg_pric": "150.0"}]
KR_POS = [{"pdno": "069500", "hldg_qty": "7", "ord_psbl_qty": "7",
           "pchs_avg_pric": "9000"}]


# ── 1. successful close ───────────────────────────────────────────────────────

def test_close_full_position_uses_live_holdings_and_live_price(monkeypatch, db, user):
    """qty omitted = close all: the backend derives qty from the live position."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=US_POS),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert resp.data["side"] == "sell"
    assert resp.data["qty"] == 10          # from ovrs_cblc_qty, NOT from the client
    assert resp.data["price"] == 175.5     # live quote, NOT pchs_avg_pric (150.0)
    assert resp.data["status"] == QT_SUBMITTED

    # exactly one broker sell, carrying the resolved qty/price
    assert orders.calls == [("sell_us", "AAPL", "NASD", 10, 175.5)]

    # a durable reservation row exists and reached submitted
    row = db.query(QuickTradeOrder).one()
    assert (row.side, row.qty, row.price, row.status) == ("sell", 10, 175.5, QT_SUBMITTED)


def test_close_kr_position_resolves_hldg_qty(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(kr=KR_POS),
                         market_data=FakeMarketData(price=9500))

    resp = quick_trade.close_position(
        _body(symbol="069500", market="kr"), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_kr", "069500", 7, 9500)]


def test_partial_close_accepts_qty_below_holdings(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=US_POS),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=4), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert resp.data["qty"] == 4
    assert orders.calls == [("sell_us", "AAPL", "NASD", 4, 175.5)]


# ── 2. over-close is rejected, never clamped ──────────────────────────────────

def test_over_close_is_rejected_and_never_clamped(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch, portfolio=FakePortfolio(us=US_POS))

    resp = quick_trade.close_position(_body(qty=25), None, user, db, _allow())

    assert resp.code == -1
    assert "exceed" in resp.msg.lower()
    assert orders.calls == []                              # broker never contacted
    assert db.query(QuickTradeOrder).count() == 0          # no reservation written


def test_non_positive_requested_qty_is_rejected(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch, portfolio=FakePortfolio(us=US_POS))

    resp = quick_trade.close_position(_body(qty=0), None, user, db, _allow())

    assert resp.code == -1
    assert orders.calls == []
    assert db.query(QuickTradeOrder).count() == 0


# ── 3. missing live position ──────────────────────────────────────────────────

def test_missing_live_position_is_rejected(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch, portfolio=FakePortfolio(us=[]))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1
    assert "position" in resp.msg.lower()
    assert orders.calls == []
    assert db.query(QuickTradeOrder).count() == 0


def test_zero_quantity_holding_is_rejected(monkeypatch, db, user):
    zero = [{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "0"}]
    orders, _, _ = _wire(monkeypatch, portfolio=FakePortfolio(us=zero))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1
    assert orders.calls == []
    assert db.query(QuickTradeOrder).count() == 0


def test_position_lookup_failure_is_rejected(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(exc=RuntimeError("KIS 500")))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1
    assert orders.calls == []
    assert db.query(QuickTradeOrder).count() == 0


# ── 4. missing / unusable live price ──────────────────────────────────────────

def test_price_lookup_failure_is_rejected_without_cost_basis_fallback(
        monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=US_POS),
                         market_data=FakeMarketData(exc=RuntimeError("quote down")))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1
    assert "price" in resp.msg.lower()
    assert orders.calls == []                      # never priced off pchs_avg_pric
    assert db.query(QuickTradeOrder).count() == 0


@pytest.mark.parametrize("bad", [0, 0.0, -3.5, None])
def test_non_positive_price_is_rejected(monkeypatch, db, user, bad):
    """NEVER submit price = 0."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=US_POS),
                         market_data=FakeMarketData(price=bad))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1
    assert orders.calls == []
    assert db.query(QuickTradeOrder).count() == 0


def test_kr_sub_unit_price_does_not_truncate_to_zero(monkeypatch, db, user):
    """KR prices are cast to int; truncation must never produce a price-0 order."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(kr=KR_POS),
                         market_data=FakeMarketData(price=0.4))

    resp = quick_trade.close_position(
        _body(symbol="069500", market="kr"), None, user, db, _allow())

    assert resp.code == -1
    assert orders.calls == []
    assert db.query(QuickTradeOrder).count() == 0


# ── 5. duplicate request idempotency ──────────────────────────────────────────

def test_duplicate_close_request_submits_to_broker_once(monkeypatch, db, user):
    """Same close twice → one broker call, one DB row.

    The derived key is pinned so the assertion cannot fail for a wall-clock
    reason: two real calls straddling a 10s bucket boundary would legitimately
    derive different keys, which is a property of ``derive_idempotency_key``,
    not of the dedupe behaviour under test here.
    """
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=US_POS),
                         market_data=FakeMarketData(price=175.5))
    monkeypatch.setattr(quick_trade, "derive_idempotency_key",
                        lambda **kw: "derived-close-key")

    first = quick_trade.close_position(_body(), None, user, db, _allow())
    second = quick_trade.close_position(_body(), None, user, db, _allow())

    assert first.code == 1 and second.code == 1
    assert len(orders.calls) == 1                     # broker contacted exactly once
    assert db.query(QuickTradeOrder).count() == 1     # single reservation
    assert first.data["order_id"] == second.data["order_id"]


def test_explicit_idempotency_key_is_honoured(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=US_POS),
                         market_data=FakeMarketData(price=175.5))

    quick_trade.close_position(_body(), "close-key-1", user, db, _allow())
    quick_trade.close_position(_body(), "close-key-1", user, db, _allow())

    assert len(orders.calls) == 1
    row = db.query(QuickTradeOrder).one()
    assert row.idempotency_key == "close-key-1"


# ── 6. reserve_and_submit is the single execution funnel ──────────────────────

def test_reserve_and_submit_called_exactly_once_with_sell_and_risk_gate(
        monkeypatch, db, user):
    _wire(monkeypatch, portfolio=FakePortfolio(us=US_POS),
          market_data=FakeMarketData(price=175.5))

    seen = []
    real = quick_trade.reserve_and_submit

    def spy(db_, **kw):
        seen.append(kw)
        return real(db_, **kw)

    monkeypatch.setattr(quick_trade, "reserve_and_submit", spy)
    gate = _allow()

    quick_trade.close_position(_body(), None, user, db, gate)

    assert len(seen) == 1
    kw = seen[0]
    assert kw["request"]["side"] == "sell"
    assert kw["request"]["qty"] == 10
    assert kw["request"]["price"] == 175.5
    assert kw["request"]["order_type"] == "limit"
    assert kw["risk_gate"] is gate          # the injected P0-05 gate, not a stub
    assert kw["user_id"] == 1 and kw["credential_id"] == 1
    assert kw["idempotency_key"] and kw["request_hash"]


# ── 7. broker is never invoked on validation failure ──────────────────────────

@pytest.mark.parametrize("portfolio,market_data,label", [
    (FakePortfolio(us=[]), FakeMarketData(price=100.0), "no position"),
    (FakePortfolio(us=US_POS), FakeMarketData(price=None), "no price"),
    (FakePortfolio(us=US_POS), FakeMarketData(exc=RuntimeError("x")), "price error"),
])
def test_no_reservation_and_no_broker_on_validation_failure(
        monkeypatch, db, user, portfolio, market_data, label):
    orders, _, _ = _wire(monkeypatch, portfolio=portfolio, market_data=market_data)

    called = []
    monkeypatch.setattr(quick_trade, "reserve_and_submit",
                        lambda *a, **k: called.append(1))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1, label
    assert called == [], label            # reservation never even attempted
    assert orders.calls == [], label
    assert db.query(QuickTradeOrder).count() == 0, label


def test_over_close_never_reaches_reserve_and_submit(monkeypatch, db, user):
    _wire(monkeypatch, portfolio=FakePortfolio(us=US_POS))
    called = []
    monkeypatch.setattr(quick_trade, "reserve_and_submit",
                        lambda *a, **k: called.append(1))

    resp = quick_trade.close_position(_body(qty=999), None, user, db, _allow())

    assert resp.code == -1
    assert called == []


# ── risk gate / broker rejection surface the real status ──────────────────────

def test_risk_denied_blocks_broker_and_reports_status(monkeypatch, db, user):
    from api.services.quick_trade_service import RiskDenied
    from api.models import QT_BLOCKED

    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=US_POS),
                         market_data=FakeMarketData(price=175.5))

    def deny():
        raise RiskDenied("trading halted by RiskManager")

    resp = quick_trade.close_position(_body(), None, user, db, deny)

    assert resp.code == -1
    assert orders.calls == []                       # broker never called
    row = db.query(QuickTradeOrder).one()           # but the block IS audited
    assert row.status == QT_BLOCKED


def test_broker_rejection_is_reported_not_masked(monkeypatch, db, user):
    from api.models import QT_REJECTED

    orders = FakeOrders(exc=RuntimeError("rt_cd=1 잔고부족"))
    _wire(monkeypatch, orders=orders,
          portfolio=FakePortfolio(us=US_POS),
          market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1                          # never a hardcoded "submitted"
    row = db.query(QuickTradeOrder).one()
    assert row.status == QT_REJECTED


def test_credential_scope_is_enforced(monkeypatch, db, user):
    """A credential that does not belong to the caller must not be usable."""
    orders, _, _ = _wire(monkeypatch, portfolio=FakePortfolio(us=US_POS))

    resp = quick_trade.close_position(_body(credential_id=999), None, user, db, _allow())

    assert resp.code == -1
    assert orders.calls == []
