"""
P1 — QuickTrade market routing (KR reachability).

Every QuickTrade endpoint took ``market`` straight from the request and treated
anything that was not ``"kr"`` as US. The UI cannot supply that value: its
toggle is ``spot``/``swap`` (crypto vocabulary inherited from QuantDinger), and
there is no ``kr``/``us`` notion anywhere in the view or the API client.

Three different mechanisms produced the same result:

* ``place-order`` / ``balance`` / ``position`` — ``api/compat.py`` faithfully
  renames ``market_type`` → ``market``, so the handler receives ``"spot"``.
* ``close-position`` — deliberately not in compat, and ``ClosePositionRequest``
  does not forbid extra fields, so ``market_type`` is dropped and ``market``
  falls back to its ``"us"`` default.

Either way ``market.lower() == "kr"`` is False and a KR request is served by the
US path. These tests pin the resolution rule that fixes all of them at once.

Deriving from the symbol rather than demanding a UI change is deliberate: the
rule already exists in the broker layer (``KISBroker._is_kr``), which is what
routes cancels and quotes today, so this reuses it rather than inventing one.
"""
import pytest

from api.routers import quick_trade
from api.routers.quick_trade import _resolve_market
from api.schemas import ClosePositionRequest, PlaceOrderRequest
from api.tests.test_quick_trade_close_position import (  # reuse the proven harness
    FakeMarketData,
    FakePortfolio,
    _allow,
    _wire,
    db,          # noqa: F401 - pytest fixtures
    engine,      # noqa: F401
    user,        # noqa: F401
)

# ``ord_psbl_qty`` is required by the P0-07 S2 sellable-quantity rule: a row
# that does not state it is blocked rather than fall back to the held figure.
# A real KIS balance row carries it, so these mirror the broker faithfully.
KR_ROW = [{"pdno": "069500", "hldg_qty": "7", "ord_psbl_qty": "7",
           "pchs_avg_pric": "9000"}]
US_ROW = [{"ovrs_pdno": "AAPL", "ovrs_cblc_qty": "10", "ord_psbl_qty": "10",
           "pchs_avg_pric": "150.0"}]


# ── the resolution rule itself ────────────────────────────────────────────────

@pytest.mark.parametrize("symbol,requested,expected", [
    # The crypto vocabulary the UI actually sends — neither value is a market.
    ("069500", "spot", "kr"),
    ("069500", "swap", "kr"),
    ("AAPL", "spot", "us"),
    ("AAPL", "swap", "us"),
    # Absent / empty → derive.
    ("069500", None, "kr"),
    ("069500", "", "kr"),
    ("AAPL", None, "us"),
    # An explicit, valid market is always honoured over the symbol.
    ("069500", "us", "us"),
    ("AAPL", "kr", "kr"),
    ("069500", "KR", "kr"),
    ("AAPL", "US", "us"),
    # Surrounding whitespace names a market too — and the *normalised* value is
    # what callers must compare against, never the raw string.
    ("AAPL", " kr ", "kr"),
    ("069500", "  US  ", "us"),
])
def test_market_resolution(symbol, requested, expected):
    assert _resolve_market(symbol, requested) == expected


def test_every_kr_etf_member_resolves_kr():
    from backend.quant.data.universe import KR_ETF

    for sym in KR_ETF:
        assert _resolve_market(sym, "spot") == "kr"


def test_the_etf_list_branch_is_honoured_for_a_non_six_digit_name(monkeypatch):
    """``_is_kr`` accepts a symbol *either* six-digit-numeric or in ``KR_ETF``.

    Today every real ``KR_ETF`` entry is six digits (``["069500", "360750",
    "091160"]``), so iterating the live list exercises only the first branch and
    proves nothing about the second — a broken ``KR_ETF`` lookup would still
    pass. Substituting a name that cannot satisfy the digit rule is the only way
    to reach the branch.
    """
    from backend.brokers import kis

    monkeypatch.setattr(kis, "KR_ETF", ["KODEX200"])

    assert _resolve_market("KODEX200", "spot") == "kr"
    assert _resolve_market("KODEX999", "spot") == "us", "not in the list, not KR"


# ── close-position: the reported defect ───────────────────────────────────────

