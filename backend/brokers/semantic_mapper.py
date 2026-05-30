"""
브로커 의미론적 매핑 레이어.

두 가지 역할을 담당:

1. BrokerSemanticMapper — 브로커 역량(BrokerCapabilities) 기반 주문 파라미터 정규화
   (가격 정밀도, 취소 kwargs, 수수료/세금 계산)

2. BrokerStatusMapper ABC + 구현체 — 브로커별 raw API 응답 → 표준 OrderStatus 변환
   (KISDomesticMapper, KISOverseasMapper, KiwoomDomesticMapper)
   모든 브로커별 필드명과 상태 문자열 파싱이 이 모듈에만 위치함.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from backend.brokers.models import BrokerCapabilities, Market, Order, OrderStatus

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── 안전한 숫자 변환 헬퍼 ─────────────────────────────────────────────────────

def _to_int(value, default: int = 0) -> int:
    """KIS API는 빈 문자열("")을 반환할 수 있음 — None과 "" 모두 default로 처리."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    """KIS API는 빈 문자열("")을 반환할 수 있음 — None과 "" 모두 default로 처리."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


# ── KIS 브로커 기본 역량 선언 ────────────────────────────────────────────────

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


# ── BrokerSemanticMapper: 주문 파라미터 정규화 ────────────────────────────────

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


# ── BrokerStatusMapper: raw API 응답 → OrderStatus 정규화 ────────────────────

class BrokerStatusMapper(ABC):
    """
    브로커별 raw API 응답 dict → 표준 OrderStatus + 스칼라 필드 추출.

    각 구현체는 정확히 하나의 브로커+시장 조합의 필드명과 상태 문자열 규칙을 알고 있음.
    이 모듈 밖에서는 브로커 고유 필드명이 나타나면 안 됨.
    """

    @abstractmethod
    def map_status(self, raw: dict, filled_qty: int, ord_qty: int) -> OrderStatus:
        """raw 응답 행에 대한 표준 상태 반환."""

    @abstractmethod
    def extract_filled_qty(self, raw: dict) -> int:
        """raw 응답에서 누적 체결 수량 반환."""

    @abstractmethod
    def extract_order_qty(self, raw: dict) -> int:
        """raw 응답에서 원주문 수량 반환."""

    @abstractmethod
    def extract_avg_price(self, raw: dict) -> float:
        """raw 응답에서 평균 체결가 반환."""

    @abstractmethod
    def extract_broker_order_id(self, submit_response: dict) -> str:
        """주문 접수 응답에서 브로커 주문 ID 추출."""

    @abstractmethod
    def extract_side(self, raw: dict) -> str:
        """raw 응답에서 'buy' 또는 'sell' 반환."""


class KISDomesticMapper(BrokerStatusMapper):
    """
    KIS 국내 주식 주문 API 필드 매핑.

    주요 필드:
      tot_ccld_qty    — 누적 체결 수량
      ord_qty         — 원주문 수량
      avg_prvs        — 평균 체결가
      ord_stts_name   — 상태명 (한국어 부분 문자열 매칭)
      sll_buy_dvsn_cd — '02' = 매수, 나머지 = 매도
    """

    _CANCEL_TOKENS = ("취소",)
    _REJECT_TOKENS = ("거부",)
    _EXPIRE_TOKENS = ("만료", "기간만료")

    def extract_filled_qty(self, raw: dict) -> int:
        return _to_int(raw.get("tot_ccld_qty"))

    def extract_order_qty(self, raw: dict) -> int:
        return _to_int(raw.get("ord_qty"))

    def extract_avg_price(self, raw: dict) -> float:
        return _to_float(raw.get("avg_prvs"))

    def extract_broker_order_id(self, submit_response: dict) -> str:
        return submit_response.get("output", {}).get("ODNO", "")

    def extract_side(self, raw: dict) -> str:
        return "buy" if raw.get("sll_buy_dvsn_cd") == "02" else "sell"

    def map_status(self, raw: dict, filled_qty: int, ord_qty: int) -> OrderStatus:
        if ord_qty == 0:
            return OrderStatus.UNKNOWN
        if filled_qty >= ord_qty:
            return OrderStatus.FILLED

        stat = raw.get("ord_stts_name", "")
        if not stat:
            # No status string: partial fill if any qty filled, else UNKNOWN
            return OrderStatus.PARTIAL_FILLED if filled_qty > 0 else OrderStatus.UNKNOWN
        if any(t in stat for t in self._CANCEL_TOKENS):
            return OrderStatus.CANCELED
        if any(t in stat for t in self._REJECT_TOKENS):
            return OrderStatus.REJECTED
        if any(t in stat for t in self._EXPIRE_TOKENS):
            return OrderStatus.EXPIRED
        if filled_qty > 0:
            return OrderStatus.PARTIAL_FILLED
        return OrderStatus.SUBMITTED


