"""P0 — a US order must carry the exchange the symbol actually trades on.

KIS requires ``OVRS_EXCG_CD`` on every overseas order and **rejects the order**
when it is wrong for the symbol (``BROKER_SEMANTICS.md:104``). The QuickTrade
handlers took the value from the request body, defaulting to ``"NASD"``:

    api/routers/quick_trade.py   exchange = body.exchange or "NASD"
    api/schemas.py               exchange: str = "NASD"

Two things made that a guaranteed misroute rather than a latent one. The field
is a plain ``str`` with a default, so ``or`` never fires and "not supplied" is
indistinguishable from "the caller chose NASD". And the client supplies nothing:
``frontend/src/views/quick-trade/index.vue`` posts
``{credential_id, symbol, side, qty, price, market_type, source}``.

So every NYSE name the picker offers — SPY, JPM, V, XOM, WMT, BRK.B and the
sector ETFs — went to KIS tagged NASD. The sibling handler ``_resolve_market``
already derives the *market* from the symbol; these tests pin the *exchange* to
the same treatment, through ``backend.market.symbols.resolve_exchange``.

The quote endpoints take a different code set — KIS's own examples call
``price(excd="NAS", symb="AAPL")`` while ``order(ovrs_excg_cd="NASD",
pdno="AAPL")`` — so the order code must be translated before it is used as a
quote code, never passed through.

Every broker call is a fake. No network, no KIS, no orders.
"""
import pytest

from api.models import QT_SUBMITTED, QuickTradeOrder
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


class RecordingOrders(FakeOrders):
    """``FakeOrders`` plus the buy side, which a close must never reach."""

    def buy_us(self, symbol, excd, qty, price):
        self.calls.append(("buy_us", symbol, excd, qty, price))
        if self.exc:
            raise self.exc
        return self.result

    def buy_kr(self, symbol, qty, price):
        self.calls.append(("buy_kr", symbol, qty, price))
        if self.exc:
            raise self.exc
        return self.result

    def cancel_us(self, org_order_no, symbol, excd, qty, price):
        self.calls.append(("cancel_us", org_order_no, symbol, excd, qty, price))
        return {"rt_cd": "0"}

    def cancel_kr(self, org_order_no, symbol, qty, price):
        self.calls.append(("cancel_kr", org_order_no, symbol, qty, price))
        return {"rt_cd": "0"}


class RecordingMarketData:
    """Records the exchange code each quote was asked for."""

    def __init__(self, price=175.5):
        self.price = price
        self.us_calls = []
        self.kr_calls = []

    def get_price_us(self, symbol, excd):
        self.us_calls.append((symbol, excd))
        return self.price

    def get_price_kr(self, symbol):
        self.kr_calls.append(symbol)
        return self.price


def _order(**kw):
    payload = {"credential_id": 1, "symbol": "AAPL", "side": "buy",
               "qty": 1, "price": 100.0}
    payload.update(kw)
    return PlaceOrderRequest(**payload)


def _us_pos(symbol, held="10"):
    return [{"ovrs_pdno": symbol, "ovrs_cblc_qty": held, "ord_psbl_qty": held,
             "pchs_avg_pric": "150.0"}]


# ── the exchange comes from the symbol ────────────────────────────────────────

@pytest.mark.parametrize("symbol,expected", [
    ("SPY", "NYSE"),      # NYSE Arca — in EXCD_MAP, and the NYSE tab's first row
    ("JPM", "NYSE"),
    ("V", "NYSE"),
    ("XLF", "NYSE"),
    ("AAPL", "NASD"),     # unchanged — the case that worked by accident
    ("QQQ", "NASD"),
])
def test_a_us_order_carries_the_symbols_own_exchange(monkeypatch, db, user, symbol, expected):
    orders, _, _ = _wire(monkeypatch, orders=RecordingOrders())

    resp = quick_trade.place_order(_order(symbol=symbol), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("buy_us", symbol, expected, 1, 100.0)]


@pytest.mark.parametrize("symbol", ["BRK.B", "XOM", "WMT"])
def test_the_nyse_names_the_picker_offers_are_mapped(monkeypatch, db, user, symbol):
    """``HOT_SYMBOLS`` lists these on the NYSE tab but ``EXCD_MAP`` never knew
    them, so they fell through to the US default and would still be rejected
    after the routing fix. The catalogue and the map have to agree."""
    orders, _, _ = _wire(monkeypatch, orders=RecordingOrders())

    resp = quick_trade.place_order(_order(symbol=symbol), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("buy_us", symbol, "NYSE", 1, 100.0)]


def test_a_kr_order_takes_the_domestic_path_with_no_exchange(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch, orders=RecordingOrders())

    resp = quick_trade.place_order(
        _order(symbol="069500", price=9000.0), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("buy_kr", "069500", 1, 9000)]


# ── a symbol with no exchange must not be given one ──────────────────────────

