"""
TASK P3-04 — Gate G3 (Postgres tier): DB/Redis restart-recovery.

Auto-skips unless TEST_DATABASE_URL points at a real Postgres (enforced by
tests/postgres/conftest.py); runs in CI's ci-postgres.yml job.

Drives the real ``StartupRecovery.run()`` against real Postgres to certify the
reconnect-and-recover path after an infrastructure bounce:

  * a **fresh** session factory (new engine/pool = reconnect) executes the DB
    probe and recovers persisted state,
  * a persisted pending order is re-registered with the shared poller,
  * the corporate-action gate and kill-switch survive the restart (fail-closed),
  * ``run()`` blocks trading when the prior session's kill-switch was HALTED.

Real production objects only — no mocks.
"""
from datetime import date, datetime

import pytest
from sqlalchemy.orm import sessionmaker

from backend.brokers.paper_broker import ScriptedPaperBroker
from backend.data.corporate_actions import ActionStatus, ActionType, CorporateAction
from backend.data.corporate_action_runtime import CorporateActionRuntime
from backend.execution.order_poller import OrderFillPoller
from backend.quant.risk.engine import _seoul_today
from backend.worker.recovery import SAFE_MODE, StartupRecovery

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


@pytest.fixture(autouse=True)
def _reset_safe_mode():
    SAFE_MODE.disable("test-init")
    yield
    SAFE_MODE.disable("test-teardown")


def _seed_position(db_factory, symbol, qty, avg, market="US"):
    from backend.database.models import Position as DBPosition
    s = db_factory()
    try:
        s.add(DBPosition(symbol=symbol, qty=qty, avg_price=avg, market=market,
                         broker=BROKER, updated_at=datetime.utcnow()))
        s.commit()
    finally:
        s.close()


def _seed_pending_order(db_factory, symbol, broker_order_id):
    from backend.database.models import Order as DBOrder
    s = db_factory()
    try:
        s.add(DBOrder(symbol=symbol, side="buy", qty=10, price=100.0,
                      status="submitted", market="US", broker=BROKER,
                      broker_order_id=broker_order_id, created_at=datetime.utcnow()))
        s.commit()
    finally:
        s.close()


def test_restart_recovery_reconnects_and_recovers_pending_order(db_factory, pg_trading_engine):
    _seed_position(db_factory, "SPY", 10, 100.0)
    _seed_pending_order(db_factory, "SPY", "BRK-PG-1")

    # "Infra bounce": discard the old factory, reconnect with a brand-new one.
    fresh = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    broker = ScriptedPaperBroker(default_price=100.0)
    broker.set_position("SPY", 10, 100.0, market="US")
    poller = OrderFillPoller(broker)

    rec = StartupRecovery(fresh, redis_client=None, broker=broker, poller=poller)
    ok = rec.run()

    assert ok is True
    assert SAFE_MODE.can_trade is True
    with poller._lock:  # noqa: SLF001
        assert "BRK-PG-1" in set(poller._entries.keys())


def test_restart_restores_ca_gate_and_halts_on_kill_switch(db_factory, pg_trading_engine):
    from backend.database.models import DailyRiskState
    _seed_position(db_factory, "SPY", 6, 100.0)
    ca = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    ca.record(CorporateAction(ActionType.SPLIT, "SPY", date.today(),
                              status=ActionStatus.UNKNOWN))   # unknown → fail-closed block
    s = db_factory()
    try:
        # Must match _seoul_today() below — PersistentLossTracker._restore_state()
        # looks up DailyRiskState keyed on Asia/Seoul's date, not the CI runner's
        # local/UTC date, which disagree for part of every day.
        s.add(DailyRiskState(trade_date=_seoul_today(), kill_switch=True,
                             kill_reason="이전 세션 손실 한도"))
        s.commit()
    finally:
        s.close()

    fresh = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    broker = ScriptedPaperBroker(default_price=100.0)
    broker.set_position("SPY", 6, 100.0, market="US")
    ca2 = CorporateActionRuntime(db_factory=fresh, broker=BROKER)

    rec = StartupRecovery(fresh, redis_client=None, broker=broker, ca_runtime=ca2)
    ok = rec.run()

    # Kill-switch from the prior session keeps trading blocked (fail-closed).
    assert ok is False
    assert SAFE_MODE.can_trade is False
    # CA gate was restored across the restart.
    assert ca2.is_blocked("SPY") is True
