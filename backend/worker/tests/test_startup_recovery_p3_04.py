"""
TASK P3-04 — Gate G3 (core tier): DB/Redis restart-recovery.

The P3-03B certification (docs/PAPER_TRADING_CERTIFICATION.md §4.3, scenarios
19/20) covered outage *tolerance* + persistence, but not the **reconnect-and-
recover** sequence after an infrastructure bounce. These tests drive the real
``StartupRecovery.run()`` 8-step sequence end-to-end and prove:

  * DB reconnect succeeds through a fresh session factory (SELECT 1),
  * a persisted pending order is re-registered with the shared ``OrderFillPoller``,
  * ``run()`` reaches ``SAFE_MODE.enable()``,
  * the Redis step degrades non-fatally whether the client is down or healthy,
  * the production DB factory keeps ``pool_pre_ping`` enabled (the reconnect
    contract that lets SQLAlchemy transparently recover a stale connection after
    a Postgres bounce).

Real production objects — ScriptedPaperBroker + real StartupRecovery/reconciler/
poller. No mocks for the runtime under test.
"""
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from backend.brokers.paper_broker import ScriptedPaperBroker
from backend.database.models import Base, Order as DBOrder, Position as DBPosition
from backend.database.testing import make_test_engine
from backend.execution.order_poller import OrderFillPoller
from backend.worker import recovery as recmod
from backend.worker.recovery import SAFE_MODE, StartupRecovery

BROKER = "kis"


@pytest.fixture()
def db_factory():
    eng = make_test_engine()
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _reset_safe_mode():
    SAFE_MODE.disable("test-init")
    yield
    SAFE_MODE.disable("test-teardown")


def _seed_pending_order(db_factory, symbol, broker_order_id):
    s = db_factory()
    try:
        s.add(DBOrder(symbol=symbol, side="buy", qty=10, price=100.0,
                      status="submitted", market="US", broker=BROKER,
                      broker_order_id=broker_order_id, created_at=datetime.utcnow()))
        s.commit()
    finally:
        s.close()


def _seed_position(db_factory, symbol, qty, avg, market="US"):
    s = db_factory()
    try:
        s.add(DBPosition(symbol=symbol, qty=qty, avg_price=avg, market=market,
                         broker=BROKER, updated_at=datetime.utcnow()))
        s.commit()
    finally:
        s.close()


# ── Full reconnect + pending-order recovery through real StartupRecovery.run() ─

def test_startup_recovery_reconnects_and_recovers_pending_order(db_factory):
    # Prior-session state persisted before the "restart".
    _seed_position(db_factory, "SPY", 10, 100.0)
    _seed_pending_order(db_factory, "SPY", "BRK-1")

    # After the bounce the broker reflects ground truth (position matches DB, so
    # reconcile is clean); a fresh shared poller stands in for the worker's.
    broker = ScriptedPaperBroker(default_price=100.0)
    broker.set_position("SPY", 10, 100.0, market="US")
    poller = OrderFillPoller(broker)

    rec = StartupRecovery(db_factory, redis_client=None, broker=broker,
                          poller=poller)
    ok = rec.run()

    assert ok is True
    assert SAFE_MODE.can_trade is True

    # The persisted pending order was re-registered with the shared poller.
    with poller._lock:  # noqa: SLF001 - inspect registered entries deterministically
        registered = set(poller._entries.keys())
    assert "BRK-1" in registered


def test_step_redis_tolerates_down_and_healthy_clients(db_factory):
    broker = ScriptedPaperBroker(default_price=100.0)

    class _DownRedis:
        def ping(self):
            raise ConnectionError("redis down")

    class _HealthyRedis:
        def ping(self):
            return True

    # None → step skipped, non-fatal.
    assert StartupRecovery(db_factory, redis_client=None, broker=broker)._step_redis() is True
    # Down → degrade to DB-authoritative mode, non-fatal.
    assert StartupRecovery(db_factory, redis_client=_DownRedis(), broker=broker)._step_redis() is True
    # Healthy → reconnected.
    assert StartupRecovery(db_factory, redis_client=_HealthyRedis(), broker=broker)._step_redis() is True


def test_step_db_reconnect_via_fresh_factory(db_factory):
    """A fresh factory after a 'restart' executes SELECT 1 — the DB reconnect
    probe StartupRecovery relies on."""
    broker = ScriptedPaperBroker(default_price=100.0)
    assert StartupRecovery(db_factory, broker=broker)._step_db() is True


def test_init_db_factory_engine_keeps_pool_pre_ping():
    """Regression guard for the Postgres reconnect contract: the production
    session factory must keep pool_pre_ping enabled so a stale connection after
    a DB bounce is transparently recycled instead of raising OperationalError."""
    from backend.database.models import init_db_factory
    factory = init_db_factory("sqlite:///:memory:")
    engine = factory.kw["bind"]
    assert getattr(engine.pool, "_pre_ping", False) is True
