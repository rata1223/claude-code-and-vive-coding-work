"""
Stage 2 engine-stabilization regression tests.

Each test pins one fail-open / silent-failure fix made in STAGE2_ENGINE_STABILIZATION:
  - recovery._step_risk      → fail-closed on risk-state restore error
  - recovery._step_reconcile → fail-closed on reconcile errors (gaps still OK)
  - runner fill callback     → sell w/o entry price still drives MDD eval + audit
  - runner fill callback     → MDD fallback uses seeded last-known equity
  - scheduler session gate    → fail-closed (no signal) on calendar error

Runs entirely on in-memory SQLite — no Redis / KIS network.
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import backend.worker.runner as runner
import backend.worker.scheduler as scheduler
from backend.brokers.models import Order as BOrder, OrderStatus
from backend.database.models import AuditLog, Base, Position as DBPosition
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import Fill, PositionTracker
from backend.worker.recovery import StartupRecovery


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _count_audit(factory, event_type):
    sess = factory()
    try:
        return sess.query(AuditLog).filter(AuditLog.event_type == event_type).count()
    finally:
        sess.close()


# ── recovery._step_risk: fail-closed ────────────────────────────────────────

class TestStepRiskFailClosed:
    def test_risk_restore_exception_blocks_trading(self, db_factory, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("redis down")
        monkeypatch.setattr("backend.quant.risk.engine.PersistentLossTracker", _boom)
        rec = StartupRecovery(db_factory)
        ok = rec._step_risk()
        # step returns True (non-fatal) but marks kill-switch active → enable_trading blocks
        assert ok is True
        assert getattr(rec, "_kill_switch_active", False) is True

    def test_risk_restore_success_does_not_block(self, db_factory, monkeypatch):
        tracker = MagicMock()
        tracker.kill_switch = False
        monkeypatch.setattr("backend.quant.risk.engine.PersistentLossTracker",
                            lambda *a, **k: tracker)
        rec = StartupRecovery(db_factory)
        assert rec._step_risk() is True
        assert getattr(rec, "_kill_switch_active", False) is False


# ── recovery._step_reconcile: fail-closed on errors ─────────────────────────

class TestStepReconcileFailClosed:
    def _patch_reconciler(self, monkeypatch, errors):
        result = MagicMock()
        result.gaps = []
        result.repairs = []
        result.errors = errors
        result.ok = not errors
        recon = MagicMock()
        recon.reconcile.return_value = result
        monkeypatch.setattr("backend.execution.reconciler.PositionReconciler",
                            lambda *a, **k: recon)

    def test_reconcile_errors_block_trading(self, db_factory, monkeypatch):
        self._patch_reconciler(monkeypatch, errors=["broker fetch failed"])
        rec = StartupRecovery(db_factory, broker=MagicMock())
        assert rec._step_reconcile() is False  # fail-closed

    def test_reconcile_clean_allows_trading(self, db_factory, monkeypatch):
        self._patch_reconciler(monkeypatch, errors=[])
        rec = StartupRecovery(db_factory, broker=MagicMock())
        assert rec._step_reconcile() is True

    def test_reconcile_exception_blocks_trading(self, db_factory, monkeypatch):
        recon = MagicMock()
        recon.reconcile.side_effect = RuntimeError("db down")
        monkeypatch.setattr("backend.execution.reconciler.PositionReconciler",
                            lambda *a, **k: recon)
        rec = StartupRecovery(db_factory, broker=MagicMock())
        assert rec._step_reconcile() is False  # fail-closed


# ── runner fill callback: risk eval never silently skipped ──────────────────

class _FakeLossTracker:
    def __init__(self):
        self.kill_switch = False
        self.kill_reason = ""
        self.calls = []
    def record_pnl(self, pnl, equity):
        self.calls.append((pnl, equity))


def _worker_for_callback(monkeypatch, loss_tracker, last_known_equity=None):
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w._poller = None
    w._loss_tracker = loss_tracker
    w._last_known_equity = last_known_equity
    monkeypatch.setattr(w, "_persist_fill", lambda *a, **k: None)
    monkeypatch.setattr(w, "_upsert_position_db", lambda *a, **k: None)
    monkeypatch.setattr(w, "_publish_order_update", lambda *a, **k: None)
    return w


def _sell_order():
    return BOrder(id="O1", symbol="005930", side="sell", qty=10, price=100.0,
                  status=OrderStatus.FILLED, filled_qty=10, avg_fill_price=100.0)


class TestFillCallbackRiskEval:
    def test_sell_without_position_still_runs_mdd_and_audits(self, db_factory, monkeypatch):
        monkeypatch.setattr(runner, "_SessionFactory", db_factory)
        bal = MagicMock(); bal.total_eval_krw = 2_000_000.0
        broker = MagicMock(); broker.get_balance.return_value = bal
        monkeypatch.setattr(runner, "get_kis_broker", lambda: broker)

        lt = _FakeLossTracker()
        w = _worker_for_callback(monkeypatch, lt)
        tracker = PositionTracker(OrderStateMachine())  # empty → no position
        cb = w._make_fill_callback(tracker, OrderStateMachine(), run_id=1)

        cb(_sell_order())

        # record_pnl MUST still run (equity-based MDD eval) with realized_pnl=0.0
        assert lt.calls == [(0.0, 2_000_000.0)]
        assert _count_audit(db_factory, "sell_without_entry_price") == 1

    def test_sell_uses_seeded_equity_when_balance_fetch_fails(self, db_factory, monkeypatch):
        monkeypatch.setattr(runner, "_SessionFactory", db_factory)
        broker = MagicMock(); broker.get_balance.side_effect = RuntimeError("api down")
        monkeypatch.setattr(runner, "get_kis_broker", lambda: broker)

        lt = _FakeLossTracker()
        # seeded last-known equity (Fix 5) → MDD eval still runs on balance failure
        w = _worker_for_callback(monkeypatch, lt, last_known_equity=1_500_000.0)
        tracker = PositionTracker(OrderStateMachine())
        tracker.on_fill(Fill(order_id="b", symbol="005930", side="buy",
                             qty=10, price=90.0, market="KR"))  # prime entry price
        cb = w._make_fill_callback(tracker, OrderStateMachine(), run_id=1)

        cb(_sell_order())

        # realized_pnl = (100-90)*10 = 100, equity falls back to seeded 1.5M
        assert lt.calls == [(100.0, 1_500_000.0)]

    def test_sell_skips_mdd_only_when_no_equity_available(self, db_factory, monkeypatch):
        monkeypatch.setattr(runner, "_SessionFactory", db_factory)
        broker = MagicMock(); broker.get_balance.side_effect = RuntimeError("api down")
        monkeypatch.setattr(runner, "get_kis_broker", lambda: broker)

        lt = _FakeLossTracker()
        w = _worker_for_callback(monkeypatch, lt, last_known_equity=None)
        tracker = PositionTracker(OrderStateMachine())
        cb = w._make_fill_callback(tracker, OrderStateMachine(), run_id=1)

        cb(_sell_order())

        assert lt.calls == []  # no equity → MDD eval skipped (audited)
        assert _count_audit(db_factory, "balance_fetch_failed") == 1


# ── scheduler session gate: fail-closed on calendar error ───────────────────

class TestSchedulerCalendarFailClosed:
    def test_kr_session_skips_signal_on_calendar_error(self, monkeypatch):
        published = []
        monkeypatch.setattr(scheduler, "_publish_session_signal", lambda ch: published.append(ch))

        def _boom():
            raise RuntimeError("calendar unavailable")
        monkeypatch.setattr("backend.data.calendar.get_calendar_service", _boom)

        scheduler._trigger_kr_session()
        assert published == []  # fail-closed: no signal on calendar error

    def test_us_session_skips_signal_on_calendar_error(self, monkeypatch):
        published = []
        monkeypatch.setattr(scheduler, "_publish_session_signal", lambda ch: published.append(ch))
        monkeypatch.setattr("backend.data.calendar.get_calendar_service",
                            lambda: (_ for _ in ()).throw(RuntimeError("x")))
        scheduler._trigger_us_session()
        assert published == []
