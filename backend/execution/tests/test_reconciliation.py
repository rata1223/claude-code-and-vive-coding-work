"""
Tests for ReconciliationEngine and FillReconciler.

Scenarios:
  1. partial fill        — broker reports PARTIAL_FILLED → Fill row created
  2. cancel              — CANCELED with no fill; CANCELED with partial fill
  3. reject              — REJECTED → no Fill row
  4. restart             — reconcile("startup") twice → no duplicate Fill rows
  5. broker reconnect    — reconcile("reconnect") is idempotent
  6. duplicate event     — sync_fills called N times → N-idempotent Fill rows
  7. manual broker trade — broker has position DB doesn't → position inserted
  8. portfolio snapshot  — get_portfolio_snapshot returns DB positions
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.brokers.models import Order as BrokerOrder, OrderStatus
from backend.brokers.models import Position as BrokerPosition
from backend.database.models import (
    AuditLog, Base, Fill as DBFill, Order as DBOrder,
    Position as DBPosition, ReconciliationLog,
)
from backend.execution.reconciliation import FillReconciler, PortfolioSnapshot, ReconciliationEngine


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_factory():
    """In-memory SQLite factory, fresh per test."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _insert_order(factory, symbol: str = "005930", status: str = "submitted",
                  broker_order_id: str = "ORD001", broker: str = "kis",
                  side: str = "buy", qty: int = 10, price: float = 70000.0,
                  filled_qty: int = 0, avg_fill_price: float = None) -> int:
    sess = factory()
    row = DBOrder(
        broker_order_id=broker_order_id,
        symbol=symbol, side=side, qty=qty, price=price,
        filled_qty=filled_qty,
        avg_fill_price=avg_fill_price,
        status=status, market="KR", broker=broker,
        created_at=datetime.utcnow(),
    )
    sess.add(row)
    sess.commit()
    pk = row.id
    sess.close()
    return pk


def _get_order(factory, order_id: int) -> dict:
    sess = factory()
    row = sess.get(DBOrder, order_id)
    data = {
        "status": row.status,
        "filled_qty": row.filled_qty,
        "avg_fill_price": row.avg_fill_price,
    } if row else {}
    sess.close()
    return data


def _get_fills(factory, order_id: int) -> list:
    sess = factory()
    rows = sess.query(DBFill).filter(DBFill.order_id == order_id).all()
    result = [{"qty": r.qty, "price": r.price} for r in rows]
    sess.close()
    return result


def _count_fills(factory, order_id: int) -> int:
    return len(_get_fills(factory, order_id))


def _total_filled_qty(factory, order_id: int) -> int:
    return sum(f["qty"] for f in _get_fills(factory, order_id))


def _broker_order(status: OrderStatus, filled_qty: int = 0,
                  avg_fill_price: float = 70000.0,
                  broker_order_id: str = "ORD001",
                  symbol: str = "005930") -> BrokerOrder:
    return BrokerOrder(
        id=broker_order_id, symbol=symbol, side="buy",
        qty=10, price=70000.0,
        status=status,
        filled_qty=filled_qty,
        avg_fill_price=avg_fill_price,
    )


def _make_engine(broker_mock, db_factory, broker_name: str = "kis") -> ReconciliationEngine:
    return ReconciliationEngine(
        broker=broker_mock,
        db_factory=db_factory,
        redis_client=None,
        broker_name=broker_name,
    )


# ── TestPartialFill ───────────────────────────────────────────────────────────