@pytest.mark.parametrize("symbol", ["BTC/USDT", "ETHUSDT"])
def test_a_non_equity_symbol_is_refused_before_the_broker(monkeypatch, db, user, symbol):
    """``resolve_exchange`` returns ``None`` for these on purpose. Coercing the
    default would submit a crypto pair to a KIS equities account — the watchlist
    can still hold one from the crypto era, and ``validateOrder`` in the view
    only checks that the symbol is non-empty."""
    orders, _, _ = _wire(monkeypatch, orders=RecordingOrders())

    resp = quick_trade.place_order(_order(symbol=symbol), None, user, db, _allow())

    assert resp.code == -1
    assert "exchange" in resp.msg.lower()
    assert orders.calls == []


def test_a_client_exchange_that_contradicts_the_symbol_is_refused(monkeypatch, db, user):
    """Ignoring it silently would leave the client believing something false.
    SPY is NYSE; a caller asserting NASD is wrong and should hear so."""
    orders, _, _ = _wire(monkeypatch, orders=RecordingOrders())

    resp = quick_trade.place_order(
        _order(symbol="SPY", exchange="NASD"), None, user, db, _allow())

    assert resp.code == -1
    assert orders.calls == []


def test_a_client_exchange_that_agrees_is_accepted(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch, orders=RecordingOrders())

    resp = quick_trade.place_order(
        _order(symbol="SPY", exchange="NYSE"), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("buy_us", "SPY", "NYSE", 1, 100.0)]


# ── close-position routes the same way ───────────────────────────────────────

def test_close_position_uses_the_symbols_exchange(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch,
                         orders=RecordingOrders(),
                         portfolio=FakePortfolio(us=_us_pos("SPY")),
                         market_data=RecordingMarketData())

    resp = quick_trade.close_position(
        ClosePositionRequest(credential_id=1, symbol="SPY"), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_us", "SPY", "NYSE", 10, 175.5)]


# ── the quote endpoints take the other code set ──────────────────────────────

@pytest.mark.parametrize("symbol,quote_excd", [
    ("SPY", "NYS"),
    ("AAPL", "NAS"),
])
def test_a_quote_is_asked_for_with_the_quote_exchange_code(
    monkeypatch, db, user, symbol, quote_excd
):
    """KIS's own examples: ``order(ovrs_excg_cd="NASD")`` but
    ``price(excd="NAS")``. Handing the order code to the quote endpoint asks
    for an exchange it does not name."""
    market = RecordingMarketData()
    _wire(monkeypatch,
          orders=RecordingOrders(),
          portfolio=FakePortfolio(us=_us_pos(symbol)),
          market_data=market)

    resp = quick_trade.close_position(
        ClosePositionRequest(credential_id=1, symbol=symbol), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert market.us_calls == [(symbol, quote_excd)]


# ── a cancel replays the order's own exchange ────────────────────────────────

def test_a_cancel_uses_the_exchange_the_order_was_placed_with(monkeypatch, db, user):
    """Not re-derived: the cancel has to name the venue the resting order sits
    on. Re-deriving would be right today and wrong the moment a symbol moves
    exchange or the mapping is corrected under a live order."""
    orders, _, _ = _wire(monkeypatch, orders=RecordingOrders())
    db.add(QuickTradeOrder(
        user_id=user.id, credential_id=1, idempotency_key="cancel-me",
        request_hash="h1", symbol="SPY", side="buy", market="us",
        exchange="NYSE", order_type="limit", qty=1, price=100.0,
        status=QT_SUBMITTED, broker_order_id="ODNO-1",
    ))
    db.commit()
    order_id = db.query(QuickTradeOrder).filter(
        QuickTradeOrder.idempotency_key == "cancel-me").one().id

    resp = quick_trade.cancel_order(
        CancelOrderRequest(credential_id=1, order_id=order_id), user, db)

    assert resp.code == 1, resp.msg
    assert orders.calls == [("cancel_us", "ODNO-1", "SPY", "NYSE", 1, 100.0)]


def test_a_cancel_on_a_row_with_no_exchange_is_refused(monkeypatch, db, user):
    """The silent ``or "NASD"`` would cancel on the wrong venue. There is no
    safe guess here — the order is resting somewhere specific."""
    orders, _, _ = _wire(monkeypatch, orders=RecordingOrders())
    db.add(QuickTradeOrder(
        user_id=user.id, credential_id=1, idempotency_key="no-exch",
        request_hash="h2", symbol="SPY", side="buy", market="us",
        exchange="", order_type="limit", qty=1, price=100.0,
        status=QT_SUBMITTED, broker_order_id="ODNO-2",
    ))
    db.commit()
    order_id = db.query(QuickTradeOrder).filter(
        QuickTradeOrder.idempotency_key == "no-exch").one().id

    resp = quick_trade.cancel_order(
        CancelOrderRequest(credential_id=1, order_id=order_id), user, db)

    assert resp.code == -1
    assert orders.calls == []
