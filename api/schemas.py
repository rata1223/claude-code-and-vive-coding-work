from datetime import datetime
from typing import Any, Optional, List
from pydantic import BaseModel, EmailStr, Field


# ─────────────────────────────────────────────
# Generic response envelope
# ─────────────────────────────────────────────
class Resp(BaseModel):
    code: int = 1
    data: Any = None
    msg: str = "ok"

    @classmethod
    def ok(cls, data: Any = None, msg: str = "ok") -> "Resp":
        return cls(code=1, data=data, msg=msg)

    @classmethod
    def err(cls, msg: str = "error", code: int = -1) -> "Resp":
        return cls(code=code, data=None, msg=msg)


# ─────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)
    nickname: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=6)


class TokenData(BaseModel):
    user_id: int
    email: str


# ─────────────────────────────────────────────
# Credentials
# ─────────────────────────────────────────────
class CredentialCreate(BaseModel):
    name: str
    exchange_id: str  # kis / kiwoom
    app_key: Optional[str] = None
    app_secret: Optional[str] = None
    account_no: Optional[str] = None
    hts_id: Optional[str] = None
    api_key: Optional[str] = None
    env: str = "paper"


class CredentialOut(BaseModel):
    id: int
    name: str
    exchange_id: str
    env: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ─────────────────────────────────────────────
# Strategy
# ─────────────────────────────────────────────
class StrategyCreate(BaseModel):
    name: str
    type: str = "indicator"       # indicator / script
    symbol: Optional[str] = None
    timeframe: Optional[str] = "1h"
    market_type: Optional[str] = "spot"
    direction: Optional[str] = "long"
    initial_capital: Optional[float] = 10000.0
    config: Optional[dict] = {}
    script_code: Optional[str] = None


class StrategyUpdate(BaseModel):
    id: int
    name: Optional[str] = None
    type: Optional[str] = None
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    market_type: Optional[str] = None
    direction: Optional[str] = None
    initial_capital: Optional[float] = None
    config: Optional[dict] = None
    script_code: Optional[str] = None


class StrategyOut(BaseModel):
    id: int
    name: str
    type: str
    status: str
    symbol: Optional[str]
    timeframe: Optional[str]
    market_type: Optional[str]
    direction: Optional[str]
    initial_capital: Optional[float]
    config: Optional[dict]
    created_at: datetime
    updated_at: Optional[datetime]

    model_config = {"from_attributes": True}


# ─────────────────────────────────────────────
# Trade
# ─────────────────────────────────────────────
class TradeOut(BaseModel):
    id: int
    strategy_id: int
    symbol: str
    side: str
    qty: float
    price: float
    filled_at: datetime
    pnl: Optional[float]
    fee: Optional[float]

    model_config = {"from_attributes": True}


# ─────────────────────────────────────────────
# StrategyLog
# ─────────────────────────────────────────────
class LogOut(BaseModel):
    id: int
    strategy_id: int
    message: str
    level: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ─────────────────────────────────────────────
# Notification
# ─────────────────────────────────────────────
class NotificationOut(BaseModel):
    id: int
    user_id: int
    strategy_id: Optional[int]
    title: str
    message: str
    is_read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ─────────────────────────────────────────────
# Watchlist
# ─────────────────────────────────────────────
class WatchlistAdd(BaseModel):
    market: str
    symbol: str
    name: Optional[str] = None


class WatchlistRemove(BaseModel):
    symbol: str


class WatchlistItemOut(BaseModel):
    id: int
    market: str
    symbol: str
    name: Optional[str]

    model_config = {"from_attributes": True}


# ─────────────────────────────────────────────
# Quick trade
# ─────────────────────────────────────────────
class PlaceOrderRequest(BaseModel):
    credential_id: int
    symbol: str
    side: str               # buy / sell
    qty: float
    price: float
    # None = not supplied. The default used to be "us", which was
    # indistinguishable from a caller explicitly choosing US and so
    # silently routed every KR order to the US path. Resolved by
    # api.routers.quick_trade._resolve_market.
    market: Optional[str] = None      # us / kr / None = derive from symbol
    exchange: str = "NASD"  # NASD / NYSE / KRX


class ClosePositionRequest(BaseModel):
    """Close an open position (P0-07C).

    Quantity and price are resolved server-side from live broker state — the
    client can never dictate execution size or price. ``qty`` is an optional
    upper bound: omit it to close the whole position. Any ``qty`` above the live
    holding is rejected, never clamped.
    """
    credential_id: int
    symbol: str
    qty: Optional[float] = None  # None = close the entire live position
    market: Optional[str] = None   # None = derive from symbol (see _resolve_market)
    exchange: str = "NASD"


# ─────────────────────────────────────────────
# User profile
# ─────────────────────────────────────────────
class ProfileUpdate(BaseModel):
    nickname: Optional[str] = None
    avatar: Optional[str] = None
