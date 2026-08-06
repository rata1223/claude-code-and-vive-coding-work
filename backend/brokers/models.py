import dataclasses
from dataclasses import dataclass, field
from datetime import time
from enum import Enum
from typing import Optional


class Market(str, Enum):
    KR = "KR"
    US = "US"


@dataclass(frozen=True)
class BrokerCapabilities:
    # Identity
    broker_id: str              # "kis" | "kiwoom" | "simulator"
    market: str                 # "US" | "KR" | "simulation"  — matches Position.market values
    currency: str               # "USD" | "KRW" | "SIM"

    # Order type support
    supports_market_buy: bool
    supports_market_sell: bool
    supports_limit_order: bool
    supports_stop_order: bool
    supports_fractional: bool   # True if qty < 1 allowed
    supports_short: bool        # True if short selling allowed

    # Session capabilities
    supports_after_hours: bool      # True if orders accepted outside regular session
    supports_websocket: bool        # True if broker provides push fill notification
    supports_realtime_quote: bool   # True if broker provides streaming price feed

    # Account capabilities
    supports_account_balance: bool  # True if get_balance() is functional
    supports_portfolio: bool        # True if get_positions() is functional

    # Execution mechanics
    fill_mechanism: str             # "polling" | "websocket" | "sync"
    price_type: str                 # "decimal" | "integer" | "float"
    requires_exchange_code: bool    # US: OVRS_EXCG_CD required per order
    requires_hashkey: bool          # KIS: Hashkey on POST required
    cancel_requires_symbol: bool
    cancel_requires_qty_price: bool
    retry_safe_on_submit: bool      # False for both KIS and Kiwoom (no client_order_id)
    rate_limit_per_sec: int
    settlement_days: int
    has_securities_tax: bool
    securities_tax_rate: float
    price_precision: dict           # {"KR": 0, "US": 2} or {"SIM": 4}
    min_order_qty: int
    session_open_kst: time | None   # None for simulator (no session constraint)
    session_close_kst: time | None


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
    EXPIRED = "expired"
    UNKNOWN = "unknown"


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
    #: How much of ``qty`` the broker will actually let us sell right now
    #: (P0-07 S2). ``None`` means the broker reported no orderable figure —
    #: callers must fail closed rather than fall back to ``qty``, since held
    #: shares can be unsettled or already committed to a resting sell order.
    #: Adapters where held is sellable by construction (the simulator, the
    #: in-memory tracker) set this equal to ``qty``.
    sellable_qty: Optional[int] = None

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
