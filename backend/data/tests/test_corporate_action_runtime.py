"""
Layer-1 tests for the corporate-action runtime glue (TASK P2-02C):
classification, DB persistence, duplicate prevention, restart restore, history.
The pure CorporateActionService is covered by test_corporate_actions*.py.
"""
from datetime import date

import pytest
from sqlalchemy.orm import sessionmaker

from backend.database.testing import make_test_engine
from backend.database.models import (
    Base, CorporateAction as CARow, CorporateActionHistory as CAHist,
)
from backend.data.corporate_actions import ActionStatus, ActionType
from backend.data.corporate_action_runtime import CorporateActionRuntime

_EFF = date(2026, 6, 5)


def _factory():
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# ── Classification ───────────────────────────────────────────────────────────

class TestClassifyBrokerJump:
    def test_two_for_one_split_confirmed(self):
        rt = CorporateActionRuntime()
        action = rt.classify_broker_jump("AAPL", db_qty=100, db_avg=150.0,
                                          broker_qty=200, broker_avg=75.0, effective_date=_EFF)
        assert action.action_type == ActionType.SPLIT
        assert action.status == ActionStatus.CONFIRMED
        assert action.ratio == 2.0

    def test_reverse_split_confirmed(self):
        rt = CorporateActionRuntime()
        action = rt.classify_broker_jump("PENNY", db_qty=1000, db_avg=0.5,
                                          broker_qty=100, broker_avg=5.0, effective_date=_EFF)
        assert action.action_type == ActionType.REVERSE_SPLIT
        assert action.status == ActionStatus.CONFIRMED
        assert action.ratio == pytest.approx(0.1)

    def test_known_ratio_but_value_not_preserved_is_unknown(self):
        # qty doubled but value 100→160 → not a value-preserving split → fail closed
        rt = CorporateActionRuntime()
        action = rt.classify_broker_jump("AAPL", db_qty=100, db_avg=1.0,
                                          broker_qty=200, broker_avg=0.8, effective_date=_EFF)
        assert action.action_type == ActionType.UNKNOWN
        assert action.status == ActionStatus.UNKNOWN

    def test_unknown_ratio_is_unknown(self):
        rt = CorporateActionRuntime()
        action = rt.classify_broker_jump("AAPL", db_qty=100, db_avg=10.0,
                                          broker_qty=137, broker_avg=7.3, effective_date=_EFF)
        assert action.action_type == ActionType.UNKNOWN

    def test_zero_db_qty_is_unknown(self):
        rt = CorporateActionRuntime()
        action = rt.classify_broker_jump("AAPL", db_qty=0, db_avg=0.0,
                                          broker_qty=200, broker_avg=75.0, effective_date=_EFF)
        assert action.action_type == ActionType.UNKNOWN


# ── Persistence + gate ───────────────────────────────────────────────────────

class TestRecordAndGate:
    def test_record_persists_and_blocks(self):
        factory = _factory()
        rt = CorporateActionRuntime(db_factory=factory, broker="kis")
        action = rt.classify_broker_jump("AAPL", 100, 150.0, 200, 75.0, _EFF)
        rt.record(action)

        assert rt.is_blocked("AAPL") is True
        sess = factory()
        try:
            rows = sess.query(CARow).filter(CARow.symbol == "AAPL").all()
        finally:
            sess.close()
        assert len(rows) == 1
        assert rows[0].action_type == "split"
        assert rows[0].status == "confirmed"

    def test_unknown_action_fails_closed_and_persists(self):
        factory = _factory()
        rt = CorporateActionRuntime(db_factory=factory)
        action = rt.classify_broker_jump("AAPL", 100, 10.0, 137, 7.3, _EFF)  # UNKNOWN
        rt.record(action)
        assert rt.is_blocked("AAPL") is True
        sess = factory()
        try:
            row = sess.query(CARow).filter(CARow.symbol == "AAPL").one()
        finally:
            sess.close()
        assert row.status == "unknown"

    def test_mark_applied_clears_gate_and_writes_history(self):
        factory = _factory()
        rt = CorporateActionRuntime(db_factory=factory)
        action = rt.classify_broker_jump("AAPL", 100, 150.0, 200, 75.0, _EFF)
        rt.record(action)
        assert rt.is_blocked("AAPL") is True

        rt.mark_applied(action, qty_before=100, avg_before=150.0,
                        qty_after=200, avg_after=75.0, cash_delta=0.0, value_preserved=True)
        assert rt.is_blocked("AAPL") is False

        sess = factory()
        try:
            ca = sess.query(CARow).filter(CARow.symbol == "AAPL").one()
            hist = sess.query(CAHist).filter(CAHist.symbol == "AAPL").all()
        finally:
            sess.close()
        assert ca.status == "applied"
        assert ca.applied_at is not None
        assert len(hist) == 1
        assert hist[0].qty_before == 100 and hist[0].qty_after == 200
        assert hist[0].value_preserved is True


# ── Duplicate prevention ─────────────────────────────────────────────────────

class TestDuplicatePrevention:
    def test_recording_same_action_twice_keeps_one_row(self):
        factory = _factory()
        rt = CorporateActionRuntime(db_factory=factory)
        action = rt.classify_broker_jump("AAPL", 100, 150.0, 200, 75.0, _EFF)
        rt.record(action)
        rt.record(action)  # second reconcile sees the same jump
        sess = factory()
        try:
            rows = sess.query(CARow).filter(CARow.symbol == "AAPL").all()
        finally:
            sess.close()
        assert len(rows) == 1  # unique (broker, symbol, effective_date, action_type)


# ── Restart restore ──────────────────────────────────────────────────────────

class TestRestartRestore:
    def test_restore_pending_rebuilds_gate(self):
        factory = _factory()
        rt1 = CorporateActionRuntime(db_factory=factory)
        action = rt1.classify_broker_jump("AAPL", 100, 10.0, 137, 7.3, _EFF)  # UNKNOWN
        rt1.record(action)

        rt2 = CorporateActionRuntime(db_factory=factory)  # fresh process
        assert rt2.is_blocked("AAPL") is False  # nothing in memory yet
        n = rt2.restore_pending()
        assert n == 1
        assert rt2.is_blocked("AAPL") is True  # gate restored → fail closed across restart

    def test_applied_action_is_not_restored(self):
        factory = _factory()
        rt1 = CorporateActionRuntime(db_factory=factory)
        action = rt1.classify_broker_jump("AAPL", 100, 150.0, 200, 75.0, _EFF)
        rt1.record(action)
        rt1.mark_applied(action, qty_before=100, avg_before=150.0,
                         qty_after=200, avg_after=75.0)
        rt2 = CorporateActionRuntime(db_factory=factory)
        assert rt2.restore_pending() == 0
        assert rt2.is_blocked("AAPL") is False


def test_no_db_factory_still_classifies_and_gates():
    rt = CorporateActionRuntime(db_factory=None)
    action = rt.classify_broker_jump("AAPL", 100, 10.0, 137, 7.3, _EFF)
    rt.record(action)
    assert rt.is_blocked("AAPL") is True
    assert rt.restore_pending() == 0
