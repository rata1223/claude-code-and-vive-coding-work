"""#182 — a stored credential that does not open never reaches the broker blank.

``decrypt()`` returns None for a value that does not open under the current
``KIS_CREDENTIAL_KEY``, and every broker path took it as ``decrypt(...) or ""``:
an unreadable app key became an empty one and KIS was called with it. For a
QuickTrade buy that was worse than an auth error — the client is built inside
``broker_submit``, after the reservation commits, so the failure was read as an
indeterminate submit and the order sat RESERVED for a recovery sweep that could
not open the credential either.

Now a stored-but-unreadable field raises ``CredentialUnreadable`` before any
client is built, and ``place_order`` checks it before reserving. An absent
(NULL) field is still allowed, as before.
"""
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet

import api.crypto as crypto
from api.models import Credential, QT_RESERVED, QT_SUBMITTED, QuickTradeOrder
from api.routers import dashboard, quick_trade
from api.schemas import PlaceOrderRequest
from api.services.quick_trade_recovery import recover_reserved_orders
from api.tests.test_quick_trade_close_position import (
    FakePortfolio,
    db,          # noqa: F401 - pytest fixtures
    engine,      # noqa: F401
    user,        # noqa: F401
)

_STALE = Fernet(Fernet.generate_key())


@pytest.fixture()
def no_client(monkeypatch):
    """Any KIS client construction fails the test — nothing may be built."""
    import kis_adapter

    def boom(*_a, **_k):
        raise AssertionError("a KIS client was built from an unreadable credential")

    monkeypatch.setattr(kis_adapter, "KISClient", boom)


def _set_fields(db, **fields):
    cred = db.get(Credential, 1)
    for column, value in fields.items():
        setattr(cred, column, value)
    db.commit()
    return cred


def _stale_secret(db):
    return _set_fields(db, app_key_enc=crypto.encrypt("app-key"),
                       app_secret_enc=_STALE.encrypt(b"old-secret").decode(),
                       account_no_enc=crypto.encrypt("12345678-01"))


def _allow():
    return lambda: None


class _Orders:
    def __init__(self):
        self.calls = []

    def buy_us(self, symbol, excd, qty, price):
        self.calls.append(("buy_us", symbol))
        return {"output": {"ODNO": "BRK-1"}}


def _buy(qty=3):
    return PlaceOrderRequest(credential_id=1, symbol="AAPL", side="buy",
                             qty=qty, price=175.5, market="us", exchange="NASD")


class TestPlaceOrder:
    def test_an_unreadable_credential_is_rejected_before_reserving(
            self, db, user, no_client):
        _stale_secret(db)

        resp = quick_trade.place_order(_buy(), None, user, db, _allow())

        assert resp.code == -1
        assert "app_secret" in resp.msg and "다시 입력" in resp.msg
        assert "old-secret" not in resp.msg
        assert db.query(QuickTradeOrder).count() == 0

    def test_a_sell_is_rejected_before_the_live_sellable_lookup(
            self, db, user, no_client):
        _stale_secret(db)
        body = PlaceOrderRequest(credential_id=1, symbol="AAPL", side="sell",
                                 qty=1, price=175.5, market="us", exchange="NASD")

        resp = quick_trade.place_order(body, None, user, db, _allow())

        assert resp.code == -1 and "app_secret" in resp.msg
        assert db.query(QuickTradeOrder).count() == 0

    def test_a_replay_of_an_existing_reservation_still_reports_it(
            self, db, user, monkeypatch):
        """The replay never reaches the broker, so the check must not hide the
        state of an order that was already sent.

        An explicit key, as a real retry sends: the server-derived key includes
        a 10-second time bucket, so two calls straddling a boundary were not a
        replay at all and the test failed intermittently (#185).
        """
        orders = _Orders()
        monkeypatch.setattr(quick_trade, "_load_kis",
                            lambda cred: (object(), orders, FakePortfolio()))
        first = quick_trade.place_order(_buy(), "retry-1", user, db, _allow())
        assert first.code == 1, first.msg

        _stale_secret(db)
        again = quick_trade.place_order(_buy(), "retry-1", user, db, _allow())

        assert again.code == 1, again.msg
        assert again.data["status"] == QT_SUBMITTED
        assert orders.calls == [("buy_us", "AAPL")]      # not re-sent

    def test_absent_optional_fields_still_trade(self, db, user, monkeypatch):
        """NULL is not unreadable: a credential without an HTS id is valid."""
        _set_fields(db, app_key_enc=crypto.encrypt("k"),
                    app_secret_enc=crypto.encrypt("s"), hts_id_enc=None)
        orders = _Orders()
        monkeypatch.setattr(quick_trade, "_load_kis",
                            lambda cred: (object(), orders, FakePortfolio()))

        resp = quick_trade.place_order(_buy(), None, user, db, _allow())

        assert resp.code == 1, resp.msg


class TestReadPaths:
    def test_position_says_the_credential_is_unreadable_not_no_position(
            self, db, user, no_client):
        _stale_secret(db)

        resp = quick_trade.get_position(1, "AAPL", "us", user, db)

        assert resp.code == -1
        assert "app_secret" in resp.msg

    def test_balance_reports_the_cause(self, db, user, no_client):
        _stale_secret(db)

        resp = quick_trade.get_balance(1, "us", user, db)

        assert resp.code == -1 and "app_secret" in resp.msg

    def test_client_builders_raise_before_building(self, db, no_client):
        cred = _stale_secret(db)
        with pytest.raises(crypto.CredentialUnreadable):
            quick_trade._load_kis(cred)
        with pytest.raises(crypto.CredentialUnreadable):
            dashboard._build_kis_client_from_cred(cred)

    def test_pending_orders_say_the_credential_is_unreadable_not_none_pending(
            self, db, user, no_client):
        """PR #184 review: an empty list read as "nothing pending"."""
        _stale_secret(db)

        resp = dashboard.get_pending_orders(1, user, db)

        assert resp.code == -1
        assert "app_secret" in resp.msg

    def test_pending_orders_use_the_account_number_the_client_was_built_with(
            self, db, user, monkeypatch):
        import kis_adapter

        seen = []

        class _MD:
            def __init__(self, client):
                pass

            def get_pending_us(self, account_no):
                seen.append(account_no)
                return []

        monkeypatch.setattr(kis_adapter, "KISMarketData", _MD)
        _set_fields(db, app_key_enc=crypto.encrypt("k"),
                    app_secret_enc=crypto.encrypt("s"),
                    account_no_enc=crypto.encrypt("12345678-01"))

        resp = dashboard.get_pending_orders(1, user, db)

        assert resp.code == 1
        assert seen == ["12345678-01"]


def test_the_recovery_sweep_skips_instead_of_querying_with_blank_credentials(
        db, no_client):
    """The real ``_load_kis``: the order stays RESERVED, skipped as a client error."""
    _stale_secret(db)
    db.add(QuickTradeOrder(
        id=1, user_id=1, credential_id=1, idempotency_key="k-1", request_hash="h-1",
        symbol="AAPL", side="buy", market="us", exchange="NASD", order_type="limit",
        qty=1.0, price=1.0, status=QT_RESERVED,
        created_at=datetime.utcnow() - timedelta(seconds=300),
    ))
    db.commit()

    summary = recover_reserved_orders(db, load_kis=quick_trade._load_kis)

    assert db.get(QuickTradeOrder, 1).status == QT_RESERVED
    assert summary.skip_reasons == {"client_error": 1}
