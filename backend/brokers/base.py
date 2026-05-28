from abc import ABC, abstractmethod
from typing import Optional
from .models import Balance, BrokerCapabilities, Order, Position


class BrokerAdapter(ABC):
    is_live: bool = True  # False for SimulatedBroker — gates SAFE_MODE and shadow mode checks

    @abstractmethod
    def get_balance(self) -> Balance: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def place_order(self, symbol: str, side: str, qty: int, price: float, order_type: str = "limit") -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str = "", qty: int = 0, price: float = 0.0) -> bool: ...

    @abstractmethod
    def get_order_status(self, order_id: str, symbol: str = "") -> Optional[Order]: ...

    @abstractmethod
    def get_price(self, symbol: str) -> float: ...
