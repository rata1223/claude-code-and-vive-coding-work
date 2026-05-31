from typing import Optional
from .base import BrokerAdapter
from .capabilities import KIWOOM_CAPABILITIES
from .models import Balance, BrokerCapabilities, Order, Position
from .validator import BrokerCapabilityValidator, OrderRequest


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
        detected_market = self._detect_market(symbol)
        BrokerCapabilityValidator(self.capabilities).validate(
            OrderRequest(symbol=symbol, side=side, qty=float(qty), price=price,
                         order_type=order_type, market=detected_market)
        )
        raise NotImplementedError("키움증권 미구현")

    def cancel_order(self, order_id: str, symbol: str = "", qty: int = 0, price: float = 0.0) -> bool:
        if symbol:
            BrokerCapabilityValidator(self.capabilities)._check_market_routing(
                OrderRequest(symbol=symbol, side="sell", qty=1.0, price=0.0,
                             order_type="limit", market=self._detect_market(symbol))
            )
        raise NotImplementedError("키움증권 미구현")

    def get_order_status(self, order_id: str, symbol: str = "") -> Optional[Order]:
        if symbol:
            BrokerCapabilityValidator(self.capabilities)._check_market_routing(
                OrderRequest(symbol=symbol, side="sell", qty=1.0, price=0.0,
                             order_type="limit", market=self._detect_market(symbol))
            )
        raise NotImplementedError("키움증권 미구현")

    def get_price(self, symbol: str) -> float:
        BrokerCapabilityValidator(self.capabilities)._check_market_routing(
            OrderRequest(symbol=symbol, side="buy", qty=1.0, price=0.0,
                         order_type="limit", market=self._detect_market(symbol))
        )
        raise NotImplementedError("키움증권 미구현")

    @staticmethod
    def _detect_market(symbol: str) -> str:
        from backend.quant.data.universe import KR_ETF
        return "KR" if (symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit())) else "US"
