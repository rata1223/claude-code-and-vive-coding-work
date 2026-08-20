"""P0 — the open-orders list that makes cancellation reachable.

A cancel endpoint with nothing to cancel is not a feature. ``get_history``
queries ``Trade ⋈ Strategy`` and never touches ``quick_trade_orders``, so the
user's own manual orders are invisible in the UI — there is no row to put a
Cancel button on.

This is the **minimum** read path, not the history fix: only orders that can
actually be cancelled (broker-acknowledged, with a broker id) are listed.
Rewriting ``get_history`` is out of scope.
"""
import pytest

from api.models import (
    QT_BLOCKED,
    QT_CANCELED,
    QT_FAILED,
    QT_REJECTED,
    QT_RESERVED,
    QT_SUBMITTED,
    Credential,
    QuickTradeOrder,
    User,
)
from api.routers import quick_trade
from api.tests.test_quick_trade_close_position import (  # reuse the proven harness
    db,          # noqa: F401 - pytest fixtures
    engine,      # noqa: F401
    user,        # noqa: F401
)


def _seed(db, *, user_id=1, credential_id=1, symbol="AAPL", side="sell",
          status=QT_SUBMITTED, broker_order_id="BRK-1", key=None, qty=5):
    key = key or f"k-{symbol}-{status}-{broker_order_id}-{qty}"
    row = QuickTradeOrder(
        user_id=user_id, credential_id=credential_id, idempotency_key=key,
        request_hash="h" + key, symbol=symbol, side=side, market="us",
        exchange="NASD", order_type="limit", qty=qty, price=175.5,
        status=status, broker_order_id=broker_order_id,
    )
    db.add(row)
    db.commit()
    return row


def test_a_submitted_order_is_listed(db, user):
    _seed(db)

    resp = quick_trade.get_open_orders(1, user, db)

    assert resp.code == 1, resp.msg
    (item,) = resp.data["items"]
    assert item["symbol"] == "AAPL"
    assert item["broker_order_id"] == "BRK-1"
    assert item["status"] == QT_SUBMITTED


def test_the_projection_carries_what_a_cancel_needs(db, user):
    _seed(db)

    (item,) = quick_trade.get_open_orders(1, user, db).data["items"]

    for field in ("id", "symbol", "side", "qty", "price", "market",
                  "exchange", "broker_order_id", "status", "created_at"):
        assert field in item, f"missing {field}"


@pytest.mark.parametrize("status", [QT_RESERVED, QT_REJECTED, QT_FAILED,
                                    QT_BLOCKED, QT_CANCELED])
def test_non_cancellable_states_are_not_listed(db, user, status):
    """Only a broker-acknowledged order can be pulled. Listing a reserved or
    already-terminal row would offer the user an action that cannot work."""
    _seed(db, status=status)

    assert quick_trade.get_open_orders(1, user, db).data["items"] == []


def test_an_order_without_a_broker_id_is_not_listed(db, user):
    """Submitted but no ODNO means we have nothing to name in the cancel."""
    _seed(db, broker_order_id=None)

    assert quick_trade.get_open_orders(1, user, db).data["items"] == []


def test_another_users_order_is_never_listed(db, user):
    other = User(id=2, email="b@example.com", password_hash="x")
    db.add(other)
    db.add(Credential(id=2, user_id=2, name="kis", exchange_id="kis", env="paper"))
    db.commit()
    _seed(db, user_id=2, credential_id=2, key="other-user")

    assert quick_trade.get_open_orders(1, user, db).data["items"] == []


def test_another_credential_is_not_listed(db, user):
    """The list is per-account: a second brokerage account's resting orders are
    not this account's to cancel."""
    db.add(Credential(id=2, user_id=1, name="kis2", exchange_id="kis", env="paper"))
    db.commit()
    _seed(db, credential_id=2, key="other-cred")

    assert quick_trade.get_open_orders(1, user, db).data["items"] == []


def test_a_missing_credential_is_reported(db, user):
    resp = quick_trade.get_open_orders(999, user, db)

    assert resp.code == -1
    assert "credential" in resp.msg.lower()


def test_buys_are_listed_too(db, user):
    """Cancellation is not a sell-side concern — a resting buy is just as stuck."""
    _seed(db, side="buy", symbol="MSFT")

    (item,) = quick_trade.get_open_orders(1, user, db).data["items"]
    assert item["side"] == "buy"
