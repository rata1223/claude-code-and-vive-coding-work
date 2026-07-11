"""
TASK P3-04 — Gate G1: Corporate-action *cash dividend* runtime ruling.

The P3-03B certification (docs/PAPER_TRADING_CERTIFICATION.md §4.1, scenario 12)
flagged that the live ``CorporateActionRuntime`` classifies **quantity jumps**;
a cash dividend produces *no* quantity jump, so it never flows through the live
reconcile/gate chain. The open question: **defect or by design?**

These tests establish the ruling as **by design** and *prove no drift*:

  1. A cash dividend (qty/avg unchanged on the broker, only price moves ex-div)
     flows through the real ``PositionReconciler`` as a **no-op** for positions —
     the qty-jump classifier is never triggered, no corporate-action gap is
     raised, and the persisted position does **not** drift.
  2. When a dividend *is* explicitly recorded, the gate blocks (fail-closed),
     survives a restart via ``restore_pending()``, and ``mark_applied`` clears
     the gate **without mutating qty/avg** — cash_delta lands only in the
     append-only history, never in the position book.

Ownership rationale (docs/CORPORATE_ACTION_RUNTIME_INTEGRATION.md,
backend/data/corporate_action_runtime.py:8-14): the broker is the sole authority
for qty/avg **and cash**; the reconciler is the sole position writer; the CA
runtime only detects/classifies/records/gates. A dividend changes only broker
cash (reflected by ``broker.get_balance()``); the runtime never posts cash_delta
to a ledger, so there is no drift by construction.

All real production objects — no mocks.
"""
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from backend.brokers.paper_broker import ScriptedPaperBroker
from backend.data.corporate_actions import ActionStatus, ActionType, CorporateAction
from backend.data.corporate_action_runtime import CorporateActionRuntime
from backend.database.models import Base, Position as DBPosition
from backend.database.testing import make_test_engine
from backend.execution.reconciler import PositionReconciler

BROKER = "kis"


@pytest.fixture()
def db_factory():
    eng = make_test_engine()
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def _seed_db_position(db_factory, symbol, qty, avg, market="US"):
    s = db_factory()
    try:
        s.add(DBPosition(symbol=symbol, qty=qty, avg_price=avg, market=market,
                         broker=BROKER))
        s.commit()
    finally:
        s.close()


def _db_positions(db_factory):
    s = db_factory()
    try:
        return {p.symbol: p for p in s.query(DBPosition).all()}
    finally:
        s.close()


# ── 1. Dividend = no qty jump ⇒ reconcile is a position no-op (no drift) ───────

def test_cash_dividend_produces_no_qty_jump_no_gap_no_drift(db_factory):
    """Ex-dividend day: the broker credits cash and the price drops, but qty and
    avg are unchanged. The live reconcile path must NOT classify this as a
    corporate action and must NOT drift the position."""
    _seed_db_position(db_factory, "SPY", 10, 100.0)

    broker = ScriptedPaperBroker(default_price=98.0)   # ex-div price drop
    broker.set_position("SPY", 10, 100.0, market="US")  # qty/avg identical
    broker.set_price("SPY", 98.0)

    ca = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    result = PositionReconciler(broker, db_factory, broker_name=BROKER,
                                ca_runtime=ca).reconcile("periodic")

    # No corporate-action gap of any kind was raised (qty matched → classifier
    # never ran).
    ca_gaps = [g for g in result.gaps if g["kind"].startswith("qty_corporate_action")]
    assert ca_gaps == [], f"unexpected CA classification on a dividend: {ca_gaps}"
    assert result.ok

    # Position book did not drift.
    pos = _db_positions(db_factory)["SPY"]
    assert pos.qty == 10
    assert pos.avg_price == pytest.approx(100.0)

    # No spurious gate block on the symbol.
    assert ca.is_blocked("SPY") is False


# ── 2. Explicit dividend: gate + restart-restore + apply preserves qty/avg ─────

def test_dividend_gate_blocks_then_restore_then_apply_preserves_book(db_factory):
    action = CorporateAction(ActionType.CASH_DIVIDEND, "SPY", date.today(),
                             status=ActionStatus.CONFIRMED, cash_amount=1.5)

    ca = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    ca.record(action)
    assert ca.is_blocked("SPY") is True            # fail-closed before apply

    # Restart: a fresh runtime rebuilds the gate from the DB.
    ca2 = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    assert ca2.restore_pending() >= 1
    assert ca2.is_blocked("SPY") is True

    # Apply the dividend: gate clears, and qty/avg are unchanged.
    ca2.mark_applied(action, qty_before=10, avg_before=100.0,
                     qty_after=10, avg_after=100.0, cash_delta=15.0)
    assert ca2.is_blocked("SPY") is False

    # cash_delta is recorded in append-only history ONLY (not the position book),
    # and qty/avg were preserved (value_preserved).
    hist = ca2.db_history_for("SPY")
    assert len(hist) == 1
    h = hist[0]
    assert h.qty_after == h.qty_before == 10
    assert h.avg_after == h.avg_before == pytest.approx(100.0)
    assert h.cash_delta == pytest.approx(15.0)


def test_dividend_apply_never_mutates_position_table(db_factory):
    """Applying a dividend must not touch the positions table — cash is
    broker-authoritative, positions are broker-truth-reconciled."""
    _seed_db_position(db_factory, "SPY", 10, 100.0)

    action = CorporateAction(ActionType.CASH_DIVIDEND, "SPY", date.today(),
                             status=ActionStatus.CONFIRMED, cash_amount=2.0)
    ca = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    ca.record(action)
    ca.mark_applied(action, qty_before=10, avg_before=100.0,
                    qty_after=10, avg_after=100.0, cash_delta=20.0)

    pos = _db_positions(db_factory)["SPY"]
    assert pos.qty == 10
    assert pos.avg_price == pytest.approx(100.0)