class KISOverseasMapper(BrokerStatusMapper):
    """
    KIS 해외주식 주문 API 필드 매핑.

    국내와 다른 주요 필드:
      ft_ccld_qty   — 누적 체결 수량 (tot_ccld_qty 아님)
      ft_ord_qty    — 원주문 수량 (ord_qty 아님)
      ft_ord_unpr3  — 지정가
      odno          — 행 매칭용 주문 ID
    상태 문자열은 한국어+영어 이중 언어.
    """

    _CANCEL_TOKENS_KO = ("취소",)
    _CANCEL_TOKENS_EN = ("cancel",)
    _REJECT_TOKENS_KO = ("거부",)
    _REJECT_TOKENS_EN = ("reject",)
    _EXPIRE_TOKENS_KO = ("만료",)
    _EXPIRE_TOKENS_EN = ("expired",)

    def extract_filled_qty(self, raw: dict) -> int:
        return _to_int(raw.get("ft_ccld_qty"))

    def extract_order_qty(self, raw: dict) -> int:
        return _to_int(raw.get("ft_ord_qty"))

    def extract_avg_price(self, raw: dict) -> float:
        return _to_float(raw.get("avg_prvs"))

    def extract_broker_order_id(self, submit_response: dict) -> str:
        return submit_response.get("output", {}).get("ODNO", "")

    def extract_side(self, raw: dict) -> str:
        return "buy" if raw.get("sll_buy_dvsn_cd") == "02" else "sell"

    def map_status(self, raw: dict, filled_qty: int, ord_qty: int) -> OrderStatus:
        if ord_qty == 0:
            return OrderStatus.UNKNOWN
        if filled_qty >= ord_qty:
            return OrderStatus.FILLED

        raw_stat = raw.get("ord_stts_name", "")
        if not raw_stat:
            return OrderStatus.PARTIAL_FILLED if filled_qty > 0 else OrderStatus.UNKNOWN
        stat_lower = raw_stat.lower()

        if any(t in raw_stat for t in self._CANCEL_TOKENS_KO) or \
                any(t in stat_lower for t in self._CANCEL_TOKENS_EN):
            return OrderStatus.CANCELED
        if any(t in raw_stat for t in self._REJECT_TOKENS_KO) or \
                any(t in stat_lower for t in self._REJECT_TOKENS_EN):
            return OrderStatus.REJECTED
        if any(t in raw_stat for t in self._EXPIRE_TOKENS_KO) or \
                any(t in stat_lower for t in self._EXPIRE_TOKENS_EN):
            return OrderStatus.EXPIRED
        if filled_qty > 0:
            return OrderStatus.PARTIAL_FILLED
        return OrderStatus.SUBMITTED


class KiwoomDomesticMapper(BrokerStatusMapper):
    """
    키움증권 국내 주식 주문 API 필드 매핑.

    Kiwoom Open API 응답 스키마 확인 전까지 필드명은 플레이스홀더.
    map_status는 확인 전까지 항상 UNKNOWN 반환.
    """

    # TBD: 실제 Kiwoom Open API 응답 스키마에서 확인 필요
    _FILLED_QTY_FIELD = "ccld_qty"
    _ORDER_QTY_FIELD = "ord_qty"
    _AVG_PRICE_FIELD = "avg_prvs"

    def extract_filled_qty(self, raw: dict) -> int:
        return _to_int(raw.get(self._FILLED_QTY_FIELD))

    def extract_order_qty(self, raw: dict) -> int:
        return _to_int(raw.get(self._ORDER_QTY_FIELD))

    def extract_avg_price(self, raw: dict) -> float:
        return _to_float(raw.get(self._AVG_PRICE_FIELD))

    def extract_broker_order_id(self, submit_response: dict) -> str:
        # TBD: Kiwoom 주문 접수 응답에서 필드명 확인 필요
        return submit_response.get("output", {}).get("ORD_NO", "")

    def extract_side(self, raw: dict) -> str:
        # TBD: Kiwoom 응답에서 필드명과 값 확인 필요
        return "buy" if raw.get("sll_buy_dvsn_cd") == "02" else "sell"

    def map_status(self, raw: dict, filled_qty: int, ord_qty: int) -> OrderStatus:
        # Kiwoom 응답 필드 의미 확인 전까지 UNKNOWN 반환
        return OrderStatus.UNKNOWN


# 모듈 레벨 싱글톤 — 매퍼는 무상태; 타입당 하나의 인스턴스면 충분.
KIS_DOMESTIC_MAPPER = KISDomesticMapper()
KIS_OVERSEAS_MAPPER = KISOverseasMapper()
KIWOOM_DOMESTIC_MAPPER = KiwoomDomesticMapper()
