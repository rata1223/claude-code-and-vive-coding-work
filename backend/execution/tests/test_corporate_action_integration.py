"""
Layer-3 tests (TASK P2-02C): the runtime integration end-to-end —
split during restart, pending-action recovery via the PositionTracker gate,
and the provider-adjusted-data ownership boundary.
"""
from datetime import date
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from backend.database.testing import make_test_engine
from backend.brokers.models import Position as BPosition
from backend.database.models import (
    Base, Position as DBPosition,
    CorporateAction as CARow, CorporateActionHistory as CAHist,
)
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import PositionTracker
from backend.data.corporate_action_runtime import CorporateActionRuntime
from backend.worker.recovery import StartupRecovery

_EFF = date(2026, 6, 5)


@pytest.fixture()
def factory():
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _insert_db_pos(f, symbol, qty, avg, market="KR"):
    sess = f()
    sess.add(DBPosition(symbol=symbol, qty=qty, avg_price=avg, market=market, broker="kis"))
    sess.commit()
    sess.close()


# ── split during restart ─────────────────────────────────────────────────────

def test_split_during_restart_is_detected_and_recorded(factory):
    """A 2:1 split happened while the worker was down. On restart, the startup
    reconcile classifies the broker jump, records the corporate action, converges
    the DB to broker truth, and writes history — without any double-adjust."""
    _insert_db_pos(factory, "AAPL", 100, 150.0)              # stale pre-split DB
    broker = MagicMock()
    broker.get_positions.return_value = [BPosition("AAPL", 200, 75.0, market="KR")]

    ca = CorporateActionRuntime(db_factory=factory, broker="kis")
    recovery = StartupRecovery(db_session_factory=factory, redis_client=None,
                               broker=broker, ca_runtime=ca)

    assert recovery._step_reconcile() is True

    sess = factory()
    try:
        pos = sess.query(DBPosition).filter(DBPosition.symbol == "AAPL").one()
        ca_row = sess.query(CARow).filter(CARow.symbol == "AAPL").one()
        hist = sess.query(CAHist).filter(CAHist.symbol == "AAPL").all()
    finally:
        sess.close()
    assert (pos.qty, pos.avg_price) == (200, 75.0)           # converged to broker
    assert ca_row.action_type == "split" and ca_row.status == "applied"
    assert len(hist) == 1 and hist[0].value_preserved is True
    assert ca.is_blocked("AAPL") is False                    # legit split → tradeable


def test_unknown_jump_during_restart_stays_blocked_after_recovery(factory):
    """An unexplained jump during downtime must keep the symbol blocked after the
    restart reconcile (fail-closed)."""
    _insert_db_pos(factory, "AAPL", 100, 10.0)
    broker = MagicMock()
    broker.get_positions.return_value = [BPosition("AAPL", 137, 7.3, market="KR")]  # UNKNOWN ratio

    ca = CorporateActionRuntime(db_factory=factory, broker="kis")
    recovery = StartupRecovery(db_session_factory=factory, redis_client=None,
                               broker=broker, ca_runtime=ca)
    recovery._step_reconcile()

    assert ca.is_blocked("AAPL") is True


# ── pending-action recovery across a fresh process ───────────────────────────

def test_pending_action_recovered_into_fresh_runtime(factory):
    """A blocking action persisted before restart is restored into a brand-new
    runtime by StartupRecovery (no broker jump needed)."""
    # process 1: persist a blocking UNKNOWN action
    ca1 = CorporateActionRuntime(db_factory=factory, broker="kis")
    action = ca1.classify_broker_jump("MSFT", 100, 10.0, 137, 7.3, _EFF)
    ca1.record(action)

    # process 2: fresh runtime + recovery; broker reports nothing anomalous
    ca2 = CorporateActionRuntime(db_factory=factory, broker="kis")
    broker = MagicMock()
    broker.get_positions.return_value = []
    recovery = StartupRecovery(db_session_factory=factory, redis_client=None,
                               broker=broker, ca_runtime=ca2)
    assert ca2.is_blocked("MSFT") is False     # nothing in memory yet
    recovery._step_reconcile()
    assert ca2.is_blocked("MSFT") is True       # restored from DB → still fail-closed


# ── PositionTracker gate (fail-closed order entry) ───────────────────────────

