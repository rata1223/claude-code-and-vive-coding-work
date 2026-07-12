"""
TASK P3-04 — Gate G2: Emergency-flatten fill-path certification.

The P3-03B certification (docs/PAPER_TRADING_CERTIFICATION.md §4.2, scenario 16)
verified that the real ``EmergencyFlattenManager`` submits sells and closes the
**broker book**, but did not certify closure through the position-update /
reconciliation path. Re-routing flatten through the poller/tracker is explicitly
architectural and out of scope (docs/EMERGENCY_FLATTEN_VALIDATION.md §7, risk R5).

The *existing* position-update mechanism for fire-and-forget flatten orders is
``PositionReconciler.reconcile()`` (broker = ground truth). These tests certify
the full documented chain end-to-end using real production objects:

    trigger → flatten_all → broker sells → settle (fill) → reconcile
    → position update (DB) → audit log

and prove **idempotency**, **no duplicate liquidation**, and **no unsafe
fail-open** — without any architecture change.
"""
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from backend.brokers.paper_broker import ScriptedPaperBroker
from backend.database.models import AuditLog, Base, Position as DBPosition
from backend.database.testing import make_test_engine
from backend.execution.reconciler import PositionReconciler
from backend.worker import emergency as emod
from backend.worker.emergency import EmergencyFlattenManager

BROKER = "kis"


@pytest.fixture()
def db_factory():
    eng = make_test_engine()
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def _seed_db_position(db_factory, symbol, qty, avg, market="US", age_hours=2.0):
    """Persist a position, backdated so the reconciler's stale-delete guard
    (_STALE_MIN_AGE_HOURS = 1h) allows it to be removed once the broker is flat."""
    s = db_factory()
    try:
        s.add(DBPosition(symbol=symbol, qty=qty, avg_price=avg, market=market,
                         broker=BROKER,
                         updated_at=datetime.utcnow() - timedelta(hours=age_hours)))
        s.commit()
    finally:
        s.close()


def _db_positions(db_factory):
    s = db_factory()
    try:
        return {p.symbol: p for p in s.query(DBPosition).all()}
    finally:
        s.close()


def _audit_events(db_factory):
    s = db_factory()
    try:
        return [a.event_type for a in s.query(AuditLog).all()]
    finally:
        s.close()


def _broker_with_positions():
    b = ScriptedPaperBroker(default_price=100.0)
    b.set_position("SPY", 10, 100.0, market="US")
    b.set_position("069500", 5, 30000.0, market="KR")
    b.set_prices({"SPY": 100.0, "069500": 30000.0})
    return b


# ── 1. Full chain: trigger → order → settle → reconcile → position update → audit

def test_flatten_full_chain_to_reconcile_and_audit(db_factory):
    broker = _broker_with_positions()
    _seed_db_position(db_factory, "SPY", 10, 100.0)
    _seed_db_position(db_factory, "069500", 5, 30000.0, market="KR")

    mgr = EmergencyFlattenManager(broker, db_factory=db_factory, dry_run=False)
    res = mgr.flatten_all("cert")

    # Flatten submitted a real sell per position.
    assert res["attempted"] == 2
    assert res["submitted"] == 2
    assert res["success"] == 2
    assert res["failed"] == []

    # Audit trail is complete and reconstructable.
    events = _audit_events(db_factory)
    assert "emergency_flatten_start" in events
    assert events.count("emergency_flatten_order") == 2
    assert "emergency_flatten_complete" in events

    # Settle the fire-and-forget sells → broker book (ground truth) is flat.
    broker.settle_all_open()
    assert broker.get_positions() == []

    # Position-update + reconciliation stage: the reconciler converges the DB to
    # the flat broker truth (delete_position), i.e. the flatten is confirmed
    # through the existing runtime path — not just the broker book.
    result = PositionReconciler(broker, db_factory, broker_name=BROKER).reconcile("startup")
    deleted = {r["symbol"] for r in result.repairs if r["kind"] == "delete_position"}
    assert deleted == {"SPY", "069500"}
    assert result.ok
    assert _db_positions(db_factory) == {}


# ── 2. Idempotency / no duplicate liquidation ─────────────────────────────────

def test_concurrent_flatten_rejected_no_duplicate_orders(db_factory):
    broker = _broker_with_positions()

    # Hold the module-level lock to simulate a flatten already in progress.
    assert emod._FLATTEN_LOCK.acquire(blocking=False)
    try:
        mgr = EmergencyFlattenManager(broker, db_factory=db_factory, dry_run=False)
        res = mgr.flatten_all("concurrent")
    finally:
        emod._FLATTEN_LOCK.release()

    assert res["status"] == "already_in_progress"
    assert res["submitted"] == 0
    assert res["attempted"] == 0
    # Broker was never touched → positions intact, no duplicate sells.
    assert len(broker.get_positions()) == 2
    assert "emergency_flatten_rejected" in _audit_events(db_factory)


def test_sequential_reflatten_after_settle_is_noop(db_factory):
    broker = _broker_with_positions()
    mgr = EmergencyFlattenManager(broker, db_factory=db_factory, dry_run=False)

    first = mgr.flatten_all("first")
    assert first["submitted"] == 2
    broker.settle_all_open()
    assert broker.get_positions() == []

    # Re-flatten with a flat book submits nothing — no duplicate liquidation.
    second = mgr.flatten_all("second")
    assert second["attempted"] == 0
    assert second["submitted"] == 0


# ── 3. No unsafe fail-open ─────────────────────────────────────────────────────

def test_audit_failure_does_not_block_or_duplicate_liquidation():
    """A broken audit sink must not stop liquidation (deliberate fail-open on the
    audit side) and must not cause duplicate sells."""
    broker = _broker_with_positions()

    def broken_factory():
        raise RuntimeError("db down")

    mgr = EmergencyFlattenManager(broker, db_factory=broken_factory, dry_run=False)
    res = mgr.flatten_all("audit-down")

    assert res["submitted"] == 2          # liquidation still proceeded
    assert res["failed"] == []
    broker.settle_all_open()
    assert broker.get_positions() == []   # exactly closed, not oversold


def test_get_price_failure_falls_back_and_liquidates_once(monkeypatch, db_factory):
    """A degraded price source must not stop an emergency liquidation, and each
    position is sold exactly once (fallback to avg_price, single submission)."""
    broker = _broker_with_positions()
    submits: list[str] = []
    real_place = broker.place_order

    def counting_place(symbol, side, qty, price, order_type="limit"):
        submits.append(symbol)
        return real_place(symbol, side, qty, price, order_type)

    monkeypatch.setattr(broker, "place_order", counting_place)
    monkeypatch.setattr(broker, "get_price",
                        lambda symbol: (_ for _ in ()).throw(RuntimeError("feed down")))

    mgr = EmergencyFlattenManager(broker, db_factory=db_factory, dry_run=False)
    res = mgr.flatten_all("price-down")

    assert res["submitted"] == 2
    assert sorted(submits) == ["069500", "SPY"]     # each symbol exactly once
    assert len(submits) == len(set(submits))
