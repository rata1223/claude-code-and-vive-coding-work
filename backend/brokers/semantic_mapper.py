"""
BrokerSemanticMapper — normalises raw broker API responses → canonical OrderStatus.

Each mapper knows the field names and status-string conventions of exactly one
broker+market combination. All broker-specific field knowledge lives here;
no raw field names should appear outside this module and the broker adapters
that call it.
"""
from abc import ABC, abstractmethod

from .models import OrderStatus


class BrokerSemanticMapper(ABC):
    """Translate one broker's raw API response dict → canonical scalar values."""

    @abstractmethod
    def map_status(self, raw: dict, filled_qty: int, ord_qty: int) -> OrderStatus:
        """Return the canonical status for this raw response row."""

    @abstractmethod
    def extract_filled_qty(self, raw: dict) -> int:
        """Return cumulative filled quantity from raw response."""

    @abstractmethod
    def extract_order_qty(self, raw: dict) -> int:
        """Return original order quantity from raw response."""

    @abstractmethod
    def extract_avg_price(self, raw: dict) -> float:
        """Return average fill price from raw response."""

    @abstractmethod
    def extract_broker_order_id(self, submit_response: dict) -> str:
        """Extract broker-assigned order ID from a placement response."""

    @abstractmethod
    def extract_side(self, raw: dict) -> str:
        """Return 'buy' or 'sell' from raw response."""


class KISDomesticMapper(BrokerSemanticMapper):
    """
    KIS Korean domestic order API field mapping.

    Key fields:
      tot_ccld_qty  — cumulative filled quantity
      ord_qty       — original order quantity
      avg_prvs      — average fill price
      ord_stts_name — status string in Korean (substring-matched)
      sll_buy_dvsn_cd — '02' = buy, else sell
    """

    _CANCEL_TOKENS = ("취소",)
    _REJECT_TOKENS = ("거부",)
    _EXPIRE_TOKENS = ("만료", "기간만료")

    def extract_filled_qty(self, raw: dict) -> int:
        return int(raw.get("tot_ccld_qty", 0))

    def extract_order_qty(self, raw: dict) -> int:
        return int(raw.get("ord_qty", 0))

    def extract_avg_price(self, raw: dict) -> float:
        return float(raw.get("avg_prvs", 0))

    def extract_broker_order_id(self, submit_response: dict) -> str:
        return submit_response.get("output", {}).get("ODNO", "")

    def extract_side(self, raw: dict) -> str:
        return "buy" if raw.get("sll_buy_dvsn_cd") == "02" else "sell"

    def map_status(self, raw: dict, filled_qty: int, ord_qty: int) -> OrderStatus:
        if ord_qty == 0:
            return OrderStatus.UNKNOWN
        if filled_qty >= ord_qty:
            return OrderStatus.FILLED
        if filled_qty > 0:
            return OrderStatus.PARTIAL_FILLED

        stat = raw.get("ord_stts_name", "")
        if not stat:
            return OrderStatus.UNKNOWN
        if any(t in stat for t in self._CANCEL_TOKENS):
            return OrderStatus.CANCELED
        if any(t in stat for t in self._REJECT_TOKENS):
            return OrderStatus.REJECTED
        if any(t in stat for t in self._EXPIRE_TOKENS):
            return OrderStatus.EXPIRED
        return OrderStatus.SUBMITTED


class KISOverseasMapper(BrokerSemanticMapper):
    """
    KIS US overseas order API field mapping.

    Key fields differ from domestic:
      ft_ccld_qty   — cumulative filled quantity (not tot_ccld_qty)
      ft_ord_qty    — original order quantity (not ord_qty)
      ft_ord_unpr3  — limit price
      odno          — order ID for row matching
    Status strings are bilingual (Korean + English).
    """

    _CANCEL_TOKENS_KO = ("취소",)
    _CANCEL_TOKENS_EN = ("cancel",)
    _REJECT_TOKENS_KO = ("거부",)
    _REJECT_TOKENS_EN = ("reject",)
    _EXPIRE_TOKENS_KO = ("만료",)
    _EXPIRE_TOKENS_EN = ("expired",)

    def extract_filled_qty(self, raw: dict) -> int:
        return int(raw.get("ft_ccld_qty", 0))

    def extract_order_qty(self, raw: dict) -> int:
        return int(raw.get("ft_ord_qty", 0))

    def extract_avg_price(self, raw: dict) -> float:
        return float(raw.get("avg_prvs", 0))

    def extract_broker_order_id(self, submit_response: dict) -> str:
        return submit_response.get("output", {}).get("ODNO", "")

    def extract_side(self, raw: dict) -> str:
        return "buy" if raw.get("sll_buy_dvsn_cd") == "02" else "sell"

    def map_status(self, raw: dict, filled_qty: int, ord_qty: int) -> OrderStatus:
        if ord_qty == 0:
            return OrderStatus.UNKNOWN
        if filled_qty >= ord_qty:
            return OrderStatus.FILLED
        if filled_qty > 0:
            return OrderStatus.PARTIAL_FILLED

        raw_stat = raw.get("ord_stts_name", "")
        if not raw_stat:
            return OrderStatus.UNKNOWN
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
        return OrderStatus.SUBMITTED


class KiwoomDomesticMapper(BrokerSemanticMapper):
    """
    Kiwoom Korean domestic order API field mapping.

    Field names are placeholders pending Kiwoom Open API doc verification.
    map_status always returns UNKNOWN until field semantics are confirmed.
    """

    # TBD: confirm against actual Kiwoom Open API response schema
    _FILLED_QTY_FIELD = "ccld_qty"
    _ORDER_QTY_FIELD = "ord_qty"
    _AVG_PRICE_FIELD = "avg_prvs"

    def extract_filled_qty(self, raw: dict) -> int:
        return int(raw.get(self._FILLED_QTY_FIELD, 0))

    def extract_order_qty(self, raw: dict) -> int:
        return int(raw.get(self._ORDER_QTY_FIELD, 0))

    def extract_avg_price(self, raw: dict) -> float:
        return float(raw.get(self._AVG_PRICE_FIELD, 0))

    def extract_broker_order_id(self, submit_response: dict) -> str:
        # TBD: confirm field name from Kiwoom placement response
        return submit_response.get("output", {}).get("ORD_NO", "")

    def extract_side(self, raw: dict) -> str:
        # TBD: confirm field name and value from Kiwoom response
        return "buy" if raw.get("sll_buy_dvsn_cd") == "02" else "sell"

    def map_status(self, raw: dict, filled_qty: int, ord_qty: int) -> OrderStatus:
        # Returns UNKNOWN until Kiwoom response field semantics are confirmed
        return OrderStatus.UNKNOWN


# Module-level singletons — mappers are stateless; one instance per type suffices.
KIS_DOMESTIC_MAPPER = KISDomesticMapper()
KIS_OVERSEAS_MAPPER = KISOverseasMapper()
KIWOOM_DOMESTIC_MAPPER = KiwoomDomesticMapper()
