"""
Unit tests for PositionReconciler.

Scenarios covered:
  1. missing_in_db       — broker has position, DB doesn't → insert
  2. qty_mismatch        — qty differs, no pending order → fix
  3. qty_mismatch_pending — qty differs, pending order exists → preserve
  4. stale_position       — DB has position, broker doesn't, age > 1h, no pending → delete
  5. stale_position_pending — DB has position, broker doesn't, pending order → preserve
  6. dry_run             — gaps detected, no DB mutations, ReconciliationLog written
  7. kiwoom_no_op        — broker raises NotImplementedError → empty gap, no crash
  8. concurrent_dedup    — unique constraint prevents duplicate Position rows
"""
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from backend.brokers.models import OrderStatus
from backend.brokers.models import Position as BPosition
from backend.database.models import (
    AuditLog, Base, Order as DBOrder, Position as DBPosition, ReconciliationLog,
)
from backend.execution.reconciler import PositionReconciler, ReconciliationResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_factory():
    """In-memory SQLite factory, fresh per test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return factory


def _broker_position(symbol: str, qty: int, avg_price: float = 100.0,
                     market: str = "KR") -> BPosition:
    return BPosition(symbol=symbol, qty=qty, avg_price=avg_price, market=market)


def _make_reconciler(broker_mock, db_factory, broker_name: str = "kis") -> PositionReconciler:
    return PositionReconciler(
        broker=broker_mock,
        db_factory=db_factory,
        redis_client=None,
        broker_name=broker_name,
    )


def _insert_db_position(factory, symbol: str, qty: int, avg_price: float = 100.0,
                        broker: str = "kis", market: str = "KR",
                        updated_at: datetime = None):
    sess = factory()
    row = DBPosition(
        symbol=symbol, qty=qty, avg_price=avg_price, market=market, broker=broker,
        updated_at=updated_at or datetime.utcnow(),
    )
    sess.add(row)
    sess.commit()
    sess.close()


def _insert_pending_order(factory, symbol: str, broker: str = "kis"):
    sess = factory()
    sess.add(DBOrder(
        broker_order_id="ORD001", symbol=symbol, side="buy", qty=10,
        price=100.0, status="submitted", market="KR", broker=broker,
    ))
    sess.commit()
    sess.close()


def _count_positions(factory, symbol: str, broker: str = "kis") -> int:
    sess = factory()
    count = sess.query(DBPosition).filter(
        DBPosition.symbol == symbol, DBPosition.broker == broker
    ).count()
    sess.close()
    return count


def _get_position(factory, symbol: str, broker: str = "kis"):
    sess = factory()
    row = sess.query(DBPosition).filter(
        DBPosition.symbol == symbol, DBPosition.broker == broker
    ).first()
    sess.close()
    return row


def _count_audit(factory, event_type: str) -> int:
    sess = factory()
    count = sess.query(AuditLog).filter(AuditLog.event_type == event_type).count()
    sess.close()
    return count


def _count_recon_logs(factory) -> int:
    sess = factory()
    count = sess.query(ReconciliationLog).count()
    sess.close()
    return count


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestMissingInDB:
    """Broker has position, DB doesn't → insert row + AuditLog."""

    def test_inserts_position(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10)]
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("test")

        assert _count_positions(db_factory, "005930") == 1
        pos = _get_position(db_factory, "005930")
        assert pos.qty == 10
        assert pos.broker == "kis"

    def test_records_gap_and_repair(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10)]
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("test")

        assert any(g["kind"] == "missing_in_db" for g in result.gaps)
        assert any(r["kind"] == "insert_position" for r in result.repairs)

    def test_writes_audit_log(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10)]
        broker.get_order_status = MagicMock(return_value=None)

        _make_reconciler(broker, db_factory).reconcile("test")

        assert _count_audit(db_factory, "reconcile_insert") == 1

    def test_persists_reconciliation_log_with_broker(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10)]
        broker.get_order_status = MagicMock(return_value=None)

        _make_reconciler(broker, db_factory, broker_name="kis").reconcile("test")

        sess = db_factory()
        log = sess.query(ReconciliationLog).first()
        sess.close()
        assert log is not None
        assert log.broker == "kis"
        assert log.gaps_found == 1
        assert log.repairs_made == 1


