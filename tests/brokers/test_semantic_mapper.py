"""Unit tests for BrokerSemanticMapper implementations."""
import pytest

from backend.brokers.models import OrderStatus
from backend.brokers.semantic_mapper import (
    KISDomesticMapper,
    KISOverseasMapper,
    KiwoomDomesticMapper,
    _to_float,
    _to_int,
)

_KR = KISDomesticMapper()
_US = KISOverseasMapper()
_KW = KiwoomDomesticMapper()


# ── Safe coercion helpers ───────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("42", 42),
    (42, 42),
    ("", 0),
    (None, 0),
    ("abc", 0),
])
def test_to_int(value, expected):
    assert _to_int(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("3.14", pytest.approx(3.14)),
    (3.14, pytest.approx(3.14)),
    ("", 0.0),
    (None, 0.0),
    ("abc", 0.0),
])
def test_to_float(value, expected):
    assert _to_float(value) == expected


# ── KISDomesticMapper: empty-string numeric field handling ──────────────────

def test_kis_domestic_empty_string_fields():
    """KIS may return '' for unfilled numeric fields — must not raise."""
    assert _KR.extract_filled_qty({"tot_ccld_qty": ""}) == 0
    assert _KR.extract_order_qty({"ord_qty": ""}) == 0
    assert _KR.extract_avg_price({"avg_prvs": ""}) == 0.0


# ── KISOverseasMapper: empty-string numeric field handling ──────────────────

def test_kis_overseas_empty_string_fields():
    assert _US.extract_filled_qty({"ft_ccld_qty": ""}) == 0
    assert _US.extract_order_qty({"ft_ord_qty": ""}) == 0
    assert _US.extract_avg_price({"avg_prvs": ""}) == 0.0


# ── KISDomesticMapper.map_status ────────────────────────────────────────────

@pytest.mark.parametrize("raw,fq,oq,expected", [
    # Fully filled
    ({"tot_ccld_qty": "100", "ord_qty": "100", "ord_stts_name": "체결완료"}, 100, 100, OrderStatus.FILLED),
    # Overfill (filled_qty > ord_qty)
    ({"tot_ccld_qty": "110", "ord_qty": "100", "ord_stts_name": ""}, 110, 100, OrderStatus.FILLED),
    # Partial fill — status string indicates normal partial
    ({"tot_ccld_qty": "50", "ord_qty": "100", "ord_stts_name": "부분체결"}, 50, 100, OrderStatus.PARTIAL_FILLED),
    # Partial fill + empty status string → PARTIAL_FILLED (no broker terminal signal)
    ({"ord_stts_name": ""}, 50, 100, OrderStatus.PARTIAL_FILLED),
    # Partially filled then canceled — CANCELED takes precedence over PARTIAL_FILLED
    ({"ord_stts_name": "주문취소"}, 50, 100, OrderStatus.CANCELED),
    # Partially filled then rejected
    ({"ord_stts_name": "주문거부"}, 50, 100, OrderStatus.REJECTED),
    # Partially filled then expired
    ({"ord_stts_name": "만료"}, 50, 100, OrderStatus.EXPIRED),
    # Canceled — Korean token (no fill)
    ({"tot_ccld_qty": "0", "ord_qty": "100", "ord_stts_name": "주문취소"}, 0, 100, OrderStatus.CANCELED),
    # Canceled — bare token
    ({"ord_stts_name": "취소"}, 0, 100, OrderStatus.CANCELED),
    # Rejected
    ({"ord_stts_name": "주문거부"}, 0, 100, OrderStatus.REJECTED),
    # Expired
    ({"ord_stts_name": "기간만료"}, 0, 100, OrderStatus.EXPIRED),
    ({"ord_stts_name": "만료"}, 0, 100, OrderStatus.EXPIRED),
    # Empty status, no fill → UNKNOWN
    ({"ord_stts_name": ""}, 0, 100, OrderStatus.UNKNOWN),
    # Missing key → UNKNOWN
    ({}, 0, 100, OrderStatus.UNKNOWN),
    # ord_qty == 0 guard → UNKNOWN
    ({"ord_stts_name": "체결완료"}, 0, 0, OrderStatus.UNKNOWN),
    # Acknowledged, waiting for fill
    ({"ord_stts_name": "접수완료"}, 0, 100, OrderStatus.SUBMITTED),
])
def test_kis_domestic_map_status(raw, fq, oq, expected):
    assert _KR.map_status(raw, fq, oq) == expected


def test_kis_domestic_extract_filled_qty():
    assert _KR.extract_filled_qty({"tot_ccld_qty": "42"}) == 42
    assert _KR.extract_filled_qty({}) == 0