class TestPartialFill:
    """broker reports PARTIAL_FILLED → Fill row created; idempotent on replay."""

    def _broker_with_partial(self, filled_qty: int = 5):
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = _broker_order(
            OrderStatus.PARTIAL_FILLED, filled_qty=filled_qty
        )
        return broker

    def test_fill_row_created(self, db_factory):
        order_id = _insert_order(db_factory)
        engine = _make_engine(self._broker_with_partial(), db_factory)

        engine.reconcile("test")

        assert _total_filled_qty(db_factory, order_id) == 5

    def test_order_status_updated(self, db_factory):
        order_id = _insert_order(db_factory)
        engine = _make_engine(self._broker_with_partial(), db_factory)

        engine.reconcile("test")

        od = _get_order(db_factory, order_id)
        assert od["status"] == "partial_filled"
        assert od["filled_qty"] == 5

    def test_idempotent_on_replay(self, db_factory):
        """Running reconcile twice with the same partial state inserts only one Fill row."""
        order_id = _insert_order(db_factory)
        engine = _make_engine(self._broker_with_partial(5), db_factory)

        engine.reconcile("test")
        engine.reconcile("test")

        assert _count_fills(db_factory, order_id) == 1
        assert _total_filled_qty(db_factory, order_id) == 5

    def test_partial_then_full_no_duplicate(self, db_factory):
        """partial(5) then filled(10) produces total filled_qty=10, no double counting."""
        order_id = _insert_order(db_factory)

        broker_partial = MagicMock()
        broker_partial.get_positions.return_value = []
        broker_partial.get_order_status.return_value = _broker_order(
            OrderStatus.PARTIAL_FILLED, filled_qty=5
        )
        _make_engine(broker_partial, db_factory).reconcile("test")

        broker_full = MagicMock()
        broker_full.get_positions.return_value = []
        broker_full.get_order_status.return_value = _broker_order(
            OrderStatus.FILLED, filled_qty=10
        )
        _make_engine(broker_full, db_factory).reconcile("test")

        assert _total_filled_qty(db_factory, order_id) == 10


# ── TestCancelReject ──────────────────────────────────────────────────────────

class TestCancelReject:
    """Cancel and reject order status sync."""

    def test_cancel_no_fill(self, db_factory):
        """Canceled with filled_qty=0 → order marked canceled, zero Fill rows."""
        order_id = _insert_order(db_factory)
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = _broker_order(
            OrderStatus.CANCELED, filled_qty=0
        )

        _make_engine(broker, db_factory).reconcile("test")

        od = _get_order(db_factory, order_id)
        assert od["status"] == "canceled"
        assert _count_fills(db_factory, order_id) == 0

    def test_cancel_with_partial_fill(self, db_factory):
        """Canceled after partial fill → Fill row recorded for executed qty."""
        order_id = _insert_order(db_factory)
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = _broker_order(
            OrderStatus.CANCELED, filled_qty=3, avg_fill_price=70000.0
        )

        _make_engine(broker, db_factory).reconcile("test")

        od = _get_order(db_factory, order_id)
        assert od["status"] == "canceled"
        assert _total_filled_qty(db_factory, order_id) == 3

    def test_reject_no_fill(self, db_factory):
        """Rejected with filled_qty=0 → order marked rejected, zero Fill rows."""
        order_id = _insert_order(db_factory)
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = _broker_order(
            OrderStatus.REJECTED, filled_qty=0
        )

        _make_engine(broker, db_factory).reconcile("test")

        od = _get_order(db_factory, order_id)
        assert od["status"] == "rejected"
        assert _count_fills(db_factory, order_id) == 0


# ── TestRestartIdempotency ────────────────────────────────────────────────────

