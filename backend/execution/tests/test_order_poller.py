"""
Unit tests for OrderFillPoller — TASK 2-2C.

Tests exercise _poll_one() and _handle_timeout() directly (no thread start)
for deterministic, fast execution. In-memory SQLite is used for AuditLog tests.

Required scenarios:
  1. full_fill
  2. partial_fill
  3. multiple_partial_fills
  4. cancel_after_partial_fill
  5. reject
  6. expire
  7. restart_during_fill (recovery re-register with pre-set last_reported_qty)
  8. duplicate_broker_response (same partial qty reported twice)
  9. delayed_broker_response (None × N, then FILLED)

Extra scenarios:
  10. audit_on_filled
  11. audit_on_timeout
  12. audit_on_cancel
  13. semantic_mapper_cancel_kwargs_used_on_timeout
  14. semantic_mapper_cancel_failure_does_not_raise
"""
import dataclasses
import json
from unittest.mock import MagicMock, call

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.brokers.models import Order, OrderStatus
from backend.database.models import AuditLog, Base
from backend.execution.order_poller import OrderFillPoller, _PollEntry


# ── Helpers ────────────────────────────────────────────────────────────────

def _db():
    """In-memory SQLite session factory, fresh per call."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _order(
    id="ORD1",
    symbol="AAPL",
    side="buy",
    qty=100,
    price=150.0,
    status=OrderStatus.SUBMITTED,
    filled_qty=0,
    avg=0.0,
) -> Order:
    return Order(
        id=id, symbol=symbol, side=side, qty=qty, price=price,
        status=status, filled_qty=filled_qty, avg_fill_price=avg,
    )


def _entry(order, on_filled=None, on_timeout=None, last_reported_qty=0) -> _PollEntry:
    e = _PollEntry(
        order=order,
        on_filled=on_filled or (lambda o: None),
        on_timeout=on_timeout or OrderFillPoller._default_timeout_handler,
    )
    e.last_reported_qty = last_reported_qty
    return e


def _poller(broker=None, db_factory=None, mapper=None) -> OrderFillPoller:
    return OrderFillPoller(
        broker=broker or MagicMock(),
        db_factory=db_factory,
        semantic_mapper=mapper,
    )


def _count_audit(factory, event_type: str) -> int:
    sess = factory()
    try:
        return sess.query(AuditLog).filter(AuditLog.event_type == event_type).count()
    finally:
        sess.close()


# ── 1. Full fill ────────────────────────────────────────────────────────────

class TestFullFill:
    def test_full_fill_calls_on_filled_once(self):
        broker = MagicMock()
        filled_order = _order(status=OrderStatus.FILLED, filled_qty=100, avg=150.5)
        broker.get_order_status.return_value = filled_order

        calls = []
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order, on_filled=calls.append)
        poller._poll_one(entry)

        assert len(calls) == 1
        assert calls[0].filled_qty == 100

    def test_full_fill_removes_entry(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.FILLED, filled_qty=100)

        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert order.id not in poller._entries

    def test_full_fill_broker_returns_none_skips(self):
        broker = MagicMock()
        broker.get_order_status.return_value = None

        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=calls.append)
        poller._poll_one(entry)

        assert calls == []


# ── 2. Partial fill ─────────────────────────────────────────────────────────

class TestPartialFill:
    def test_partial_fill_calls_on_filled_with_incremental_qty(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.PARTIAL_FILLED, filled_qty=50, avg=150.0
        )

        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=calls.append)
        poller._poll_one(entry)

        assert len(calls) == 1
        assert calls[0].filled_qty == 50

    def test_partial_fill_keeps_entry_registered(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.PARTIAL_FILLED, filled_qty=50
        )

        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert order.id in poller._entries

    def test_partial_fill_updates_last_reported_qty(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.PARTIAL_FILLED, filled_qty=60
        )

        poller = _poller(broker=broker)
        entry = _entry(_order())
        poller._poll_one(entry)

        assert entry.last_reported_qty == 60


# ── 3. Multiple partial fills ────────────────────────────────────────────────

class TestMultiplePartialFills:
    def test_increments_are_correct_across_polls(self):
        """Three polls: PARTIAL(30) → PARTIAL(70) → FILLED(100).
        on_filled should receive increments 30, 40, 30."""
        broker = MagicMock()
        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=lambda o: calls.append(o.filled_qty))
        poller._entries[entry.order.id] = entry

        broker.get_order_status.return_value = _order(
            status=OrderStatus.PARTIAL_FILLED, filled_qty=30
        )
        poller._poll_one(entry)

        broker.get_order_status.return_value = _order(
            status=OrderStatus.PARTIAL_FILLED, filled_qty=70
        )
        poller._poll_one(entry)

        broker.get_order_status.return_value = _order(
            status=OrderStatus.FILLED, filled_qty=100
        )
        poller._poll_one(entry)

        assert calls == [30, 40, 30]
        assert entry.order.id not in poller._entries

    def test_three_partials_total_qty_matches_order_qty(self):
        broker = MagicMock()
        increments = []
        poller = _poller(broker=broker)
        entry = _entry(_order(qty=90), on_filled=lambda o: increments.append(o.filled_qty))
        poller._entries[entry.order.id] = entry

        for cum in [30, 60, 90]:
            status = OrderStatus.FILLED if cum == 90 else OrderStatus.PARTIAL_FILLED
            broker.get_order_status.return_value = _order(
                qty=90, status=status, filled_qty=cum
            )
            poller._poll_one(entry)

        assert sum(increments) == 90


# ── 4. Cancel after partial fill ─────────────────────────────────────────────

class TestCancelAfterPartialFill:
    def test_partial_then_cancel_calls_on_filled_once(self):
        broker = MagicMock()
        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=lambda o: calls.append(o.filled_qty))
        poller._entries[entry.order.id] = entry

        broker.get_order_status.return_value = _order(
            status=OrderStatus.PARTIAL_FILLED, filled_qty=50
        )
        poller._poll_one(entry)

        broker.get_order_status.return_value = _order(
            status=OrderStatus.CANCELED, filled_qty=50
        )
        poller._poll_one(entry)

        assert calls == [50]
        assert entry.order.id not in poller._entries

    def test_cancel_after_partial_does_not_call_on_filled_again(self):
        broker = MagicMock()
        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=lambda o: calls.append(o.filled_qty))
        poller._entries[entry.order.id] = entry

        broker.get_order_status.return_value = _order(
            status=OrderStatus.PARTIAL_FILLED, filled_qty=40
        )
        poller._poll_one(entry)
        assert len(calls) == 1

        broker.get_order_status.return_value = _order(status=OrderStatus.CANCELED)
        poller._poll_one(entry)
        assert len(calls) == 1  # no second call


# ── 5. Reject ────────────────────────────────────────────────────────────────

class TestReject:
    def test_reject_removes_entry(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.REJECTED)

        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert order.id not in poller._entries

    def test_reject_does_not_call_on_filled(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.REJECTED)

        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=calls.append)
        poller._poll_one(entry)

        assert calls == []


# ── 6. Expire ────────────────────────────────────────────────────────────────

class TestExpire:
    def test_expire_removes_entry(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.EXPIRED)

        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert order.id not in poller._entries

    def test_expire_does_not_call_on_filled(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.EXPIRED)

        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=calls.append)
        poller._poll_one(entry)

        assert calls == []


# ── 7. Restart during fill (recovery re-register) ────────────────────────────

class TestRestartDuringFill:
    def test_recovery_register_with_last_reported_qty_prevents_double_count(self):
        """Simulate re-register after restart where 50 qty was already persisted.
        Broker now reports FILLED(100); on_filled should receive incremental=50 only."""
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.FILLED, filled_qty=100, avg=151.0
        )

        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=calls.append, last_reported_qty=50)
        poller._entries[entry.order.id] = entry
        poller._poll_one(entry)

        assert len(calls) == 1
        assert calls[0].filled_qty == 50  # incremental only — 50 already persisted pre-restart

    def test_recovery_with_full_already_reported_skips_callback(self):
        """If last_reported_qty == filled_qty at poll time: duplicate, skip."""
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.FILLED, filled_qty=100
        )

        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=calls.append, last_reported_qty=100)
        poller._entries[entry.order.id] = entry
        poller._poll_one(entry)

        assert calls == []


# ── 8. Duplicate broker response ─────────────────────────────────────────────

class TestDuplicateBrokerResponse:
    def test_same_partial_qty_reported_twice_calls_on_filled_once(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.PARTIAL_FILLED, filled_qty=50
        )

        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=lambda o: calls.append(o.filled_qty))
        poller._entries[entry.order.id] = entry

        poller._poll_one(entry)
        poller._poll_one(entry)  # same qty — should be skipped

        assert len(calls) == 1
        assert calls[0] == 50

    def test_same_filled_response_twice_calls_on_filled_once(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.FILLED, filled_qty=100
        )

        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=lambda o: calls.append(o.filled_qty))
        poller._entries[entry.order.id] = entry

        poller._poll_one(entry)
        # entry removed after first FILLED; second call uses a new entry with last_reported=100
        entry2 = _entry(_order(), on_filled=lambda o: calls.append(o.filled_qty),
                        last_reported_qty=100)
        poller._entries[entry2.order.id] = entry2
        poller._poll_one(entry2)

        assert len(calls) == 1


# ── 9. Delayed broker response ───────────────────────────────────────────────

class TestDelayedBrokerResponse:
    def test_none_responses_then_filled(self):
        broker = MagicMock()
        broker.get_order_status.side_effect = [None, None, None,
                                               _order(status=OrderStatus.FILLED, filled_qty=100)]

        calls = []
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order, on_filled=calls.append)
        poller._entries[order.id] = entry

        for _ in range(4):
            poller._poll_one(entry)

        assert len(calls) == 1
        assert calls[0].filled_qty == 100
        assert order.id not in poller._entries

    def test_none_responses_do_not_call_on_filled(self):
        broker = MagicMock()
        broker.get_order_status.return_value = None

        calls = []
        poller = _poller(broker=broker)
        entry = _entry(_order(), on_filled=calls.append)

        for _ in range(5):
            poller._poll_one(entry)

        assert calls == []


# ── 10–12. Audit logging ─────────────────────────────────────────────────────

class TestAuditLog:
    def test_audit_on_filled(self):
        db = _db()
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.FILLED, filled_qty=100)

        poller = _poller(broker=broker, db_factory=db)
        entry = _entry(_order())
        poller._poll_one(entry)

        assert _count_audit(db, "poller_filled") == 1

    def test_audit_on_partial_filled(self):
        db = _db()
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.PARTIAL_FILLED, filled_qty=50
        )

        poller = _poller(broker=broker, db_factory=db)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert _count_audit(db, "poller_partial_filled") == 1

    def test_audit_on_cancel(self):
        db = _db()
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.CANCELED)

        poller = _poller(broker=broker, db_factory=db)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert _count_audit(db, "poller_canceled") == 1

    def test_audit_on_reject(self):
        db = _db()
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.REJECTED)

        poller = _poller(broker=broker, db_factory=db)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert _count_audit(db, "poller_rejected") == 1

    def test_audit_on_timeout(self):
        db = _db()
        poller = _poller(db_factory=db)
        order = _order()
        entry = _entry(order)
        poller._handle_timeout(entry)

        assert _count_audit(db, "poller_timeout") == 1

    def test_audit_detail_contains_expected_fields_on_fill(self):
        db = _db()
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.FILLED, filled_qty=100, avg=152.5
        )

        poller = _poller(broker=broker, db_factory=db)
        entry = _entry(_order())
        poller._poll_one(entry)

        sess = db()
        row = sess.query(AuditLog).filter(AuditLog.event_type == "poller_filled").first()
        sess.close()
        detail = json.loads(row.detail)
        assert "incremental" in detail
        assert "avg" in detail

    def test_no_audit_without_db_factory(self):
        """Poller without db_factory must not raise."""
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.FILLED, filled_qty=100)

        poller = _poller(broker=broker, db_factory=None)
        entry = _entry(_order())
        poller._poll_one(entry)  # should not raise


# ── 13–14. BrokerSemanticMapper integration ──────────────────────────────────

class TestSemanticMapperTimeout:
    def test_cancel_called_with_mapper_kwargs_on_timeout(self):
        broker = MagicMock()
        broker.cancel_order.return_value = True

        mapper = MagicMock()
        mapper.cancel_kwargs.return_value = {
            "order_id": "ORD1", "symbol": "AAPL", "qty": 100, "price": 150.0
        }

        poller = _poller(broker=broker, mapper=mapper)
        order = _order()
        entry = _entry(order)
        poller._handle_timeout(entry)

        mapper.cancel_kwargs.assert_called_once_with(order)
        broker.cancel_order.assert_called_once_with(
            order_id="ORD1", symbol="AAPL", qty=100, price=150.0
        )

    def test_cancel_failure_does_not_raise_and_on_timeout_still_called(self):
        broker = MagicMock()
        broker.cancel_order.side_effect = RuntimeError("network error")

        timeout_calls = []
        mapper = MagicMock()
        mapper.cancel_kwargs.return_value = {"order_id": "ORD1"}

        poller = _poller(broker=broker, mapper=mapper)
        order = _order()
        entry = _entry(order, on_timeout=lambda o: timeout_calls.append(o.id))
        poller._handle_timeout(entry)

        assert timeout_calls == ["ORD1"]

    def test_no_mapper_does_not_call_cancel_on_timeout(self):
        broker = MagicMock()
        poller = _poller(broker=broker, mapper=None)
        entry = _entry(_order())
        poller._handle_timeout(entry)  # should not raise

        broker.cancel_order.assert_not_called()
