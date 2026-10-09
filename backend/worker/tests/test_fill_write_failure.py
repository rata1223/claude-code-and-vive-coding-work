"""P1-10 — a fill that cannot be recorded stops new trading and tells the operator.

A failed fill write used to be a warning: `fills` and `orders.filled_qty` fell
behind the broker, a restart restored from them, and nobody knew. Now it latches
`SAFE_MODE` until a restart (`RECORD_FAILURE`: entries blocked, exits allowed —
the in-memory tracker has the fill), alerts once per process and is audited
when the database allows. Nothing in the process reopens a latched gate.
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
def gate():
    """Open, and put back as found after the test (the root conftest only
    clears what a latched test leaves)."""
    g = recovery.SAFE_MODE
    saved = (g._can_trade, g._reason, g._cause, g._latch)
    g._latch = None
    g.enable()
    yield g
    g._can_trade, g._reason, g._cause, g._latch = saved


@pytest.fixture()
def alerts(monkeypatch, gate):
    sent = []
    monkeypatch.setattr("bot.notifier.alert_emergency", sent.append)
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
    def test_latches_the_gate_and_alerts(self, factory, alerts, monkeypatch):
        _broken_session(monkeypatch)

        _worker()._persist_fill(_fill(), _order(), cumulative=5)   # does not raise

        assert not recovery.SAFE_MODE.can_trade
        assert recovery.SAFE_MODE.halt_cause is HaltCause.RECORD_FAILURE
        assert ODNO in recovery.SAFE_MODE.reason
        assert len(alerts) == 1 and ODNO in alerts[0] and "재시작" in alerts[0]

    def test_a_second_failure_does_not_alert_again(self, factory, alerts, monkeypatch):
        _broken_session(monkeypatch)
        w = _worker()

        w._persist_fill(_fill(), _order(), cumulative=5)
        w._persist_fill(_fill(), _order(10, OrderStatus.FILLED), cumulative=10)

        assert len(alerts) == 1
        assert not recovery.SAFE_MODE.can_trade

    def test_an_alert_that_fails_still_leaves_the_gate_shut(self, factory, gate, monkeypatch):
        def broken(msg):
            raise ConnectionError("telegram down")
        monkeypatch.setattr("bot.notifier.alert_emergency", broken)
        _broken_session(monkeypatch)

        _worker()._persist_fill(_fill(), _order(), cumulative=5)

        assert gate.halt_cause is HaltCause.RECORD_FAILURE

    def test_the_alert_names_what_is_missing_after_the_broker_total(
            self, factory, alerts, monkeypatch):
        """Nothing on file, an increment of 5 bringing the broker to 10: 10
        shares are unrecorded, and that is what the operator must be told."""
        _seed(factory)

        def failing_commit(self):
            raise RuntimeError("commit 실패")
        monkeypatch.setattr("sqlalchemy.orm.Session.commit", failing_commit)
        _worker()._persist_fill(_fill(), _order(10, OrderStatus.FILLED), cumulative=10)

        assert "10주" in alerts[0]

    def test_a_fill_with_no_row_to_file_it_under(self, factory, alerts):
        """No order row: the fill is recorded nowhere — the same failure."""
        _worker()._persist_fill(_fill(), _order(), cumulative=5)

        assert recovery.SAFE_MODE.halt_cause is HaltCause.RECORD_FAILURE
        assert len(alerts) == 1
        with factory() as s:
            assert s.query(AuditLog).filter(
                AuditLog.event_type == "fill_write_failed").count() == 1


class TestNothingReopensALatchedGate:
    def test_enable_is_refused(self, gate, alerts):
        recovery.report_fill_write_failure(ODNO, "005930", 5, 70000.0, RuntimeError("x"))
        gate.enable()
        assert not gate.can_trade

    def test_a_later_risk_halt_does_not_make_it_resumable(self, gate, alerts):
        """The kill switch trips after the failure: the resume poll reopens a
        RISK_BREACH halt on release, so the cause must not become one."""
        recovery.report_fill_write_failure(ODNO, "005930", 5, 70000.0, RuntimeError("x"))
        gate.disable("일일 손실 한도 초과", cause=HaltCause.RISK_BREACH)
        assert gate.halt_cause is HaltCause.RECORD_FAILURE

    def test_exits_stay_allowed_under_a_risk_halt_that_came_first(self, gate, alerts):
        from backend.risk.halt_policy import OperationClass, is_allowed
        gate.disable("MDD 한도 초과", cause=HaltCause.RISK_BREACH)
        recovery.report_fill_write_failure(ODNO, "005930", 5, 70000.0, RuntimeError("x"))
        assert is_allowed(gate.halt_cause, OperationClass.EXIT)
        assert not is_allowed(gate.halt_cause, OperationClass.ENTRY)

    def test_a_risk_halt_does_not_loosen_an_untrusted_latch(self, gate, alerts):
        """Latched during recovery (untrusted): a later risk halt must not
        turn it into RECORD_FAILURE, which would allow exits."""
        gate.disable("초기화 중", cause=HaltCause.UNTRUSTED_STATE)
        recovery.report_fill_write_failure(ODNO, "005930", 5, 70000.0, RuntimeError("x"))
        gate.disable("일일 손실 한도 초과", cause=HaltCause.RISK_BREACH)
        assert gate.halt_cause is HaltCause.UNTRUSTED_STATE

    def test_an_untrusted_state_stays_untrusted(self, gate, alerts):
        gate.disable("복구 미완료", cause=HaltCause.UNTRUSTED_STATE)
        recovery.report_fill_write_failure(ODNO, "005930", 5, 70000.0, RuntimeError("x"))
        assert gate.halt_cause is HaltCause.UNTRUSTED_STATE
        gate.disable("또 다른 정지", cause=HaltCause.UNTRUSTED_STATE)
        assert gate.halt_cause is HaltCause.UNTRUSTED_STATE


    def test_no_session_factory_still_latches(self, factory, alerts, monkeypatch):
        """The factory itself cannot be built — that is the failure."""
        _broken_session(monkeypatch)

        def no_factory():
            raise RuntimeError("DB 초기화 실패")
        monkeypatch.setattr(runner, "_get_session_factory", no_factory)

        _worker()._persist_fill(_fill(), _order(), cumulative=5)

        assert recovery.SAFE_MODE.halt_cause is HaltCause.RECORD_FAILURE
        assert len(alerts) == 1

    def test_a_failed_audit_is_not_retried_for_every_fill(self, alerts):
        calls = {"n": 0}

        def down():
            calls["n"] += 1
            raise RuntimeError("DB 연결 끊김")
        for _ in range(3):
            recovery.report_fill_write_failure(ODNO, "005930", 5, 70000.0, RuntimeError("x"),
                                               session_factory=down)
        assert calls["n"] == 1


class TestDecisionsAreNotFailures:
    def test_a_skipped_redelivery(self, factory, alerts):
        _seed(factory)
        w = _worker()
        w._persist_fill(_fill(), _order(), cumulative=5)
        w._persist_fill(_fill(), _order(), cumulative=5)

        assert recovery.SAFE_MODE.can_trade and alerts == []

    def test_a_redelivered_last_fill_after_its_row_closed(self, factory, alerts):
        """The row closed with the last fill and the pipeline forgot it; the
        same fill again finds no open row — its fills already reach the total."""
        _seed(factory)
        with factory() as s:
            s.query(DBOrder).update({"trade_date": date(2026, 10, 9)})
            s.commit()
            row_id = s.query(DBOrder.id).scalar()
        w = _worker()
        w._persist_fill(_fill(qty=10), _order(10, OrderStatus.FILLED), row_id=row_id,
                        cumulative=10)

        w._persist_fill(_fill(qty=10), _order(10, OrderStatus.FILLED), cumulative=10)

        assert recovery.SAFE_MODE.can_trade and alerts == []

    def test_yesterdays_closed_row_with_the_number_is_another_order(self, factory, alerts):
        """KIS numbers restart daily (#168): yesterday's filled order with this
        number says nothing about today's fill, which has no row."""
        _seed(factory)
        with factory() as s:
            s.query(DBOrder).update({"trade_date": date(2026, 10, 8), "status": "filled"})
            s.add(DBFill(order_id=s.query(DBOrder.id).scalar(), qty=10, price=70000.0))
            s.commit()

        _worker()._persist_fill(_fill(), _order(), cumulative=5)

        assert recovery.SAFE_MODE.halt_cause is HaltCause.RECORD_FAILURE

    def test_a_closed_row_that_falls_short_is_still_a_failure(self, factory, alerts):
        _seed(factory)
        with factory() as s:
            s.query(DBOrder).update({"trade_date": date(2026, 10, 9), "status": "canceled"})
            s.commit()

        _worker()._persist_fill(_fill(), _order(), cumulative=5)

        assert recovery.SAFE_MODE.halt_cause is HaltCause.RECORD_FAILURE

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

        r = recovery.StartupRecovery.__new__(recovery.StartupRecovery)
        r._actions = []
        assert r._step_enable_trading() is False
        assert not recovery.SAFE_MODE.can_trade

    def test_the_recovery_failure_reason_names_the_unrecorded_fill(self, alerts, monkeypatch):
        """Through `run()`: its generic failure must still say why."""
        recovery.report_fill_write_failure(ODNO, "005930", 5, 70000.0, RuntimeError("x"))
        r = recovery.StartupRecovery.__new__(recovery.StartupRecovery)
        r._actions, r._should_abort, r.halted_by_risk = [], lambda: False, False
        steps = [m for m in dir(r) if m.startswith("_step_")]
        for m in steps:
            if m != "_step_enable_trading":
                monkeypatch.setattr(r, m, lambda: True)
        try:
            r.run()
        except Exception:
            pass
        assert ODNO in recovery.SAFE_MODE.reason

    def test_a_restored_kill_switch_does_not_make_it_resumable(self, alerts):
        """The kill-switch branch marks the worker resumable on release; it
        must not be reached when a fill could not be recorded."""
        recovery.report_fill_write_failure(ODNO, "005930", 5, 70000.0, RuntimeError("x"))
        r = recovery.StartupRecovery.__new__(recovery.StartupRecovery)
        r._actions = []
        r._kill_switch_active, r._kill_switch_from_row = True, True
        r.halted_by_risk = False

        assert r._step_enable_trading() is False
        assert r.halted_by_risk is False
        assert recovery.SAFE_MODE.halt_cause is HaltCause.RECORD_FAILURE

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

        assert recovery.SAFE_MODE.halt_cause is HaltCause.RECORD_FAILURE
        assert len(alerts) == 1

    def test_the_recovery_stub_reports_a_missing_row(self, factory, alerts):
        from unittest.mock import MagicMock
        _seed(factory)
        poller = MagicMock()
        r = recovery.StartupRecovery.__new__(recovery.StartupRecovery)
        r._factory, r._broker, r._shared_poller = factory, MagicMock(), poller
        r._step_pending_orders()
        stub = poller.register.call_args.kwargs["on_filled"]
        with factory() as s:
            s.query(DBOrder).delete()
            s.commit()

        stub(_order())

        assert recovery.SAFE_MODE.halt_cause is HaltCause.RECORD_FAILURE

    def test_the_recovery_stub_failure_is_audited(self, factory, alerts, monkeypatch):
        """Its session fails only on commit; the audit goes through a new one."""
        from unittest.mock import MagicMock
        _seed(factory)
        poller = MagicMock()
        r = recovery.StartupRecovery.__new__(recovery.StartupRecovery)
        r._factory, r._broker, r._shared_poller = factory, MagicMock(), poller
        r._step_pending_orders()
        stub = poller.register.call_args.kwargs["on_filled"]
        calls = {"n": 0}
        real_commit = factory.class_.commit

        def commit_once_failing(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("commit 실패")
            return real_commit(self)
        monkeypatch.setattr(factory.class_, "commit", commit_once_failing)
        stub(_order())

        with factory() as s:
            assert s.query(AuditLog).filter(
                AuditLog.event_type == "fill_write_failed").count() == 1
