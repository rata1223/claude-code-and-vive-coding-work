"""
주문 체결 폴링 엔진.

KIS API에는 실시간 체결 푸시가 없으므로 주문 제출 후 주기적으로
get_order_status()를 호출해 체결 여부를 확인한다.

백오프 스케줄: 10s → 30s → 60s → 120s → 300s (이후 300s 고정)
타임아웃: 30분 후 미체결 주문은 자동 취소 시도 후 콜백 호출.
"""
import dataclasses
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from backend.brokers.base import BrokerAdapter
from backend.brokers.models import Order, OrderStatus

logger = logging.getLogger(__name__)

_POLL_INTERVALS = [10, 30, 60, 120, 300]
_TIMEOUT_MINUTES = 30

# Valid status transitions the poller may observe on the wire.
# Defined locally to avoid importing from order_machine (circular-import risk).
# Unexpected transitions are logged as warnings but never block processing —
# the OrderStateMachine enforces hard validation at callback time.
_VALID_POLLER_TRANSITIONS: dict[OrderStatus, frozenset] = {
    OrderStatus.PENDING:        frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED,
                                           OrderStatus.CANCELED}),
    OrderStatus.SUBMITTED:      frozenset({OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED,
                                           OrderStatus.CANCELED, OrderStatus.REJECTED,
                                           OrderStatus.EXPIRED, OrderStatus.UNKNOWN}),
    OrderStatus.PARTIAL_FILLED: frozenset({OrderStatus.FILLED, OrderStatus.CANCELED,
                                           OrderStatus.EXPIRED, OrderStatus.UNKNOWN}),
    OrderStatus.UNKNOWN:        frozenset({OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED,
                                           OrderStatus.FILLED, OrderStatus.CANCELED,
                                           OrderStatus.REJECTED, OrderStatus.EXPIRED}),
}


@dataclass
class _PollEntry:
    order: Order
    on_filled: Callable[[Order], None]
    on_timeout: Callable[[Order], None]
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    poll_index: int = 0
    next_poll_at: float = field(default_factory=time.monotonic)
    last_reported_qty: int = 0  # tracks cumulative filled qty to prevent double-counting

    @property
    def is_timed_out(self) -> bool:
        elapsed = datetime.now(timezone.utc) - self.registered_at
        return elapsed > timedelta(minutes=_TIMEOUT_MINUTES)

    def advance(self) -> float:
        """Advance poll schedule; return seconds until next poll."""
        idx = min(self.poll_index, len(_POLL_INTERVALS) - 1)
        wait = _POLL_INTERVALS[idx]
        self.poll_index += 1
        self.next_poll_at = time.monotonic() + wait
        return wait


