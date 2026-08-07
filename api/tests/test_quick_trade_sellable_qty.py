"""
P0-07 S2 — QuickTrade close-position consumes the sellable quantity (T4, T9, T10).

``close-position`` used to derive its quantity from the held figure
(``ovrs_cblc_qty`` / ``hldg_qty``). Held is not sellable: shares can be
unsettled or already committed to a resting sell order, and asking for the full
holding gets the whole order rejected.

These tests drive the real handler with KIS-shaped balance rows, so they also
pin the field name (``ord_psbl_qty``) the adapter depends on.
"""
import pytest

from api.models import QT_REJECTED, QT_RESERVED, QT_SUBMITTED, QuickTradeOrder
from api.routers import quick_trade
from api.schemas import ClosePositionRequest
from api.tests.test_quick_trade_close_position import (  # reuse the proven harness
    FakeMarketData,
    FakePortfolio,
    _allow,
    _wire,
    db,          # noqa: F401 - pytest fixtures, imported for this module's use
    engine,      # noqa: F401
    user,        # noqa: F401
)


def _body(**kw):
    payload = {"credential_id": 1, "symbol": "AAPL", "market": "us", "exchange": "NASD"}
    payload.update(kw)
    return ClosePositionRequest(**payload)


def _us_row(held="10", orderable="10"):
    row = {"ovrs_pdno": "AAPL", "ovrs_cblc_qty": held, "pchs_avg_pric": "150.0"}
    if orderable is not None:
        row["ord_psbl_qty"] = orderable
    return [row]


def _seed_open_sell(db, user, qty, status=QT_RESERVED, key="other-key", symbol="AAPL"):
    db.add(QuickTradeOrder(
        user_id=user.id, credential_id=1, idempotency_key=key,
        request_hash="h" + key, symbol=symbol, side="sell", market="us",
        exchange="NASD", order_type="limit", qty=qty, price=175.5, status=status,
    ))
    db.commit()


# ── T4: the close consumes sellable, not held ────────────────────────────────

def test_t4_close_all_uses_the_sellable_figure_not_held(monkeypatch, db, user):
    """10 held but only 4 orderable → close 4, not 10."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="4")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert resp.data["qty"] == 4
    assert orders.calls == [("sell_us", "AAPL", "NASD", 4, 175.5)]


def test_t4_explicit_qty_above_sellable_is_rejected(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="4")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=10), None, user, db, _allow())

    assert resp.code == -1
    assert "sellable" in resp.msg.lower()
    assert orders.calls == []                    # broker never contacted


def test_t4_explicit_qty_within_sellable_is_accepted(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="4")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=3), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_us", "AAPL", "NASD", 3, 175.5)]


def test_t4_quantity_is_never_clamped_to_sellable(monkeypatch, db, user):
    """An over-ask is refused outright — never quietly reduced."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="4")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=9), None, user, db, _allow())

    assert resp.code == -1
    assert orders.calls == []                    # not silently turned into 4


# ── T3 on this path: unreported orderable fails closed ───────────────────────

