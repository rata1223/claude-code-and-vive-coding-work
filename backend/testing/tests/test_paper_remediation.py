"""
P3-02B 런타임 교정 검증 — 터미널 브로커 이벤트 전파(인메모리).

실제 OrderFillPoller + OrderStateMachine + PositionTracker + ScriptedPaperBroker 를
PaperHarness 로 구동한다. 하니스의 터미널 콜백은 프로덕션과 동일한
backend.execution.order_events.apply_terminal_event 로 위임되므로, 이 테스트는
실제 런타임 터미널 처리 경로를 그대로 검증한다.

필수 시나리오: cancel · reject · partial→cancel · partial→reject · repeated cancel
· duplicate broker event.
"""
import pytest

from backend.brokers.models import OrderStatus
from backend.brokers.paper_broker import ScriptedPaperBroker, FillStep
from backend.testing.paper_harness import PaperHarness


@pytest.fixture()
def broker():
    b = ScriptedPaperBroker(default_price=100.0)
    b.set_price("SPY", 100.0)
    return b


@pytest.fixture()
def h(broker):
    return PaperHarness(broker)


# ── cancel order ─────────────────────────────────────────────────────────────
def test_cancel_reaches_runtime(h):
    res = h.submit_order("SPY", "buy", 10, 100.0, no_fill=True)
    assert h.broker.cancel_order(res.order.id) is True
    h.pump()
    assert res.order.id in h.cancels
    assert h.order(res.order.id).status == OrderStatus.CANCELED   # 상태머신 전파
    assert h.position_qty("SPY") == 0
    assert h.tracker.can_place_order("SPY") is True               # pending 해제(스테일 없음)


# ── rejected order (async, poller 경로) ──────────────────────────────────────
def test_async_reject_reaches_runtime(h):
    res = h.submit_order("SPY", "buy", 10, 100.0,
                         fill_steps=[FillStep(0, OrderStatus.REJECTED)])
    h.pump()
    assert res.order.id in h.rejects
    assert h.order(res.order.id).status == OrderStatus.REJECTED
    assert h.position_qty("SPY") == 0
    assert h.tracker.can_place_order("SPY") is True


# ── partial fill → cancel (체결분 보존) ──────────────────────────────────────
def test_partial_then_cancel_preserves_fill(h):
    res = h.submit_order("SPY", "buy", 100, 100.0,
                         fill_steps=[FillStep(40, OrderStatus.PARTIAL_FILLED)])
    h.pump()                                    # 40 부분체결 노출
    assert h.position_qty("SPY") == 40
    assert h.broker.cancel_order(res.order.id) is True
    h.pump()                                    # 취소 관측
    assert h.order(res.order.id).status == OrderStatus.CANCELED
    assert h.position_qty("SPY") == 40          # 체결분 보존
    assert h.tracker.can_place_order("SPY") is True


# ── partial fill → reject (거부→취소로 수렴, 체결분 보존) ────────────────────
def test_partial_then_reject_converges_canceled(h):
    res = h.submit_order("SPY", "buy", 100, 100.0,
                         fill_steps=[FillStep(40, OrderStatus.PARTIAL_FILLED),
                                     FillStep(40, OrderStatus.REJECTED)])
    h.pump()
    assert h.order(res.order.id).status == OrderStatus.CANCELED   # PARTIAL→REJECTED 금지 → CANCELED
    assert h.total_filled(res.order.id) == 40
    assert h.position_qty("SPY") == 40
    assert h.tracker.can_place_order("SPY") is True


# ── repeated cancel (멱등) ───────────────────────────────────────────────────
def test_repeated_cancel_is_idempotent(h):
    res = h.submit_order("SPY", "buy", 10, 100.0, no_fill=True)
    assert h.broker.cancel_order(res.order.id) is True
    assert h.broker.cancel_order(res.order.id) is False   # 두 번째 취소는 무효
    h.pump()
    # 하니스 콜백을 한 번 더 직접 호출(중복 전달 모사) → 상태 불변
    h._on_canceled(h.order(res.order.id))
    assert h.order(res.order.id).status == OrderStatus.CANCELED
    assert h.position_qty("SPY") == 0


# ── duplicate broker event (중복 전달 안전) ─────────────────────────────────
def test_duplicate_terminal_event_safe(h):
    res = h.submit_order("SPY", "buy", 10, 100.0,
                         fill_steps=[FillStep(0, OrderStatus.REJECTED)])
    h.pump()
    o = h.order(res.order.id)
    # 동일 REJECTED 이벤트가 재전달되어도 상태·포지션 불변, 예외 없음
    h._on_rejected(o)
    h._on_rejected(o)
    assert h.order(res.order.id).status == OrderStatus.REJECTED
    assert h.position_qty("SPY") == 0
