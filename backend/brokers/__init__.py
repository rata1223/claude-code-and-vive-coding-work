from .models import Balance, BrokerCapabilities, Order, OrderSide, OrderStatus, OrderType, Position
from .base import BrokerAdapter
from .validator import BrokerCapabilityValidator, OrderRequest, UnsupportedCapabilityError

__all__ = [
    "BrokerAdapter",
    "Balance",
    "BrokerCapabilities",
    "BrokerCapabilityValidator",
    "Order",
    "OrderRequest",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Position",
    "UnsupportedCapabilityError",
]


def __getattr__(name):
    if name == "KISBroker":
        from .kis import KISBroker
        return KISBroker
    if name == "KiwoomBroker":
        from .kiwoom import KiwoomBroker
        return KiwoomBroker
    if name == "ScriptedPaperBroker":
        from .paper_broker import ScriptedPaperBroker
        return ScriptedPaperBroker
    if name == "FillStep":
        from .paper_broker import FillStep
        return FillStep
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
