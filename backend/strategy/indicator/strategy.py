import logging
from typing import TYPE_CHECKING

from backend.strategy.base import StrategyBase

if TYPE_CHECKING:
    from backend.brokers.base import BrokerAdapter
    from backend.execution.order_machine import OrderStateMachine
    from backend.execution.position_tracker import PositionTracker

logger = logging.getLogger(__name__)


class IndicatorStrategy(StrategyBase):
    """
    설정 기반 인디케이터 전략.
    config 예시:
    {
        "universe": ["SPY", "QQQ"],
        "buy_conditions": {"sma200": true, "rsi_lt": 70, "momentum_gt": 0},
        "sell_conditions": {"rsi_gt": 80, "sma200_cross_below": true},
        "position_size_pct": 0.05
    }
    """

    def __init__(self, broker: "BrokerAdapter", tracker: "PositionTracker",
                 machine: "OrderStateMachine", name: str, config: dict):
        super().__init__(broker, name)
        self._tracker = tracker
        self._machine = machine
        self._config = config
        self._universe: list[str] = config.get("universe", ["SPY", "QQQ"])
        self._pos_size_pct: float = float(config.get("position_size_pct", 0.05))

    def on_start(self):
        logger.info("[%s] 인디케이터 전략 시작 (종목: %s)", self.name, self._universe)

    def on_market_open(self):
        logger.info("[%s] 장 시작 — 신호 스캔", self.name)
        self._scan_and_trade()

    def on_bar(self, bar: dict):
        symbol = bar.get("symbol")
        if not symbol:
            return
        pos = self._tracker.get_position(symbol)
        if pos:
            self._check_exit(symbol, bar["close"], pos.avg_price)

    def on_stop(self):
        logger.info("[%s] 전략 중단", self.name)

    def _scan_and_trade(self):
        from strategy.signals import MultiTimeframeSignals
        signals = MultiTimeframeSignals()
        positions_map = {p.symbol: p.avg_price for p in self._tracker.all_positions()}
        result = signals.scan_universe(positions_map)

        for symbol, reason in result.get("sell", []):
            self._execute_sell(symbol, reason)

        for symbol in result.get("buy", []):
            if not self._tracker.can_place_order(symbol):
                logger.debug("중복 주문 방지: %s", symbol)
                continue
            self._execute_buy(symbol)

    def _execute_buy(self, symbol: str):
        try:
            balance = self._broker.get_balance()
            capital = balance.total_eval_krw
            price = self._broker.get_price(symbol)
            amount_krw = capital * self._pos_size_pct
            qty = max(1, int(amount_krw / price))
            order = self.buy(symbol, qty, price)
            self._tracker.mark_pending(symbol, order.id)
            logger.info("[%s] 매수 실행: %s qty=%d @%.2f", self.name, symbol, qty, price)
        except Exception as e:
            logger.warning("[%s] 매수 실패 %s: %s", self.name, symbol, e)

    def _execute_sell(self, symbol: str, reason: str):
        pos = self._tracker.get_position(symbol)
        if pos is None:
            return
        try:
            price = self._broker.get_price(symbol)
            order = self.sell(symbol, pos.qty, price)
            self._tracker.mark_pending(symbol, order.id)
            logger.info("[%s] 매도 실행: %s qty=%d @%.2f (%s)", self.name, symbol, pos.qty, price, reason)
        except Exception as e:
            logger.warning("[%s] 매도 실패 %s: %s", self.name, symbol, e)

    def _check_exit(self, symbol: str, current_price: float, entry_price: float):
        stop_pct = float(self._config.get("stop_loss_pct", 0.07))
        if current_price <= entry_price * (1 - stop_pct):
            self._execute_sell(symbol, f"손절 {(current_price/entry_price - 1)*100:.1f}%")
