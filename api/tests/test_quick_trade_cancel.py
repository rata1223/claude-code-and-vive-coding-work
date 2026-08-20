"""P0 — cancelling a resting QuickTrade order.

A user could place a limit order and had no way to pull it: no UI control, no
route, no client method — while the broker supports cancellation the whole time.
An order that cannot be cancelled is an open-ended commitment the user cannot
withdraw, which on a limit order that never fills is the product quietly holding
their capital hostage.

Two properties matter more than the happy path:

* **Tenancy.** The cancel must go out on the *caller's* credential. The obvious
  implementation — ``KISBroker.cancel_order`` — is reached through a process
  singleton built from ``os.environ``, so it would pull an order on whatever
  account the process happens to hold. Guarded by an explicit sentinel below.
* **Truthfulness.** ``rt_cd != "0"`` means the order is *still resting*. If that
  renders as success the user believes they are flat when they are not, which is
  strictly worse than showing an error.
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
from api.schemas import CancelOrderRequest
from api.tests.test_quick_trade_close_position import (  # reuse the proven harness
    db,          # noqa: F401 - pytest fixtures
    engine,      # noqa: F401
    user,        # noqa: F401
)


class _FakeOrders:
    """Records cancel calls. ``exc`` simulates a transport failure."""

    def __init__(self, response=None, exc=None):
        self.calls = []
        self.response = response if response is not None else {"rt_cd": "0"}
        self.exc = exc

    def cancel_kr(self, org_order_no, symbol, qty, price):
        self.calls.append(("cancel_kr", org_order_no, symbol, qty, price))
        if self.exc:
            raise self.exc
        return self.response

    def cancel_us(self, org_order_no, symbol, excd, qty, price):
        self.calls.append(("cancel_us", org_order_no, symbol, excd, qty, price))
        if self.exc:
            raise self.exc
        return self.response


def _wire(monkeypatch, orders):
    monkeypatch.setattr(quick_trade, "_load_kis",
                        lambda cred: (object(), orders, object()))
    return orders


def _seed(db, *, user_id=1, credential_id=1, symbol="AAPL", side="sell",
          status=QT_SUBMITTED, broker_order_id="BRK-1", key="ck", qty=5,
          market="us", exchange="NASD"):
    row = QuickTradeOrder(
        user_id=user_id, credential_id=credential_id, idempotency_key=key,
        request_hash="h" + key, symbol=symbol, side=side, market=market,
        exchange=exchange, order_type="limit", qty=qty, price=175.5,
        status=status, broker_order_id=broker_order_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _body(order_id, credential_id=1):
    return CancelOrderRequest(credential_id=credential_id, order_id=order_id)


# ── happy path ────────────────────────────────────────────────────────────────

def test_a_submitted_us_order_is_cancelled(monkeypatch, db, user):
    row = _seed(db)
    orders = _wire(monkeypatch, _FakeOrders())

    resp = quick_trade.cancel_order(_body(row.id), user, db)

    assert resp.code == 1, resp.msg
    assert orders.calls == [("cancel_us", "BRK-1", "AAPL", "NASD", 5, 175.5)]
    db.refresh(row)
    assert row.status == QT_CANCELED


def test_a_kr_order_routes_to_the_kr_cancel(monkeypatch, db, user):
    row = _seed(db, symbol="069500", market="kr", broker_order_id="KR-9")
    orders = _wire(monkeypatch, _FakeOrders())

    resp = quick_trade.cancel_order(_body(row.id), user, db)

    assert resp.code == 1, resp.msg
    assert orders.calls == [("cancel_kr", "KR-9", "069500", 5, 175.5)]


# ── truthfulness: a refusal is never reported as success ──────────────────────

def test_a_broker_refusal_leaves_the_order_resting(monkeypatch, db, user):
    row = _seed(db)
    refusal = {"rt_cd": "1", "msg1": "취소할 수 있는 수량이 없습니다"}
    _wire(monkeypatch, _FakeOrders(response=refusal))

    resp = quick_trade.cancel_order(_body(row.id), user, db)

    assert resp.code == -1
    assert "취소할 수 있는 수량이 없습니다" in resp.msg
    db.refresh(row)
    assert row.status == QT_SUBMITTED, "a refused cancel must not mark it canceled"


def test_a_transport_failure_is_an_error_envelope_not_a_500(monkeypatch, db, user):
    row = _seed(db)
    _wire(monkeypatch, _FakeOrders(exc=RuntimeError("connection reset")))

    resp = quick_trade.cancel_order(_body(row.id), user, db)

    assert resp.code == -1
    assert "connection reset" in resp.msg
    db.refresh(row)
    assert row.status == QT_SUBMITTED


def test_a_response_without_rt_cd_is_treated_as_failure(monkeypatch, db, user):
    """Absent rt_cd is not consent. Fail closed."""
    row = _seed(db)
    _wire(monkeypatch, _FakeOrders(response={}))

    resp = quick_trade.cancel_order(_body(row.id), user, db)

    assert resp.code == -1
    db.refresh(row)
    assert row.status == QT_SUBMITTED


# ── tenancy ───────────────────────────────────────────────────────────────────

def test_the_process_singleton_is_never_used(monkeypatch, db, user):
    """The cancel must ride the caller's credential, not the process account."""
    import backend.brokers.kis as kis_mod

    def _boom():
        raise AssertionError("get_kis_broker() must not be used on the QT path")

    monkeypatch.setattr(kis_mod, "get_kis_broker", _boom)
    row = _seed(db, symbol="069500", market="kr")
    _wire(monkeypatch, _FakeOrders())

    resp = quick_trade.cancel_order(_body(row.id), user, db)

    assert resp.code == 1, resp.msg


