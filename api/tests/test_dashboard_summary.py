"""The dashboard never reports an unknown portfolio as zeros.

``GET /api/dashboard/summary`` used to wrap both markets in one ``try`` and
fall back to 0 on any failure, so a broker outage, an unreadable credential,
a one-sided failure and a genuinely empty account all read "total assets 0"
on the home screen. Unknown values are now ``None`` and ``portfolio_status``
says why; a real zero stays a zero. ``/pendingOrders`` likewise stops turning
a failed lookup into "nothing pending".
"""
import pytest
from cryptography.fernet import Fernet

import api.crypto as crypto
from api.models import Credential
from api.routers import dashboard
from api.tests.test_quick_trade_close_position import (
    db,          # noqa: F401 - pytest fixtures
    engine,      # noqa: F401
    user,        # noqa: F401
)

_KR_FIELDS = ("total_assets_krw", "total_profit_krw", "total_profit_rate", "kr_positions")
_US_FIELDS = ("total_assets_usd", "us_positions")
_SECRET = "EGW00123 internal token detail"


class _Portfolio:
    def __init__(self, kr=None, us=None):
        self.kr, self.us = kr, us

    @staticmethod
    def _answer(value):
        if isinstance(value, Exception):
            raise value
        return value

    def get_kr_balance(self):
        return self._answer(self.kr)

    def get_us_balance(self):
        return self._answer(self.us)


def _kr(eval_amt="1100000", pnl="100000"):
    return {"summary": {"tot_evlu_amt": eval_amt, "evlu_pfls_smtl_amt": pnl},
            "positions": [{"pdno": "069500"}]}


def _us(eval_amt="1234.5"):
    return {"summary": {"tot_evlu_amt": eval_amt}, "positions": [{"ovrs_pdno": "AAPL"}]}


@pytest.fixture()
def portfolio(monkeypatch):
    holder = {}

    def build(_cred):
        return object(), holder["p"]

    monkeypatch.setattr(dashboard, "_build_kis_client_from_cred", build)

    def use(p):
        holder["p"] = p

    return use


def _summary(user, db):
    resp = dashboard.get_summary(user, db)
    assert resp.code == 1, resp.msg      # DB figures are valid either way
    return resp.data


def test_both_markets_read(db, user, portfolio):
    portfolio(_Portfolio(kr=_kr(), us=_us()))
    data = _summary(user, db)

    assert data["portfolio_status"] == "ok"
    assert data["portfolio_errors"] == {}
    assert data["total_assets_krw"] == 1_100_000.0
    assert data["total_profit_krw"] == 100_000.0
    assert data["total_profit_rate"] == 10.0
    assert data["total_assets_usd"] == 1234.5
    assert data["kr_positions"] == [{"pdno": "069500"}]
    assert data["us_positions"] == [{"ovrs_pdno": "AAPL"}]


def test_an_empty_account_is_still_zero(db, user, portfolio):
    portfolio(_Portfolio(kr={"summary": {"tot_evlu_amt": "0"}, "positions": []},
                         us={"summary": {"tot_evlu_amt": "0"}, "positions": []}))
    data = _summary(user, db)

    assert data["portfolio_status"] == "ok"
    assert data["total_assets_krw"] == 0.0 and data["total_assets_usd"] == 0.0
    assert data["total_profit_rate"] == 0.0
    assert data["kr_positions"] == [] and data["us_positions"] == []


def test_a_position_written_down_to_zero_is_minus_100_percent(db, user, portfolio):
    """Review: the old ``eval > 0`` guard reported a total loss as 0%."""
    portfolio(_Portfolio(kr=_kr(eval_amt="0", pnl="-500000"), us=_us()))
    data = _summary(user, db)

    assert data["total_assets_krw"] == 0.0
    assert data["total_profit_rate"] == -100.0


def test_one_failing_market_does_not_blank_the_other(db, user, portfolio):
    portfolio(_Portfolio(kr=RuntimeError(_SECRET), us=_us()))
    data = _summary(user, db)

    assert data["portfolio_status"] == "partial"
    assert set(data["portfolio_errors"]) == {"kr"}
    assert _SECRET not in data["portfolio_errors"]["kr"]   # detail stays in the log
    assert all(data[f] is None for f in _KR_FIELDS)
    assert data["total_assets_usd"] == 1234.5


def test_the_us_side_failing_is_partial_too(db, user, portfolio):
    portfolio(_Portfolio(kr=_kr(), us=RuntimeError(_SECRET)))
    data = _summary(user, db)

    assert data["portfolio_status"] == "partial"
    assert set(data["portfolio_errors"]) == {"us"}
    assert all(data[f] is None for f in _US_FIELDS)
    assert data["total_assets_krw"] == 1_100_000.0


def test_an_outage_is_unknown_not_zero(db, user, portfolio):
    portfolio(_Portfolio(kr=RuntimeError(_SECRET), us=ConnectionError(_SECRET)))
    data = _summary(user, db)

    assert data["portfolio_status"] == "unavailable"
    assert set(data["portfolio_errors"]) == {"kr", "us"}
    assert all(data[f] is None for f in _KR_FIELDS + _US_FIELDS)
    assert data["strategy_count"] == 0 and data["recent_orders"] == []


def test_a_one_row_list_summary_is_read(db, user, portfolio):
    """KIS returns ``output2`` as a dict or a one-row list; both read the same."""
    kr = _kr()
    kr["summary"] = [kr["summary"]]
    portfolio(_Portfolio(kr=kr, us=_us()))
    data = _summary(user, db)

    assert data["portfolio_status"] == "ok"
    assert data["total_assets_krw"] == 1_100_000.0
    assert data["total_profit_rate"] == 10.0