def test_kr_close_position_reaches_the_kr_broker_path(monkeypatch, db, user):
    """The regression: a KR close used to resolve against the US balance, find
    nothing, and report "No open position" for a position the user holds."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(kr=KR_ROW),
                         market_data=FakeMarketData(price=9500))

    # Exactly what the UI produces today: no usable market in the payload.
    body = ClosePositionRequest(credential_id=1, symbol="069500")
    resp = quick_trade.close_position(body, None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_kr", "069500", 7, 9500)]


def test_us_close_position_is_unaffected(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=US_ROW),
                         market_data=FakeMarketData(price=175.5))

    body = ClosePositionRequest(credential_id=1, symbol="AAPL")
    resp = quick_trade.close_position(body, None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_us", "AAPL", "NASD", 10, 175.5)]


def test_an_explicit_market_still_wins_on_close(monkeypatch, db, user):
    """A caller that does know the market keeps control of it.

    The symbol is deliberately one that *derives* to US, and the balance holds
    it on the KR side only: the KR path can be reached here solely by honouring
    ``body.market``. Passing a KR-deriving symbol would prove nothing, since
    derivation alone would produce the same call.
    """
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(
                             kr=[{"pdno": "AAPL", "hldg_qty": "7",
                                  "ord_psbl_qty": "7",
                                  "pchs_avg_pric": "9000"}]),
                         market_data=FakeMarketData(price=9500))

    body = ClosePositionRequest(credential_id=1, symbol="AAPL", market="kr")
    resp = quick_trade.close_position(body, None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_kr", "AAPL", 7, 9500)]


def test_a_close_replay_matches_when_the_body_carries_no_market(monkeypatch, db, user):
    """The persisted row stores the *resolved* market, so a replay must be
    compared against that.

    P0-07 S2's replay short-circuit checks ``replay.market == body.market``.
    Once the market is derived rather than sent, those two differ for exactly
    the payload the UI produces — stored ``"kr"`` versus a body carrying
    ``None`` — and an honest retry is refused as "different parameters".
    """
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(kr=KR_ROW),
                         market_data=FakeMarketData(price=9500))

    body = ClosePositionRequest(credential_id=1, symbol="069500", qty=7)

    first = quick_trade.close_position(body, "replay-kr", user, db, _allow())
    assert first.code == 1, first.msg

    second = quick_trade.close_position(body, "replay-kr", user, db, _allow())

    assert second.code == 1, second.msg
    assert len(orders.calls) == 1, "the replay must not reach the broker again"


# ── place-order: same root cause, reached via compat ──────────────────────────

class _PlaceOrders:
    def __init__(self):
        self.calls = []

    def _record(self, name, *args):
        self.calls.append((name, *args))
        return {"output": {"ODNO": "BRK-1"}}

    def buy_kr(self, s, q, p):
        return self._record("buy_kr", s, q, p)

    def sell_kr(self, s, q, p):
        return self._record("sell_kr", s, q, p)

    def buy_us(self, s, e, q, p):
        return self._record("buy_us", s, e, q, p)

    def sell_us(self, s, e, q, p):
        return self._record("sell_us", s, e, q, p)


def test_kr_buy_reaches_the_kr_broker_path(monkeypatch, db, user):
    """compat renames market_type→market, so the handler sees "spot"."""
    orders = _PlaceOrders()
    monkeypatch.setattr(quick_trade, "_load_kis",
                        lambda cred: (object(), orders, FakePortfolio(kr=KR_ROW)))

    body = PlaceOrderRequest(credential_id=1, symbol="069500", side="buy",
                             qty=3, price=9000, market="spot")
    resp = quick_trade.place_order(body, None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("buy_kr", "069500", 3, 9000)]


def test_us_buy_is_unaffected(monkeypatch, db, user):
    orders = _PlaceOrders()
    monkeypatch.setattr(quick_trade, "_load_kis",
                        lambda cred: (object(), orders, FakePortfolio(us=US_ROW)))

    body = PlaceOrderRequest(credential_id=1, symbol="AAPL", side="buy",
                             qty=3, price=175.5, market="spot")
    resp = quick_trade.place_order(body, None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("buy_us", "AAPL", "NASD", 3, 175.5)]
