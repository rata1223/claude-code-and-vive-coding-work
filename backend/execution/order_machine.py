import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from backend.brokers.models import Order, OrderStatus

logger = logging.getLogger(__name__)

# 허용된 상태 전환 맵
VALID_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING:        {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.CANCELED},
    OrderStatus.SUBMITTED:      {OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED},
    OrderStatus.PARTIAL_FILLED: {OrderStatus.FILLED, OrderStatus.CANCELED},
    OrderStatus.FILLED:         set(),
    OrderStatus.CANCELED:       set(),
    OrderStatus.REJECTED:       set(),
}


@dataclass
class FillEvent:
    order_id: str
    filled_qty: int
    fill_price: float
    filled_at: datetime = field(default_factory=datetime.utcnow)


class OrderStateMachine:
    """
    브로커 독립적인 주문 상태 관리.
    on_state_change 콜백을 통해 DB 저장·알림 등 부수 효과 주입.
    """

    def __init__(self, on_state_change: Optional[Callable[[Order], None]] = None):
        self._orders: dict[str, Order] = {}
        self._on_change = on_state_change or (lambda o: None)

    # ── 등록 ──────────────────────────────────────────────────────────────
    def register(self, order: Order) -> Order:
        self._orders[order.id] = order
        logger.info("주문 등록: id=%s symbol=%s side=%s qty=%d", order.id, order.symbol, order.side, order.qty)
        return order

    # ── 상태 전환 ──────────────────────────────────────────────────────────
    def transition(self, order_id: str, new_status: OrderStatus) -> Order:
        order = self._get(order_id)
        self._assert_valid(order, new_status)
        order.status = new_status
        logger.info("상태 전환: id=%s → %s", order_id, new_status.value)
        self._on_change(order)
        return order

    def process_fill(self, event: FillEvent) -> Order:
        order = self._get(event.order_id)
        order.filled_qty += event.filled_qty
        if order.filled_qty > 0:
            # 가중평균 체결가 계산
            prev_val = order.avg_fill_price * (order.filled_qty - event.filled_qty)
            new_val = event.fill_price * event.filled_qty
            order.avg_fill_price = (prev_val + new_val) / order.filled_qty

        if order.filled_qty >= order.qty:
            new_status = OrderStatus.FILLED
        elif order.filled_qty > 0:
            new_status = OrderStatus.PARTIAL_FILLED
        else:
            new_status = order.status

        if new_status != order.status:
            self._assert_valid(order, new_status)
            order.status = new_status

        logger.info(
            "체결 처리: id=%s filled=%d/%d avg=%.4f status=%s",
            event.order_id, order.filled_qty, order.qty, order.avg_fill_price, order.status.value,
        )
        self._on_change(order)
        return order

    def submit(self, order_id: str, broker_order_id: str = "") -> Order:
        order = self._get(order_id)
        if broker_order_id:
            order.id = broker_order_id
            self._orders[broker_order_id] = order
        return self.transition(order_id if not broker_order_id else broker_order_id, OrderStatus.SUBMITTED)

    def cancel(self, order_id: str) -> Order:
        return self.transition(order_id, OrderStatus.CANCELED)

    def reject(self, order_id: str) -> Order:
        return self.transition(order_id, OrderStatus.REJECTED)

    # ── 조회 ──────────────────────────────────────────────────────────────
    def get(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def active_orders(self) -> list[Order]:
        return [o for o in self._orders.values()
                if o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED)]

    # ── 내부 ──────────────────────────────────────────────────────────────
    def _get(self, order_id: str) -> Order:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"주문 없음: {order_id}")
        return order

    def _assert_valid(self, order: Order, new_status: OrderStatus):
        allowed = VALID_TRANSITIONS.get(order.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"유효하지 않은 상태 전환: {order.status.value} → {new_status.value} (order_id={order.id})"
            )
