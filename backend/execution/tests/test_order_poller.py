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
  15. terminal-state callbacks (on_canceled, on_rejected, on_expired)
  16. health monitor metrics (in-memory counters)
"""
import dataclasses
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, call

import pytest
from backend.database.testing import make_test_engine
from sqlalchemy.orm import sessionmaker

from backend.brokers.models import Order, OrderStatus
from backend.database.models import AuditLog, Base
from backend.execution.order_poller import (
    OrderFillPoller,
    PollingHealth,
    PollingHealthMonitor,
    _PollEntry,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _db():
    """In-memory SQLite session factory, fresh per call."""
    engine = make_test_engine()
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

    def test_audit_on_expire(self):
        db = _db()
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.EXPIRED)

        poller = _poller(broker=broker, db_factory=db)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert _count_audit(db, "poller_expired") == 1

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


# ── D4-a: Fill loss — on_filled exception handling ───────────────────────────

class TestFillLoss:
    def test_on_filled_exception_entry_kept_for_filled_then_retried(self):
        """FILLED: a lost callback must NOT drop the fill (P3-02C-B). The entry
        stays registered so the next poll re-drives the same increment, and the
        persistent watermark keeps it from being double-counted on success."""
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.FILLED, filled_qty=100)

        applied = []
        state = {"fail_next": True}

        def flaky_callback(o):
            if state["fail_next"]:
                state["fail_next"] = False
                raise RuntimeError("downstream failure")
            applied.append(o.filled_qty)

        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order, on_filled=flaky_callback)
        poller._entries[order.id] = entry

        # First poll: callback raises → entry retained, watermark not advanced.
        poller._poll_one(entry)
        assert order.id in poller._entries
        assert entry.last_reported_qty == 0
        assert applied == []

        # Second poll: callback succeeds → applied exactly once, entry removed.
        poller._poll_one(entry)
        assert order.id not in poller._entries
        assert applied == [100]

    def test_on_filled_exception_entry_kept_for_partial(self):
        """PARTIAL_FILLED: if on_filled raises, the entry stays (polling continues)."""
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.PARTIAL_FILLED, filled_qty=50
        )

        def bad_callback(o):
            raise RuntimeError("downstream failure")

        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order, on_filled=bad_callback)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert order.id in poller._entries  # still registered for next poll


# ── D4-b: Broker exception ───────────────────────────────────────────────────

class TestBrokerException:
    def test_broker_exception_advances_schedule(self):
        """get_order_status() raises → entry stays, poll_index advances for backoff."""
        broker = MagicMock()
        broker.get_order_status.side_effect = RuntimeError("connection timeout")

        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        before_index = entry.poll_index
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert order.id in poller._entries
        assert entry.poll_index > before_index


# ── D4-c: Transition validation ──────────────────────────────────────────────

class TestTransitionValidation:
    def test_invalid_transition_logs_warning(self, caplog):
        """PARTIAL_FILLED → SUBMITTED is not a valid transition and must be warned."""
        import logging
        broker = MagicMock()
        # Broker regresses to SUBMITTED from PARTIAL_FILLED
        broker.get_order_status.return_value = _order(
            status=OrderStatus.SUBMITTED, filled_qty=0
        )

        poller = _poller(broker=broker)
        order = _order(status=OrderStatus.PARTIAL_FILLED, filled_qty=50)
        entry = _entry(order)
        entry.last_reported_qty = 50

        with caplog.at_level(logging.WARNING, logger="backend.execution.order_poller"):
            poller._poll_one(entry)

        assert any("예상치 못한 상태 전환" in r.message for r in caplog.records)

    def test_valid_transition_no_warning(self, caplog):
        """SUBMITTED → FILLED is a valid transition; no unexpected-transition warning."""
        import logging
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.FILLED, filled_qty=100
        )

        poller = _poller(broker=broker)
        order = _order(status=OrderStatus.SUBMITTED)
        entry = _entry(order)
        poller._entries[order.id] = entry

        with caplog.at_level(logging.WARNING, logger="backend.execution.order_poller"):
            poller._poll_one(entry)

        unexpected_warns = [r for r in caplog.records
                            if "예상치 못한 상태 전환" in r.message]
        assert unexpected_warns == []


# ── D4-d: Timeout cutoff property ────────────────────────────────────────────

class TestTimeoutCutoff:
    def test_is_timed_out_false_for_recent_entry(self):
        """Fresh entry created now is not timed out."""
        order = _order()
        entry = _entry(order)
        assert entry.is_timed_out is False

    def test_is_timed_out_true_after_31_minutes(self):
        """Entry with registered_at 31 minutes in the past is timed out."""
        order = _order()
        entry = _entry(order)
        entry.registered_at = entry.registered_at - timedelta(minutes=31)
        assert entry.is_timed_out is True


# ── 15. Terminal-state callbacks ──────────────────────────────────────────────

class TestTerminalCallbacks:
    """Tests for on_canceled, on_rejected, on_expired callbacks (TASK 2-2C additions)."""

    def test_on_canceled_called_when_broker_returns_canceled(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.CANCELED)

        canceled_calls = []
        poller = _poller(broker=broker)
        order = _order()
        poller.register(order, on_filled=MagicMock(),
                        on_canceled=lambda o: canceled_calls.append(o.id))
        with poller._lock:
            entry = poller._entries[order.id]
        poller._poll_one(entry)

        assert canceled_calls == ["ORD1"]

    def test_on_canceled_not_called_without_registration(self):
        """on_canceled defaults to None — no crash when not provided."""
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.CANCELED)

        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)  # must not raise

        assert order.id not in poller._entries

    def test_on_rejected_called_when_broker_returns_rejected(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.REJECTED)

        rejected_calls = []
        poller = _poller(broker=broker)
        order = _order()
        poller.register(order, on_filled=MagicMock(),
                        on_rejected=lambda o: rejected_calls.append(o.id))
        with poller._lock:
            entry = poller._entries[order.id]
        poller._poll_one(entry)

        assert rejected_calls == ["ORD1"]

    def test_on_expired_called_when_broker_returns_expired(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.EXPIRED)

        expired_calls = []
        poller = _poller(broker=broker)
        order = _order()
        poller.register(order, on_filled=MagicMock(),
                        on_expired=lambda o: expired_calls.append(o.id))
        with poller._lock:
            entry = poller._entries[order.id]
        poller._poll_one(entry)

        assert expired_calls == ["ORD1"]

    def test_on_filled_not_called_on_canceled(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.CANCELED)

        fill_calls = []
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order, on_filled=fill_calls.append)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert fill_calls == []

    def test_on_filled_not_called_on_rejected(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.REJECTED)

        fill_calls = []
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order, on_filled=fill_calls.append)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        assert fill_calls == []

    def test_terminal_callback_exception_does_not_prevent_entry_removal(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.CANCELED)

        def bad_cancel(o):
            raise RuntimeError("callback failure")

        poller = _poller(broker=broker)
        order = _order()
        poller.register(order, on_filled=MagicMock(), on_canceled=bad_cancel)
        with poller._lock:
            entry = poller._entries[order.id]
        poller._poll_one(entry)

        assert order.id not in poller._entries  # cleaned up even if callback raised


# ── 16. Health monitor ────────────────────────────────────────────────────────

class TestHealthMonitorUnit:
    """Unit tests for PollingHealthMonitor in isolation."""

    def test_initial_state(self):
        mon = PollingHealthMonitor()
        h = mon.get_health()
        assert h.total_registered == 0
        assert h.is_healthy is True
        assert h.consecutive_poll_errors == 0

    def test_register_increments(self):
        mon = PollingHealthMonitor()
        mon.record_register()
        mon.record_register()
        assert mon.get_health().total_registered == 2

    def test_fill_increments(self):
        mon = PollingHealthMonitor()
        mon.record_fill()
        assert mon.get_health().total_fills_detected == 1

    def test_consecutive_errors_tracked(self):
        mon = PollingHealthMonitor()
        for _ in range(5):
            mon.record_poll_error()
        h = mon.get_health()
        assert h.consecutive_poll_errors == 5
        assert h.total_poll_errors == 5
        assert h.is_healthy is True  # < 10

    def test_10_consecutive_errors_unhealthy(self):
        mon = PollingHealthMonitor()
        for _ in range(10):
            mon.record_poll_error()
        assert mon.get_health().is_healthy is False

    def test_success_resets_consecutive(self):
        mon = PollingHealthMonitor()
        for _ in range(5):
            mon.record_poll_error()
        mon.record_poll_success()
        h = mon.get_health()
        assert h.consecutive_poll_errors == 0
        assert h.is_healthy is True

    def test_get_health_returns_copy(self):
        mon = PollingHealthMonitor()
        h1 = mon.get_health()
        mon.record_fill()
        h2 = mon.get_health()
        assert h1.total_fills_detected == 0  # original snapshot unchanged
        assert h2.total_fills_detected == 1

    def test_last_successful_poll_at_is_timezone_aware(self):
        """last_successful_poll_at must be comparable to datetime.now(timezone.utc)."""
        mon = PollingHealthMonitor()
        mon.record_poll_success()
        h = mon.get_health()
        assert h.last_successful_poll_at is not None
        # Should not raise TypeError
        delta = datetime.now(timezone.utc) - h.last_successful_poll_at
        assert delta.total_seconds() >= 0


class TestHealthMonitorIntegration:
    """Health metrics observed through the poller (end-to-end)."""

    def test_register_increments_health(self):
        poller = _poller()
        poller.register(_order(), on_filled=MagicMock())
        assert poller.health.total_registered == 1

    def test_fill_increments_health(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.FILLED, filled_qty=100)
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)
        assert poller.health.total_fills_detected == 1

    def test_partial_fill_increments_health(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(
            status=OrderStatus.PARTIAL_FILLED, filled_qty=50
        )
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)
        assert poller.health.total_partial_fills == 1

    def test_cancel_increments_health(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.CANCELED)
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)
        assert poller.health.total_cancels == 1

    def test_reject_increments_health(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.REJECTED)
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)
        assert poller.health.total_rejects == 1

    def test_expired_increments_health(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.EXPIRED)
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)
        assert poller.health.total_expired == 1

    def test_timeout_increments_health(self):
        poller = _poller()
        entry = _entry(_order())
        poller._handle_timeout(entry)
        assert poller.health.total_timeouts == 1

    def test_broker_error_increments_health(self):
        broker = MagicMock()
        broker.get_order_status.side_effect = RuntimeError("down")
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)
        h = poller.health
        assert h.total_poll_errors == 1
        assert h.consecutive_poll_errors == 1

    def test_consecutive_errors_reset_after_success(self):
        broker = MagicMock()
        broker.get_order_status.side_effect = RuntimeError("down")
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._poll_one(entry)

        broker.get_order_status.side_effect = None
        broker.get_order_status.return_value = _order(status=OrderStatus.SUBMITTED)
        poller._poll_one(entry)

        assert poller.health.consecutive_poll_errors == 0

    def test_10_consecutive_errors_is_unhealthy(self):
        broker = MagicMock()
        broker.get_order_status.side_effect = RuntimeError("down")
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry

        for _ in range(10):
            poller._poll_one(entry)

        assert poller.health.is_healthy is False

    def test_pending_count_updated_on_register(self):
        poller = _poller()
        assert poller.health.pending_count == 0
        poller.register(_order(), on_filled=MagicMock())
        assert poller.health.pending_count == 1

    def test_pending_count_updated_on_unregister(self):
        poller = _poller()
        poller.register(_order(), on_filled=MagicMock())
        poller.unregister("ORD1")
        assert poller.health.pending_count == 0

    def test_pending_count_decremented_on_fill(self):
        broker = MagicMock()
        broker.get_order_status.return_value = _order(status=OrderStatus.FILLED, filled_qty=100)
        poller = _poller(broker=broker)
        order = _order()
        entry = _entry(order)
        poller._entries[order.id] = entry
        poller._health.set_pending_count(1)
        poller._poll_one(entry)
        assert poller.health.pending_count == 0