def test_unreported_orderable_blocks_the_close(monkeypatch, db, user):
    """Only EmergencyFlatten falls back to held; this path must not."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable=None)),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1
    assert "sellable quantity unavailable" in resp.msg.lower()
    assert orders.calls == []


def test_zero_orderable_blocks_the_close(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="0")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1 and orders.calls == []


def test_no_position_is_still_reported_as_no_position(monkeypatch, db, user):
    """An empty book and an unreported figure are different failures."""
    orders, _, _ = _wire(monkeypatch, portfolio=FakePortfolio(us=[]),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1
    assert "no open position" in resp.msg.lower()
    assert orders.calls == []


# ── T9: our own open sell orders reserve sellable quantity ───────────────────

def test_t9_open_sell_order_reduces_the_closeable_quantity(monkeypatch, db, user):
    _seed_open_sell(db, user, qty=4, status=QT_RESERVED)
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=7), None, user, db, _allow())

    assert resp.code == -1                       # 10 sellable - 4 pending = 6
    assert "pending" in resp.msg.lower()
    assert orders.calls == []


def test_t9_within_the_remaining_quantity_is_accepted(monkeypatch, db, user):
    _seed_open_sell(db, user, qty=4, status=QT_RESERVED)
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=6), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_us", "AAPL", "NASD", 6, 175.5)]


def test_t9_terminal_sell_orders_release_their_quantity(monkeypatch, db, user):
    _seed_open_sell(db, user, qty=9, status=QT_REJECTED)
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=10), None, user, db, _allow())

    assert resp.code == 1, resp.msg              # the rejected order holds nothing
    assert orders.calls == [("sell_us", "AAPL", "NASD", 10, 175.5)]


def test_t9_another_symbols_pending_sell_does_not_reserve(monkeypatch, db, user):
    _seed_open_sell(db, user, qty=9, symbol="MSFT", key="msft-key")
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=10), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_us", "AAPL", "NASD", 10, 175.5)]


# ── T11: an idempotent replay is not blocked by its own reservation ──────────

def test_t11_identical_retry_is_not_blocked_by_its_own_reservation(monkeypatch, db, user):
    """The first close reserves the quantity; the identical retry submits
    nothing and returns the existing order — it must not read as a second ask."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    first = quick_trade.close_position(_body(qty=10), "fixed-key", user, db, _allow())
    second = quick_trade.close_position(_body(qty=10), "fixed-key", user, db, _allow())

    assert first.code == 1 and second.code == 1, (first.msg, second.msg)
    assert len(orders.calls) == 1                # exactly one broker submission


# ── T10: the reservation survives a restart ──────────────────────────────────

def test_t10_pending_reservation_is_recomputed_from_durable_rows(monkeypatch, db, user):
    """The pending figure comes from persisted rows, so a restarted process
    reaches the same answer — nothing is held only in memory."""
    _seed_open_sell(db, user, qty=4, status=QT_RESERVED)

    # A fresh session over the same data == the post-restart view.
    assert quick_trade._open_sell_qty(db, user.id, 1, "us", "AAPL") == 4

    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))
    resp = quick_trade.close_position(_body(qty=7), None, user, db, _allow())

    assert resp.code == -1 and orders.calls == []


# ── T5: a direct/manual SELL cannot exceed sellable either ───────────────────

def _place_body(**kw):
    from api.schemas import PlaceOrderRequest
    payload = {"credential_id": 1, "symbol": "AAPL", "side": "sell", "qty": 5,
               "price": 175.5, "market": "us", "exchange": "NASD"}
    payload.update(kw)
    return PlaceOrderRequest(**payload)


class _PlaceOrders:
    """Records both sides. The close-position module's fake refuses buys by
    design, so place-order needs its own."""

    def __init__(self):
        self.calls = []

    def _record(self, name, *args):
        self.calls.append((name, *args))
        return {"output": {"ODNO": "BRK-1"}}

    def sell_us(self, symbol, excd, qty, price):
        return self._record("sell_us", symbol, excd, qty, price)

    def buy_us(self, symbol, excd, qty, price):
        return self._record("buy_us", symbol, excd, qty, price)

    def sell_kr(self, symbol, qty, price):
        return self._record("sell_kr", symbol, qty, price)

    def buy_kr(self, symbol, qty, price):
        return self._record("buy_kr", symbol, qty, price)


def _wire_place(monkeypatch, portfolio, orders=None):
    orders = orders or _PlaceOrders()
    monkeypatch.setattr(quick_trade, "_load_kis",
                        lambda cred: (object(), orders, portfolio))
    return orders