def test_kis_domestic_extract_order_qty():
    assert _KR.extract_order_qty({"ord_qty": "100"}) == 100


def test_kis_domestic_extract_avg_price():
    assert _KR.extract_avg_price({"avg_prvs": "55000.5"}) == pytest.approx(55000.5)


def test_kis_domestic_extract_broker_order_id():
    resp = {"output": {"ODNO": "0000123456"}}
    assert _KR.extract_broker_order_id(resp) == "0000123456"
    assert _KR.extract_broker_order_id({}) == ""


def test_kis_domestic_extract_side():
    assert _KR.extract_side({"sll_buy_dvsn_cd": "02"}) == "buy"
    assert _KR.extract_side({"sll_buy_dvsn_cd": "01"}) == "sell"
    assert _KR.extract_side({}) == "sell"


# ── KISOverseasMapper.map_status ────────────────────────────────────────────

@pytest.mark.parametrize("raw,fq,oq,expected", [
    # Fully filled
    ({"ft_ccld_qty": "10", "ft_ord_qty": "10", "ord_stts_name": "Filled"}, 10, 10, OrderStatus.FILLED),
    # Partial fill — no terminal signal
    ({"ord_stts_name": "Partial"}, 5, 10, OrderStatus.PARTIAL_FILLED),
    # Partial fill + empty status → PARTIAL_FILLED
    ({"ord_stts_name": ""}, 5, 10, OrderStatus.PARTIAL_FILLED),
    # Partially filled then canceled — CANCELED takes precedence
    ({"ord_stts_name": "Canceled"}, 5, 10, OrderStatus.CANCELED),
    # Partially filled then expired
    ({"ord_stts_name": "Expired"}, 5, 10, OrderStatus.EXPIRED),
    # Canceled — English
    ({"ord_stts_name": "Canceled"}, 0, 10, OrderStatus.CANCELED),
    ({"ord_stts_name": "CANCELED"}, 0, 10, OrderStatus.CANCELED),
    # Canceled — Korean
    ({"ord_stts_name": "취소"}, 0, 10, OrderStatus.CANCELED),
    # Rejected — English
    ({"ord_stts_name": "Rejected"}, 0, 10, OrderStatus.REJECTED),
    # Rejected — Korean
    ({"ord_stts_name": "거부"}, 0, 10, OrderStatus.REJECTED),
    # Expired — English
    ({"ord_stts_name": "Expired"}, 0, 10, OrderStatus.EXPIRED),
    # Expired — Korean
    ({"ord_stts_name": "만료"}, 0, 10, OrderStatus.EXPIRED),
    # Empty, no fill → UNKNOWN
    ({"ord_stts_name": ""}, 0, 10, OrderStatus.UNKNOWN),
    ({}, 0, 10, OrderStatus.UNKNOWN),
    # ord_qty == 0 → UNKNOWN
    ({"ord_stts_name": "Open"}, 0, 0, OrderStatus.UNKNOWN),
    # Waiting
    ({"ord_stts_name": "Open"}, 0, 10, OrderStatus.SUBMITTED),
])
def test_kis_overseas_map_status(raw, fq, oq, expected):
    assert _US.map_status(raw, fq, oq) == expected


def test_kis_overseas_extract_filled_qty():
    assert _US.extract_filled_qty({"ft_ccld_qty": "7"}) == 7
    assert _US.extract_filled_qty({}) == 0


def test_kis_overseas_extract_order_qty():
    assert _US.extract_order_qty({"ft_ord_qty": "10"}) == 10


def test_kis_overseas_extract_avg_price():
    assert _US.extract_avg_price({"avg_prvs": "123.45"}) == pytest.approx(123.45)
    assert _US.extract_avg_price({"avg_prvs": ""}) == 0.0
    assert _US.extract_avg_price({}) == 0.0


def test_kis_overseas_extract_side():
    assert _US.extract_side({"sll_buy_dvsn_cd": "02"}) == "buy"
    assert _US.extract_side({"sll_buy_dvsn_cd": "01"}) == "sell"
    assert _US.extract_side({}) == "sell"


def test_kis_overseas_extract_broker_order_id():
    resp = {"output": {"ODNO": "US-9999"}}
    assert _US.extract_broker_order_id(resp) == "US-9999"


# ── KiwoomDomesticMapper ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,fq,oq", [
    ({}, 0, 100),
    ({"ccld_qty": "100", "ord_qty": "100"}, 100, 100),
    ({"ord_stts": "취소"}, 0, 100),
])
def test_kiwoom_always_unknown(raw, fq, oq):
    assert _KW.map_status(raw, fq, oq) == OrderStatus.UNKNOWN
