"""
브로커 의미론적 매핑 레이어.

KR / US 시장별 주문 파라미터 정규화:
- 가격 정밀도 (KR: int원, US: float$)
- 취소 요청 kwargs (US: symbol+qty+price 필수)
- 비용 계산 (수수료 + 슬리피지 + 증권거래세)
- 시장 식별 (심볼 형식 기반)

사용 예:
    mapper = BrokerSemanticMapper(kis_capabilities)
    market = mapper.market_for_symbol("069500")  # Market.KR
    price = mapper.normalize_price(10250.5, market)  # 10250 (int→float)
    kwargs = mapper.cancel_kwargs(order)            # includes symbol/qty/price for US
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from backend.brokers.models import BrokerCapabilities, Market, Order

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# KIS 브로커 기본 역량 선언
KIS_CAPABILITIES = BrokerCapabilities(
    markets=[Market.KR, Market.US],
    supports_streaming=False,       # HTTP polling only (KIS WebSocket not integrated)
    supports_fractional=False,      # 1주 최소 단위
    cancel_requires_symbol=True,    # US 취소에 symbol 필수
    cancel_requires_qty_price=True, # US 취소에 qty+price 필수
    rate_limit_per_sec=15,          # 실전 15/s (모의: 5/s)
    settlement_days=2,              # T+2
    has_securities_tax=True,        # KR 매도 시 증권거래세
    securities_tax_rate=0.002,      # ETF: 0.20%
    price_precision={"KR": 0, "US": 2},
    min_order_qty=1,
)

KIS_PAPER_CAPABILITIES = BrokerCapabilities(
    **{k: v for k, v in KIS_CAPABILITIES.__dict__.items()},
)
KIS_PAPER_CAPABILITIES.rate_limit_per_sec = 5


class BrokerSemanticMapper:
    """
    브로커 역량 선언 기반의 주문 파라미터 변환기.
    KISBroker 내부에서 직접 사용하거나, 전략 레이어에서 독립적으로 사용 가능.
    """

    def __init__(self, capabilities: BrokerCapabilities):
        self.caps = capabilities

    def market_for_symbol(self, symbol: str) -> Market:
        """심볼 형식으로 KR/US 시장 판별 (6자리 숫자 = KR)."""
        from backend.quant.data.universe import KR_ETF
        if symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit()):
            return Market.KR
        return Market.US

    def normalize_price(self, price: float, market: Market) -> float:
        """시장별 가격 정밀도 정규화. KR: 정수원, US: 소수점2자리."""
        precision = self.caps.price_precision.get(market.value, 2)
        if precision == 0:
            return float(int(price))
        return round(price, precision)

    def cancel_kwargs(self, order: Order) -> dict:
        """
        cancel_order() 호출을 위한 kwargs 반환.
        US는 symbol+qty+price 포함, KR은 order_id만.
        """
        base = {"order_id": order.id}
        if self.caps.cancel_requires_symbol:
            market = self.market_for_symbol(order.symbol)
            if market == Market.US:
                base["symbol"] = order.symbol
                base["qty"] = order.qty
                base["price"] = float(order.price or 0)
        return base

    def apply_costs(self, price: float, qty: int, side: str, market: Market) -> float:
        """
        거래 비용 계산 (수수료 + 슬리피지 + 증권거래세).
        Returns total cost for buy, total proceeds deduction for sell.
        """
        notional = price * qty
        commission = notional * 0.00015  # KIS 0.015%
        slippage = notional * 0.001      # 0.1% 가정
        tax = 0.0
        if (market == Market.KR and side == "sell" and self.caps.has_securities_tax):
            tax = notional * self.caps.securities_tax_rate
        return commission + slippage + tax

    def net_proceeds(self, price: float, qty: int, side: str, market: Market) -> float:
        """매매 후 순수익/순비용 (양수 = 순수입, 음수 = 비용)."""
        notional = price * qty
        costs = self.apply_costs(price, qty, side, market)
        if side == "sell":
            return notional - costs
        return -(notional + costs)