class TestQtyMismatch:
    """Qty differs between broker and DB."""

    def test_fixes_qty_when_no_pending(self, db_factory):
        _insert_db_position(db_factory, "005930", qty=5)
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10)]
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("test")

        pos = _get_position(db_factory, "005930")
        assert pos.qty == 10
        assert any(r["kind"] == "fix_qty" for r in result.repairs)
        assert _count_audit(db_factory, "reconcile_fix_qty") == 1

    def test_preserves_qty_when_pending_order(self, db_factory):
        _insert_db_position(db_factory, "005930", qty=5)
        _insert_pending_order(db_factory, "005930")
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10)]
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("test")

        pos = _get_position(db_factory, "005930")
        assert pos.qty == 5  # unchanged
        assert any(g["kind"] == "qty_mismatch_pending" for g in result.gaps)
        assert _count_audit(db_factory, "reconcile_fix_qty") == 0

    def test_fixes_avg_price_drift(self, db_factory):
        _insert_db_position(db_factory, "005930", qty=10, avg_price=95.0)
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10, avg_price=100.0)]
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("test")

        pos = _get_position(db_factory, "005930")
        assert abs(pos.avg_price - 100.0) < 0.01
        assert any(r["kind"] == "fix_avg_price" for r in result.repairs)


class TestStalePosition:
    """DB has position, broker doesn't."""

    def test_deletes_when_old_and_no_pending(self, db_factory):
        old_time = datetime.utcnow() - timedelta(hours=2)
        _insert_db_position(db_factory, "005930", qty=5, updated_at=old_time)
        broker = MagicMock()
        broker.get_positions.return_value = []  # empty
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("test")

        assert _count_positions(db_factory, "005930") == 0
        assert any(g["kind"] == "stale_db_position" for g in result.gaps)
        assert any(r["kind"] == "delete_position" for r in result.repairs)
        assert _count_audit(db_factory, "reconcile_delete") == 1

    def test_preserves_when_pending_order(self, db_factory):
        old_time = datetime.utcnow() - timedelta(hours=2)
        _insert_db_position(db_factory, "005930", qty=5, updated_at=old_time)
        _insert_pending_order(db_factory, "005930")
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("test")

        assert _count_positions(db_factory, "005930") == 1  # still there
        assert any(g["kind"] == "stale_position_pending" for g in result.gaps)
        assert _count_audit(db_factory, "reconcile_delete") == 0

    def test_preserves_when_too_young(self, db_factory):
        # updated_at = 30 minutes ago (< 1h threshold)
        young_time = datetime.utcnow() - timedelta(minutes=30)
        _insert_db_position(db_factory, "005930", qty=5, updated_at=young_time)
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("test")

        assert _count_positions(db_factory, "005930") == 1
        assert any(g["kind"] == "stale_position_too_young" for g in result.gaps)


class TestDryRun:
    """dry_run=True: gaps detected, no DB mutations."""

    def test_no_db_changes_on_missing(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10)]
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("test", dry_run=True)

        assert _count_positions(db_factory, "005930") == 0  # not inserted
        assert any(g["kind"] == "missing_in_db" for g in result.gaps)
        assert result.repairs == []  # no repairs

    def test_no_audit_log_on_dry_run(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10)]
        broker.get_order_status = MagicMock(return_value=None)

        _make_reconciler(broker, db_factory).reconcile("test", dry_run=True)

        assert _count_audit(db_factory, "reconcile_insert") == 0

    def test_reconciliation_log_written_with_dry_run_flag(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10)]
        broker.get_order_status = MagicMock(return_value=None)

        _make_reconciler(broker, db_factory).reconcile("test", dry_run=True)

        sess = db_factory()
        log = sess.query(ReconciliationLog).first()
        sess.close()
        assert log is not None
        detail = json.loads(log.detail)
        assert detail.get("dry_run") is True


