import logging
import uuid
from datetime import datetime
from typing import Optional

from backend.brokers.base import BrokerAdapter
from backend.brokers.models import Balance, Order, OrderStatus, Position
from backend.execution.order_machine import FillEvent, OrderStateMachine
from backend.execution.position_tracker import Fill, PositionTracker

logger = logging.getLogger(__name__)

# Import canonical cost constants to stay in sync with live execution
from backend.quant.risk.position_sizer import DEFAULT_COMMISSION, DEFAULT_SLIPPAGE, KR_SECURITIES_TAX


class SimulatedBroker(BrokerAdapter):
    """
    백테스트 및 드라이런용 시뮬레이션 브로커.
    BrokerAdapter 동일 인터페이스 — 전략 코드는 실전/모의 구분 없음.
    실전 비용 모델: 수수료 0.015% + 슬리피지 0.10% + 한국 ETF 매도세 0.20%.
    Round-trip KR ETF ~0.43%. 이전 수수료만 차감 방식(0.03% RT)은 14× 과소 추정.
    """

    is_live: bool = False  # disables SAFE_MODE and ENABLE_LIVE_TRADING gates

    def __init__(self, initial_cash_krw: float = 2_000_000.0):
        self._cash = initial_cash_krw
        self._machine = OrderStateMachine(on_state_change=self._persist)
        self._tracker = PositionTracker(self._machine)
        self._prices: dict[str, float] = {}
        self._fill_callbacks: list = []

    # ── BrokerAdapter 구현 ───────────────────────────────────────────────
    def get_balance(self) -> Balance:
        total_eval = self._cash
        for pos in self._tracker.all_positions():
            price = self._prices.get(pos.symbol, pos.avg_price)
            total_eval += pos.qty * price
        return Balance(cash_krw=self._cash, cash_usd=0.0, total_eval_krw=total_eval)

    def get_positions(self) -> list[Position]:
        return self._tracker.all_positions()

    def get_position(self, symbol: str) -> Optional[Position]:
        return self._tracker.get_position(symbol)

    def place_order(self, symbol: str, side: str, qty: int, price: float, order_type: str = "limit") -> Order:
        order_id = str(uuid.uuid4())[:12]
        order = Order(id=order_id, symbol=symbol, side=side, qty=qty, price=price,
                      status=OrderStatus.PENDING)
        self._machine.register(order)
        self._machine.transition(order_id, OrderStatus.SUBMITTED)

        # 즉시 체결 — 실전과 동일한 비용 모델 적용
        is_kr = len(symbol) == 6 and symbol.isdigit()
        commission = price * qty * DEFAULT_COMMISSION
        slippage = price * qty * DEFAULT_SLIPPAGE
        tax = price * qty * KR_SECURITIES_TAX if (is_kr and side == "sell") else 0.0

        if side == "buy":
            # 매수: 슬리피지만큼 가격이 높아짐 (시장충격)
            fill_price = price * (1 + DEFAULT_SLIPPAGE)
            cost = fill_price * qty + commission
            if cost > self._cash:
                logger.warning("잔고 부족: 필요=%.0f 보유=%.0f", cost, self._cash)
                self._machine.reject(order_id)
                return self._machine.get(order_id)
            self._cash -= cost
            price = fill_price  # 실제 체결가에 슬리피지 반영
        elif side == "sell":
            # 매도: 슬리피지만큼 가격이 낮아짐
            fill_price = price * (1 - DEFAULT_SLIPPAGE)
            self._cash += fill_price * qty - commission - tax
            price = fill_price

        fill_event = FillEvent(order_id=order_id, filled_qty=qty, fill_price=price)
        self._machine.process_fill(fill_event)

        fill = Fill(order_id=order_id, symbol=symbol, side=side,
                    qty=qty, price=price, market="SIM")
        self._tracker.on_fill(fill)

        for cb in self._fill_callbacks:
            try:
                cb(fill)
            except Exception as e:
                logger.warning("fill 콜백 오류: %s", e)

        return self._machine.get(order_id)

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._machine.cancel(order_id)
            return True
        except Exception as e:
            logger.warning("취소 실패: %s", e)
            return False

    def get_price(self, symbol: str) -> float:
        price = self._prices.get(symbol)
        if price is None:
            raise ValueError(f"시뮬레이터에 {symbol} 가격 없음 — feed_price() 먼저 호출")
        return price

    # ── 시뮬레이터 전용 ──────────────────────────────────────────────────
    def feed_price(self, symbol: str, price: float):
        """봉 데이터 주입 — 전략 on_bar 호출 전 실행."""
        self._prices[symbol] = price
        self._tracker.update_prices({symbol: price})

    def feed_prices(self, prices: dict[str, float]):
        self._prices.update(prices)
        self._tracker.update_prices(prices)

    def add_fill_callback(self, cb):
        self._fill_callbacks.append(cb)

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def tracker(self) -> PositionTracker:
        return self._tracker

    def _persist(self, order: Order):
        pass  # 백테스트 시 DB 저장 불필요. 실전 Worker가 오버라이드.
