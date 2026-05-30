"""Tests for EXPIRED and UNKNOWN state transitions in OrderStateMachine."""
import pytest

from backend.brokers.models import Order, OrderStatus
from backend.execution.order_machine import OrderStateMachine


def _make_order(status=OrderStatus.SUBMITTED) -> Order:
    o = Order(id="test-1", symbol="AAPL", side="buy", qty=10, price=150.0, status=status)
    return o


def _machine_with(order: Order) -> OrderStateMachine:
    m = OrderStateMachine()
    m._orders[order.id] = order
    return m


# ── EXPIRED transitions ──────────────────────────────────────────────────────

def test_submitted_to_expired():
    o = _make_order(OrderStatus.SUBMITTED)
    m = _machine_with(o)
    result = m.transition("test-1", OrderStatus.EXPIRED)
    assert result.status == OrderStatus.EXPIRED


def test_partial_filled_to_expired():
    o = _make_order(OrderStatus.PARTIAL_FILLED)
    m = _machine_with(o)
    result = m.transition("test-1", OrderStatus.EXPIRED)
    assert result.status == OrderStatus.EXPIRED


def test_expired_is_terminal():
    o = _make_order(OrderStatus.EXPIRED)
    m = _machine_with(o)
    with pytest.raises(ValueError):
        m.transition("test-1", OrderStatus.SUBMITTED)


# ── UNKNOWN transitions ──────────────────────────────────────────────────────

def test_submitted_to_unknown():
    o = _make_order(OrderStatus.SUBMITTED)
    m = _machine_with(o)
    result = m.transition("test-1", OrderStatus.UNKNOWN)
    assert result.status == OrderStatus.UNKNOWN


def test_partial_filled_to_unknown():
    o = _make_order(OrderStatus.PARTIAL_FILLED)
    m = _machine_with(o)
    result = m.transition("test-1", OrderStatus.UNKNOWN)
    assert result.status == OrderStatus.UNKNOWN


def test_unknown_to_filled():
    o = _make_order(OrderStatus.UNKNOWN)
    m = _machine_with(o)
    result = m.transition("test-1", OrderStatus.FILLED)
    assert result.status == OrderStatus.FILLED


def test_unknown_to_canceled():
    o = _make_order(OrderStatus.UNKNOWN)
    m = _machine_with(o)
    result = m.transition("test-1", OrderStatus.CANCELED)
    assert result.status == OrderStatus.CANCELED


def test_unknown_to_expired():
    o = _make_order(OrderStatus.UNKNOWN)
    m = _machine_with(o)
    result = m.transition("test-1", OrderStatus.EXPIRED)
    assert result.status == OrderStatus.EXPIRED


def test_filled_to_unknown_is_invalid():
    o = _make_order(OrderStatus.FILLED)
    m = _machine_with(o)
    with pytest.raises(ValueError):
        m.transition("test-1", OrderStatus.UNKNOWN)


# ── active_orders includes UNKNOWN ──────────────────────────────────────────

def test_active_orders_includes_unknown():
    m = OrderStateMachine()
    for oid, status in [
        ("o1", OrderStatus.PENDING),
        ("o2", OrderStatus.SUBMITTED),
        ("o3", OrderStatus.PARTIAL_FILLED),
        ("o4", OrderStatus.UNKNOWN),
        ("o5", OrderStatus.FILLED),
        ("o6", OrderStatus.CANCELED),
        ("o7", OrderStatus.EXPIRED),
    ]:
        m._orders[oid] = Order(id=oid, symbol="X", side="buy", qty=1, price=1.0, status=status)

    active_ids = {o.id for o in m.active_orders()}
    assert active_ids == {"o1", "o2", "o3", "o4"}
