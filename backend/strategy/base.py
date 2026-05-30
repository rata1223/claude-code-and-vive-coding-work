import logging
import os
from abc import ABC
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.brokers.base import BrokerAdapter
    from backend.execution.position_tracker import Fill

logger = logging.getLogger(__name__)


def _live_trade_allowed(broker, name: str, symbol: str, side: str):
    """
    Returns (allowed: bool, rejected_order_or_None).
    Only enforced when broker.is_live is True (KISBroker).
    Skipped entirely for SimulatedBroker (backtests, dry-runs).
    """
    from backend.brokers.models import Order, OrderStatus
    if not getattr(broker, "is_live", True):
        return True, None

    # 1. SAFE_MODE gate (startup recovery must complete first)
    try:
        from backend.worker.recovery import SAFE_MODE
        if not SAFE_MODE.can_trade:
            logger.warning("[%s] SAFE_MODE — %s 차단: %s (%s)",
                           name, side, symbol, SAFE_MODE._reason)
            return False, Order(id="", symbol=symbol, side=side, qty=0,
                                price=0, status=OrderStatus.REJECTED)
    except ImportError:
        pass  # not running in worker context

    # 2. Shadow execution gate (ENABLE_LIVE_TRADING env var)
    if os.environ.get("ENABLE_LIVE_TRADING", "false").lower() != "true":
        logger.info("[SHADOW] %s %s — ENABLE_LIVE_TRADING=false (주문 미제출)", side, symbol)
        return False, Order(id="", symbol=symbol, side=side, qty=0,
                            price=0, status=OrderStatus.REJECTED)

    return True, None


_BAR_STALE_SECONDS = int(os.environ.get("BAR_STALE_SECONDS", "600"))


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

    def _is_bar_stale(self, bar: dict) -> bool:
        """Returns True if bar['ts'] exceeds BAR_STALE_SECONDS age on a live broker.
        Always returns False for simulated brokers (backtests use historical timestamps)."""
        if not getattr(self._broker, "is_live", True):
            return False
        ts = bar.get("ts")
        if ts is None:
            return False
        try:
            now = datetime.now(timezone.utc)
            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_secs = (now - ts).total_seconds()
            if age_secs > _BAR_STALE_SECONDS:
                logger.warning(
                    "[%s] 스테일 캔들 무시: %s age=%.0fs (한도 %ds)",
                    self.name, bar.get("symbol"), age_secs, _BAR_STALE_SECONDS,
                )
                return True
        except Exception as e:
            logger.debug("캔들 타임스탬프 파싱 오류: %s", e)
        return False

    def on_fill(self, fill: "Fill"):
        """체결 이벤트 수신 시 호출."""

    # ── 매매 편의 메서드 ─────────────────────────────────────────────────
    def buy(self, symbol: str, qty: int, price: Optional[float] = None, order_type: str = "limit"):
        from backend.brokers.models import Order, OrderStatus
        from backend.brokers.validator import BrokerCapabilityValidator, OrderRequest, UnsupportedCapabilityError
        allowed, rejected = _live_trade_allowed(self._broker, self.name, symbol, "buy")
        if not allowed:
            return rejected
        if price is None:
            price = self._broker.get_price(symbol)
            order_type = "market"
        try:
            req = BrokerCapabilityValidator(self._broker.capabilities).validate(
                OrderRequest(symbol=symbol, side="buy", qty=float(qty),
                             price=price, order_type=order_type)
            )
            price = req.price
            order_type = req.order_type
        except UnsupportedCapabilityError as e:
            logger.warning("[%s] 매수 차단 — %s", self.name, e)
            return Order(id="", symbol=symbol, side="buy", qty=qty,
                         price=price or 0.0, status=OrderStatus.REJECTED)
        logger.info("[%s] 매수 요청: %s qty=%d price=%.4f", self.name, symbol, qty, price)
        return self._broker.place_order(symbol, "buy", qty, price, order_type)

    def sell(self, symbol: str, qty: int, price: Optional[float] = None, order_type: str = "limit"):
        from backend.brokers.models import Order, OrderStatus
        from backend.brokers.validator import BrokerCapabilityValidator, OrderRequest, UnsupportedCapabilityError
        allowed, rejected = _live_trade_allowed(self._broker, self.name, symbol, "sell")
        if not allowed:
            return rejected
        if price is None:
            price = self._broker.get_price(symbol)
            order_type = "market"
        try:
            req = BrokerCapabilityValidator(self._broker.capabilities).validate(
                OrderRequest(symbol=symbol, side="sell", qty=float(qty),
                             price=price, order_type=order_type)
            )
            price = req.price
            order_type = req.order_type
        except UnsupportedCapabilityError as e:
            logger.warning("[%s] 매도 차단 — %s", self.name, e)
            return Order(id="", symbol=symbol, side="sell", qty=qty,
                         price=price or 0.0, status=OrderStatus.REJECTED)
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
