from .models import Balance, Order, OrderSide, OrderStatus, OrderType, Position
from .base import BrokerAdapter

__all__ = [
    "BrokerAdapter",
    "Balance",
    "Order",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Position",
]


def __getattr__(name):
    if name == "KISBroker":
        from .kis import KISBroker
        return KISBroker
    if name == "KiwoomBroker":
        from .kiwoom import KiwoomBroker
        return KiwoomBroker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
