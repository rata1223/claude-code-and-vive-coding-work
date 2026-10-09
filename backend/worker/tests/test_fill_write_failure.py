"""P1-10 — a fill that cannot be recorded stops trading and tells the operator.

A failed fill write used to be a warning: `fills` and `orders.filled_qty` fell
behind the broker, a restart restored from them, and nobody knew. Now it closes
`SAFE_MODE` as untrusted state (the kill-switch resume poll only reopens a risk
halt — `test_release_baseline::test_an_untrusted_halt_is_not_resumed`), alerts
once per process and is audited when the database allows.
"""
from datetime import date, datetime

import pytest
from sqlalchemy.orm import sessionmaker

import backend.worker.recovery as recovery
import backend.worker.runner as runner
from backend.brokers.models import Order as BOrder, OrderStatus
from backend.database.models import AuditLog, Base, Fill as DBFill, Order as DBOrder
from backend.database.testing import make_test_engine
from backend.execution.position_tracker import Fill
from backend.risk.halt_policy import HaltCause

ODNO = "0000222"


@pytest.fixture()
def factory(monkeypatch):
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    f = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(runner, "_SessionFactory", f)
    monkeypatch.setattr("backend.database.models.seoul_date", lambda: date(2026, 10, 9))
    return f


@pytest.fixture()
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr("bot.notifier.alert_emergency", sent.append)
    recovery.SAFE_MODE.enable()
    return sent


def _worker():
    return runner.StrategyWorker.__new__(runner.StrategyWorker)


def _seed(factory):
    with factory() as s:
        s.add(DBOrder(broker_order_id=ODNO, symbol="005930", side="buy", qty=10,
                      price=70000.0, filled_qty=0, status="submitted",
                      market="KR", broker="kis", created_at=datetime.utcnow()))
        s.commit()


def _order(filled=5, status=OrderStatus.PARTIAL_FILLED):
    return BOrder(id=ODNO, symbol="005930", side="buy", qty=10, price=70000.0,
                  status=status, filled_qty=filled, avg_fill_price=70000.0)


def _fill(qty=5):
    return Fill(order_id=ODNO, symbol="005930", side="buy", qty=qty, price=70000.0, market="KR")


def _broken_session(monkeypatch):
    def boom():
        raise RuntimeError("DB 연결 끊김")
    monkeypatch.setattr(runner, "_session", boom)


class TestAFailedWrite:
    def test_closes_the_gate_as_untrusted_and_alerts(self, factory, alerts, monkeypatch):
        _broken_session(monkeypatch)

        _worker()._persist_fill(_fill(), _order(), cumulative=5)   # does not raise

        assert not recovery.SAFE_MODE.can_trade
        assert recovery.SAFE_MODE.halt_cause is HaltCause.UNTRUSTED_STATE
        assert ODNO in recovery.SAFE_MODE.reason
        assert len(alerts) == 1 and ODNO in alerts[0] and "재시작" in alerts[0]

    def test_a_second_failure_does_not_alert_again(self, factory, alerts, monkeypatch):
        _broken_session(monkeypatch)
        w = _worker()

        w._persist_fill(_fill(), _order(), cumulative=5)
        w._persist_fill(_fill(), _order(10, OrderStatus.FILLED), cumulative=10)

        assert len(alerts) == 1
        assert not recovery.SAFE_MODE.can_trade

    def test_an_alert_that_fails_still_leaves_the_gate_shut(self, factory, monkeypatch):
        def broken(msg):
            raise ConnectionError("telegram down")
        monkeypatch.setattr("bot.notifier.alert_emergency", broken)
        recovery.SAFE_MODE.enable()
        _broken_session(monkeypatch)

        _worker()._persist_fill(_fill(), _order(), cumulative=5)

        assert recovery.SAFE_MODE.halt_cause is HaltCause.UNTRUSTED_STATE

    def test_a_fill_with_no_row_to_file_it_under(self, factory, alerts):
        """No order row: the fill is recorded nowhere — the same failure."""
        _worker()._persist_fill(_fill(), _order(), cumulative=5)

        assert recovery.SAFE_MODE.halt_cause is HaltCause.UNTRUSTED_STATE
        assert len(alerts) == 1
        with factory() as s:
            assert s.query(AuditLog).filter(
                AuditLog.event_type == "fill_write_failed").count() == 1


class TestDecisionsAreNotFailures:
    def test_a_skipped_redelivery(self, factory, alerts):
        _seed(factory)
        w = _worker()
        w._persist_fill(_fill(), _order(), cumulative=5)
        w._persist_fill(_fill(), _order(), cumulative=5)

        assert recovery.SAFE_MODE.can_trade and alerts == []

    def test_a_refused_overfill(self, factory, alerts):
        _seed(factory)
        w = _worker()
        w._persist_fill(_fill(qty=10), _order(10))
        w._persist_fill(_fill(qty=10), _order(10))       # no total: 20 of 10

        assert recovery.SAFE_MODE.can_trade and alerts == []
        with factory() as s:
            assert s.query(DBFill).count() == 1


class TestStartupRecovery:
    def test_a_failure_during_recovery_keeps_the_gate_shut_at_its_end(self, alerts):
        """The recovery stub's write fails while recovery still runs; its last
        step must not open the gate again."""
        recovery.report_fill_write_failure(ODNO, "005930", 5, 70000.0, RuntimeError("x"))
        assert recovery.fill_write_failure() is not None

        r = recovery.StartupRecovery.__new__(recovery.StartupRecovery)
        r._actions = []
        assert r._step_enable_trading() is False
        assert not recovery.SAFE_MODE.can_trade

    def test_the_recovery_fill_stub_reports_its_failure(self, factory, alerts):
        """The DB-only callback recovery registers for still-open orders."""
        from unittest.mock import MagicMock
        _seed(factory)
        poller = MagicMock()
        r = recovery.StartupRecovery.__new__(recovery.StartupRecovery)
        r._factory, r._broker, r._shared_poller = factory, MagicMock(), poller
        r._step_pending_orders()
        stub = poller.register.call_args.kwargs["on_filled"]

        class Broken:
            def get(self, *a):
                raise RuntimeError("DB 연결 끊김")

            def close(self):
                pass
        r._factory = Broken
        stub(_order())

        assert recovery.SAFE_MODE.halt_cause is HaltCause.UNTRUSTED_STATE
        assert len(alerts) == 1