def test_t5_direct_sell_within_sellable_is_accepted(monkeypatch, db, user):
    orders = _wire_place(monkeypatch, FakePortfolio(us=_us_row(held="10", orderable="10")))

    resp = quick_trade.place_order(_place_body(qty=5), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_us", "AAPL", "NASD", 5, 175.5)]


def test_t5_direct_sell_above_sellable_is_blocked(monkeypatch, db, user):
    """10 held but only 4 orderable → a 5-share manual sell is refused."""
    orders = _wire_place(monkeypatch, FakePortfolio(us=_us_row(held="10", orderable="4")))

    resp = quick_trade.place_order(_place_body(qty=5), None, user, db, _allow())

    assert resp.code == -1
    assert "매도가능수량 초과" in resp.msg or "sellable" in resp.msg.lower()
    assert orders.calls == []                    # broker never contacted


def test_t5_direct_sell_with_unreported_orderable_fails_closed(monkeypatch, db, user):
    orders = _wire_place(monkeypatch, FakePortfolio(us=_us_row(held="10", orderable=None)))

    resp = quick_trade.place_order(_place_body(qty=1), None, user, db, _allow())

    assert resp.code == -1 and orders.calls == []


def test_t5_direct_sell_of_an_unheld_symbol_is_blocked(monkeypatch, db, user):
    orders = _wire_place(monkeypatch, FakePortfolio(us=[]))

    resp = quick_trade.place_order(_place_body(qty=1), None, user, db, _allow())

    assert resp.code == -1 and orders.calls == []


def test_t5_buys_are_unaffected_by_the_sellable_guard(monkeypatch, db, user):
    """The guard is sell-only — a buy must not need a position at all."""
    orders = _wire_place(monkeypatch, FakePortfolio(us=[]))

    resp = quick_trade.place_order(_place_body(side="buy", qty=5), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("buy_us", "AAPL", "NASD", 5, 175.5)]


def test_t5_direct_sell_subtracts_our_own_pending_sells(monkeypatch, db, user):
    """10 orderable, 6 already committed to an open sell → a 5-share direct
    sell is refused. The broker figure lags a resting order, so without this
    subtraction two consecutive full-size sells would both pass."""
    _seed_open_sell(db, user, qty=6, status=QT_RESERVED)
    orders = _wire_place(monkeypatch, FakePortfolio(us=_us_row(held="10", orderable="10")))

    resp = quick_trade.place_order(_place_body(qty=5), None, user, db, _allow())

    assert resp.code == -1
    assert "대기매도" in resp.msg or "pending" in resp.msg.lower()
    assert orders.calls == []


def test_t5_direct_sell_within_the_remaining_quantity_is_accepted(monkeypatch, db, user):
    _seed_open_sell(db, user, qty=6, status=QT_RESERVED)
    orders = _wire_place(monkeypatch, FakePortfolio(us=_us_row(held="10", orderable="10")))

    resp = quick_trade.place_order(_place_body(qty=4), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_us", "AAPL", "NASD", 4, 175.5)]


def test_t5_direct_sell_retry_is_not_blocked_by_its_own_reservation(monkeypatch, db, user):
    """The replay must not count the row it is replaying as somebody else's."""
    orders = _wire_place(monkeypatch, FakePortfolio(us=_us_row(held="10", orderable="10")))

    first = quick_trade.place_order(_place_body(qty=10), "fixed-key", user, db, _allow())
    second = quick_trade.place_order(_place_body(qty=10), "fixed-key", user, db, _allow())

    assert first.code == 1 and second.code == 1, (first.msg, second.msg)
    assert len(orders.calls) == 1                # exactly one broker submission


# ── Pending lookup is case-insensitive ───────────────────────────────────────

def test_pending_sell_is_matched_regardless_of_symbol_case(db, user):
    """Symbol and side are persisted verbatim from the request, so a lowercase
    order and an uppercase one describe one holding. Matching exactly would
    under-report pending and admit an over-ask."""
    _seed_open_sell(db, user, qty=4, status=QT_RESERVED, key="lower", symbol="aapl")

    assert quick_trade._open_sell_qty(db, user.id, 1, "us", "AAPL") == 4
    assert quick_trade._open_sell_qty(db, user.id, 1, "us", "aapl") == 4


def test_differently_cased_pending_sell_still_reserves_on_the_close(monkeypatch, db, user):
    _seed_open_sell(db, user, qty=4, status=QT_RESERVED, key="lower", symbol="aapl")
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=7), None, user, db, _allow())

    assert resp.code == -1                       # 10 sellable - 4 pending = 6
    assert orders.calls == []


