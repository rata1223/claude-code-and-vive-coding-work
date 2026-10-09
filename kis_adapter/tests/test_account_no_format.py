"""P3-02 — a KIS account reaches KIS as CANO (8) + ACNT_PRDT_CD (2).

The order, balance and quote paths split the account as ``[:8]``/``[8:]``. The
usual written form is ``50123456-01`` (it is what ``.env.example`` shows), and
kept as typed the hyphen became the product code: ``-01``. The account is now
normalised where it enters ``KISAuth`` — from the env and from injected
credentials alike — so either form works and a plain 10 digits is unchanged.
"""
import pytest

from kis_adapter import KISCredentials, KISOrders
from kis_adapter.auth import KISAuth, normalize_account_no


class _NoRedis:
    def get(self, *a, **k):
        return None

    def set(self, *a, **k):
        return None

    def delete(self, *a, **k):
        return None


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr("redis.from_url", lambda *a, **k: _NoRedis())


@pytest.mark.parametrize("written", ["50123456-01", "5012345601", " 50123456 - 01 ", "50123456\t01"])
def test_the_written_forms_normalise_to_ten_digits(written):
    assert normalize_account_no(written) == "5012345601"


def test_nothing_but_hyphens_and_whitespace_is_removed():
    """Other characters stay, for the API or KIS to reject — not to be
    silently turned into a different account."""
    assert normalize_account_no("50123456_01") == "50123456_01"
    assert normalize_account_no(None) == ""


def _order_body(orders):
    sent = {}

    def post(path, tr_id, body, **kw):
        sent.update(body)
        return {"rt_cd": "0"}
    orders._client.post = post
    orders.buy_kr("005930", 1, 70000)
    return sent["CANO"], sent["ACNT_PRDT_CD"]


def test_the_env_account_with_a_hyphen_splits_correctly(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY", "K")
    monkeypatch.setenv("KIS_APP_SECRET", "S")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "50123456-01")
    monkeypatch.setenv("KIS_ENV", "paper")

    assert _order_body(KISOrders()) == ("50123456", "01")


def test_injected_credentials_with_a_hyphen_split_correctly():
    creds = KISCredentials(app_key="K", app_secret="S", account_no="50123456-01", env="paper")
    assert KISAuth(creds).account_no == "5012345601"
    assert _order_body(KISOrders(credentials=creds)) == ("50123456", "01")


def test_the_env_fallback_in_require_account_is_normalised(monkeypatch):
    """Env-sourced auth whose account was empty at construction reads the env
    at use time — that read is normalised too."""
    monkeypatch.setenv("KIS_APP_KEY", "K")
    monkeypatch.setenv("KIS_APP_SECRET", "S")
    monkeypatch.delenv("KIS_ACCOUNT_NO", raising=False)
    auth = KISAuth()
    monkeypatch.setenv("KIS_ACCOUNT_NO", "50123456-01")
    assert auth.require_account() == "5012345601"


def test_a_stored_account_of_the_wrong_length_is_reported(caplog):
    """Rows saved before the check (the old mobile form asked for 12)."""
    creds = KISCredentials(app_key="K", app_secret="S", account_no="123456789012", env="paper")
    with caplog.at_level("WARNING", logger="kis_adapter.auth"):
        KISAuth(creds)
    assert "10자리" in caplog.text


def test_the_worker_broker_splits_a_hyphenated_env_account(monkeypatch):
    """`backend/brokers/kis.py` cancels and status queries split the account
    themselves; they read it through the auth, normalised."""
    monkeypatch.setenv("KIS_APP_KEY", "K")
    monkeypatch.setenv("KIS_APP_SECRET", "S")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "50123456-01")
    monkeypatch.setenv("KIS_ENV", "paper")
    from backend.brokers.kis import KISBroker
    broker = KISBroker()
    assert (broker._account[:8], broker._account[8:]) == ("50123456", "01")


def test_digits_of_other_scripts_are_not_an_account():
    from kis_adapter.auth import is_kis_account_no
    assert is_kis_account_no("5012345601")
    assert not is_kis_account_no("50123456٠١")
    assert not is_kis_account_no("501234560")
