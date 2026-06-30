"""
is_live 게이트 검증 — TASK P3-01A (감사 갭 #2: is_live=False 우회 사각지대).

ScriptedPaperBroker(is_live=False)는 SAFE_MODE / ENABLE_LIVE_TRADING 게이트를
의도적으로 건너뛴다(backend/strategy/base.py). 그 게이트들은 '페이퍼 프로덕션
경로'의 일부이므로, 순수 시뮬레이션 하니스만으로는 절대 실행되지 않는다.

이 테스트는 네트워크 없이 다음을 결정론적으로 고정한다:
- is_live=True 브로커: SAFE_MODE 게이트가 NEW 주문을 차단한다
- is_live=True 브로커: ENABLE_LIVE_TRADING 미설정 시 shadow 게이트가 차단한다
- is_live=False 브로커: 두 게이트를 모두 우회한다(place_order 호출됨)

실제 KIS VTS 네트워크/TR 파싱까지의 검증은 test_live_paper_smoke.py(opt-in).
"""
import pytest

from backend.brokers.capabilities import SIMULATOR_CAPABILITIES
from backend.brokers.models import Order, OrderStatus
from backend.strategy.base import StrategyBase
from backend.worker.recovery import SAFE_MODE


class _FakeBroker:
    """StrategyBase.buy 가 요구하는 최소 인터페이스만 흉내내는 브로커."""

    def __init__(self, is_live: bool):
        self.is_live = is_live
        self.capabilities = SIMULATOR_CAPABILITIES
        self.placed: list[tuple] = []

    def get_price(self, symbol: str) -> float:
        return 100.0

    def place_order(self, symbol, side, qty, price, order_type="limit") -> Order:
        self.placed.append((symbol, side, qty, price, order_type))
        return Order(id="X1", symbol=symbol, side=side, qty=qty, price=price,
                     status=OrderStatus.SUBMITTED)


@pytest.fixture(autouse=True)
def restore_safe_mode():
    """전역 SAFE_MODE 싱글톤을 테스트 후 원복."""
    original = SAFE_MODE.can_trade
    yield
    if original:
        SAFE_MODE.enable()
    else:
        SAFE_MODE.disable("test teardown")


@pytest.fixture()
def strat():
    s = StrategyBase(broker=None, name="gate-test")
    return s


def test_live_broker_blocked_by_safe_mode(strat, monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")  # shadow 게이트는 통과
    SAFE_MODE.disable("recovery incomplete")           # SAFE_MODE 게이트가 차단해야

    broker = _FakeBroker(is_live=True)
    strat._broker = broker
    result = strat.buy("SPY", 10, price=100.0)

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []  # 주문 미제출


def test_live_broker_blocked_by_shadow_gate(strat, monkeypatch):
    monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)  # shadow 게이트가 차단
    SAFE_MODE.enable()                                        # SAFE_MODE 는 통과

    broker = _FakeBroker(is_live=True)
    strat._broker = broker
    result = strat.buy("SPY", 10, price=100.0)

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []


def test_sim_broker_bypasses_both_gates(strat, monkeypatch):
    # 두 게이트 모두 '닫힌' 상태로 만들어도 is_live=False 면 우회되어야 한다
    monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
    SAFE_MODE.disable("intentionally closed")

    broker = _FakeBroker(is_live=False)
    strat._broker = broker
    result = strat.buy("SPY", 10, price=100.0)

    assert result.status == OrderStatus.SUBMITTED
    assert len(broker.placed) == 1  # 게이트 우회 → 실제 제출됨
