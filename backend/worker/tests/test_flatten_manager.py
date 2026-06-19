"""
TASK P1-03B: EmergencyFlattenManager Runtime Validation

Scenarios:
  1. Single position flatten (happy path)
  2. Multiple position flatten
  3. Partial failure — some orders fail, rest proceed
  4. Repeated / concurrent flatten requests (idempotency)
  5. Broker timeout — get_price / place_order fails
  6. Worker restart during flatten
  7. Already-closed position (qty=0)
  8. Empty portfolio

Verify:
  - Re-entry lock prevents duplicate liquidation orders
  - No fail-open: price fallback uses avg_price, not skipping the order
  - Audit log writes per event; DB failure does not abort flatten
  - Audit logs survive simulated restart (persistent DB)
"""
import json
import threading
import time
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.brokers.base import BrokerAdapter
from backend.brokers.models import Order as BOrder, OrderStatus, Position as BPosition
from backend.database.models import AuditLog, Base
from backend.worker.emergency import EmergencyFlattenManager


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _pos(symbol, qty, avg_price=100.0, market="US"):
    return BPosition(symbol=symbol, qty=qty, avg_price=avg_price, market=market)


def _order(symbol, order_id="ORD001", status=OrderStatus.SUBMITTED):
    return BOrder(id=order_id, symbol=symbol, side="sell", qty=10,
                  price=100.0, status=status)


def _query_audit(factory, event_type=None):
    db = factory()
    try:
        q = db.query(AuditLog)
        if event_type:
            q = q.filter_by(event_type=event_type)
        return q.all()
    finally:
        db.close()


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def broker():
    b = MagicMock(spec=BrokerAdapter)
    b.get_price.return_value = 100.0
    return b


# ─── 1. Single position ───────────────────────────────────────────────────────

class TestSinglePositionFlatten:
    def test_places_sell_order_for_one_position(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]
        broker.place_order.return_value = _order("AAPL")

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all("test")

        assert result["attempted"] == 1
        assert result["success"] == 1
        assert result["failed"] == []
        broker.place_order.assert_called_once_with("AAPL", "sell", 10, 100.0)

    def test_uses_live_price(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=5, avg_price=90.0)]
        broker.get_price.return_value = 150.0
        broker.place_order.return_value = _order("AAPL")

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        mgr.flatten_all()

        broker.place_order.assert_called_once_with("AAPL", "sell", 5, 150.0)

    def test_dry_run_does_not_place_order(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=True)
        result = mgr.flatten_all()

        broker.place_order.assert_not_called()
        assert result["success"] == 1


# ─── 2. Multiple positions ────────────────────────────────────────────────────

class TestMultiplePositionFlatten:
    def test_places_orders_for_all_positions(self, broker, db_factory):
        broker.get_positions.return_value = [
            _pos("AAPL", qty=10),
            _pos("MSFT", qty=5),
            _pos("NVDA", qty=3),
        ]
        broker.place_order.side_effect = [
            _order("AAPL", "O1"),
            _order("MSFT", "O2"),
            _order("NVDA", "O3"),
        ]

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all()

        assert result["attempted"] == 3
        assert result["success"] == 3
        assert result["failed"] == []
        assert broker.place_order.call_count == 3

    def test_correct_symbols_and_qtys_sent(self, broker, db_factory):
        broker.get_positions.return_value = [
            _pos("SPY", qty=20),
            _pos("QQQ", qty=15),
        ]
        broker.place_order.side_effect = [_order("SPY"), _order("QQQ")]

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        mgr.flatten_all()

        calls = broker.place_order.call_args_list
        assert calls[0].args == ("SPY", "sell", 20, 100.0)
        assert calls[1].args == ("QQQ", "sell", 15, 100.0)


# ─── 3. Partial failure ───────────────────────────────────────────────────────

class TestPartialFailure:
    def test_one_failure_still_processes_remaining(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10), _pos("MSFT", qty=5)]
        broker.place_order.side_effect = [RuntimeError("rejected"), _order("MSFT", "O2")]

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all()

        assert result["attempted"] == 2
        assert result["success"] == 1
        assert len(result["failed"]) == 1
        assert "AAPL" in result["failed"][0]

    def test_failed_list_contains_symbol_and_error_text(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]
        broker.place_order.side_effect = Exception("insufficient margin")

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all()

        assert result["success"] == 0
        assert "AAPL" in result["failed"][0]
        assert "insufficient margin" in result["failed"][0]

    def test_price_failure_falls_back_to_avg_price_and_still_places_order(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10, avg_price=88.0)]
        broker.get_price.side_effect = Exception("market data unavailable")
        broker.place_order.return_value = _order("AAPL")

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all()

        assert result["success"] == 1
        broker.place_order.assert_called_once_with("AAPL", "sell", 10, 88.0)

    def test_circuit_breaker_price_falls_back_to_avg_price(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10, avg_price=77.0)]
        broker.get_price.side_effect = RuntimeError("circuit breaker open — get_price")
        broker.place_order.return_value = _order("AAPL")

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all()

        assert result["success"] == 1
        broker.place_order.assert_called_once_with("AAPL", "sell", 10, 77.0)


