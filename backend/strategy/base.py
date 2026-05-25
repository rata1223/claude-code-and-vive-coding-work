import logging
from abc import ABC
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.brokers.base import BrokerAdapter
    from backend.execution.position_tracker import Fill

logger = logging.getLogger(__name__)


class StrategyBase(ABC):
    """
    모든 전략의 기반 클래스.
    broker는 BrokerAdapter(실전/모의 모두 동일 인터페이스).
    이벤트 메서드를 오버라이드해 전략 구현.
    """

    def __init__(self, broker: "BrokerAdapter", name: str = ""):
        self._broker = broker
        self.name = name or self.__class__.__name__
        self._running = False
        logger.info("전략 초기화: %s", self.name)

    # ── 생명주기 이벤트 ──────────────────────────────────────────────────
    def on_start(self):
        """전략 시작 시 호출 — 초기화 로직."""

    def on_stop(self):
        """전략 중단 시 호출 — 정리 로직."""

    def on_market_open(self):
        """장 시작 시 호출."""

    def on_market_close(self):
        """장 마감 시 호출."""

    def on_bar(self, bar: dict):
        """
        새 봉 데이터 수신 시 호출.
        bar = {"symbol": str, "open": float, "high": float, "low": float,
                "close": float, "volume": float, "ts": datetime}
        """

    def on_fill(self, fill: "Fill"):
        """체결 이벤트 수신 시 호출."""

    # ── 매매 편의 메서드 ─────────────────────────────────────────────────
    def buy(self, symbol: str, qty: int, price: Optional[float] = None, order_type: str = "limit"):
        if price is None:
            price = self._broker.get_price(symbol)
            order_type = "market"
        logger.info("[%s] 매수 요청: %s qty=%d price=%.4f", self.name, symbol, qty, price)
        return self._broker.place_order(symbol, "buy", qty, price, order_type)

    def sell(self, symbol: str, qty: int, price: Optional[float] = None, order_type: str = "limit"):
        if price is None:
            price = self._broker.get_price(symbol)
            order_type = "market"
        logger.info("[%s] 매도 요청: %s qty=%d price=%.4f", self.name, symbol, qty, price)
        return self._broker.place_order(symbol, "sell", qty, price, order_type)

    def get_price(self, symbol: str) -> float:
        return self._broker.get_price(symbol)

    def get_balance(self):
        return self._broker.get_balance()

    def get_positions(self):
        return self._broker.get_positions()

    # ── 내부 제어 ─────────────────────────────────────────────────────────
    def start(self):
        self._running = True
        self.on_start()

    def stop(self):
        self._running = False
        self.on_stop()

    @property
    def is_running(self) -> bool:
        return self._running
