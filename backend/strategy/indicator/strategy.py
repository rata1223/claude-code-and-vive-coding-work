import logging
from typing import TYPE_CHECKING, Callable, Optional

from backend.brokers.models import OrderStatus
from backend.execution.circuit_breaker import ConsecutiveFailureBreaker
from backend.strategy.base import StrategyBase

if TYPE_CHECKING:
    from backend.brokers.base import BrokerAdapter
    from backend.execution.order_machine import OrderStateMachine
    from backend.execution.order_poller import OrderFillPoller
    from backend.execution.position_tracker import PositionTracker

logger = logging.getLogger(__name__)

MIN_POSITION_VALUE_KRW = 100_000  # skip orders below 100K KRW to avoid over-fragmentation


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

    def __init__(
        self,
        broker: "BrokerAdapter",
        tracker: "PositionTracker",
        machine: "OrderStateMachine",
        name: str,
        config: dict,
        poller: Optional["OrderFillPoller"] = None,
        on_filled_cb: Optional[Callable] = None,
        on_timeout_cb: Optional[Callable] = None,
    ):
        super().__init__(broker, name)
        self._tracker = tracker
        self._machine = machine
        self._config = config
        self._universe: list[str] = config.get("universe", ["SPY", "QQQ"])
        self._pos_size_pct: float = float(config.get("position_size_pct", 0.05))
        self._poller = poller
        self._on_filled_cb = on_filled_cb
        self._on_timeout_cb = on_timeout_cb or self._default_timeout_handler
        self._breaker = ConsecutiveFailureBreaker(threshold=3, cooldown_minutes=30)

    def on_start(self):
        logger.info("[%s] 인디케이터 전략 시작 (종목: %s)", self.name, self._universe)

    def on_market_open(self):
        logger.info("[%s] 장 시작 — 신호 스캔", self.name)
        self._scan_and_trade()

    def on_bar(self, bar: dict):
        if self._is_bar_stale(bar):
            return
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

        try:
            capital = self._broker.get_balance().total_eval_krw
        except Exception as e:
            logger.warning("[%s] 잔고 조회 실패: %s — 매수 스킵", self.name, e)
            return

        active_universe = self._capital_aware_universe(capital)
        for symbol in result.get("buy", []):
            if symbol not in active_universe:
                continue
            if not self._tracker.can_place_order(symbol):
                logger.debug("중복 주문 방지: %s", symbol)
                continue
            self._execute_buy(symbol, capital)

    def _capital_aware_universe(self, capital: float) -> list[str]:
        """Reduce scan universe for small capital to avoid over-fragmentation."""
        full = self._universe
        if capital < 3_000_000:
            return full[:3]
        if capital < 10_000_000:
            return full[:8]
        return full

    def _execute_buy(self, symbol: str, capital: Optional[float] = None):
        if self._breaker.is_open():
            return
        try:
            if capital is None:
                capital = self._broker.get_balance().total_eval_krw
            price = self._broker.get_price(symbol)
            if price <= 0:
                logger.warning("[%s] 가격 0 — 매수 스킵: %s", self.name, symbol)
                return
            amount_krw = capital * self._pos_size_pct
            if amount_krw < MIN_POSITION_VALUE_KRW:
                logger.info("[%s] 포지션 최소금액 미달 — 스킵: %s (%.0f원)", self.name, symbol, amount_krw)
                return
            qty = int(amount_krw / price)
            if qty <= 0:
                logger.info("[%s] 매수 수량 0 — 자본 부족 스킵: %s", self.name, symbol)
                return
            order = self.buy(symbol, qty, price)
            if order and order.status != OrderStatus.REJECTED:
                self._breaker.record_success()
            else:
                self._breaker.record_failure()
            self._register_order(order, symbol)
            logger.info("[%s] 매수 실행: %s qty=%d @%.2f", self.name, symbol, qty, price)
        except Exception as e:
            self._breaker.record_failure()
            logger.warning("[%s] 매수 실패 %s: %s", self.name, symbol, e)

    def _execute_sell(self, symbol: str, reason: str):
        if self._breaker.is_open():
            return
        pos = self._tracker.get_position(symbol)
        if pos is None:
            return
        try:
            price = self._broker.get_price(symbol)
            order = self.sell(symbol, pos.qty, price)
            if order and order.status != OrderStatus.REJECTED:
                self._breaker.record_success()
            else:
                self._breaker.record_failure()
            self._register_order(order, symbol)
            logger.info("[%s] 매도 실행: %s qty=%d @%.2f (%s)", self.name, symbol, pos.qty, price, reason)
        except Exception as e:
            self._breaker.record_failure()
            logger.warning("[%s] 매도 실패 %s: %s", self.name, symbol, e)

    def _register_order(self, order, symbol: str):
        """Wire order into machine + poller + pending lock after placement."""
        if order is None or order.status == OrderStatus.REJECTED or not order.id:
            return
        try:
            self._machine.register(order)
        except Exception as e:
            logger.debug("machine.register 스킵 (이미 등록): %s", e)
        self._tracker.mark_pending(symbol, order.id)
        if self._poller is not None and self._on_filled_cb is not None:
            self._poller.register(
                order,
                on_filled=self._on_filled_cb,
                on_timeout=self._on_timeout_cb,
            )

    def _check_exit(self, symbol: str, current_price: float, entry_price: float):
        stop_pct = float(self._config.get("stop_loss_pct", 0.07))
        if current_price <= entry_price * (1 - stop_pct):
            self._execute_sell(symbol, f"손절 {(current_price/entry_price - 1)*100:.1f}%")

    def _default_timeout_handler(self, order):
        logger.error("[%s] 주문 타임아웃 — 수동 취소 필요: %s %s %s",
                     self.name, order.id, order.side, order.symbol)
        self._tracker.unmark_pending(order.symbol)