def test_tracker_blocks_order_entry_for_gated_symbol(factory):
    ca = CorporateActionRuntime(db_factory=factory, broker="kis")
    action = ca.classify_broker_jump("AAPL", 100, 10.0, 137, 7.3, _EFF)  # UNKNOWN → blocks
    ca.record(action)

    tracker = PositionTracker(OrderStateMachine(), corporate_action_runtime=ca)
    # gated symbol: order entry refused
    assert tracker.can_place_order("AAPL") is False
    assert tracker.try_mark_pending("AAPL") is False
    # unrelated symbol: unaffected
    assert tracker.can_place_order("GOOG") is True
    assert tracker.try_mark_pending("GOOG") is True


def test_tracker_without_ca_runtime_is_unchanged():
    tracker = PositionTracker(OrderStateMachine())  # no CA runtime
    assert tracker.can_place_order("AAPL") is True
    assert tracker.try_mark_pending("AAPL") is True


class _RaisingGateRuntime:
    def is_blocked(self, symbol):
        raise RuntimeError("gate runtime down")


def test_tracker_fails_closed_when_gate_runtime_errors():
    """If the CA gate cannot be verified, order entry must be refused (fail closed),
    never silently allowed."""
    tracker = PositionTracker(OrderStateMachine(),
                              corporate_action_runtime=_RaisingGateRuntime())
    assert tracker.can_place_order("AAPL") is False
    assert tracker.try_mark_pending("AAPL") is False


class _RaisingRestoreRuntime:
    def classify_broker_jump(self, *a, **k):  # pragma: no cover - must not be called here
        raise AssertionError("classify should not run when broker has no positions")
    def restore_pending(self):
        raise RuntimeError("db unavailable during restore")


def test_recovery_fails_closed_when_gate_restore_errors(factory):
    """A failed gate restore must keep SafeMode disabled (return False), not boot
    with an empty gate."""
    broker = MagicMock()
    broker.get_positions.return_value = []
    recovery = StartupRecovery(db_session_factory=factory, redis_client=None,
                               broker=broker, ca_runtime=_RaisingRestoreRuntime())
    assert recovery._step_reconcile() is False


def test_tracker_unblocks_after_split_applied(factory):
    ca = CorporateActionRuntime(db_factory=factory, broker="kis")
    action = ca.classify_broker_jump("AAPL", 100, 150.0, 200, 75.0, _EFF)  # CONFIRMED split
    ca.record(action)
    tracker = PositionTracker(OrderStateMachine(), corporate_action_runtime=ca)
    assert tracker.can_place_order("AAPL") is False          # blocked while pending
    ca.mark_applied(action, qty_before=100, avg_before=150.0, qty_after=200, avg_after=75.0)
    assert tracker.can_place_order("AAPL") is True            # cleared once applied


# ── provider-adjusted data ownership ─────────────────────────────────────────

def test_runtime_never_adjusts_prices_or_positions(factory):
    """The position-CA runtime is a detector/recorder/gate — it owns no price or
    position *adjustment*. Provider-adjusted (e.g. yfinance auto_adjust) price data
    is therefore never re-adjusted by this path (no double-adjust)."""
    ca = CorporateActionRuntime(db_factory=factory, broker="kis")
    # No bar/price/position mutation entry points exist on the runtime surface.
    for forbidden in ("adjust_bar", "adjust_bars", "adjust_price", "adjust_position",
                      "factor_for"):
        assert not hasattr(ca, forbidden)

    # A provider-adjusted price series handed around the system is untouched by
    # recording a corporate action (the position path never receives bars).
    provider_bars = [{"symbol": "AAPL", "close": 100.0}, {"symbol": "AAPL", "close": 101.0}]
    snapshot = [dict(b) for b in provider_bars]
    action = ca.classify_broker_jump("AAPL", 100, 150.0, 200, 75.0, _EFF)
    ca.record(action)
    ca.mark_applied(action, qty_before=100, avg_before=150.0, qty_after=200, avg_after=75.0)
    assert provider_bars == snapshot   # bars unchanged — provider remains price authority


def test_history_stores_broker_values_verbatim_not_recomputed(factory):
    """mark_applied records exactly the broker-resolved before/after the reconciler
    passed — it does not re-derive position values from a ratio (single source)."""
    ca = CorporateActionRuntime(db_factory=factory, broker="kis")
    action = ca.classify_broker_jump("AAPL", 100, 150.0, 200, 75.0, _EFF)
    ca.record(action)
    ca.mark_applied(action, qty_before=100, avg_before=150.0,
                    qty_after=201, avg_after=74.6, value_preserved=True)  # broker's exact numbers
    rows = ca.db_history_for("AAPL")
    assert len(rows) == 1
    assert rows[0].qty_after == 201 and rows[0].avg_after == 74.6  # verbatim, not 200/75
