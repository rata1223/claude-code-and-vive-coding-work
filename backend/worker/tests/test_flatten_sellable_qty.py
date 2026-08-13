"""
P0-07 S2 — EmergencyFlatten never over-asks the broker (T7).

Flatten used to submit the full held quantity. Held is not sellable: with 6 of
10 shares unsettled, asking for 10 gets the whole order rejected and nothing is
liquidated — while the run still reports a submission.

Confirmed policy for this path:
  * shortfall (sellable < held) → submit the sellable part, report the
    remainder in ``failed`` and audit it. A partial liquidation using the
    broker's own number beats no liquidation, and no quantity is invented.
  * unknown orderable → EmergencyFlatten *alone* falls back to held, so a KIS
    field change can never freeze the last-resort liquidation. Every other sell
    path fails closed. This mirrors S1, where flatten is the halt-immune path.

G2 pricing behaviour is untouched by this module.
"""
import json

import pytest
from backend.database.testing import make_test_engine
from sqlalchemy.orm import sessionmaker

from backend.brokers.models import Order, OrderStatus, Position
from backend.database.models import AuditLog, Base
from backend.worker.emergency import EmergencyFlattenManager


@pytest.fixture
def db_factory():
    eng = make_test_engine()
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def _audit(factory, event_type=None):
    s = factory()
    try:
        q = s.query(AuditLog)
        if event_type:
            q = q.filter_by(event_type=event_type)
        return q.all()
    finally:
        s.close()


class _Broker:
    def __init__(self, positions, price=100.0):
        self._positions = positions
        self._price = price
        self.placed: list[tuple] = []

    def get_positions(self):
        return self._positions

    def get_price(self, symbol):
        return self._price

    def place_order(self, symbol, side, qty, price, order_type="limit"):
        self.placed.append((symbol, side, qty, price))
        return Order(id=f"O-{symbol}", symbol=symbol, side=side, qty=qty,
                     price=price, status=OrderStatus.SUBMITTED)


def _pos(symbol="SPY", qty=10, sellable=None):
    return Position(symbol=symbol, qty=qty, avg_price=100.0, market="US",
                    sellable_qty=sellable)


# ── Full sellable: unchanged behaviour ───────────────────────────────────────

def test_fully_sellable_position_flattens_the_whole_holding(db_factory):
    broker = _Broker([_pos(qty=10, sellable=10)])
    res = EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert broker.placed == [("SPY", "sell", 10, 100.0)]
    assert res["submitted"] == 1 and res["failed"] == []


# ── T7: shortfall → sell what is sellable, report the rest ───────────────────

def test_shortfall_submits_only_the_sellable_quantity(db_factory):
    broker = _Broker([_pos(qty=10, sellable=4)])
    res = EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert broker.placed == [("SPY", "sell", 4, 100.0)]     # not 10
    assert res["submitted"] == 1


def test_shortfall_is_reported_not_hidden(db_factory):
    broker = _Broker([_pos(qty=10, sellable=4)])
    res = EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert len(res["failed"]) == 1
    assert "SPY" in res["failed"][0]
    assert "6" in res["failed"][0]          # the un-sellable remainder


def test_shortfall_is_audited_with_both_quantities(db_factory):
    broker = _Broker([_pos(qty=10, sellable=4)])
    EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    rows = _audit(db_factory, "emergency_flatten_partial_sellable")
    assert len(rows) == 1
    detail = json.loads(rows[0].detail)
    assert detail["held_qty"] == 10 and detail["sellable_qty"] == 4
    assert detail["shortfall"] == 6


def test_zero_sellable_submits_nothing_and_reports(db_factory):
    broker = _Broker([_pos(qty=10, sellable=0)])
    res = EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert broker.placed == []
    assert res["submitted"] == 0 and len(res["failed"]) == 1


# ── Unknown orderable → flatten falls back to held (this path only) ──────────

