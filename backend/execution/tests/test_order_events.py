"""
apply_terminal_event 단위 테스트 — TASK P3-02B.

실제 OrderStateMachine + PositionTracker 를 사용(목 아님). 터미널 브로커
이벤트가 런타임에 반영되는지(상태 전환 + pending 락 해제 + 멱등성)를 검증.
"""
import pytest

from backend.brokers.models import Order, OrderStatus
from backend.execution.order_events import apply_terminal_event
from backend.execution.order_machine import FillEvent, OrderStateMachine
from backend.execution.position_tracker import PositionTracker


def _order(status=OrderStatus.SUBMITTED, oid="SIM-1", symbol="SPY", side="buy", qty=10):
    return Order(id=oid, symbol=symbol, side=side, qty=qty, price=100.0, status=status)


@pytest.fixture()
def rt():
    machine = OrderStateMachine()
    tracker = PositionTracker(machine)
    return machine, tracker


def _register_submitted(machine, tracker, o):
    machine.register(o)  # registers as SUBMITTED
    tracker.mark_pending(o.symbol, o.id)


def test_cancel_transitions_and_releases_lock(rt):
    machine, tracker = rt
    o = _order()
    _register_submitted(machine, tracker, o)

    changed = apply_terminal_event(machine, tracker, _order(status=OrderStatus.CANCELED))
    assert changed is True
    assert machine.get("SIM-1").status == OrderStatus.CANCELED
    assert tracker.can_place_order("SPY") is True  # pending 해제됨


def test_reject_transitions(rt):
    machine, tracker = rt
    o = _order()
    _register_submitted(machine, tracker, o)
    changed = apply_terminal_event(machine, tracker, _order(status=OrderStatus.REJECTED))
    assert changed is True
    assert machine.get("SIM-1").status == OrderStatus.REJECTED


def test_expired_transitions(rt):
    machine, tracker = rt
    _register_submitted(machine, tracker, _order())
    changed = apply_terminal_event(machine, tracker, _order(status=OrderStatus.EXPIRED))
    assert changed is True
    assert machine.get("SIM-1").status == OrderStatus.EXPIRED


def test_partial_then_reject_converges_to_canceled(rt):
    """PARTIAL_FILLED → REJECTED 는 상태머신이 금지 → CANCELLED 로 수렴(체결분 보존)."""
    machine, tracker = rt
    o = _order(qty=100)
    _register_submitted(machine, tracker, o)
    machine.process_fill(FillEvent("SIM-1", 40, 100.0))  # PARTIAL_FILLED
    assert machine.get("SIM-1").status == OrderStatus.PARTIAL_FILLED

    changed = apply_terminal_event(machine, tracker, _order(status=OrderStatus.REJECTED, qty=100))
    assert changed is True
    assert machine.get("SIM-1").status == OrderStatus.CANCELED   # 거부→취소로 수렴
    assert machine.get("SIM-1").filled_qty == 40                 # 체결분 보존


def test_idempotent_repeated_terminal(rt):
    machine, tracker = rt
    _register_submitted(machine, tracker, _order())
    first = apply_terminal_event(machine, tracker, _order(status=OrderStatus.CANCELED))
    second = apply_terminal_event(machine, tracker, _order(status=OrderStatus.CANCELED))
    assert first is True and second is False           # 두 번째는 전환 없음
    assert machine.get("SIM-1").status == OrderStatus.CANCELED


def test_unknown_order_still_releases_lock(rt):
    machine, tracker = rt
    tracker.mark_pending("SPY", "SIM-unknown")          # 머신엔 없지만 락은 걸림
    changed = apply_terminal_event(machine, tracker, _order(oid="SIM-unknown",
                                                            status=OrderStatus.CANCELED))
    assert changed is False                             # 머신에 없어 전환 없음
    assert tracker.can_place_order("SPY") is True       # 그래도 락 해제(오펀 방지)


def test_timeout_target_status_forces_canceled(rt):
    machine, tracker = rt
    _register_submitted(machine, tracker, _order())
    # 타임아웃 시 order.status 는 여전히 SUBMITTED → target_status 로 CANCELLED 강제
    changed = apply_terminal_event(machine, tracker, _order(status=OrderStatus.SUBMITTED),
                                   target_status=OrderStatus.CANCELED)
    assert changed is True
    assert machine.get("SIM-1").status == OrderStatus.CANCELED


def test_non_terminal_status_ignored(rt):
    machine, tracker = rt
    _register_submitted(machine, tracker, _order())
    changed = apply_terminal_event(machine, tracker, _order(status=OrderStatus.SUBMITTED))
    assert changed is False
    assert machine.get("SIM-1").status == OrderStatus.SUBMITTED  # 변경 없음
