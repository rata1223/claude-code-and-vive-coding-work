from abc import ABC, abstractmethod
from .models import Balance, Position, Order


class BrokerAdapter(ABC):
    is_live: bool = True  # False for SimulatedBroker — gates SAFE_MODE and shadow mode checks

    @abstractmethod
    def get_balance(self) -> Balance: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def place_order(self, symbol: str, side: str, qty: int, price: float, order_type: str = "limit") -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_price(self, symbol: str) -> float: ...