class OrderFillPoller:
    """
    백그라운드 스레드에서 pending 주문들을 주기적으로 폴링.

    사용 예:
        poller = OrderFillPoller(broker)
        poller.start()
        poller.register(order, on_filled=my_fill_handler, on_timeout=my_timeout_handler)
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        db_factory: Optional[Callable] = None,
        semantic_mapper=None,  # Optional[BrokerSemanticMapper] — avoids circular import
    ):
        self._broker = broker
        self._db_factory = db_factory
        self._semantic_mapper = semantic_mapper
        self._entries: dict[str, _PollEntry] = {}  # order_id → entry
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="order-poller")
        self._thread.start()
        logger.info("OrderFillPoller 시작")

    def stop(self) -> None:
        self._stop.set()

    def register(
        self,
        order: Order,
        on_filled: Callable[[Order], None],
        on_timeout: Optional[Callable[[Order], None]] = None,
    ) -> None:
        if not order.id:
            logger.warning("주문 ID 없음 — 폴링 등록 스킵: %s %s", order.side, order.symbol)
            return
        entry = _PollEntry(
            order=order,
            on_filled=on_filled,
            on_timeout=on_timeout or self._default_timeout_handler,
        )
        with self._lock:
            self._entries[order.id] = entry
        logger.info("폴링 등록: %s %s %s", order.id, order.side, order.symbol)

    def unregister(self, order_id: str) -> None:
        with self._lock:
            self._entries.pop(order_id, None)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._entries)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                due = [e for e in self._entries.values() if e.next_poll_at <= now]

            for entry in due:
                if entry.is_timed_out:
                    self._handle_timeout(entry)
                    continue
                self._poll_one(entry)

            self._stop.wait(timeout=5)

    def _poll_one(self, entry: _PollEntry) -> None:
        try:
            updated = self._broker.get_order_status(entry.order.id, entry.order.symbol)
        except Exception as e:
            logger.warning("폴링 실패 %s: %s", entry.order.id, e)
            entry.advance()
            return

        if updated is None:
            entry.advance()
            return

        # Transition validation — warn on unexpected broker status regression.
        prev_status = entry.order.status
        if updated.status != prev_status:
            valid = _VALID_POLLER_TRANSITIONS.get(prev_status, frozenset())
            if updated.status not in valid:
                logger.warning(
                    "예상치 못한 상태 전환 %s → %s (order=%s)",
                    prev_status.value, updated.status.value, updated.id,
                )

        entry.order = updated

        if updated.status == OrderStatus.FILLED:
            incremental = (updated.filled_qty or 0) - entry.last_reported_qty
            entry.last_reported_qty = updated.filled_qty
            logger.info("체결 확인 %s: %s qty=%d (증분=%d) avg=%.4f",
                        updated.id, updated.symbol, updated.filled_qty,
                        incremental, updated.avg_fill_price or 0)
            with self._lock:
                self._entries.pop(updated.id, None)
            if incremental > 0:
                self._audit("poller_filled", updated,
                            {"incremental": incremental, "total": updated.filled_qty,
                             "avg": updated.avg_fill_price or 0})
                # Pass a copy with filled_qty=incremental so the callback records
                # only the new quantity — same convention as PARTIAL_FILLED.
                final_fill = dataclasses.replace(updated, filled_qty=incremental)
                try:
                    entry.on_filled(final_fill)
                except Exception as e:
                    logger.error("on_filled 콜백 오류: %s", e)
            else:
                logger.warning("체결 중복 감지 — 콜백 스킵: %s", updated.id)

        elif updated.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            logger.warning("주문 취소/거부/만료: %s status=%s", updated.id, updated.status.value)
            with self._lock:
                self._entries.pop(updated.id, None)
            self._audit(f"poller_{updated.status.value}", updated,
                        {"prev_status": prev_status.value,
                         "partial_qty": entry.last_reported_qty})

        elif updated.status == OrderStatus.PARTIAL_FILLED:
            incremental = (updated.filled_qty or 0) - entry.last_reported_qty
            if incremental > 0:
                entry.last_reported_qty = updated.filled_qty
                logger.info("부분체결 %s: 증분=%d (누적=%d/%d)",
                            updated.id, incremental, updated.filled_qty, updated.qty)
                # Pass a copy with filled_qty=incremental so the callback records
                # only the new quantity without double-counting prior partials.
                partial = dataclasses.replace(updated, filled_qty=incremental)
                self._audit("poller_partial_filled", updated,
                            {"incremental": incremental, "cumulative": updated.filled_qty})
                try:
                    entry.on_filled(partial)
                except Exception as e:
                    logger.error("on_filled 콜백 오류 (부분체결): %s", e)
            entry.advance()

        else:
            entry.advance()

    def _handle_timeout(self, entry: _PollEntry) -> None:
        logger.warning("주문 타임아웃 (%dm): %s %s %s",
                       _TIMEOUT_MINUTES, entry.order.id, entry.order.side, entry.order.symbol)
        with self._lock:
            self._entries.pop(entry.order.id, None)

        # Auto-cancel via broker using market-appropriate kwargs.
        if self._semantic_mapper is not None:
            try:
                kwargs = self._semantic_mapper.cancel_kwargs(entry.order)
                self._broker.cancel_order(**kwargs)
                logger.info("타임아웃 자동취소 성공: %s", entry.order.id)
            except Exception as e:
                logger.warning("타임아웃 자동취소 실패 %s: %s", entry.order.id, e)

        self._audit("poller_timeout", entry.order,
                    {"elapsed_minutes": _TIMEOUT_MINUTES,
                     "last_reported_qty": entry.last_reported_qty})

        try:
            entry.on_timeout(entry.order)
        except Exception as e:
            logger.error("on_timeout 콜백 오류: %s", e)

    def _audit(self, event_type: str, order: Order, detail: dict) -> None:
        """Fire-and-forget AuditLog write. Never raises."""
        if self._db_factory is None:
            return
        try:
            from backend.database.models import AuditLog
            sess = self._db_factory()
            try:
                sess.add(AuditLog(
                    event_type=event_type,
                    symbol=order.symbol,
                    order_id=order.id,
                    actor="poller",
                    detail=json.dumps(detail, ensure_ascii=False),
                ))
                sess.commit()
            finally:
                sess.close()
        except Exception as e:
            logger.warning("AuditLog 쓰기 실패 (%s): %s", event_type, e)

    @staticmethod
    def _default_timeout_handler(order: Order) -> None:
        logger.error("주문 타임아웃 미처리: %s %s %s — 수동 취소 필요",
                     order.id, order.side, order.symbol)
