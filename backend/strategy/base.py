import logging
import os
from abc import ABC
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.brokers.base import BrokerAdapter
    from backend.execution.position_tracker import Fill

logger = logging.getLogger(__name__)


def _live_trade_allowed(broker, name: str, symbol: str, side: str,
                        qty=None, price=None):
    """
    Returns (allowed: bool, rejected_order_or_None).
    Only enforced when broker.is_live is True (KISBroker).
    Skipped entirely for SimulatedBroker (backtests, dry-runs).

    P0-07 S1 (Policy B): a halt blocks the creation of new risk but not its
    reduction. A buy is always ENTRY. A sell counts as EXIT only when a *live*
    position lookup proves it reduces an existing long (0 < qty <= held_qty);
    otherwise it is treated as ENTRY and blocked (R1). Under UNTRUSTED_STATE
    even a proven exit is blocked, because the position data behind the proof
    is exactly what cannot be trusted. Any failure while evaluating the gate
    fails closed (R3).
    """
    from backend.brokers.models import Order, OrderStatus
    from backend.risk.halt_policy import (
        HaltCause, OperationClass, is_allowed, is_valid_execution_price, prove_exit,
    )

    def _reject():
        return False, Order(id="", symbol=symbol, side=side, qty=0,
                            price=0, status=OrderStatus.REJECTED)

    if not getattr(broker, "is_live", True):
        return True, None

    # 1. SAFE_MODE gate (startup recovery must complete first)
    try:
        from backend.worker.recovery import SAFE_MODE
        cause = SAFE_MODE.halt_cause
    except ImportError:
        cause = None  # not running in worker context

    if cause is not None:
        # Classify the operation. Only a proven risk-reducing sell is EXIT.
        op = OperationClass.ENTRY
        proof_reason = ""
        if side == "sell":
            proven, proof_reason = prove_exit(
                getattr(broker, "get_positions", None) or (lambda: []), symbol, qty
            )
            if proven:
                op = OperationClass.EXIT

        if not is_allowed(cause, op):
            logger.warning("[%s] SAFE_MODE[%s] — %s %s 차단 (%s)%s",
                           name, cause.value, side, symbol, op.value,
                           f": {proof_reason}" if proof_reason else "")
            return _reject()

        # A stale feed may still be exited, but only at a validated live price
        # (P0-07 G2 rules) — never at a stale, missing or fabricated one.
        if cause is HaltCause.DEGRADED_FEED and op is OperationClass.EXIT:
            resolved = price
            if resolved is None:
                try:
                    resolved = broker.get_price(symbol)
                except Exception as e:  # noqa: BLE001 - no quote, no order
                    logger.warning("[%s] DEGRADED_FEED — %s 시세 조회 실패, 매도 차단: %s",
                                   name, symbol, e)
                    return _reject()
            if not is_valid_execution_price(resolved):
                logger.warning("[%s] DEGRADED_FEED — %s 유효 실행가 없음(%r), 매도 차단",
                               name, symbol, resolved)
                return _reject()

        logger.warning("[%s] SAFE_MODE[%s] — %s %s 허용 (위험 감소)",
                       name, cause.value, side, symbol)

    # 2. Shadow execution gate (ENABLE_LIVE_TRADING env var)
    if os.environ.get("ENABLE_LIVE_TRADING", "false").lower() != "true":
        logger.info("[SHADOW] %s %s — ENABLE_LIVE_TRADING=false (주문 미제출)", side, symbol)
        return False, Order(id="", symbol=symbol, side=side, qty=0,
                            price=0, status=OrderStatus.REJECTED)

    return True, None


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
        """Returns True if bar['ts'] exceeds the intraday-bar freshness threshold
        on a live broker. Always returns False for simulated brokers (backtests
        use historical timestamps).

        Delegates to the unified FreshnessGate (R-11) so the threshold lives in
        one place (backend/data/freshness_config.py), not a local constant."""
        if not getattr(self._broker, "is_live", True):
            return False
        # A missing ts is NOT treated as fresh — the gate resolves it to UNKNOWN
        # (fail-closed) so a timestamp-less live bar is skipped.
        from backend.data.freshness_gate import get_freshness_gate
        from backend.data.freshness_config import FreshnessTier
        gate = get_freshness_gate()
        result = gate.validate_timestamp(
            bar.get("symbol", "unknown"), bar.get("ts"),
            tier=FreshnessTier.INTRADAY_BAR,
            source="live_bar", raise_on_block=False,
        )
        return gate.is_blocking(result)

    def on_fill(self, fill: "Fill"):
        """체결 이벤트 수신 시 호출."""

    # ── 매매 편의 메서드 ─────────────────────────────────────────────────
    def buy(self, symbol: str, qty: int, price: Optional[float] = None, order_type: str = "limit"):
        from backend.brokers.models import Order, OrderStatus
        from backend.brokers.validator import BrokerCapabilityValidator, OrderRequest, UnsupportedCapabilityError
        allowed, rejected = _live_trade_allowed(self._broker, self.name, symbol, "buy",
                                                qty=qty, price=price)
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
        allowed, rejected = _live_trade_allowed(self._broker, self.name, symbol, "sell",
                                                qty=qty, price=price)
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
