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
    _seed_open_sell(db, user, qty=4, status=QT_SUBMITTED)
    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))

    resp = quick_trade.close_position(_body(qty=7), None, user, db, _allow())

    assert resp.code == -1                       # 10 sellable - 4 pending = 6
    assert "pending" in resp.msg.lower()
    assert orders.calls == []


def test_t9_within_the_remaining_quantity_is_accepted(monkeypatch, db, user):
    _seed_open_sell(db, user, qty=4, status=QT_SUBMITTED)
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
    assert quick_trade._open_sell_qty(db, user.id, "AAPL") == 4

    orders, _, _ = _wire(monkeypatch,
                         portfolio=FakePortfolio(us=_us_row(held="10", orderable="10")),
                         market_data=FakeMarketData(price=175.5))
    resp = quick_trade.close_position(_body(qty=7), None, user, db, _allow())

    assert resp.code == -1 and orders.calls == []
