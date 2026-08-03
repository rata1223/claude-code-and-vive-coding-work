"""
P0-07 G2: EmergencyFlatten executable-price hardening.

The defect this pins down: when the live quote was unavailable the flatten loop
silently substituted ``position.avg_price`` — a cost basis, not a market price —
and submitted a *limit* sell at it (KIS has no market sell; see
docs/P0_07_CLOSE_POSITION_AUDIT.md §1.3 G2). In a crash that is exactly the
moment the cost basis is furthest from the market, so the "emergency" order
rested unfilled while the position kept losing.

The rule these tests enforce: the live quote is the ONLY source of an executable
sell price. If it is missing, raises, or fails validation, the position is NOT
submitted — it is reported as an explicit failure and audited. No fabricated
price, ever.
"""
import json
import math

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


def _audit_rows(factory, event_type=None):
    sess = factory()
    try:
        q = sess.query(AuditLog)
        if event_type:
            q = q.filter_by(event_type=event_type)
        return q.all()
    finally:
        sess.close()


class _Broker:
    """Broker stub whose price source is fully configurable.

    ``price`` may be a value or a callable (to raise). ``avg_price`` on the
    position is deliberately a *plausible* number in these tests, so a fallback
    to it would look like success — that is the trap being guarded against.
    """

    def __init__(self, positions=None, price=100.0):
        self._positions = positions or []
        self._price = price
        self.placed = []

    def get_positions(self):
        return self._positions

    def get_price(self, symbol):
        if callable(self._price):
            return self._price(symbol)
        return self._price

    def place_order(self, symbol, side, qty, price, order_type="limit"):
        self.placed.append((symbol, side, qty, price))
        return Order(id=f"O-{symbol}", symbol=symbol, side=side, qty=qty,
                     price=price, status=OrderStatus.SUBMITTED)


def _pos(symbol="SPY", qty=10, avg=88.0):
    return Position(symbol=symbol, qty=qty, avg_price=avg, market="US")


def _raises(exc):
    def _boom(_symbol):
        raise exc
    return _boom


# ── 1. Valid live price → normal flatten proceeds (no regression) ─────────────

def test_valid_live_price_submits_normally(db_factory):
    broker = _Broker(positions=[_pos(qty=10, avg=88.0)], price=150.0)
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

    res = mgr.flatten_all("g2")

    assert broker.placed == [("SPY", "sell", 10, 150.0)]
    assert res["submitted"] == 1 and res["success"] == 1 and res["failed"] == []
    assert mgr.last_submitted == 1 and mgr.last_failed_count == 0


# ── 2. Live price unavailable (None) → no submission ─────────────────────────

def test_missing_live_price_submits_nothing(db_factory):
    broker = _Broker(positions=[_pos()], price=None)
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

    res = mgr.flatten_all("g2")

    assert broker.placed == []
    assert res["submitted"] == 0 and res["success"] == 0
    assert len(res["failed"]) == 1 and "SPY" in res["failed"][0]


# ── 3. Price lookup raises → explicit failure, no submission ─────────────────

@pytest.mark.parametrize("exc", [
    RuntimeError("circuit breaker open — get_price"),   # the old fallback path
    RuntimeError("feed down"),
    Exception("market data unavailable"),
    TimeoutError("quote timed out"),
])
def test_price_lookup_exception_fails_closed(db_factory, exc):
    broker = _Broker(positions=[_pos()], price=_raises(exc))
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

    res = mgr.flatten_all("g2")

    assert broker.placed == []
    assert res["success"] == 0 and res["submitted"] == 0
    assert len(res["failed"]) == 1
    assert mgr.last_failed_count == 1


# ── 4. avg_price present + live price gone → avg_price never used ────────────

def test_avg_price_is_never_used_as_execution_price(db_factory):
    """The core G2 regression: a healthy-looking cost basis must not become the
    order price when the quote is gone."""
    broker = _Broker(positions=[_pos(qty=10, avg=88.0)], price=_raises(RuntimeError("feed down")))
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

    res = mgr.flatten_all("g2")

    assert broker.placed == []                       # nothing at all was sent
    assert all(88.0 not in call for call in broker.placed)
    assert res["submitted"] == 0


