from typing import Optional
from .base import BrokerAdapter
from .capabilities import KIWOOM_CAPABILITIES
from .models import Balance, BrokerCapabilities, Order, Position


class KiwoomBroker(BrokerAdapter):
    """키움증권 브로커 어댑터 — 미구현 스텁.
    Kiwoom uses a Windows COM API (HTS Ocx), not REST. Implementation requires
    a Windows sidecar process with a COM-to-REST bridge — not compatible with Docker.
    """

    @property
    def capabilities(self) -> BrokerCapabilities:
        return KIWOOM_CAPABILITIES

    def get_balance(self) -> Balance:
        raise NotImplementedError("키움증권 미구현")

    def get_positions(self) -> list[Position]:
        raise NotImplementedError("키움증권 미구현")

    def place_order(self, symbol: str, side: str, qty: int, price: float, order_type: str = "limit") -> Order:
        raise NotImplementedError("키움증권 미구현")

    def cancel_order(self, order_id: str, symbol: str = "", qty: int = 0, price: float = 0.0) -> bool:
        raise NotImplementedError("키움증권 미구현")

    def get_order_status(self, order_id: str, symbol: str = "") -> Optional[Order]:
        raise NotImplementedError("키움증권 미구현")

    def get_price(self, symbol: str) -> float:
        raise NotImplementedError("키움증권 미구현")