class TestKiwoomNoOp:
    """Broker raises NotImplementedError → empty gap, no crash."""

    def test_returns_cleanly(self, db_factory):
        broker = MagicMock()
        broker.get_positions.side_effect = NotImplementedError("키움증권 미구현")
        # get_order_status also unimplemented
        broker.get_order_status.side_effect = NotImplementedError("키움증권 미구현")

        result = _make_reconciler(broker, db_factory, broker_name="kiwoom").reconcile("test")

        assert result.ok  # no errors; gap recorded
        assert any(g["kind"] == "broker_unimplemented" for g in result.gaps)
        assert result.repairs == []

    def test_writes_reconciliation_log(self, db_factory):
        broker = MagicMock()
        broker.get_positions.side_effect = NotImplementedError("키움증권 미구현")
        broker.get_order_status.side_effect = NotImplementedError("키움증권 미구현")

        _make_reconciler(broker, db_factory, broker_name="kiwoom").reconcile("test")

        assert _count_recon_logs(db_factory) == 1
        sess = db_factory()
        log = sess.query(ReconciliationLog).first()
        sess.close()
        assert log.broker == "kiwoom"


class TestBrokerScoping:
    """DB queries are filtered by broker; KIS and Kiwoom rows don't interfere."""

    def test_kis_reconciler_ignores_kiwoom_positions(self, db_factory):
        # Insert a kiwoom position that should be invisible to KIS reconciler
        _insert_db_position(db_factory, "005930", qty=5, broker="kiwoom")

        broker = MagicMock()
        broker.get_positions.return_value = []  # KIS has no positions
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory, broker_name="kis").reconcile("test")

        # Kiwoom row untouched
        assert _count_positions(db_factory, "005930", broker="kiwoom") == 1
        # No stale_db_position gap for the kiwoom row
        assert not any(g["symbol"] == "005930" for g in result.gaps)

    def test_unique_constraint_prevents_duplicate_position(self, db_factory):
        """Two concurrent inserts for same (symbol, broker) → second is rejected."""
        _insert_db_position(db_factory, "005930", qty=5)

        # Try to insert again directly
        sess = db_factory()
        sess.add(DBPosition(symbol="005930", qty=10, avg_price=100.0, market="KR", broker="kis"))
        with pytest.raises(Exception):  # IntegrityError or similar
            sess.commit()
        sess.close()

        # Original row unchanged
        assert _count_positions(db_factory, "005930") == 1

    def test_reconciliation_log_broker_column(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status = MagicMock(return_value=None)

        _make_reconciler(broker, db_factory, broker_name="kis").reconcile("periodic")

        sess = db_factory()
        log = sess.query(ReconciliationLog).first()
        sess.close()
        assert log.broker == "kis"


# ── Req 1: Server Restart Recovery ────────────────────────────────────────────

class TestStartupRecovery:
    """startup trigger detects position drift accumulated during downtime."""

    def test_startup_detects_qty_drift(self, db_factory):
        _insert_db_position(db_factory, "005930", qty=5)
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10)]
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("startup")

        assert result.trigger == "startup"
        assert any(g["kind"] == "qty_mismatch" for g in result.gaps)
        assert _get_position(db_factory, "005930").qty == 10

    def test_startup_inserts_position_acquired_during_downtime(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("AAPL", qty=5, market="US")]
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("startup")

        assert any(g["kind"] == "missing_in_db" for g in result.gaps)
        assert _count_positions(db_factory, "AAPL") == 1


# ── Req 2: lost_order gap (pending order check) ────────────────────────────────

class TestLostOrderDetection:
    """_reconcile_pending_orders: broker no longer knows about an old order."""

    def test_old_order_marked_canceled_when_broker_returns_none(self, db_factory):
        sess = db_factory()
        old_time = datetime.utcnow() - timedelta(hours=2)
        order = DBOrder(
            broker_order_id="LOST001", symbol="005930", side="buy", qty=10,
            price=100.0, status="submitted", market="KR", broker="kis",
            created_at=old_time,
        )
        sess.add(order)
        sess.commit()
        order_id = order.id
        sess.close()

        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = None

        result = _make_reconciler(broker, db_factory).reconcile("periodic")

        assert any(g["kind"] == "lost_order" for g in result.gaps)
        assert any(r["kind"] == "cancel_lost_order" for r in result.repairs)
        sess = db_factory()
        row = sess.get(DBOrder, order_id)
        sess.close()
        assert row.status == "canceled"

    def test_young_order_not_marked_lost(self, db_factory):
        """Order < 1h old is not treated as lost even when broker returns None."""
        sess = db_factory()
        young_time = datetime.utcnow() - timedelta(minutes=30)
        sess.add(DBOrder(
            broker_order_id="YOUNG001", symbol="005930", side="buy", qty=10,
            price=100.0, status="submitted", market="KR", broker="kis",
            created_at=young_time,
        ))
        sess.commit()
        sess.close()

        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = None

        result = _make_reconciler(broker, db_factory).reconcile("periodic")

        assert not any(g["kind"] == "lost_order" for g in result.gaps)
        assert not any(r["kind"] == "cancel_lost_order" for r in result.repairs)