def test_pending_sell_side_is_matched_regardless_of_case(db, user):
    db.add(QuickTradeOrder(
        user_id=user.id, credential_id=1, idempotency_key="upper-side",
        request_hash="h-upper-side", symbol="AAPL", side="SELL", market="us",
        exchange="NASD", order_type="limit", qty=4, price=175.5,
        status=QT_RESERVED,
    ))
    db.commit()

    assert quick_trade._open_sell_qty(db, user.id, 1, "us", "AAPL") == 4


# ── Zero orderable is not the same answer as no position ─────────────────────

def test_zero_orderable_is_reported_as_nothing_sellable_not_no_position(
        monkeypatch, db, user):
    """Telling an operator mid-close that a position they hold does not exist
    is a different — and worse — answer than 'none of it can be sold yet'."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="0")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1
    assert "no sellable quantity" in resp.msg.lower()
    assert "no open position" not in resp.msg.lower()
    assert orders.calls == []


# ── Pending reservations are scoped to one account and market ────────────────

def _seed_scoped_sell(db, user, qty, key, credential_id=1, market="us", symbol="AAPL"):
    # ``QuickTradeOrder.credential_id`` is a real foreign key and the shared
    # fixture only creates credential 1, so a second account has to be created
    # before its order can be. Without this the row only inserts where foreign
    # keys happen not to be enforced.
    from api.models import Credential

    if not db.query(Credential).filter(Credential.id == credential_id).first():
        db.add(Credential(id=credential_id, user_id=user.id, name=f"kis-{credential_id}",
                          exchange_id="kis", env="paper"))
        db.commit()

    db.add(QuickTradeOrder(
        user_id=user.id, credential_id=credential_id, idempotency_key=key,
        request_hash="h" + key, symbol=symbol, side="sell", market=market,
        exchange="NASD", order_type="limit", qty=qty, price=175.5,
        status=QT_RESERVED,
    ))
    db.commit()


def test_pending_sell_on_another_credential_does_not_reserve(db, user):
    """The broker figure it is subtracted from describes one account. Counting
    a second account's resting sell against this one refuses a valid sell."""
    _seed_scoped_sell(db, user, qty=9, key="other-cred", credential_id=2)

    assert quick_trade._open_sell_qty(db, user.id, 1, "us", "AAPL") == 0
    assert quick_trade._open_sell_qty(db, user.id, 2, "us", "AAPL") == 9


def test_pending_sell_in_another_market_does_not_reserve(db, user):
    """The same ticker can be held in both KR and US."""
    _seed_scoped_sell(db, user, qty=9, key="other-market", market="kr")

    assert quick_trade._open_sell_qty(db, user.id, 1, "us", "AAPL") == 0
    assert quick_trade._open_sell_qty(db, user.id, 1, "kr", "AAPL") == 9


def test_market_is_matched_regardless_of_case(db, user):
    _seed_scoped_sell(db, user, qty=4, key="upper-market", market="US")

    assert quick_trade._open_sell_qty(db, user.id, 1, "us", "AAPL") == 4


def test_a_close_is_not_blocked_by_another_credentials_pending_sell(monkeypatch, db, user):
    _seed_scoped_sell(db, user, qty=9, key="other-cred", credential_id=2)
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=10), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_us", "AAPL", "NASD", 10, 175.5)]


# ── An idempotent replay must not be re-judged against a moved broker figure ──

