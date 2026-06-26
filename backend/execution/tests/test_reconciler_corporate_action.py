"""
Layer-2 tests (TASK P2-02C): the reconciler's classify-before-absorb seam.

Verifies that wiring a CorporateActionRuntime into PositionReconciler:
  * labels & records a value-preserving qty jump as a corporate action,
  * keeps the reconciler the SOLE writer of position qty/avg (broker-adjusted),
  * never double-adjusts (idempotent across reconcile runs),
  * fails closed (gates the symbol) on an UNKNOWN jump.
The reconciler's pre-existing behavior (ca_runtime=None) is covered elsewhere.
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from backend.database.testing import make_test_engine
from backend.brokers.models import Position as BPosition
from backend.database.models import (
    Base, Position as DBPosition,
    CorporateAction as CARow, CorporateActionHistory as CAHist,
)
from backend.execution.reconciler import PositionReconciler
from backend.data.corporate_action_runtime import CorporateActionRuntime


@pytest.fixture()
def db_factory():
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _bpos(symbol, qty, avg, market="KR"):
    return BPosition(symbol=symbol, qty=qty, avg_price=avg, market=market)


def _insert_db_pos(factory, symbol, qty, avg, market="KR"):
    sess = factory()
    sess.add(DBPosition(symbol=symbol, qty=qty, avg_price=avg, market=market, broker="kis"))
    sess.commit()
    sess.close()


def _reconciler(broker_positions, factory):
    broker = MagicMock()
    broker.get_positions.return_value = broker_positions
    ca = CorporateActionRuntime(db_factory=factory, broker="kis")
    rec = PositionReconciler(broker=broker, db_factory=factory, redis_client=None,
                             broker_name="kis", ca_runtime=ca)
    return rec, ca


def _db_pos(factory, symbol):
    sess = factory()
    try:
        return sess.query(DBPosition).filter(DBPosition.symbol == symbol).one()
    finally:
        sess.close()


# ── reconciliation after split ───────────────────────────────────────────────

def test_reconciliation_after_split_records_and_converges(db_factory):
    _insert_db_pos(db_factory, "AAPL", 100, 150.0)         # pre-split DB
    rec, ca = _reconciler([_bpos("AAPL", 200, 75.0)], db_factory)  # broker post-split (2:1)

    result = rec.reconcile("periodic")

    # broker-adjusted positions: reconciler converged DB to broker (sole writer)
    row = _db_pos(db_factory, "AAPL")
    assert row.qty == 200 and row.avg_price == 75.0
    # labeled as a corporate action, not a generic mismatch
    assert any(g["kind"] == "qty_corporate_action" for g in result.gaps)

    # CA recorded + applied; history written; value preserved
    sess = db_factory()
    try:
        ca_row = sess.query(CARow).filter(CARow.symbol == "AAPL").one()
        hist = sess.query(CAHist).filter(CAHist.symbol == "AAPL").all()
    finally:
        sess.close()
    assert ca_row.action_type == "split" and ca_row.status == "applied"
    assert len(hist) == 1
    assert hist[0].qty_before == 100 and hist[0].qty_after == 200
    assert hist[0].value_preserved is True
    # a legit split does not leave the symbol blocked
    assert ca.is_blocked("AAPL") is False


# ── broker-adjusted positions / one owner ────────────────────────────────────

def test_reconciler_is_sole_position_writer(db_factory):
    _insert_db_pos(db_factory, "AAPL", 100, 150.0)
    rec, _ca = _reconciler([_bpos("AAPL", 200, 75.0)], db_factory)
    rec.reconcile("periodic")
    # The CA runtime never wrote qty/avg — only the reconciler did. Final value is
    # exactly the broker's, proving exactly one adjustment owner.
    row = _db_pos(db_factory, "AAPL")
    assert (row.qty, row.avg_price) == (200, 75.0)


# ── double-adjust prevention ─────────────────────────────────────────────────

def test_no_double_adjust_across_repeated_reconciles(db_factory):
    _insert_db_pos(db_factory, "AAPL", 100, 150.0)
    rec, _ca = _reconciler([_bpos("AAPL", 200, 75.0)], db_factory)

    rec.reconcile("periodic")
    rec.reconcile("periodic")  # second run: DB already == broker → no jump
    rec.reconcile("periodic")

    row = _db_pos(db_factory, "AAPL")
    assert (row.qty, row.avg_price) == (200, 75.0)  # not adjusted again
    sess = db_factory()
    try:
        ca_rows = sess.query(CARow).filter(CARow.symbol == "AAPL").all()
        hist = sess.query(CAHist).filter(CAHist.symbol == "AAPL").all()
    finally:
        sess.close()
    assert len(ca_rows) == 1   # idempotent: one corporate_actions row
    assert len(hist) == 1      # one history row


# ── UNKNOWN fails closed ─────────────────────────────────────────────────────

def test_unknown_jump_fails_closed_and_blocks(db_factory):
    _insert_db_pos(db_factory, "AAPL", 100, 10.0)
    # broker reports 137 shares @ 7.3 — not a known split ratio → UNKNOWN
    rec, ca = _reconciler([_bpos("AAPL", 137, 7.3)], db_factory)

    result = rec.reconcile("periodic")

    assert any(g["kind"] == "qty_corporate_action_unknown" for g in result.gaps)
    # reconciler still converged to broker truth (existing behavior preserved)
    row = _db_pos(db_factory, "AAPL")
    assert row.qty == 137
    # but the symbol is now gated — fail closed for trading until resolved
    assert ca.is_blocked("AAPL") is True
    sess = db_factory()
    try:
        ca_row = sess.query(CARow).filter(CARow.symbol == "AAPL").one()
    finally:
        sess.close()
    assert ca_row.status == "unknown"


# ── dry_run does not persist ─────────────────────────────────────────────────

def test_dry_run_classifies_but_does_not_persist_or_gate(db_factory):
    _insert_db_pos(db_factory, "AAPL", 100, 150.0)
    rec, ca = _reconciler([_bpos("AAPL", 200, 75.0)], db_factory)

    result = rec.reconcile("manual", dry_run=True)

    # gap is labeled (classification is pure) ...
    assert any(g["kind"] == "qty_corporate_action" for g in result.gaps)
    # ... but nothing was written or gated
    row = _db_pos(db_factory, "AAPL")
    assert (row.qty, row.avg_price) == (100, 150.0)  # DB untouched
    assert ca.is_blocked("AAPL") is False
    sess = db_factory()
    try:
        assert sess.query(CARow).count() == 0
    finally:
        sess.close()
