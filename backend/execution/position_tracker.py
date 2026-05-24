import logging
from dataclasses import dataclass
from typing import Optional

from backend.brokers.models import Position
from backend.execution.order_machine import OrderStateMachine

logger = logging.getLogger(__name__)


# Fill 데이터클래스가 brokers/models.py에 없으므로 여기서 정의
@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str
    qty: int
    price: float
    market: str  # KR/US


class PositionTracker:
    """
    체결 이벤트를 받아 인메모리 포지션을 유지.
    재시작 시 DB에서 포지션을 복원하는 restore_positions() 사용.
    중복 주문 방지: 동일 symbol의 활성 주문이 있으면 place_order를 거부.
    """

    def __init__(self, machine: OrderStateMachine):
        self._machine = machine
        self._positions: dict[str, Position] = {}  # symbol → Position
        self._pending_symbols: set[str] = set()    # 활성 주문 중인 symbol

    # ── 포지션 조회 ────────────────────────────────────────────────────────
    def get_position(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def all_positions(self) -> list[Position]:
        return list(self._positions.values())

    # ── 중복 주문 방지 ────────────────────────────────────────────────────
    def can_place_order(self, symbol: str) -> bool:
        return symbol not in self._pending_symbols

    def mark_pending(self, symbol: str, order_id: str):
        self._pending_symbols.add(symbol)
        logger.debug("pending 등록: %s (order_id=%s)", symbol, order_id)

    def unmark_pending(self, symbol: str):
        self._pending_symbols.discard(symbol)

    # ── 체결 처리 ─────────────────────────────────────────────────────────
    def on_fill(self, fill: Fill):
        """체결 이벤트 수신 → 포지션 업데이트."""
        symbol = fill.symbol
        self.unmark_pending(symbol)

        pos = self._positions.get(symbol)

        if fill.side == "buy":
            if pos is None:
                self._positions[symbol] = Position(
                    symbol=symbol,
                    qty=fill.qty,
                    avg_price=fill.price,
                    market=fill.market,
                    current_price=fill.price,
                )
            else:
                total_qty = pos.qty + fill.qty
                pos.avg_price = (pos.avg_price * pos.qty + fill.price * fill.qty) / total_qty
                pos.qty = total_qty
                pos.current_price = fill.price
            logger.info("매수 체결 반영: %s qty=%d avg=%.4f", symbol, fill.qty, fill.price)

        elif fill.side == "sell":
            if pos is None:
                logger.warning("매도 체결이지만 포지션 없음: %s", symbol)
                return
            pos.qty -= fill.qty
            pos.current_price = fill.price
            if pos.qty <= 0:
                del self._positions[symbol]
                logger.info("포지션 청산: %s", symbol)
            else:
                logger.info("매도 체결 반영: %s 잔여 qty=%d", symbol, pos.qty)

    # ── DB 복원 ──────────────────────────────────────────────────────────
    def restore_positions(self, positions: list[Position]):
        """재시작 시 DB에서 읽어온 포지션으로 인메모리 상태 초기화."""
        self._positions.clear()
        for p in positions:
            self._positions[p.symbol] = p
        logger.info("포지션 복원 완료: %d개", len(positions))

    # ── 현재가 업데이트 ──────────────────────────────────────────────────
    def update_prices(self, prices: dict[str, float]):
        """symbol→price 맵으로 current_price 일괄 갱신."""
        for symbol, price in prices.items():
            if symbol in self._positions:
                self._positions[symbol].current_price = price