class _DecrementingPortfolio:
    """Models what a real broker does: orderable drops once a sell is resting.

    The static fake hid the replay bug — with it, a retry re-ran the guard
    against the original figure and happened to pass.
    """

    def __init__(self, held=10, orderable=10):
        self._held, self._orderable = held, orderable

    def sell_happened(self, qty):
        self._orderable = max(0, self._orderable - qty)

    def get_us_balance(self):
        return {"positions": [{"ovrs_pdno": "AAPL",
                               "ovrs_cblc_qty": str(self._held),
                               "ord_psbl_qty": str(self._orderable),
                               "pchs_avg_pric": "150.0"}]}

    def get_kr_balance(self):
        return {"positions": []}


def test_direct_sell_replay_survives_the_broker_figure_dropping(monkeypatch, db, user):
    """After the first sell rests, orderable is 0. The identical retry must
    short-circuit to the existing order, not be refused as an over-ask."""
    portfolio = _DecrementingPortfolio(held=10, orderable=10)
    orders = _wire_place(monkeypatch, portfolio)

    first = quick_trade.place_order(_place_body(qty=10), "replay-key", user, db, _allow())
    assert first.code == 1, first.msg
    portfolio.sell_happened(10)                  # broker now reports 0 orderable

    second = quick_trade.place_order(_place_body(qty=10), "replay-key", user, db, _allow())

    assert second.code == 1, second.msg
    assert len(orders.calls) == 1                # exactly one broker submission


def test_close_replay_survives_the_broker_figure_dropping(monkeypatch, db, user):
    portfolio = _DecrementingPortfolio(held=10, orderable=10)
    orders, _, _ = _wire(monkeypatch, portfolio=portfolio,
                         market_data=FakeMarketData(price=175.5))

    first = quick_trade.close_position(_body(qty=10), "replay-close", user, db, _allow())
    assert first.code == 1, first.msg
    portfolio.sell_happened(10)

    second = quick_trade.close_position(_body(qty=10), "replay-close", user, db, _allow())

    assert second.code == 1, second.msg
    assert len(orders.calls) == 1


# ── An acknowledged sell does not reserve quantity forever ───────────────────

def test_submitted_sells_do_not_reserve_quantity(db, user):
    """``QT_SUBMITTED`` is terminal in api/models.py — no fill tracking, no row
    cleanup. Counting it as pending would reserve that quantity permanently:
    sell 3 today and 7 tomorrow and the symbol is unsellable for good. Once the
    broker has acknowledged, its own ``ord_psbl_qty`` accounts for the resting
    order, so subtracting it again also double-counts."""
    _seed_open_sell(db, user, qty=4, status=QT_SUBMITTED, key="ack")

    assert quick_trade._open_sell_qty(db, user.id, 1, "us", "AAPL") == 0


def test_a_history_of_submitted_sells_still_leaves_the_position_sellable(
        monkeypatch, db, user):
    """The regression this guards: three past sells must not add up to a block."""
    for i, q in enumerate((3, 4, 3)):
        _seed_open_sell(db, user, qty=q, status=QT_SUBMITTED, key=f"past-{i}")
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=10), None, user, db, _allow())

    assert resp.code == 1, resp.msg
    assert orders.calls == [("sell_us", "AAPL", "NASD", 10, 175.5)]


def test_a_reserved_sell_still_reserves(db, user):
    """The pre-acknowledgement window is exactly what local pending is for."""
    _seed_open_sell(db, user, qty=4, status=QT_RESERVED, key="inflight")

    assert quick_trade._open_sell_qty(db, user.id, 1, "us", "AAPL") == 4


# ── Close-all closes the net, and a replay still checks its parameters ───────