def test_circuit_breaker_does_not_fall_back_to_avg_price(db_factory):
    broker = _Broker(positions=[_pos(qty=10, avg=77.0)],
                     price=_raises(RuntimeError("circuit breaker open — get_price")))
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

    res = mgr.flatten_all("g2")

    assert broker.placed == []
    assert res["success"] == 0


# ── 5. avg_price itself invalid → still no fabricated price ──────────────────

@pytest.mark.parametrize("avg", [0.0, -5.0, float("nan")])
def test_invalid_avg_price_never_synthesizes_an_order(db_factory, avg):
    broker = _Broker(positions=[_pos(avg=avg)], price=_raises(RuntimeError("feed down")))
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

    res = mgr.flatten_all("g2")

    assert broker.placed == []
    assert res["submitted"] == 0 and len(res["failed"]) == 1


# ── 6. Non-finite / non-positive live price → rejected, fail-closed ──────────

@pytest.mark.parametrize("bad", [
    0, 0.0, -1.0, -0.01,
    float("nan"), float("inf"), float("-inf"),
    "150.0",        # a string is not an executable price
    True,           # bool is an int subclass — must not slip through `> 0`
    None,
])
def test_invalid_live_price_is_rejected(db_factory, bad):
    broker = _Broker(positions=[_pos()], price=bad)
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

    res = mgr.flatten_all("g2")

    assert broker.placed == [], f"submitted an order at invalid price {bad!r}"
    assert res["submitted"] == 0 and res["success"] == 0
    assert len(res["failed"]) == 1


def test_price_is_not_rounded_or_coerced_to_pass_validation(db_factory):
    """A sub-tick positive price is still a real quote — pass it through as-is,
    do not round it to make it 'valid'."""
    broker = _Broker(positions=[_pos()], price=0.0001)
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

    mgr.flatten_all("g2")

    assert broker.placed == [("SPY", "sell", 10, 0.0001)]


# ── 7. Dry run ───────────────────────────────────────────────────────────────

def test_dry_run_with_valid_price_keeps_existing_semantics(db_factory):
    broker = _Broker(positions=[_pos(), _pos("QQQ")], price=100.0)
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=True)

    res = mgr.flatten_all("g2")

    assert broker.placed == []                 # dry run never submits
    assert res["success"] == 2                 # …but reports both as processed
    assert res["submitted"] == 0 and res["dry_run"] is True
    assert res["failed"] == []


def test_dry_run_reports_price_rejection_instead_of_false_success(db_factory):
    """A dry run must not claim it would have flattened when the live price is
    unavailable — that is the false confidence this guard exists to remove."""
    broker = _Broker(positions=[_pos()], price=_raises(RuntimeError("feed down")))
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=True)

    res = mgr.flatten_all("g2")

    assert broker.placed == []
    assert res["success"] == 0
    assert len(res["failed"]) == 1


# ── 8. Audit trail ───────────────────────────────────────────────────────────

def test_price_rejection_is_audited_with_cause(db_factory):
    broker = _Broker(positions=[_pos()], price=_raises(RuntimeError("feed down")))
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

    mgr.flatten_all("g2")

    rows = _audit_rows(db_factory, "emergency_flatten_price_rejected")
    assert len(rows) == 1
    assert rows[0].symbol == "SPY"
    detail = json.loads(rows[0].detail)
    assert detail["qty"] == 10
    assert "price" not in detail or detail["price"] is None   # no fabricated price
    assert detail.get("cause")


def test_start_and_complete_audit_events_still_emitted(db_factory):
    broker = _Broker(positions=[_pos()], price=_raises(RuntimeError("feed down")))
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

    mgr.flatten_all("g2")

    types = {a.event_type for a in _audit_rows(db_factory)}
    assert "emergency_flatten_start" in types
    assert "emergency_flatten_complete" in types


def test_rejected_position_does_not_stop_the_rest_of_the_book(db_factory):
    """One dead quote must not abort the whole liquidation."""
    def price(symbol):
        if symbol == "SPY":
            raise RuntimeError("feed down")
        return 200.0

    broker = _Broker(positions=[_pos("SPY"), _pos("QQQ")], price=price)
    mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

    res = mgr.flatten_all("g2")

    assert broker.placed == [("QQQ", "sell", 10, 200.0)]   # healthy one still went
    assert res["submitted"] == 1
    assert res["attempted"] == 2
    assert len(res["failed"]) == 1 and "SPY" in res["failed"][0]
