from dataclasses import dataclass, field
from enum import Enum


class Market(str, Enum):
    KR = "KR"
    US = "US"


@dataclass
class BrokerCapabilities:
    markets: list[Market]
    supports_streaming: bool         # True if WebSocket fills available
    supports_fractional: bool        # True if fractional shares supported
    cancel_requires_symbol: bool     # US cancel requires symbol+qty+price
    cancel_requires_qty_price: bool
    rate_limit_per_sec: int
    settlement_days: int
    has_securities_tax: bool         # KR sells: 0.20% tax
    securities_tax_rate: float
    price_precision: dict            # {"KR": 0, "US": 2} integer vs 2dp
    min_order_qty: int = 1


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


@dataclass
class Order:
    id: str
    symbol: str
    side: str
    qty: int
    price: float
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    raw: dict = field(default_factory=dict)


@dataclass
class Position:
    symbol: str
    qty: int
    avg_price: float
    market: str  # "KR" | "US"
    current_price: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.avg_price) * self.qty

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.avg_price == 0:
            return 0.0
        return (self.current_price - self.avg_price) / self.avg_price


@dataclass
class Balance:
    cash_krw: float
    cash_usd: float
    total_eval_krw: float