def test_unknown_orderable_falls_back_to_held_for_emergency_only(db_factory):
    """A KIS field change must never freeze the last-resort liquidation."""
    broker = _Broker([_pos(qty=10, sellable=None)])
    res = EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert broker.placed == [("SPY", "sell", 10, 100.0)]
    assert res["submitted"] == 1


def test_unknown_orderable_fallback_is_audited(db_factory):
    broker = _Broker([_pos(qty=10, sellable=None)])
    EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    rows = _audit(db_factory, "emergency_flatten_sellable_unknown")
    assert len(rows) == 1
    assert json.loads(rows[0].detail)["held_qty"] == 10


# ── No fabrication, and one bad position does not stop the book ──────────────

def test_quantity_is_never_invented(db_factory):
    """Whatever is submitted must be a real count from the broker — never
    derived from price, notional, or cost basis."""
    broker = _Broker([_pos(qty=10, sellable=4)])
    EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert all(qty in (4, 10) for _s, _side, qty, _p in broker.placed)


def test_shortfall_on_one_symbol_does_not_stop_the_rest(db_factory):
    broker = _Broker([_pos("SPY", qty=10, sellable=0), _pos("QQQ", qty=5, sellable=5)])
    res = EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert broker.placed == [("QQQ", "sell", 5, 100.0)]
    assert res["attempted"] == 2 and res["submitted"] == 1
    assert len(res["failed"]) == 1


def test_dry_run_reports_the_sellable_quantity_without_submitting(db_factory):
    broker = _Broker([_pos(qty=10, sellable=4)])
    res = EmergencyFlattenManager(broker, db_factory, dry_run=True).flatten_all("t")

    assert broker.placed == []
    assert res["dry_run"] is True


# ── Unreported vs unreadable are different failures ──────────────────────────

class _FailingPriceBroker(_Broker):
    def get_price(self, symbol):
        raise RuntimeError("quote feed down")


def test_untrusted_orderable_is_audited_separately_from_unreported(db_factory):
    """A field the broker never sent and a field that came back unreadable want
    different follow-up, so they must not share one audit event."""
    broker = _Broker([_pos(symbol="SPY", qty=10, sellable="garbage")])
    EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert _audit(db_factory, "emergency_flatten_sellable_untrusted")
    assert not _audit(db_factory, "emergency_flatten_sellable_unknown")


def test_unreported_orderable_keeps_its_own_audit_event(db_factory):
    broker = _Broker([_pos(symbol="SPY", qty=10, sellable=None)])
    EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert _audit(db_factory, "emergency_flatten_sellable_unknown")
    assert not _audit(db_factory, "emergency_flatten_sellable_untrusted")


def test_untrusted_orderable_still_falls_back_to_held(db_factory):
    """Flatten is the one path where refusing to sell is the worse outcome:
    freezing the last-resort liquidation over a malformed field is exactly what
    the fallback exists to prevent. ``held`` is a real count from the same row."""
    broker = _Broker([_pos(symbol="SPY", qty=10, sellable=float("nan"))])
    res = EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert broker.placed == [("SPY", "sell", 10, 100.0)]
    assert res["submitted"] == 1


# ── Shortfall is resolved before the quote ───────────────────────────────────

def test_zero_sellable_is_reported_even_when_the_quote_also_fails(db_factory):
    """Resolving the quantity after the price check would record only the price
    failure and lose the shortfall entirely."""
    broker = _FailingPriceBroker([_pos(symbol="SPY", qty=10, sellable=0)])
    res = EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert broker.placed == []
    assert len(res["failed"]) == 1
    assert "SPY" in res["failed"][0]
    assert _audit(db_factory, "emergency_flatten_partial_sellable")


def test_zero_sellable_skips_the_quote_request_entirely(db_factory):
    class _CountingBroker(_Broker):
        quotes = 0

        def get_price(self, symbol):
            type(self).quotes += 1
            return 100.0

    broker = _CountingBroker([_pos(symbol="SPY", qty=10, sellable=0)])
    EmergencyFlattenManager(broker, db_factory, dry_run=False).flatten_all("t")

    assert type(broker).quotes == 0
    assert broker.placed == []
