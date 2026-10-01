"""QuickTrade tells the caller when a US order's exchange was assumed.

A US ticker absent from ``EXCD_MAP`` routes as ``NASD`` (known issue 5). That
is right for most Nasdaq names and wrong for an NYSE/AMEX one — and then KIS
rejects the order (tickers are unique across US venues, so it never fills the
wrong security). Routing is unchanged; the response now says the venue was a
guess: ``exchange_assumed`` on success, a note on a rejection.
"""
from api.routers import quick_trade
from api.schemas import ClosePositionRequest, PlaceOrderRequest
from api.tests.test_quick_trade_close_position import (
    FakeMarketData,
    FakePortfolio,
    _allow,
    db,          # noqa: F401 - pytest fixtures
    engine,      # noqa: F401
    user,        # noqa: F401
)


class _Orders:
    def __init__(self, exc=None):
        self.calls = []
        self.exc = exc

    def _record(self, *call):
        self.calls.append(call)
        if self.exc:
            raise self.exc
        return {"output": {"ODNO": "BRK-1"}}

    def buy_us(self, s, e, q, p):
        return self._record("buy_us", s, e, q, p)

    def sell_us(self, s, e, q, p):
        return self._record("sell_us", s, e, q, p)


def _wire(monkeypatch, orders, portfolio=None):
    monkeypatch.setattr(quick_trade, "_load_kis",
                        lambda cred: (object(), orders, portfolio or FakePortfolio()))
    monkeypatch.setattr(quick_trade, "_load_market_data",
                        lambda client: FakeMarketData(price=100.0))


def _buy(symbol):
    return PlaceOrderRequest(credential_id=1, symbol=symbol, side="buy",
                             qty=1, price=100.0, market="us")


def test_an_unmapped_ticker_is_flagged_as_assumed(monkeypatch, db, user):
    orders = _Orders()
    _wire(monkeypatch, orders)
    resp = quick_trade.place_order(_buy("AMD"), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert resp.data["exchange_assumed"] is True
    assert orders.calls == [("buy_us", "AMD", "NASD", 1, 100.0)]   # routing unchanged


def test_a_rejection_says_the_venue_was_assumed(monkeypatch, db, user):
    _wire(monkeypatch, _Orders(exc=RuntimeError("KIS API error: 종목코드 오류")))
    resp = quick_trade.place_order(_buy("KO"), None, user, db, _allow())

    assert resp.code != 1
    assert "NASD로 추정" in resp.msg


def test_no_note_when_the_order_never_reached_the_broker(monkeypatch, db, user):
    """Code review: "sent as NASD and rejected" is false for a blocked order."""
    _wire(monkeypatch, _Orders())

    def deny():
        raise RuntimeError("risk gate: halted")

    resp = quick_trade.place_order(_buy("KO"), None, user, db, deny)
    assert resp.code != 1
    assert "추정" not in resp.msg


def test_a_mapped_ticker_carries_no_flag_or_note(monkeypatch, db, user):
    _wire(monkeypatch, _Orders())
    ok = quick_trade.place_order(_buy("AAPL"), None, user, db, _allow())
    assert ok.code == 1 and "exchange_assumed" not in ok.data

    _wire(monkeypatch, _Orders(exc=RuntimeError("KIS API error: x")))
    bad = quick_trade.place_order(_buy("MSFT"), "k-2", user, db, _allow())
    assert bad.code != 1 and "추정" not in bad.msg


def test_a_close_of_an_unmapped_holding_is_flagged(monkeypatch, db, user):
    row = [{"ovrs_pdno": "AMD", "ovrs_cblc_qty": "2", "ord_psbl_qty": "2",
            "pchs_avg_pric": "90"}]
    _wire(monkeypatch, _Orders(), FakePortfolio(us=row))
    resp = quick_trade.close_position(
        ClosePositionRequest(credential_id=1, symbol="AMD", market="us"),
        None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert resp.data["exchange_assumed"] is True