@pytest.mark.parametrize("bad_kr", [
    {"summary": "garbage", "positions": []},                       # wrong shape
    {"summary": {"tot_evlu_amt": "n/a"}, "positions": []},        # not a number
])
def test_an_unparseable_response_fails_that_market_not_the_summary(
        db, user, portfolio, bad_kr):
    """Code review: parsing sat outside the guard, so a bad shape was a 500
    that also took the strategy counts and recent trades with it."""
    portfolio(_Portfolio(kr=bad_kr, us=_us()))
    data = _summary(user, db)

    assert data["portfolio_status"] == "partial"
    assert set(data["portfolio_errors"]) == {"kr"}
    assert all(data[f] is None for f in _KR_FIELDS)
    assert data["total_assets_usd"] == 1234.5


def test_no_credential_is_unknown_not_zero(db, user):
    db.query(Credential).delete()
    db.commit()
    data = _summary(user, db)

    assert data["portfolio_status"] == "no_credential"
    assert data["portfolio_errors"] == {}
    assert all(data[f] is None for f in _KR_FIELDS + _US_FIELDS)


def test_an_unreadable_credential_names_the_field(db, user, monkeypatch):
    import kis_adapter

    def boom(*_a, **_k):
        raise AssertionError("a KIS client was built from an unreadable credential")

    monkeypatch.setattr(kis_adapter, "KISClient", boom)
    cred = db.get(Credential, 1)
    cred.app_key_enc = crypto.encrypt("app-key")
    cred.app_secret_enc = Fernet(Fernet.generate_key()).encrypt(b"old").decode()
    cred.account_no_enc = crypto.encrypt("12345678-01")
    db.commit()

    data = _summary(user, db)

    assert data["portfolio_status"] == "unavailable"
    assert "app_secret" in data["portfolio_errors"]["credential"]
    assert all(data[f] is None for f in _KR_FIELDS + _US_FIELDS)


class TestPendingOrders:
    def test_a_failed_lookup_is_an_error_not_nothing_pending(self, db, user, monkeypatch):
        import kis_adapter

        class _MD:
            def __init__(self, client):
                pass

            def get_pending_us(self, account_no):
                raise RuntimeError(_SECRET)

        class _Client:
            class auth:
                account_no = "12345678-01"

        monkeypatch.setattr(kis_adapter, "KISMarketData", _MD)
        monkeypatch.setattr(dashboard, "_build_kis_client_from_cred",
                            lambda _c: (_Client(), None))

        resp = dashboard.get_pending_orders(1, user, db)

        assert resp.code == -1
        assert _SECRET not in resp.msg

    def test_no_credential_is_still_an_empty_list(self, db, user):
        db.query(Credential).delete()
        db.commit()

        resp = dashboard.get_pending_orders(None, user, db)

        assert resp.code == 1
        assert resp.data == {"items": []}


class TestPerformance:
    """Home KPIs: a ratio with nothing to divide is ``None`` ("—"), never 0."""

    @staticmethod
    def _trades(db, *pnls, user_id=1, strategy_id=10):
        from api.models import Strategy, Trade, User

        if not db.get(User, user_id):
            db.add(User(id=user_id, email=f"u{user_id}@example.com", password_hash="x"))
        if not db.get(Strategy, strategy_id):
            db.add(Strategy(id=strategy_id, user_id=user_id, name="s", type="script"))
        for p in pnls:
            db.add(Trade(strategy_id=strategy_id, symbol="AAPL", side="sell",
                         qty=1, price=100.0, pnl=p))
        db.commit()

    def test_no_trades_is_unknown_not_zero(self, db, user, portfolio):
        portfolio(_Portfolio(kr=_kr(), us=_us()))
        perf = _summary(user, db)["performance"]
        # nothing writes strategy_trades yet: an empty table is "not recorded"
        assert perf == {"total_trades": None, "win_rate": None, "profit_factor": None}

    def test_win_rate_and_profit_factor_come_from_closed_trades(self, db, user, portfolio):
        portfolio(_Portfolio(kr=_kr(), us=_us()))
        self._trades(db, 0.0, 30.0, 10.0, -20.0)      # 0.0 is an opening buy
        perf = _summary(user, db)["performance"]
        assert perf["total_trades"] == 4
        assert perf["win_rate"] == 66.7                # 2 of 3 closed
        assert perf["profit_factor"] == 2.0            # 40 / 20

    def test_no_losing_trade_has_no_profit_factor(self, db, user, portfolio):
        portfolio(_Portfolio(kr=_kr(), us=_us()))
        self._trades(db, 5.0)
        perf = _summary(user, db)["performance"]
        assert perf["win_rate"] == 100.0
        assert perf["profit_factor"] is None

    def test_other_users_trades_are_not_counted(self, db, user, portfolio):
        portfolio(_Portfolio(kr=_kr(), us=_us()))
        self._trades(db, 50.0, user_id=2, strategy_id=20)
        assert _summary(user, db)["performance"]["total_trades"] is None

    def test_the_performance_block_survives_a_broker_outage(self, db, user, portfolio):
        portfolio(_Portfolio(kr=RuntimeError(_SECRET), us=RuntimeError(_SECRET)))
        self._trades(db, 5.0)
        data = _summary(user, db)
        assert data["portfolio_status"] == "unavailable"
        assert data["performance"]["total_trades"] == 1
