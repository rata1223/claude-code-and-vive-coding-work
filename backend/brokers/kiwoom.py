from .base import BrokerAdapter
from .models import Balance, Order, Position


class KiwoomBroker(BrokerAdapter):
    """키움증권 브로커 어댑터 — 미구현 스텁."""

    def get_balance(self) -> Balance:
        raise NotImplementedError("키움증권 미구현")

    def get_positions(self) -> list[Position]:
        raise NotImplementedError("키움증권 미구현")

    def place_order(self, symbol: str, side: str, qty: int, price: float, order_type: str = "limit") -> Order:
        raise NotImplementedError("키움증권 미구현")

    def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError("키움증권 미구현")

    def get_price(self, symbol: str) -> float:
        raise NotImplementedError("키움증권 미구현")
