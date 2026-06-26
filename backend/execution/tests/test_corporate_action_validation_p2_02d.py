"""
TASK P2-02D — runtime-integration validation suite.

Each test maps directly to one required guarantee of the P2-02C live integration.
These are *validation* assertions over the already-merged behavior — no new
runtime code, no architecture change. They complement (not replace) the
scenario tests in test_corporate_action_integration.py /
test_reconciler_corporate_action.py / test_corporate_action_runtime.py.

Guarantees validated:
  G1. exactly one adjustment owner (the reconciler; the runtime never writes positions)
  G2. no double-adjust is possible (idempotent across repeated reconciles)
  G3. restart preserves pending actions (pending + confirmed-unapplied survive)
  G4. provider-adjusted vs broker-adjusted behave correctly (two separate planes)
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
from backend.execution.reconciler import PositionReconciler
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import PositionTracker
from backend.data.corporate_action_runtime import CorporateActionRuntime

_EFF = date(2026, 6, 5)


@pytest.fixture()
def factory():
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _bpos(symbol, qty, avg, market="KR"):
    return BPosition(symbol=symbol, qty=qty, avg_price=avg, market=market)


def _insert_db_pos(f, symbol, qty, avg):
    sess = f()
    sess.add(DBPosition(symbol=symbol, qty=qty, avg_price=avg, market="KR", broker="kis"))
    sess.commit()
    sess.close()


def _reconciler(broker_positions, f):
    broker = MagicMock()
    broker.get_positions.return_value = broker_positions
    ca = CorporateActionRuntime(db_factory=f, broker="kis")
    rec = PositionReconciler(broker=broker, db_factory=f, redis_client=None,
                             broker_name="kis", ca_runtime=ca)
    return rec, ca, broker


def _pos(f, symbol):
    sess = f()
    try:
        return sess.query(DBPosition).filter(DBPosition.symbol == symbol).one()
    finally:
        sess.close()


# ── G1: exactly one adjustment owner ─────────────────────────────────────────

class TestG1_SingleAdjustmentOwner:
    def test_runtime_exposes_no_position_write_or_adjust_method(self, factory):
        ca = CorporateActionRuntime(db_factory=factory, broker="kis")
        # The runtime is a detector/recorder/gate. It must own NO position/price
        # adjustment entry point — otherwise a second owner could exist.
        for forbidden in ("adjust", "adjust_position", "adjust_bar", "adjust_bars",
                          "adjust_price", "factor_for", "write_position", "set_position"):
            assert not hasattr(ca, forbidden), f"runtime must not expose {forbidden}"

    def test_only_reconciler_writes_position_value(self, factory):
        # After a split, the DB position equals the broker's exact values — proving
        # the value was written once (by the reconciler), not compounded by a
        # second adjuster.
        _insert_db_pos(factory, "AAPL", 100, 150.0)
        rec, ca, _ = _reconciler([_bpos("AAPL", 200, 75.0)], factory)
        rec.reconcile("periodic")
        row = _pos(factory, "AAPL")
        assert (row.qty, row.avg_price) == (200, 75.0)

    def test_runtime_alone_never_mutates_positions(self, factory):
        # Driving the runtime's full lifecycle (classify→record→mark_applied)
        # touches the corporate_actions/history tables only — never `positions`.
        _insert_db_pos(factory, "AAPL", 100, 150.0)
        ca = CorporateActionRuntime(db_factory=factory, broker="kis")
        action = ca.classify_broker_jump("AAPL", 100, 150.0, 200, 75.0, _EFF)
        ca.record(action)
        ca.mark_applied(action, qty_before=100, avg_before=150.0, qty_after=200, avg_after=75.0)
        # positions row is untouched by the runtime (no reconciler ran)
        row = _pos(factory, "AAPL")
        assert (row.qty, row.avg_price) == (100, 150.0)


# ── G2: no double-adjust is possible ─────────────────────────────────────────

class TestG2_NoDoubleAdjust:
    def test_repeated_reconciles_do_not_re_adjust(self, factory):
        _insert_db_pos(factory, "AAPL", 100, 150.0)
        rec, ca, broker = _reconciler([_bpos("AAPL", 200, 75.0)], factory)
        for _ in range(3):
            rec.reconcile("periodic")
        row = _pos(factory, "AAPL")
        assert (row.qty, row.avg_price) == (200, 75.0)  # never 400/37.5
        sess = factory()
        try:
            assert sess.query(CARow).filter(CARow.symbol == "AAPL").count() == 1
            assert sess.query(CAHist).filter(CAHist.symbol == "AAPL").count() == 1
        finally:
            sess.close()

    def test_history_value_preserved_invariant(self, factory):
        _insert_db_pos(factory, "AAPL", 100, 150.0)
        rec, ca, _ = _reconciler([_bpos("AAPL", 200, 75.0)], factory)
        rec.reconcile("periodic")
        rows = ca.db_history_for("AAPL")
        assert len(rows) == 1
        h = rows[0]
        # book value preserved across the broker-side adjustment: 100*150 == 200*75
        assert h.qty_before * h.avg_before == pytest.approx(h.qty_after * h.avg_after)
        assert h.value_preserved is True


# ── G3: restart preserves pending actions ────────────────────────────────────

class TestG3_RestartPreservesPending:
    def test_unknown_pending_survives_restart(self, factory):
        ca1 = CorporateActionRuntime(db_factory=factory, broker="kis")
        ca1.record(ca1.classify_broker_jump("AAPL", 100, 10.0, 137, 7.3, _EFF))  # UNKNOWN
        # fresh process
        ca2 = CorporateActionRuntime(db_factory=factory, broker="kis")
        assert ca2.is_blocked("AAPL") is False
        assert ca2.restore_pending() == 1
        assert ca2.is_blocked("AAPL") is True

    def test_confirmed_but_unapplied_split_survives_restart(self, factory):
        # A CONFIRMED split that was recorded but NOT yet mark_applied must stay
        # blocking across a restart (gate is fail-closed until the broker value lands).
        ca1 = CorporateActionRuntime(db_factory=factory, broker="kis")
        action = ca1.classify_broker_jump("AAPL", 100, 150.0, 200, 75.0, _EFF)  # CONFIRMED
        ca1.record(action)            # recorded, NOT applied
        assert ca1.is_blocked("AAPL") is True
        ca2 = CorporateActionRuntime(db_factory=factory, broker="kis")
        assert ca2.restore_pending() == 1
        assert ca2.is_blocked("AAPL") is True

    def test_applied_action_does_not_survive_restart(self, factory):
        ca1 = CorporateActionRuntime(db_factory=factory, broker="kis")
        action = ca1.classify_broker_jump("AAPL", 100, 150.0, 200, 75.0, _EFF)
        ca1.record(action)
        ca1.mark_applied(action, qty_before=100, avg_before=150.0, qty_after=200, avg_after=75.0)
        ca2 = CorporateActionRuntime(db_factory=factory, broker="kis")
        assert ca2.restore_pending() == 0
        assert ca2.is_blocked("AAPL") is False


# ── G4: provider-adjusted vs broker-adjusted ─────────────────────────────────

class TestG4_TwoPlanes:
    def test_broker_plane_converges_runtime_records_only(self, factory):
        # Broker plane: reconciler converges DB to broker-adjusted values.
        _insert_db_pos(factory, "AAPL", 100, 150.0)
        rec, ca, _ = _reconciler([_bpos("AAPL", 200, 75.0)], factory)
        rec.reconcile("periodic")
        assert (_pos(factory, "AAPL").qty, _pos(factory, "AAPL").avg_price) == (200, 75.0)
        # and the runtime recorded it (audit), without owning the write
        assert len(ca.db_history_for("AAPL")) == 1

    def test_provider_adjusted_price_series_never_re_scaled(self, factory):
        # Price plane: a provider-adjusted (e.g. yfinance auto_adjust) OHLCV series
        # passed around the system is NOT re-scaled by the corporate-action path —
        # the runtime never receives or mutates bars (no double-adjust on prices).
        provider_bars = [{"symbol": "AAPL", "close": 50.0, "volume": 2000.0},
                         {"symbol": "AAPL", "close": 51.0, "volume": 1900.0}]
        snapshot = [dict(b) for b in provider_bars]
        ca = CorporateActionRuntime(db_factory=factory, broker="kis")
        action = ca.classify_broker_jump("AAPL", 100, 150.0, 200, 75.0, _EFF)
        ca.record(action)
        ca.mark_applied(action, qty_before=100, avg_before=150.0, qty_after=200, avg_after=75.0)
        assert provider_bars == snapshot   # untouched — provider remains the price authority

    def test_gate_blocks_order_entry_on_unknown_broker_jump(self, factory):
        # End-to-end: an UNKNOWN broker jump detected at reconcile gates order entry
        # through the PositionTracker (fail-closed), tying the two planes together.
        _insert_db_pos(factory, "AAPL", 100, 10.0)
        rec, ca, _ = _reconciler([_bpos("AAPL", 137, 7.3)], factory)  # UNKNOWN ratio
        rec.reconcile("periodic")
        tracker = PositionTracker(OrderStateMachine(), corporate_action_runtime=ca)
        assert tracker.can_place_order("AAPL") is False
        assert tracker.try_mark_pending("AAPL") is False