def test_another_users_order_cannot_be_cancelled(monkeypatch, db, user):
    other = User(id=2, email="b@example.com", password_hash="x")
    db.add(other)
    db.add(Credential(id=2, user_id=2, name="kis", exchange_id="kis", env="paper"))
    db.commit()
    row = _seed(db, user_id=2, credential_id=2, key="theirs")
    orders = _wire(monkeypatch, _FakeOrders())

    resp = quick_trade.cancel_order(_body(row.id), user, db)

    assert resp.code == -1
    assert orders.calls == [], "the broker must never be contacted"
    db.refresh(row)
    assert row.status == QT_SUBMITTED


def test_a_missing_credential_is_refused(monkeypatch, db, user):
    row = _seed(db)
    orders = _wire(monkeypatch, _FakeOrders())

    resp = quick_trade.cancel_order(_body(row.id, credential_id=999), user, db)

    assert resp.code == -1
    assert orders.calls == []


# ── state guards ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [QT_RESERVED, QT_REJECTED, QT_FAILED,
                                    QT_BLOCKED, QT_CANCELED])
def test_non_cancellable_states_are_refused(monkeypatch, db, user, status):
    row = _seed(db, status=status)
    orders = _wire(monkeypatch, _FakeOrders())

    resp = quick_trade.cancel_order(_body(row.id), user, db)

    assert resp.code == -1
    assert orders.calls == [], "the broker must never be contacted"


def test_an_order_without_a_broker_id_is_refused(monkeypatch, db, user):
    row = _seed(db, broker_order_id=None)
    orders = _wire(monkeypatch, _FakeOrders())

    resp = quick_trade.cancel_order(_body(row.id), user, db)

    assert resp.code == -1
    assert orders.calls == []


def test_an_unknown_order_id_is_refused(monkeypatch, db, user):
    orders = _wire(monkeypatch, _FakeOrders())

    resp = quick_trade.cancel_order(_body(424242), user, db)

    assert resp.code == -1
    assert orders.calls == []


def test_cancelling_twice_is_refused_the_second_time(monkeypatch, db, user):
    row = _seed(db)
    orders = _wire(monkeypatch, _FakeOrders())

    assert quick_trade.cancel_order(_body(row.id), user, db).code == 1
    second = quick_trade.cancel_order(_body(row.id), user, db)

    assert second.code == -1
    assert len(orders.calls) == 1, "the broker must not be asked twice"