# ─── 4. Idempotency / re-entry ────────────────────────────────────────────────

class TestIdempotency:
    def test_concurrent_call_blocked_while_first_is_in_progress(self, broker, db_factory):
        """Second flatten_all() while first is executing must not place orders."""
        in_place_order = threading.Event()
        allow_finish = threading.Event()
        results = {}

        def _slow_place(symbol, side, qty, price):
            in_place_order.set()
            allow_finish.wait(timeout=5)
            return _order(symbol)

        broker.get_positions.return_value = [_pos("AAPL", qty=10)]
        broker.place_order.side_effect = _slow_place

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)

        def _first():
            results["first"] = mgr.flatten_all("first call")

        t1 = threading.Thread(target=_first, daemon=True)
        t1.start()

        # Wait until first is inside place_order (lock is released, _flattening=True)
        in_place_order.wait(timeout=5)

        # Second call while first is in-progress
        results["second"] = mgr.flatten_all("second call")

        allow_finish.set()
        t1.join(timeout=5)

        assert broker.place_order.call_count == 1, (
            f"Expected 1 order call — got {broker.place_order.call_count} (duplicate orders)")
        assert results["second"]["status"] == "already_in_progress"
        assert results["second"]["failed"] == []

    def test_sequential_calls_both_allowed(self, broker, db_factory):
        """After the first flatten_all() completes, the lock resets and a second call is allowed."""
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]
        broker.place_order.return_value = _order("AAPL")

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        mgr.flatten_all("first")
        mgr.flatten_all("second")  # must not be blocked

        assert broker.place_order.call_count == 2

    def test_lock_released_after_exception(self, broker, db_factory):
        """If _do_flatten raises unexpectedly, the re-entry lock must still be released."""
        broker.get_positions.side_effect = [RuntimeError("boom"), []]

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        mgr.flatten_all("failing call")  # first call fails internally

        # Lock must be released — second call should run
        result = mgr.flatten_all("after failure")
        assert result["attempted"] == 0  # empty portfolio
        assert not mgr._flattening


# ─── 5. Broker timeout ────────────────────────────────────────────────────────

class TestBrokerTimeout:
    def test_get_positions_timeout_returns_zero_results(self, broker, db_factory):
        broker.get_positions.side_effect = TimeoutError("broker timed out")

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all()

        assert result["attempted"] == 0
        assert result["success"] == 0
        assert len(result["failed"]) == 1

    def test_place_order_timeout_recorded_not_propagated(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]
        broker.place_order.side_effect = TimeoutError("order API timed out")

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all()

        assert result["attempted"] == 1
        assert result["success"] == 0
        assert len(result["failed"]) == 1

    def test_remaining_positions_processed_after_timeout(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10), _pos("MSFT", qty=5)]
        broker.place_order.side_effect = [TimeoutError("timeout"), _order("MSFT", "O2")]

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all()

        assert result["success"] == 1
        assert len(result["failed"]) == 1
        assert "AAPL" in result["failed"][0]
        assert "timeout" in result["failed"][0]


# ─── 6. Worker restart during flatten ────────────────────────────────────────

class TestWorkerRestart:
    def test_audit_logs_survive_restart(self, broker, db_factory):
        """Logs written before restart are readable by a new manager instance."""
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]
        broker.place_order.return_value = _order("AAPL", "ORD_PRE_RESTART")

        mgr1 = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        mgr1.flatten_all("pre_restart")

        # Simulate restart — new in-memory manager, same DB
        mgr2 = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        assert not mgr2._flattening  # lock state resets on restart

        logs = _query_audit(db_factory, "emergency_flatten_start")
        assert len(logs) >= 1

    def test_restart_allows_re_flatten(self, broker, db_factory):
        """New instance has a fresh lock — can flatten again."""
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]
        broker.place_order.return_value = _order("AAPL")

        mgr1 = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        mgr1.flatten_all()

        mgr2 = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr2.flatten_all()

        assert result["attempted"] == 1

    def test_order_id_preserved_in_audit_log(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]
        broker.place_order.return_value = _order("AAPL", order_id="PERSIST_XYZ")

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        mgr.flatten_all()

        logs = _query_audit(db_factory, "emergency_flatten_order")
        assert len(logs) == 1
        detail = json.loads(logs[0].detail)
        assert detail["order_id"] == "PERSIST_XYZ"


