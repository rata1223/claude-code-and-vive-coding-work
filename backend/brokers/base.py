from abc import ABC, abstractmethod
from typing import Optional
from .models import Balance, BrokerCapabilities, Order, Position

#: A KRW-per-USD rate outside this range is a misread, not a market move. The
#: one bound shared by the KIS rate cache and the strategy's US buy sizing.
FX_USDKRW_MIN = 900.0
FX_USDKRW_MAX = 2000.0


def fx_rate_plausible(rate) -> bool:
    try:
        return FX_USDKRW_MIN < float(rate) < FX_USDKRW_MAX
    except (TypeError, ValueError):
        return False


class BrokerAdapter(ABC):
    is_live: bool = True  # False for SimulatedBroker — gates SAFE_MODE and shadow mode checks

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities: ...

    @abstractmethod
    def get_balance(self) -> Balance: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def place_order(self, symbol: str, side: str, qty: int, price: float, order_type: str = "limit") -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str = "", qty: int = 0, price: float = 0.0) -> bool: ...

    @abstractmethod
    def get_order_status(self, order_id: str, symbol: str = "") -> Optional[Order]:
        """The order as the broker reports it.

        ``None`` means the lookup succeeded and the broker has no such order.
        A lookup that could not be completed must **raise** — callers act on
        ``None`` (the reconciler cancels a stale order it believes lost), so
        "unknown" must never be reported as "absent".
        """

    @abstractmethod
    def get_price(self, symbol: str) -> float: ...

    def get_fx_rate(self) -> float:
        """KRW per USD for sizing a US order against capital valued in won.
        Raises when no recent rate is known — callers must not size on a guess.
        An adapter without a rate (the simulator, which prices everything in one
        currency) raises ``NotImplementedError``."""
        raise NotImplementedError
