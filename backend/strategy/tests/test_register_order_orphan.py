"""
IndicatorStrategy._register_order 오펀(broker id 없음) 처리 — TASK P3-02B 리뷰 Finding 1.

broker id 없는 주문은 fail-closed: pending 락을 유지(중복 방지)하고 오펀 감사 콜백을
호출한다. 정상 주문은 machine/poller 에 등록된다. IndicatorStrategy 는 pandas 를 지연
임포트하므로 생성/‑register_order 는 pandas 없이 구동된다.
"""
import pytest

from backend.brokers.models import Order, OrderStatus
from backend.execution.order_machine import OrderStateMachine
from backend.execution.order_poller import OrderFillPoller
from backend.execution.position_tracker import PositionTracker
from backend.strategy.indicator.strategy import IndicatorStrategy


class _Broker:
    is_live = False
    def get_price(self, s): return 100.0
    def cancel_order(self, *a, **k): return True


def _make(on_orphan=None):
    machine = OrderStateMachine()
    tracker = PositionTracker(machine)
    poller = OrderFillPoller(_Broker())
    strat = IndicatorStrategy(
        broker=_Broker(), tracker=tracker, machine=machine, name="t", config={},
        poller=poller, on_filled_cb=lambda o: None, on_terminal_cb=lambda o: None,
        on_orphan_cb=on_orphan,
    )
    return strat, tracker, machine, poller


def test_empty_broker_id_fails_closed_keeps_lock_and_audits():
    seen = []
    strat, tracker, machine, poller = _make(on_orphan=lambda sym, o: seen.append((sym, o)))
    tracker.mark_pending("SPY", "tmp")                     # 진입 시 caller 가 건 락
    order = Order(id="", symbol="SPY", side="buy", qty=10, price=100.0,
                  status=OrderStatus.SUBMITTED)            # broker id 없음

    strat._register_order(order, "SPY")

    assert tracker.can_place_order("SPY") is False         # fail-closed: 락 유지(중복 방지)
    assert machine.get("") is None                         # 머신 미등록
    assert poller.pending_count() == 0                     # 폴러 미등록
    assert len(seen) == 1 and seen[0][0] == "SPY"          # 오펀 감사 호출


def test_valid_order_registers_normally():
    strat, tracker, machine, poller = _make()
    tracker.mark_pending("SPY", "SIM-1")
    order = Order(id="SIM-1", symbol="SPY", side="buy", qty=10, price=100.0,
                  status=OrderStatus.SUBMITTED)

    strat._register_order(order, "SPY")

    assert machine.get("SIM-1") is not None                # 등록됨
    assert poller.pending_count() == 1                     # 폴러 등록됨


def test_rejected_order_not_registered():
    strat, tracker, machine, poller = _make()
    order = Order(id="SIM-2", symbol="SPY", side="buy", qty=10, price=100.0,
                  status=OrderStatus.REJECTED)
    strat._register_order(order, "SPY")
    assert machine.get("SIM-2") is None
    assert poller.pending_count() == 0