class TestRestartIdempotency:
    """reconcile("startup") called twice — no duplicate Fill rows."""

    def test_no_duplicate_fills_on_startup_replay(self, db_factory):
        order_id = _insert_order(db_factory)
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = _broker_order(
            OrderStatus.PARTIAL_FILLED, filled_qty=5
        )
        engine = _make_engine(broker, db_factory)

        engine.reconcile("startup")
        engine.reconcile("startup")

        assert _count_fills(db_factory, order_id) == 1
        assert _total_filled_qty(db_factory, order_id) == 5

    def test_reconciliation_log_written_each_startup(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = None  # no open orders
        engine = _make_engine(broker, db_factory)

        engine.reconcile("startup")
        engine.reconcile("startup")

        sess = db_factory()
        count = sess.query(ReconciliationLog).count()
        sess.close()
        assert count == 2


# ── TestBrokerReconnect ───────────────────────────────────────────────────────

class TestBrokerReconnect:
    """reconcile("reconnect") after connection loss is idempotent."""

    def test_reconnect_does_not_duplicate_fills(self, db_factory):
        order_id = _insert_order(db_factory)
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = _broker_order(
            OrderStatus.PARTIAL_FILLED, filled_qty=5
        )
        engine = _make_engine(broker, db_factory)

        engine.reconcile("reconnect")
        engine.reconcile("reconnect")

        assert _count_fills(db_factory, order_id) == 1


# ── TestDuplicateEvent ────────────────────────────────────────────────────────

class TestDuplicateEvent:
    """FillReconciler called N times with same state → N-idempotent result."""

    def test_sync_fills_called_three_times(self, db_factory):
        """Direct FillReconciler.sync_fills_all_open_orders is idempotent."""
        order_id = _insert_order(
            db_factory, status="partial_filled", filled_qty=5, avg_fill_price=70000.0
        )
        from backend.execution.reconciler import ReconciliationResult
        filler = FillReconciler(db_factory, broker_name="kis")

        for _ in range(3):
            result = ReconciliationResult("dup_test")
            filler.sync_fills_all_open_orders(result)

        assert _count_fills(db_factory, order_id) == 1
        assert _total_filled_qty(db_factory, order_id) == 5

    def test_full_reconcile_called_five_times(self, db_factory):
        order_id = _insert_order(db_factory)
        broker = MagicMock()
        broker.get_positions.return_value = []
        broker.get_order_status.return_value = _broker_order(
            OrderStatus.PARTIAL_FILLED, filled_qty=7
        )
        engine = _make_engine(broker, db_factory)

        for _ in range(5):
            engine.reconcile("periodic")

        assert _total_filled_qty(db_factory, order_id) == 7


# ── TestManualBrokerTrade ─────────────────────────────────────────────────────

class TestManualBrokerTrade:
    """Broker has a position DB doesn't know about → position inserted."""

    def test_position_inserted(self, db_factory):
        """External buy on broker app → missing_in_db gap → position row created."""
        broker = MagicMock()
        broker.get_positions.return_value = [
            BrokerPosition(symbol="AAPL", qty=5, avg_price=150.0, market="US")
        ]
        broker.get_order_status.return_value = None  # no open orders

        engine = _make_engine(broker, db_factory, broker_name="kis")
        result = engine.reconcile("test")

        sess = db_factory()
        pos = sess.query(DBPosition).filter(DBPosition.symbol == "AAPL").first()
        sess.close()
        assert pos is not None
        assert pos.qty == 5
        assert pos.broker == "kis"

    def test_missing_in_db_gap_recorded(self, db_factory):
        broker = MagicMock()
        broker.get_positions.return_value = [
            BrokerPosition(symbol="AAPL", qty=5, avg_price=150.0, market="US")
        ]
        broker.get_order_status.return_value = None

        result = _make_engine(broker, db_factory).reconcile("test")

        assert any(g["kind"] == "missing_in_db" and g["symbol"] == "AAPL" for g in result.gaps)
        assert any(r["kind"] == "insert_position" for r in result.repairs)


# ── TestPortfolioSnapshot ─────────────────────────────────────────────────────

class TestPortfolioSnapshot:
    """get_portfolio_snapshot returns current DB positions for the broker."""

    def _insert_position(self, factory, symbol: str, qty: int, broker: str = "kis"):
        sess = factory()
        sess.add(DBPosition(symbol=symbol, qty=qty, avg_price=100.0, market="KR", broker=broker))
        sess.commit()
        sess.close()

    def test_snapshot_contains_positions(self, db_factory):
        self._insert_position(db_factory, "005930", qty=10)
        self._insert_position(db_factory, "AAPL", qty=5)
        broker = MagicMock()
        broker.get_positions.return_value = []

        engine = _make_engine(broker, db_factory)
        snap = engine.get_portfolio_snapshot()

        assert isinstance(snap, PortfolioSnapshot)
        assert snap.broker == "kis"
        symbols = snap.symbols()
        assert "005930" in symbols
        assert "AAPL" in symbols

    def test_total_qty_for(self, db_factory):
        self._insert_position(db_factory, "005930", qty=10)
        broker = MagicMock()
        engine = _make_engine(broker, db_factory)
        snap = engine.get_portfolio_snapshot()

        assert snap.total_qty_for("005930") == 10
        assert snap.total_qty_for("UNKNOWN") == 0

    def test_snapshot_scoped_to_broker(self, db_factory):
        """Kiwoom positions are invisible to KIS snapshot."""
        self._insert_position(db_factory, "005930", qty=10, broker="kis")
        self._insert_position(db_factory, "069500", qty=5, broker="kiwoom")
        broker = MagicMock()

        kis_engine = _make_engine(broker, db_factory, broker_name="kis")
        snap = kis_engine.get_portfolio_snapshot()

        assert snap.total_qty_for("005930") == 10
        assert snap.total_qty_for("069500") == 0
        assert len(snap.positions) == 1
