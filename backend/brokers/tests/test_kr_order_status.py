"""
Stage 2 fix: KISBroker._get_kr_order_status must match the requested odno and
never fall back to output[0] (a different order's row).
"""
from backend.brokers.kis import KISBroker
from backend.brokers.models import OrderStatus


class _StubClient:
    def __init__(self, resp):
        self._resp = resp
    def get(self, path, tr_id, params):
        return self._resp


def _broker(resp):
    b = KISBroker.__new__(KISBroker)
    b._paper = True
    b._account = "123456789012"
    b._client = _StubClient(resp)
    return b


def _row(odno, pdno, filled, qty):
    return {
        "odno": odno, "pdno": pdno,
        "tot_ccld_qty": str(filled), "ord_qty": str(qty),
        "avg_prvs": "100.0", "ord_unpr": "100", "sll_buy_dvsn_cd": "02",
    }


def test_matches_requested_order_not_first_row():
    # Target order is the SECOND row; the first row is an unrelated order.
    resp = {"output1": [_row("ORD-999", "000660", 50, 50),
                        _row("ORD-123", "005930", 10, 10)]}
    order = _broker(resp)._get_kr_order_status("ORD-123")
    assert order is not None
    assert order.id == "ORD-123"
    assert order.symbol == "005930"          # matched row, not the first row
    assert order.status == OrderStatus.FILLED


def test_returns_none_when_no_row_matches():
    # Only an unrelated order is returned — must NOT adopt its status.
    resp = {"output1": [_row("ORD-999", "000660", 50, 50)]}
    assert _broker(resp)._get_kr_order_status("ORD-123") is None


def test_returns_none_on_empty_output():
    assert _broker({"output1": []})._get_kr_order_status("ORD-123") is None
