import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from backend.brokers.models import Position
from backend.execution.order_machine import OrderStateMachine

logger = logging.getLogger(__name__)

_PENDING_LOCK_TTL = 1800  # 30 minutes — auto-release stale locks

#: ``claim_ca_exit`` reason when the symbol's pending lock is already held.
CA_EXIT_PENDING = "pending"


# Fill 데이터클래스가 brokers/models.py에 없으므로 여기서 정의
@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str
    qty: int
    price: float
    market: str  # KR/US



def _with_sellable(pos):
    """Stamp ``sellable_qty`` on a tracked position (P0-07 S2).

    The tracker models no settlement or resting-order reservation, so every
    tracked share is orderable. Applied at the read boundary because ``qty`` is
    mutated in place as fills arrive — a value stamped at construction would go
    stale. Callers must never see ``sellable_qty=None`` from here, since that
    means "the broker did not say" and fails closed.
    """
    from dataclasses import replace
    if pos is None:
        return None
    return replace(pos, sellable_qty=pos.qty)

class PositionTracker:
    """
    체결 이벤트를 받아 인메모리 포지션을 유지.
    재시작 시 DB에서 포지션을 복원하는 restore_positions() 사용.
    중복 주문 방지: 동일 symbol의 활성 주문이 있으면 place_order를 거부.
    Thread-safe: RLock guards all mutations of _positions and _pending_symbols.
    """

    def __init__(self, machine: OrderStateMachine, corporate_action_runtime=None):
        self._machine = machine
        self._positions: dict[str, Position] = {}  # symbol → Position
        self._pending_symbols: dict[str, float] = {}  # symbol → lock_timestamp
        self._lock = threading.RLock()
        # P2-02C: optional CorporateActionRuntime. When a symbol has a blocking
        # corporate action (pending/UNKNOWN), order entry is refused (fail-closed).
        # None → behavior unchanged. The tracker NEVER adjusts positions itself.
        self._ca = corporate_action_runtime

    # ── 포지션 조회 ────────────────────────────────────────────────────────
    def get_position(self, symbol: str) -> Optional[Position]:
        with self._lock:
            return _with_sellable(self._positions.get(symbol))

    def all_positions(self) -> list[Position]:
        with self._lock:
            return [_with_sellable(p) for p in self._positions.values()]

    # ── 중복 주문 방지 ────────────────────────────────────────────────────
    def can_place_order(self, symbol: str) -> bool:
        if self._ca_blocked(symbol):
            return False
        with self._lock:
            ts = self._pending_symbols.get(symbol)
            if ts is None:
                return True
            if time.monotonic() - ts > _PENDING_LOCK_TTL:
                logger.warning("pending 락 자동 해제 (TTL 초과): %s", symbol)
                del self._pending_symbols[symbol]
                return True
            return False

    def try_mark_pending(self, symbol: str) -> bool:
        """Atomically check and set pending lock. Returns False if already locked (race-safe)
        or if a corporate action is blocking the symbol (fail-closed, P2-02C)."""
        if self._ca_blocked(symbol):
            return False
        with self._lock:
            ts = self._pending_symbols.get(symbol)
            if ts is not None and time.monotonic() - ts <= _PENDING_LOCK_TTL:
                return False
            self._pending_symbols[symbol] = time.monotonic()
            return True

    def is_ca_blocked(self, symbol: str) -> bool:
        """True if a corporate action gates this symbol (fail-closed on a
        gate-check error)."""
        return self._ca_blocked(symbol)

    def claim_ca_exit(self, symbol: str, get_positions: Callable[[], Iterable]) -> tuple[Optional[int], str]:
        """Claim the pending lock for an exit on a corporate-action-gated symbol
        and return ``(qty_to_sell, "")``, or ``(None, reason)`` with no lock held.

        The gate stops new risk, not its reduction — but while it holds, this
        tracker's qty/avg may be wrong (an unrecorded 2:1 split doubles the
        broker qty and halves the price). So the exit is sized from a live
        broker lookup: the broker's sellable qty. The broker's held qty and avg
        (KIS adjusts it for splits) are adopted into the tracker before the
        order goes out, so the fill's realized P&L is measured against the
        broker's cost basis and partial fills reduce the right quantity. A
        failed lookup, no broker position, an unknown or zero sellable figure,
        or an invalid avg refuses the exit (fail-closed).

        This is the only way past the gate: there is no flag that skips it
        without the broker sizing."""
        from backend.risk.halt_policy import is_valid_execution_price
        from backend.risk.sellable_qty import sellable_from_position
        try:
            positions = get_positions()
        except Exception as e:  # noqa: BLE001 - no lookup, no exit
            return None, f"브로커 포지션 조회 실패: {e}"
        match = next((p for p in positions or [] if getattr(p, "symbol", None) == symbol), None)
        if match is None:
            return None, "브로커 보유 없음"
        sellable = sellable_from_position(match)
        if not sellable.known or sellable.qty <= 0:
            return None, f"매도가능수량 없음 ({sellable.reason})"
        if not is_valid_execution_price(match.avg_price):
            return None, f"브로커 평단 이상 ({match.avg_price!r})"
        with self._lock:
            ts = self._pending_symbols.get(symbol)
            if ts is not None and time.monotonic() - ts <= _PENDING_LOCK_TTL:
                return None, CA_EXIT_PENDING
            self._pending_symbols[symbol] = time.monotonic()
            prev = self._positions.get(symbol)
            self._positions[symbol] = Position(
                symbol=symbol,
                qty=int(match.qty),
                avg_price=float(match.avg_price),
                market=match.market,
                current_price=prev.current_price if prev else 0.0,
            )
        logger.warning("기업행위 차단 중 청산 — 브로커 값 채택: %s 보유 %d(추적 %s) 평단 %.4f, 매도 %d",
                       symbol, int(match.qty), prev.qty if prev else None,
                       float(match.avg_price), sellable.qty)
        return sellable.qty, ""

    def _ca_blocked(self, symbol: str) -> bool:
        """True if a corporate action gates this symbol. When no CA runtime is
        attached, never blocks. When the gate check itself errors we **fail
        closed** — block the order — because an unverifiable gate must not
        silently enable trading on a symbol that may have an open corporate
        action."""
        if self._ca is None:
            return False
        try:
            if self._ca.is_blocked(symbol):
                logger.warning("주문 차단 — 기업행위 대기 중: %s", symbol)
                return True
            return False
        except Exception as exc:  # noqa: BLE001 - fail closed on any gate-check error
            logger.warning("기업행위 게이트 확인 오류 — 안전을 위해 주문 차단 (%s): %s", symbol, exc)
            return True

    def mark_pending(self, symbol: str, order_id: str):
        with self._lock:
            self._pending_symbols[symbol] = time.monotonic()
            logger.debug("pending 등록: %s (order_id=%s)", symbol, order_id)

    def unmark_pending(self, symbol: str):
        with self._lock:
            self._pending_symbols.pop(symbol, None)

    # ── 체결 처리 ─────────────────────────────────────────────────────────
    def on_fill(self, fill: Fill):
        """체결 이벤트 수신 → 포지션 업데이트."""
        with self._lock:
            symbol = fill.symbol
            # Inline unmark_pending — already hold lock, avoid re-acquire overhead
            self._pending_symbols.pop(symbol, None)

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
        with self._lock:
            self._positions.clear()
            for p in positions:
                self._positions[p.symbol] = p
            logger.info("포지션 복원 완료: %d개", len(positions))

    # ── 현재가 업데이트 ──────────────────────────────────────────────────
    def update_prices(self, prices: dict[str, float]):
        """symbol→price 맵으로 current_price 일괄 갱신."""
        with self._lock:
            for symbol, price in prices.items():
                if symbol in self._positions:
                    self._positions[symbol].current_price = price
