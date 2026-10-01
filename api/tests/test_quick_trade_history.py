"""Quick Trade history lists the caller's own orders, not an empty table.

``get_history`` and the home screen's recent list read ``strategy_trades``,
which nothing writes, so both were always empty while the user's manual orders
sat in ``quick_trade_orders``. They now list those rows — orders with their
submission outcome, not fills. The worker's ``orders``/``fills`` are not used:
they belong to the single ``.env`` account and carry no user.
"""
from datetime import datetime, timedelta

import pytest

from api.models import (
    QT_BLOCKED, QT_CANCELED, QT_FAILED, QT_REJECTED, QT_RESERVED, QT_SUBMITTED,
    Credential, QuickTradeOrder, User,
)
from api.routers import dashboard, quick_trade
from api.tests.test_quick_trade_close_position import (  # reuse the proven harness
    db,          # noqa: F401 - pytest fixtures
    engine,      # noqa: F401
    user,        # noqa: F401
)

_T0 = datetime(2026, 9, 1, 12, 0, 0)


def _seed(db, n, *, user_id=1, credential_id=1, status=QT_SUBMITTED, symbol="AAPL"):
    row = QuickTradeOrder(
        user_id=user_id, credential_id=credential_id, idempotency_key=f"k{user_id}-{n}",
        request_hash=f"h{n}", symbol=symbol, side="buy", market="us", exchange="NASD",
        order_type="limit", qty=n, price=100.0 + n, status=status,
        broker_order_id=f"B{n}" if status in (QT_SUBMITTED, QT_CANCELED) else None,
        created_at=_T0 + timedelta(minutes=n),
    )
    db.add(row)
    db.commit()
    return row


def _history(user, db, **kw):
    kw = {"credential_id": None, "page": 1, "page_size": 20, **kw}
    resp = quick_trade.get_history(current_user=user, db=db, **kw)
    assert resp.code == 1, resp.msg
    return resp.data


def _other_user(db):
    db.add(User(id=2, email="b@example.com", password_hash="x"))
    db.add(Credential(id=2, user_id=2, name="kis", exchange_id="kis", env="paper"))
    db.commit()


# ── /quick-trade/history ───────────────────────────────────────────────────

@pytest.mark.parametrize("status", [QT_RESERVED, QT_SUBMITTED, QT_REJECTED,
                                    QT_FAILED, QT_BLOCKED, QT_CANCELED])
def test_an_order_in_any_status_is_listed_with_that_status(db, user, status):
    _seed(db, 1, status=status)

    data = _history(user, db)

    assert data["total"] == 1
    (item,) = data["items"]
    assert item["status"] == status
    assert (item["symbol"], item["side"], item["qty"], item["price"]) == ("AAPL", "buy", 1, 101.0)
    # stored naive UTC; the offset keeps a browser from reading it as local time
    assert item["created_at"] == "2026-09-01T12:01:00+00:00"


def test_it_is_an_order_list_not_a_fill_list(db, user):
    """No ``pnl``/``filled_at``: an order row has neither, and a 0 would read
    as a real figure."""
    _seed(db, 1)

    (item,) = _history(user, db)["items"]

    assert "pnl" not in item and "filled_at" not in item
    assert set(item) == {"id", "credential_id", "symbol", "side", "qty", "price", "market",
                         "exchange", "broker_order_id", "status", "created_at"}


def test_newest_first_and_paged(db, user):
    for n in range(1, 6):
        _seed(db, n)

    first = _history(user, db, page_size=2)
    second = _history(user, db, page=2, page_size=2)

    assert first["total"] == second["total"] == 5
    assert [i["qty"] for i in first["items"]] == [5, 4]
    assert [i["qty"] for i in second["items"]] == [3, 2]


def test_another_users_orders_are_never_listed(db, user):
    _other_user(db)
    _seed(db, 1, user_id=2, credential_id=2)

    assert _history(user, db) == {"total": 0, "items": []}


def test_the_credential_filter_narrows_to_one_account(db, user):
    db.add(Credential(id=3, user_id=1, name="kis2", exchange_id="kis", env="paper"))
    db.commit()
    _seed(db, 1, credential_id=1)
    _seed(db, 2, credential_id=3)

    assert [i["qty"] for i in _history(user, db)["items"]] == [2, 1]
    assert [i["qty"] for i in _history(user, db, credential_id=3)["items"]] == [2]


def test_another_users_credential_id_shows_nothing(db, user):
    """The filter narrows the caller's rows; it never widens to someone else's."""
    _other_user(db)
    _seed(db, 1, user_id=2, credential_id=2)

    assert _history(user, db, credential_id=2)["items"] == []


# ── home screen: /dashboard/summary.recent_orders ──────────────────────────

class _Portfolio:
    def get_kr_balance(self):
        return {"summary": {"tot_evlu_amt": "0"}, "positions": []}

    def get_us_balance(self):
        return {"summary": {"tot_evlu_amt": "0"}, "positions": []}


@pytest.fixture()
def summary(monkeypatch):
    monkeypatch.setattr(dashboard, "_build_kis_client_from_cred",
                        lambda _c: (object(), _Portfolio()))

    def call(user, db):
        resp = dashboard.get_summary(user, db)
        assert resp.code == 1, resp.msg
        return resp.data

    return call


def test_the_home_list_is_the_latest_five_orders(db, user, summary):
    for n in range(1, 8):
        _seed(db, n, status=QT_REJECTED if n == 7 else QT_SUBMITTED)

    recent = summary(user, db)["recent_orders"]

    assert [i["qty"] for i in recent] == [7, 6, 5, 4, 3]
    assert recent[0]["status"] == QT_REJECTED
    assert recent[0] == _history(user, db)["items"][0]     # one projection


def test_the_home_list_is_per_user(db, user, summary):
    _other_user(db)
    _seed(db, 1, user_id=2, credential_id=2)

    data = summary(user, db)
    assert data["recent_orders"] == []
    assert "recent_trades" not in data