# ─── 7. Already-closed position (qty=0) ──────────────────────────────────────

class TestZeroQtyPosition:
    def test_zero_qty_position_not_submitted(self, broker, db_factory):
        """A position with qty=0 must not generate a sell order."""
        broker.get_positions.return_value = [_pos("AAPL", qty=0)]

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all()

        broker.place_order.assert_not_called()
        assert result["attempted"] == 0

    def test_negative_qty_also_skipped(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=-5)]

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all()

        broker.place_order.assert_not_called()
        assert result["attempted"] == 0

    def test_mixed_open_and_zero_qty(self, broker, db_factory):
        broker.get_positions.return_value = [
            _pos("AAPL", qty=0),   # closed
            _pos("MSFT", qty=5),   # open
        ]
        broker.place_order.return_value = _order("MSFT", "O1")

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all()

        broker.place_order.assert_called_once_with("MSFT", "sell", 5, 100.0)
        assert result["attempted"] == 1
        assert result["success"] == 1


# ─── 8. Empty portfolio ───────────────────────────────────────────────────────

class TestEmptyPortfolio:
    def test_empty_portfolio_returns_zero_counts(self, broker, db_factory):
        broker.get_positions.return_value = []

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        result = mgr.flatten_all("test")

        assert result["attempted"] == 0
        assert result["success"] == 0
        assert result["failed"] == []
        broker.place_order.assert_not_called()

    def test_empty_portfolio_still_writes_start_audit(self, broker, db_factory):
        broker.get_positions.return_value = []

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        mgr.flatten_all("empty test")

        logs = _query_audit(db_factory, "emergency_flatten_start")
        assert len(logs) == 1


# ─── 9. Audit log ─────────────────────────────────────────────────────────────

class TestAuditLog:
    def test_flatten_start_event_written(self, broker, db_factory):
        broker.get_positions.return_value = []

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        mgr.flatten_all("kill_switch_triggered")

        logs = _query_audit(db_factory, "emergency_flatten_start")
        assert len(logs) == 1
        assert "kill_switch_triggered" in logs[0].detail
        assert logs[0].actor == "emergency"

    def test_each_order_audit_log_written(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10), _pos("MSFT", qty=5)]
        broker.place_order.side_effect = [_order("AAPL", "O1"), _order("MSFT", "O2")]

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        mgr.flatten_all()

        logs = _query_audit(db_factory, "emergency_flatten_order")
        assert len(logs) == 2
        symbols = {l.symbol for l in logs}
        assert symbols == {"AAPL", "MSFT"}

    def test_no_factory_does_not_raise(self, broker):
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]
        broker.place_order.return_value = _order("AAPL")

        mgr = EmergencyFlattenManager(broker, db_factory=None, dry_run=False)
        result = mgr.flatten_all()  # must not raise

        assert result["success"] == 1

    def test_audit_db_failure_does_not_abort_flatten(self, broker):
        """Even if every DB write fails, position liquidation must complete."""
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]
        broker.place_order.return_value = _order("AAPL")

        broken_factory = MagicMock(side_effect=RuntimeError("DB down"))
        mgr = EmergencyFlattenManager(broker, broken_factory, dry_run=False)
        result = mgr.flatten_all()

        assert result["success"] == 1

    def test_failed_order_not_written_to_order_audit(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]
        broker.place_order.side_effect = RuntimeError("rejected")

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=False)
        mgr.flatten_all()

        # emergency_flatten_order event must NOT be written for failed orders
        order_logs = _query_audit(db_factory, "emergency_flatten_order")
        assert len(order_logs) == 0

    def test_dry_run_does_not_write_order_audit(self, broker, db_factory):
        broker.get_positions.return_value = [_pos("AAPL", qty=10)]

        mgr = EmergencyFlattenManager(broker, db_factory, dry_run=True)
        mgr.flatten_all()

        order_logs = _query_audit(db_factory, "emergency_flatten_order")
        assert len(order_logs) == 0
