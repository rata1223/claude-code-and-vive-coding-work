"""
EmergencyFlattenManager runtime audit — minimal safety-fix coverage.

Validates the observability + duplicate-guard fixes (the architectural items —
market-order fill verification, poller registration, auto-flatten on kill switch
— are documented as remaining risks in docs/EMERGENCY_FLATTEN_VALIDATION.md and
NOT exercised here).
"""
import json

import pytest
from backend.database.testing import make_test_engine
from sqlalchemy.orm import sessionmaker

from backend.brokers.models import Order, OrderStatus, Position
from backend.database.models import AuditLog, Base
from backend.worker import emergency as emod
from backend.worker.emergency import EmergencyFlattenManager


@pytest.fixture
def db_factory():
    eng = make_test_engine()
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def _events(factory):
    sess = factory()
    try:
        return {a.event_type: a for a in sess.query(AuditLog).all()}
    finally:
        sess.close()


class _Broker:
    """Minimal broker stub: configurable positions / price / place_order."""

    def __init__(self, positions=None, place_fail=None, positions_error=None):
        self._positions = positions or []
        self._place_fail = place_fail or set()   # symbols whose place_order raises
        self._positions_error = positions_error
        self.placed = []

    def get_positions(self):
        if self._positions_error:
            raise self._positions_error
        return self._positions

    def get_price(self, symbol):
        return 100.0

    def place_order(self, symbol, side, qty, price, order_type="limit"):
        if symbol in self._place_fail:
            raise RuntimeError("broker rejected")
        self.placed.append((symbol, side, qty, price))
        return Order(id=f"O-{symbol}", symbol=symbol, side=side, qty=qty,
                     price=price, status=OrderStatus.SUBMITTED)


def _pos(symbol, qty=10, avg=90.0):
    return Position(symbol=symbol, qty=qty, avg_price=avg, market="KR")


def test_dry_run_submits_nothing_but_reports_processed(db_factory):
    broker = _Broker(positions=[_pos("005930"), _pos("000660")])
    mgr = EmergencyFlattenManager(broker, db_factory=db_factory, dry_run=True)
    res = mgr.flatten_all(reason="t")
    assert res["dry_run"] is True
    assert res["attempted"] == 2
    assert res["success"] == 2          # processed (logged)
    assert res["submitted"] == 0        # no real orders
    assert broker.placed == []
    ev = _events(db_factory)
    assert "emergency_flatten_start" in ev
    assert "emergency_flatten_complete" in ev


def test_live_submits_and_counts_submitted(db_factory):
    broker = _Broker(positions=[_pos("005930"), _pos("000660")])
    mgr = EmergencyFlattenManager(broker, db_factory=db_factory, dry_run=False)
    res = mgr.flatten_all(reason="t")
    assert res["dry_run"] is False
    assert res["success"] == 2
    assert res["submitted"] == 2
    assert len(broker.placed) == 2
    ev = _events(db_factory)
    # one order audit per symbol + completion summary
    assert "emergency_flatten_order" in ev
    complete = json.loads(ev["emergency_flatten_complete"].detail)
    assert complete["submitted"] == 2 and complete["failed"] == []


def test_per_position_failure_is_audited_and_reported(db_factory):
    broker = _Broker(positions=[_pos("005930"), _pos("000660")],
                     place_fail={"000660"})
    mgr = EmergencyFlattenManager(broker, db_factory=db_factory, dry_run=False)
    res = mgr.flatten_all(reason="t")
    assert res["submitted"] == 1
    assert any("000660" in f for f in res["failed"])
    ev = _events(db_factory)
    assert "emergency_flatten_failed" in ev
    assert ev["emergency_flatten_failed"].symbol == "000660"


def test_positions_fetch_failure_is_audited(db_factory):
    broker = _Broker(positions_error=RuntimeError("broker down"))
    mgr = EmergencyFlattenManager(broker, db_factory=db_factory, dry_run=False)
    res = mgr.flatten_all(reason="t")
    assert res["attempted"] == 0
    assert res["failed"] == ["broker down"]
    ev = _events(db_factory)
    assert "emergency_flatten_positions_error" in ev
    # must NOT report a completion when the fetch never succeeded
    assert "emergency_flatten_complete" not in ev


def test_empty_book_reports_complete(db_factory):
    mgr = EmergencyFlattenManager(_Broker(positions=[]), db_factory=db_factory, dry_run=False)
    res = mgr.flatten_all(reason="t")
    assert res == {"attempted": 0, "success": 0, "submitted": 0,
                   "dry_run": False, "failed": []}
    assert "emergency_flatten_complete" in _events(db_factory)


def test_concurrent_flatten_is_rejected(db_factory):
    broker = _Broker(positions=[_pos("005930")])
    mgr = EmergencyFlattenManager(broker, db_factory=db_factory, dry_run=False)
    # Simulate an in-progress flatten by holding the module-level lock.
    assert emod._FLATTEN_LOCK.acquire(blocking=False)
    try:
        res = mgr.flatten_all(reason="dup")
    finally:
        emod._FLATTEN_LOCK.release()
    assert res["status"] == "already_in_progress"
    assert res["attempted"] == 0
    assert broker.placed == []
    assert "emergency_flatten_rejected" in _events(db_factory)
