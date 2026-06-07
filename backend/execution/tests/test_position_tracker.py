"""
Unit tests for PositionTracker pending-order locking — TASK 2-3D.

The atomic check-and-set lock (try_mark_pending) is the mechanism that
guarantees "identical order cannot be reissued" and "race conditions are
blocked" for the idempotent execution system. It previously had no direct
unit coverage — only the read-only can_place_order() was exercised
(indirectly, via recovery tests).

Covered:
  1. try_mark_pending claims an unlocked symbol and rejects a second claim
  2. try_mark_pending is symbol-scoped (no cross-symbol interference)
  3. unmark_pending releases the lock so a new claim succeeds
  4. on_fill releases the lock as a side effect (inline unmark)
  5. TTL auto-release lets a stale lock be reclaimed
  6. can_place_order reflects lock state without mutating it
"""
import time

import pytest

from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import Fill, PositionTracker, _PENDING_LOCK_TTL


def _tracker() -> PositionTracker:
    return PositionTracker(OrderStateMachine())


def _fill(symbol="005930", side="buy", qty=10, price=70000.0, order_id="ORD1") -> Fill:
    return Fill(order_id=order_id, symbol=symbol, side=side, qty=qty, price=price, market="KR")


class TestTryMarkPendingAtomicity:
    def test_first_claim_succeeds_second_claim_rejected(self):
        """Core idempotency guarantee: identical order cannot be reissued while pending."""
        tracker = _tracker()
        assert tracker.try_mark_pending("005930") is True
        assert tracker.try_mark_pending("005930") is False
        assert tracker.try_mark_pending("005930") is False  # repeated attempts stay locked

    def test_lock_is_scoped_per_symbol(self):
        tracker = _tracker()
        assert tracker.try_mark_pending("005930") is True
        assert tracker.try_mark_pending("000660") is True
        assert tracker.try_mark_pending("005930") is False
        assert tracker.try_mark_pending("000660") is False


class TestLockRelease:
    def test_unmark_pending_allows_new_claim(self):
        tracker = _tracker()
        assert tracker.try_mark_pending("005930") is True
        tracker.unmark_pending("005930")
        assert tracker.try_mark_pending("005930") is True

    def test_unmark_pending_unknown_symbol_is_noop(self):
        tracker = _tracker()
        tracker.unmark_pending("005930")  # must not raise
        assert tracker.try_mark_pending("005930") is True

    def test_on_fill_releases_pending_lock(self):
        """Fill arrival must release the lock so the next signal can place an order."""
        tracker = _tracker()
        assert tracker.try_mark_pending("005930") is True
        tracker.on_fill(_fill(symbol="005930"))
        assert tracker.try_mark_pending("005930") is True


class TestTTLAutoRelease:
    def test_stale_lock_is_reclaimed_after_ttl(self, monkeypatch):
        tracker = _tracker()
        assert tracker.try_mark_pending("005930") is True

        # Simulate TTL expiry by advancing the monotonic clock the tracker reads.
        real_monotonic = time.monotonic
        future = real_monotonic() + _PENDING_LOCK_TTL + 1
        monkeypatch.setattr(
            "backend.execution.position_tracker.time.monotonic", lambda: future
        )

        assert tracker.try_mark_pending("005930") is True

    def test_fresh_lock_is_not_reclaimed_before_ttl(self, monkeypatch):
        tracker = _tracker()
        assert tracker.try_mark_pending("005930") is True

        real_monotonic = time.monotonic
        almost_expired = real_monotonic() + _PENDING_LOCK_TTL - 1
        monkeypatch.setattr(
            "backend.execution.position_tracker.time.monotonic", lambda: almost_expired
        )

        assert tracker.try_mark_pending("005930") is False


class TestCanPlaceOrderIsReadOnly:
    def test_reflects_lock_state_without_mutating(self):
        tracker = _tracker()
        assert tracker.can_place_order("005930") is True
        # Repeated reads must not themselves acquire the lock.
        assert tracker.can_place_order("005930") is True
        assert tracker.can_place_order("005930") is True

        assert tracker.try_mark_pending("005930") is True
        assert tracker.can_place_order("005930") is False
        assert tracker.can_place_order("005930") is False  # still locked — no side effect

        tracker.unmark_pending("005930")
        assert tracker.can_place_order("005930") is True

    def test_ttl_expiry_clears_lock_as_side_effect(self, monkeypatch):
        """can_place_order proactively evicts stale entries (documented behavior)."""
        tracker = _tracker()
        assert tracker.try_mark_pending("005930") is True

        real_monotonic = time.monotonic
        future = real_monotonic() + _PENDING_LOCK_TTL + 1
        monkeypatch.setattr(
            "backend.execution.position_tracker.time.monotonic", lambda: future
        )

        assert tracker.can_place_order("005930") is True
        with tracker._lock:
            assert "005930" not in tracker._pending_symbols
