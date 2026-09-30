"""A failed order lookup is "unknown", never "absent" (CLAUDE.md known issue 6).

``KISBroker._get_{kr,us}_order_status`` used to turn every exception into
``None``, and ``PositionReconciler`` reads ``None`` as "the broker has no such
order": for an order over an hour old it cancels it at the broker and commits
the row CANCELED. A timeout or a pagination error thus became a financial state
change. The broker now raises when it could not ask, and the reconciler — which
already treats an exception as an error — leaves the order alone.

No network: the KIS client is a scripted fake.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from backend.brokers.kis import KISBroker
from backend.database.models import Base, Order as DBOrder
from backend.database.testing import make_test_engine
from backend.execution.reconciler import PositionReconciler


class _Client:
    """``get_page`` plays back ``pages``; an Exception entry is raised."""

    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = 0

    def get_page(self, path, tr_id, params, tr_cont=""):
        self.calls += 1
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def _broker(client):
    b = KISBroker.__new__(KISBroker)
    b._paper = True
    b._account = "123456789012"
    b._client = client
    return b


def _row(odno):
    return {"odno": odno, "pdno": "005930", "tot_ccld_qty": "0", "ord_qty": "10",
            "avg_prvs": "0", "ord_unpr": "100", "sll_buy_dvsn_cd": "02",
            "ft_ccld_qty": "0", "ft_ord_qty": "10", "ft_ccld_unpr3": "0",
            "ft_ord_unpr3": "100"}


# ── the broker says what it knows ──────────────────────────────────────────

class TestBrokerContract:
    @pytest.mark.parametrize("market", ["kr", "us"])
    def test_a_failed_lookup_raises(self, market):
        b = _broker(_Client(ConnectionError("timed out")))
        with pytest.raises(RuntimeError, match="주문 조회 실패"):
            if market == "kr":
                b._get_kr_order_status("A")
            else:
                b._get_us_order_status("A", "AAPL")

    def test_get_order_status_routes_the_raise_too(self):
        with pytest.raises(RuntimeError):
            _broker(_Client(ConnectionError("x"))).get_order_status("A", "005930")

    def test_a_malformed_page_raises(self):
        # continuation without a key: pagination refuses to return a partial list
        b = _broker(_Client(({"output1": [_row("Z")], "ctx_area_nk100": ""}, "M")))
        with pytest.raises(RuntimeError):
            b._get_kr_order_status("A")

    @pytest.mark.parametrize("market", ["kr", "us"])
    def test_a_completed_search_without_the_order_is_none(self, market):
        key = "output1" if market == "kr" else "output"
        b = _broker(_Client(({key: [_row("Z")]}, "D")))
        found = (b._get_kr_order_status("A") if market == "kr"
                 else b._get_us_order_status("A", "AAPL"))
        assert found is None

    def test_an_order_on_page_one_survives_a_page_two_failure(self):
        b = _broker(_Client(({"output1": [_row("A")], "ctx_area_nk100": "K"}, "M"),
                            ConnectionError("page 2")))
        assert b._get_kr_order_status("A").id == "A"


# ── end to end: the reconciler does not cancel an order it could not see ───

@pytest.fixture()
def db_factory():
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _stale_order(factory):
    sess = factory()
    row = DBOrder(broker_order_id="A", symbol="005930", side="buy", qty=10, price=100.0,
                  status="submitted", market="KR", broker="kis",
                  created_at=datetime.utcnow() - timedelta(hours=2))
    sess.add(row)
    sess.commit()
    order_id = row.id
    sess.close()
    return order_id


def _reconcile(factory, kis_broker):
    """The real reconciler over a broker whose order lookup is the real
    ``KISBroker`` one; positions are empty and cancels are recorded."""
    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.get_order_status.side_effect = kis_broker.get_order_status
    result = PositionReconciler(broker=broker, db_factory=factory, redis_client=None,
                                broker_name="kis").reconcile("periodic")
    return result, broker


def _status(factory, order_id):
    sess = factory()
    try:
        return sess.get(DBOrder, order_id).status
    finally:
        sess.close()


def test_a_failed_lookup_leaves_the_order_alone(db_factory):
    order_id = _stale_order(db_factory)
    result, broker = _reconcile(db_factory, _broker(_Client(ConnectionError("timed out"))))

    assert _status(db_factory, order_id) == "submitted"
    assert result.errors and "주문 조회 오류" in result.errors[0]
    assert not any(g["kind"] == "lost_order" for g in result.gaps)
    broker.cancel_order.assert_not_called()


def test_a_confirmed_absence_is_still_handled_as_lost(db_factory):
    order_id = _stale_order(db_factory)
    result, broker = _reconcile(db_factory, _broker(_Client(({"output1": []}, "D"))))

    assert _status(db_factory, order_id) == "canceled"
    assert any(g["kind"] == "lost_order" for g in result.gaps)
    broker.cancel_order.assert_called_once()
