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
        from backend.quant.signals.fusion import default_fusion
        from backend.quant.data.loader import DataLoader

        loader = DataLoader()
        fusion = default_fusion()

        current_held = {p.symbol for p in self._tracker.all_positions()}
        symbols_to_scan = list(dict.fromkeys(list(self._universe) + list(current_held)))

        try:
            capital = self._broker.get_balance().total_eval_krw
        except Exception as e:
            logger.warning("[%s] 잔고 조회 실패: %s — 매수 스킵", self.name, e)
            capital = None

        active_universe = self._capital_aware_universe(capital or 0.0)

        sell_candidates: list[tuple[str, str]] = []
        buy_candidates: list[str] = []

        for symbol in symbols_to_scan:
            try:
                df = loader.fetch(symbol, period="1y")
            except Exception as e:
                logger.warning("[%s] OHLCV 로드 실패 %s: %s", self.name, symbol, e)
                continue
            if df is None or len(df) < 50:
                logger.warning("[%s] OHLCV 데이터 부족 스킵: %s", self.name, symbol)
                continue
            # Staleness gate: skip symbol if last candle is more than 3 calendar days old
            try:
                from datetime import datetime, timezone as _tz
                last_idx = df.index[-1]
                if hasattr(last_idx, "to_pydatetime"):
                    last_dt = last_idx.to_pydatetime()
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=_tz.utc)
                else:
                    last_dt = datetime.combine(last_idx, datetime.min.time()).replace(tzinfo=_tz.utc)
                age_days = (datetime.now(_tz.utc) - last_dt).days
                if age_days > 3:
                    logger.warning("[%s] OHLCV 오래됨 스킵: %s (최종봉 %d일 전)", self.name, symbol, age_days)
                    continue
            except Exception:
                pass  # staleness check failure is non-fatal
            try:
                result = fusion.evaluate(df, symbol=symbol)
            except Exception as e:
                logger.warning("[%s] 신호 계산 실패 %s: %s", self.name, symbol, e)
                continue

            if result.signal == -1 and symbol in current_held:
                sell_candidates.append((symbol, f"매도신호 score={result.score:.3f}"))
            elif result.signal == 1 and symbol in active_universe:
                buy_candidates.append(symbol)

        for symbol, reason in sell_candidates:
            self._execute_sell(symbol, reason)

        if capital is None:
            return

        for symbol in buy_candidates:
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
        # Atomically claim pending lock before broker call — prevents duplicate orders
        if not self._tracker.try_mark_pending(symbol):
            logger.debug("[%s] 중복 주문 방지: %s", self.name, symbol)
            return
        try:
            if capital is None:
                capital = self._broker.get_balance().total_eval_krw
            price = self._broker.get_price(symbol)
            if price <= 0:
                logger.warning("[%s] 가격 0 — 매수 스킵: %s", self.name, symbol)
                self._tracker.unmark_pending(symbol)
                return
            amount_krw = capital * self._pos_size_pct
            if amount_krw < MIN_POSITION_VALUE_KRW:
                logger.info("[%s] 포지션 최소금액 미달 — 스킵: %s (%.0f원)", self.name, symbol, amount_krw)
                self._tracker.unmark_pending(symbol)
                return
            qty = int(amount_krw / price)
            if qty <= 0:
                logger.info("[%s] 매수 수량 0 — 자본 부족 스킵: %s", self.name, symbol)
                self._tracker.unmark_pending(symbol)
                return
            order = self.buy(symbol, qty, price)
            if order and order.status != OrderStatus.REJECTED:
                self._breaker.record_success()
                self._register_order(order, symbol)
                logger.info("[%s] 매수 실행: %s qty=%d @%.2f", self.name, symbol, qty, price)
            else:
                self._breaker.record_failure()
                self._tracker.unmark_pending(symbol)
        except Exception as e:
            self._breaker.record_failure()
            self._tracker.unmark_pending(symbol)
            logger.warning("[%s] 매수 실패 %s: %s", self.name, symbol, e)

    def _execute_sell(self, symbol: str, reason: str):
        if self._breaker.is_open():
            return
        pos = self._tracker.get_position(symbol)
        if pos is None:
            return
        # Atomically claim pending lock before broker call
        if not self._tracker.try_mark_pending(symbol):
            logger.debug("[%s] 매도 중복 주문 방지: %s", self.name, symbol)
            return
        try:
            price = self._broker.get_price(symbol)
            order = self.sell(symbol, pos.qty, price)
            if order and order.status != OrderStatus.REJECTED:
                self._breaker.record_success()
                self._register_order(order, symbol)
                logger.info("[%s] 매도 실행: %s qty=%d @%.2f (%s)", self.name, symbol, pos.qty, price, reason)
            else:
                self._breaker.record_failure()
                self._tracker.unmark_pending(symbol)
        except Exception as e:
            self._breaker.record_failure()
            self._tracker.unmark_pending(symbol)
            logger.warning("[%s] 매도 실패 %s: %s", self.name, symbol, e)

    def _register_order(self, order, symbol: str):
        """Wire order into machine + poller after placement. Pending lock already set by caller."""
        if order is None or order.status == OrderStatus.REJECTED or not order.id:
            return
        try:
            self._machine.register(order)
        except Exception as e:
            logger.debug("machine.register 스킵 (이미 등록): %s", e)
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
        logger.warning("[%s] 주문 타임아웃 — 브로커 취소 시도: %s %s %s",
                       self.name, order.id, order.side, order.symbol)
        try:
            cancelled = self._broker.cancel_order(
                order_id=order.id,
                symbol=order.symbol,
                qty=order.qty,
                price=float(order.price or 0),
            )
            if not cancelled:
                logger.error("[%s] 타임아웃 취소 실패 — 미추적 포지션 위험: %s", self.name, order.id)
        except Exception as e:
            logger.error("[%s] 타임아웃 취소 예외 %s: %s", self.name, order.id, e)
        finally:
            self._tracker.unmark_pending(order.symbol)