# ── Req 3: gap_kind strings ────────────────────────────────────────────────────

class TestGapKindStrings:
    """All gaps carry 'kind' and 'symbol' fields; lost_order is a valid kind."""

    def test_all_gaps_have_required_fields(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = [_broker_position("005930", qty=10)]
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("test")

        for gap in result.gaps:
            assert "kind" in gap and gap["kind"]
            assert "symbol" in gap

    def test_lost_order_gap_kind_is_produced(self, db_factory):
        sess = db_factory()
        old_time = datetime.utcnow() - timedelta(hours=2)
        sess.add(DBOrder(
            broker_order_id="LO001", symbol="005930", side="buy", qty=5,
            price=100.0, status="submitted", market="KR", broker="kis",
            created_at=old_time,
        ))
        sess.commit()
        sess.close()

        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = None

        result = _make_reconciler(broker, db_factory).reconcile("test")

        assert any(g["kind"] == "lost_order" for g in result.gaps)


# ── Req 4: Audit log per repair ────────────────────────────────────────────────

class TestAuditLogPerRepair:
    """One AuditLog entry is written for each position-level repair."""

    def test_audit_count_matches_position_repairs(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = [
            _broker_position("005930", qty=10),
            _broker_position("000660", qty=5),
        ]
        broker.get_order_status = MagicMock(return_value=None)

        result = _make_reconciler(broker, db_factory).reconcile("test")

        assert len(result.repairs) == 2
        sess = db_factory()
        audit_count = sess.query(AuditLog).filter(
            AuditLog.event_type == "reconcile_insert"
        ).count()
        sess.close()
        assert audit_count == 2


# ── Req 6: No duplicate position mutation (concurrent) ─────────────────────────

class TestConcurrentReconciliation:
    """Second concurrent reconcile call is skipped immediately (non-blocking lock)."""

    def test_second_call_skips_while_first_runs(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status = MagicMock(return_value=None)

        reconciler = _make_reconciler(broker, db_factory)

        # Simulate a concurrent run already holding the lock
        assert reconciler._reconcile_lock.acquire(blocking=False)
        try:
            r2 = reconciler.reconcile("concurrent")
        finally:
            reconciler._reconcile_lock.release()

        assert any("진행 중" in e for e in r2.errors), \
            "concurrent call must be skipped with 'in progress' error"
        assert not r2.ok


# ── Req 7: No silent state changes ────────────────────────────────────────────

class TestSilentChangeGuard:
    """Exceptions and state mutations are logged; nothing is swallowed silently."""

    def test_cancel_order_failure_logs_warning(self, db_factory, caplog):
        import logging
        sess = db_factory()
        old_time = datetime.utcnow() - timedelta(hours=2)
        sess.add(DBOrder(
            broker_order_id="ERR001", symbol="005930", side="buy", qty=10,
            price=100.0, status="submitted", market="KR", broker="kis",
            created_at=old_time,
        ))
        sess.commit()
        sess.close()

        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = None
        broker.cancel_order.side_effect = RuntimeError("KIS API 오류")

        with caplog.at_level(logging.WARNING, logger="backend.execution.reconciler"):
            result = _make_reconciler(broker, db_factory).reconcile("periodic")

        assert any("cancel_order 실패" in m for m in caplog.messages), \
            "cancel_order failure must produce a warning log"
        assert any(r["kind"] == "cancel_lost_order" for r in result.repairs)

    def test_stale_position_deletion_writes_audit_log(self, db_factory):
        """Deleting a stale position is recorded in AuditLog — not a silent delete."""
        old_time = datetime.utcnow() - timedelta(hours=2)
        _insert_db_position(db_factory, "005930", qty=5, updated_at=old_time)

        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status = MagicMock(return_value=None)

        _make_reconciler(broker, db_factory).reconcile("test")

        assert _count_audit(db_factory, "reconcile_delete") == 1, \
            "stale position deletion must write an AuditLog entry"