def test_close_all_is_refused_while_one_of_our_sells_is_in_flight(monkeypatch, db, user):
    """Netting pending into the close-all quantity was tried and reverted: the
    server-derived key is a function of that quantity, so local state moving it
    makes two identical clicks derive different keys and sell the position
    twice. Refusing while our own sell is in flight is the safe outcome."""
    _seed_open_sell(db, user, qty=4, status=QT_RESERVED, key="inflight-close")
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1
    assert "pending" in resp.msg.lower()
    assert orders.calls == []


def test_repeated_close_all_clicks_submit_exactly_one_order(monkeypatch, db, user):
    """The regression that reversal guards: the derived key must not move with
    local pending state between two identical clicks."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    first = quick_trade.close_position(_body(), None, user, db, _allow())
    second = quick_trade.close_position(_body(), None, user, db, _allow())

    assert first.code == 1, first.msg
    assert len(orders.calls) == 1, orders.calls


def test_close_all_with_everything_reserved_is_refused(monkeypatch, db, user):
    _seed_open_sell(db, user, qty=10, status=QT_RESERVED, key="all-inflight")
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(), None, user, db, _allow())

    assert resp.code == -1
    assert "pending" in resp.msg.lower()
    assert orders.calls == []


def test_a_replay_key_reused_for_another_symbol_is_a_conflict(monkeypatch, db, user):
    """The short-circuit must not weaken the conflict check it bypasses —
    otherwise a reused key hands back an unrelated order."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))
    first = quick_trade.close_position(_body(qty=5), "shared-key", user, db, _allow())
    assert first.code == 1, first.msg

    clash = quick_trade.close_position(_body(qty=5, symbol="MSFT"), "shared-key",
                                       user, db, _allow())

    assert clash.code == -1
    assert "duplicate idempotency key" in clash.msg.lower()
    assert len(orders.calls) == 1


def test_a_replay_key_reused_for_another_quantity_is_a_conflict(monkeypatch, db, user):
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))
    assert quick_trade.close_position(_body(qty=5), "qty-key", user, db, _allow()).code == 1

    clash = quick_trade.close_position(_body(qty=6), "qty-key", user, db, _allow())

    assert clash.code == -1
    assert "duplicate idempotency key" in clash.msg.lower()
    assert len(orders.calls) == 1


def test_a_derived_key_retry_is_not_blocked_by_its_own_reservation(monkeypatch, db, user):
    """The replay skip has to cover the server-derived key too. Without it a
    retry after an indeterminate submit is refused by the very reservation it
    is replaying, and can never make progress."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    first = quick_trade.close_position(_body(qty=10), None, user, db, _allow())
    second = quick_trade.close_position(_body(qty=10), None, user, db, _allow())

    assert first.code == 1, first.msg
    assert second.code == 1, second.msg
    assert len(orders.calls) == 1


def test_a_replay_key_whose_order_is_a_buy_is_a_conflict(monkeypatch, db, user):
    """Returning a buy row as a close replay would tell the caller their
    position was closed — by a purchase."""
    db.add(QuickTradeOrder(
        user_id=user.id, credential_id=1, idempotency_key="buy-key",
        request_hash="h-buy", symbol="AAPL", side="buy", market="us",
        exchange="NASD", order_type="limit", qty=5, price=175.5,
        status=QT_SUBMITTED,
    ))
    db.commit()
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=5), "buy-key", user, db, _allow())

    assert resp.code == -1
    assert "duplicate idempotency key" in resp.msg.lower()
    assert orders.calls == []


def test_a_fractional_replay_qty_does_not_match_a_stored_whole_one(monkeypatch, db, user):
    """5.9 must not short-circuit onto a stored 5 when the same 5.9 is refused
    as a fractional share on a fresh key."""
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))
    assert quick_trade.close_position(_body(qty=5), "frac-key", user, db, _allow()).code == 1

    resp = quick_trade.close_position(_body(qty=5.9), "frac-key", user, db, _allow())

    assert resp.code == -1
    assert "duplicate idempotency key" in resp.msg.lower()
    assert len(orders.calls) == 1
