"""
TASK P3-04 — Gates G1 & G2 on the Postgres tier.

Auto-skips unless TEST_DATABASE_URL points at a real Postgres (enforced by
tests/postgres/conftest.py); runs in CI's ci-postgres.yml job.

Mirrors the core-tier certification against real Postgres:

  * G1 — a cash dividend causes no qty jump ⇒ the live reconcile path is a
    position no-op (no corporate-action gap, no drift); an explicit dividend
    gates, restart-restores, and applies without mutating qty/avg.
  * G2 — emergency flatten → settle → reconcile converges the DB to the flat
    broker book (delete_position) and writes the full audit trail.

Real production objects only — no mocks.
"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from backend.brokers.paper_broker import ScriptedPaperBroker
from backend.data.corporate_actions import ActionStatus, ActionType, CorporateAction
from backend.data.corporate_action_runtime import CorporateActionRuntime
from backend.execution.reconciler import PositionReconciler
from backend.worker.emergency import EmergencyFlattenManager

BROKER = "kis"


@pytest.fixture()
def db_factory(pg_trading_engine):
    from backend.database.models import (
        Position as DBPosition, Order as DBOrder, Fill, AuditLog,
        ReconciliationLog, CorporateAction as DBCA, CorporateActionHistory,
        DailyRiskState,
    )
    Factory = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    s = Factory()
    try:
        for model in (CorporateActionHistory, DBCA, Fill, DBOrder, DBPosition,
                      ReconciliationLog, AuditLog, DailyRiskState):
            s.query(model).delete()
        s.commit()
    finally:
        s.close()
    return Factory


def _seed_db_position(db_factory, symbol, qty, avg, market="US", age_hours=2.0):
    from backend.database.models import Position as DBPosition
    s = db_factory()
    try:
        s.add(DBPosition(symbol=symbol, qty=qty, avg_price=avg, market=market,
                         broker=BROKER,
                         updated_at=datetime.utcnow() - timedelta(hours=age_hours)))
        s.commit()
    finally:
        s.close()


def _db_positions(db_factory):
    from backend.database.models import Position as DBPosition
    s = db_factory()
    try:
        return {p.symbol: p for p in s.query(DBPosition).all()}
    finally:
        s.close()


def _audit_events(db_factory):
    from backend.database.models import AuditLog
    s = db_factory()
    try:
        return [a.event_type for a in s.query(AuditLog).all()]
    finally:
        s.close()


# ── G1 — dividend runtime ─────────────────────────────────────────────────────

def test_pg_dividend_no_qty_jump_no_gap_no_drift(db_factory):
    _seed_db_position(db_factory, "SPY", 10, 100.0)
    broker = ScriptedPaperBroker(default_price=98.0)
    broker.set_position("SPY", 10, 100.0, market="US")
    broker.set_price("SPY", 98.0)

    ca = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    result = PositionReconciler(broker, db_factory, broker_name=BROKER,
                                ca_runtime=ca).reconcile("periodic")

    assert [g for g in result.gaps if g["kind"].startswith("qty_corporate_action")] == []
    assert result.ok
    pos = _db_positions(db_factory)["SPY"]
    assert pos.qty == 10 and pos.avg_price == pytest.approx(100.0)
    assert ca.is_blocked("SPY") is False


def test_pg_dividend_apply_preserves_book(db_factory):
    action = CorporateAction(ActionType.CASH_DIVIDEND, "SPY", date.today(),
                             status=ActionStatus.CONFIRMED, cash_amount=1.5)
    ca = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    ca.record(action)
    assert ca.is_blocked("SPY") is True

    ca2 = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    assert ca2.restore_pending() >= 1
    assert ca2.is_blocked("SPY") is True

    ca2.mark_applied(action, qty_before=10, avg_before=100.0,
                     qty_after=10, avg_after=100.0, cash_delta=15.0)
    assert ca2.is_blocked("SPY") is False
    hist = ca2.db_history_for("SPY")
    assert len(hist) == 1
    assert hist[0].qty_after == hist[0].qty_before == 10
    assert hist[0].cash_delta == pytest.approx(15.0)


# ── G2 — emergency flatten fill path ──────────────────────────────────────────

def test_pg_flatten_settle_reconcile_converges_and_audits(db_factory):
    broker = ScriptedPaperBroker(default_price=100.0)
    broker.set_position("SPY", 10, 100.0, market="US")
    broker.set_position("069500", 5, 30000.0, market="KR")
    _seed_db_position(db_factory, "SPY", 10, 100.0)
    _seed_db_position(db_factory, "069500", 5, 30000.0, market="KR")

    mgr = EmergencyFlattenManager(broker, db_factory=db_factory, dry_run=False)
    res = mgr.flatten_all("cert")
    assert res["submitted"] == 2 and res["failed"] == []

    broker.settle_all_open()
    assert broker.get_positions() == []

    result = PositionReconciler(broker, db_factory, broker_name=BROKER).reconcile("startup")
    deleted = {r["symbol"] for r in result.repairs if r["kind"] == "delete_position"}
    assert deleted == {"SPY", "069500"}
    assert _db_positions(db_factory) == {}

    events = _audit_events(db_factory)
    assert "emergency_flatten_start" in events
    assert events.count("emergency_flatten_order") == 2
    assert "emergency_flatten_complete" in events
