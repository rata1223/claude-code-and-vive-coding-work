"""#182 — ``decrypt_required`` / ``kis_credential_fields`` fail closed.

The router-level behaviour (no reservation, no client, recovery skip) lives in
``api/tests/test_quick_trade_credential_unreadable.py`` because it needs
FastAPI; this file needs only ``cryptography`` so it runs in CI.
"""
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

import api.crypto as crypto


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("KIS_CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(crypto, "_mismatch_reported", False)


def _stale(value: bytes) -> str:
    return Fernet(Fernet.generate_key()).encrypt(value).decode()


class TestDecryptRequired:
    def test_nothing_stored_is_none(self):
        assert crypto.decrypt_required(None, "hts_id") is None
        assert crypto.decrypt_required("", "hts_id") is None

    def test_a_readable_value_opens(self):
        assert crypto.decrypt_required(crypto.encrypt("abc"), "app_key") == "abc"

    def test_an_unreadable_value_raises_naming_only_the_field(self):
        stale = _stale(b"old-secret")
        with pytest.raises(crypto.CredentialUnreadable) as exc:
            crypto.decrypt_required(stale, "app_secret")
        msg = str(exc.value)
        assert exc.value.field == "app_secret" and "app_secret" in msg
        assert "old-secret" not in msg and stale not in msg

    def test_it_shares_the_one_warning_per_process(self, caplog):
        with caplog.at_level("WARNING", logger="api.crypto"):
            for _ in range(2):
                with pytest.raises(crypto.CredentialUnreadable):
                    crypto.decrypt_required(_stale(b"x"), "app_key")
            assert crypto.decrypt(_stale(b"x")) is None
        assert sum("복호화되지 않는다" in r.getMessage() for r in caplog.records) == 1


def _cred(**fields):
    base = dict(app_key_enc=None, app_secret_enc=None,
                account_no_enc=None, hts_id_enc=None)
    base.update(fields)
    return SimpleNamespace(**base)


class TestKisCredentialFields:
    def test_all_fields_open(self):
        cred = _cred(app_key_enc=crypto.encrypt("k"), app_secret_enc=crypto.encrypt("s"),
                     account_no_enc=crypto.encrypt("1234"), hts_id_enc=crypto.encrypt("h"))
        assert crypto.kis_credential_fields(cred) == {
            "app_key": "k", "app_secret": "s", "account_no": "1234", "hts_id": "h"}

    def test_absent_fields_are_empty_as_before(self):
        cred = _cred(app_key_enc=crypto.encrypt("k"), app_secret_enc=crypto.encrypt("s"))
        fields = crypto.kis_credential_fields(cred)
        assert fields["hts_id"] == "" and fields["account_no"] == ""

    @pytest.mark.parametrize("column, field", [
        ("app_key_enc", "app_key"), ("app_secret_enc", "app_secret"),
        ("account_no_enc", "account_no"), ("hts_id_enc", "hts_id"),
    ])
    def test_any_unreadable_field_fails_closed(self, column, field):
        cred = _cred(app_key_enc=crypto.encrypt("k"), app_secret_enc=crypto.encrypt("s"))
        setattr(cred, column, _stale(b"old"))
        with pytest.raises(crypto.CredentialUnreadable) as exc:
            crypto.kis_credential_fields(cred)
        assert exc.value.field == field
