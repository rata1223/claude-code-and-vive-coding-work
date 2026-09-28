"""Issue #176 — build the DB engine once, not on every event.

``init_db_factory`` creates an engine and runs ``create_all``. Two periodic
paths called it per event and never disposed the result:

* ``WorkerWatchdog._alert_dead_worker`` — once per worker outage, in every
  gunicorn worker of kis-api;
* ``scheduler._periodic_reconcile`` — every 30 minutes, in the worker.

Each call left an engine and its connection pool behind, and ran DDL checks at
the moment the halt was supposed to be written.
"""
import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, DailyRiskState, trading_day
from backend.database.testing import make_test_engine


@pytest.fixture()
def counted_factory(monkeypatch):
    """``init_db_factory`` replaced by one that counts and hands out SQLite."""
    import backend.database.models as models
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    calls = []

    def init(url):
        calls.append(url)
        return factory

    monkeypatch.setattr(models, "init_db_factory", init)
    return factory, calls


@pytest.fixture(autouse=True)
def _quiet_alerts(monkeypatch):
    monkeypatch.setattr("bot.notifier.alert_emergency", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr("backend.websocket.server.publish_alert",
                        lambda *_a, **_k: None, raising=False)


class TestWatchdog:
    def test_repeated_outages_reuse_one_factory(self, counted_factory):
        from backend.worker.heartbeat import WorkerWatchdog
        factory, calls = counted_factory
        w = WorkerWatchdog(redis_client=None)

        w._alert_dead_worker()
        w._alert_dead_worker()
        w._alert_dead_worker()

        assert len(calls) == 1
        with factory() as s:
            assert s.get(DailyRiskState, trading_day()).kill_switch is True

    def test_a_failed_build_is_retried_at_the_next_outage(self, counted_factory, monkeypatch):
        """Not cached while the DB is down — otherwise the watchdog could never
        write a halt again for the life of the process."""
        import backend.database.models as models
        from backend.worker.heartbeat import WorkerWatchdog
        factory, calls = counted_factory
        good = models.init_db_factory

        def down(url):
            calls.append(url)
            raise RuntimeError("db down")

        monkeypatch.setattr(models, "init_db_factory", down)
        w = WorkerWatchdog(redis_client=None)
        w._alert_dead_worker()
        assert w._db_factory is None

        monkeypatch.setattr(models, "init_db_factory", good)
        w._alert_dead_worker()
        assert len(calls) == 2
        with factory() as s:
            assert s.get(DailyRiskState, trading_day()).kill_switch is True


class TestPeriodicReconcile:
    def test_it_uses_the_modules_one_factory(self, counted_factory, monkeypatch):
        import backend.worker.scheduler as scheduler
        factory, calls = counted_factory
        monkeypatch.setattr(scheduler, "_DB_FACTORY", None)
        seen = []

        class FakeReconciler:
            def __init__(self, broker, db_factory, redis_client):
                seen.append(db_factory)

            def reconcile(self, trigger):
                class R:
                    gaps, repairs = [], []
                return R()

        monkeypatch.setattr("backend.execution.reconciler.PositionReconciler", FakeReconciler)
        monkeypatch.setattr("backend.brokers.kis.get_kis_broker", lambda: object())
        monkeypatch.setattr("redis.from_url", lambda *_a, **_k: object())

        scheduler._periodic_reconcile()
        scheduler._periodic_reconcile()

        assert len(calls) == 1
        assert seen == [factory, factory]
