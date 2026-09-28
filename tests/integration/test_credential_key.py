"""ROADMAP P0-11 — the credential key is checked when the API starts.

What the roadmap item asked for was already mostly true, or did not apply:
``docker-compose.yml`` refuses to start the ``api`` service without
``KIS_CREDENTIAL_KEY`` (``${…:?…}``), and the worker never decrypts stored
credentials. Two gaps were real:

* a **malformed** key surfaced only on the first credential request, as a 500,
  long after the API had reported itself up;
* a **valid key that does not match** the stored data (rotated, mistyped) was
  silent: ``decrypt`` swallowed the error and returned None, and callers sent
  an empty app key to the broker.

Runs on SQLite with the API's own models; needs only ``cryptography``.
"""
import logging

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import api.crypto as crypto


@pytest.fixture()
def key(monkeypatch):
    """A fresh valid key, and the module's cached state reset around it."""
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("KIS_CREDENTIAL_KEY", k)
    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(crypto, "_mismatch_reported", False)
    return k


@pytest.fixture()
def factory():
    from api.database import Base
    import api.models  # noqa: F401 - registers the tables
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _store(factory, *ciphertexts):
    from api.models import Credential
    with factory() as s:
        for i, c in enumerate(ciphertexts):
            s.add(Credential(user_id=1, name=f"c{i}", exchange_id="kis", app_key_enc=c))
        s.commit()


class TestTheKeyItself:
    def test_a_missing_key_fails_startup(self, monkeypatch):
        monkeypatch.delenv("KIS_CREDENTIAL_KEY", raising=False)
        monkeypatch.setattr(crypto, "_fernet", None)
        with pytest.raises(RuntimeError, match="not set"):
            crypto.validate_key()

    def test_a_malformed_key_fails_startup(self, monkeypatch):
        monkeypatch.setenv("KIS_CREDENTIAL_KEY", "not-a-fernet-key")
        monkeypatch.setattr(crypto, "_fernet", None)
        with pytest.raises(RuntimeError, match="not a valid Fernet key"):
            crypto.validate_key()

    def test_a_good_key_passes(self, key):
        assert crypto.validate_key() == 0


class TestAKeyThatDoesNotMatchTheStoredData:
    def test_matching_credentials_report_nothing(self, key, factory):
        _store(factory, crypto.encrypt("app-key-1"), crypto.encrypt("app-key-2"))
        assert crypto.validate_key(factory) == 0

    def test_mismatched_credentials_are_counted_and_logged_without_values(
            self, key, factory, caplog):
        other = Fernet(Fernet.generate_key())
        stale = other.encrypt(b"old-app-key").decode()
        _store(factory, crypto.encrypt("app-key-1"), stale, stale)

        with caplog.at_level(logging.CRITICAL, logger="api.crypto"):
            assert crypto.validate_key(factory) == 2

        text = " ".join(r.getMessage() for r in caplog.records)
        assert "2/3" in text
        assert stale not in text and "old-app-key" not in text

    def test_a_mismatch_does_not_refuse_startup(self, key, factory):
        """Re-entering credentials through the API is the fix — refusing to
        start would lock the operator out of it."""
        stale = Fernet(Fernet.generate_key()).encrypt(b"x").decode()
        _store(factory, stale)
        crypto.validate_key(factory)            # returns, does not raise

    def test_every_encrypted_field_is_checked_not_only_the_app_key(self, key, factory):
        """PR #183 review: an app key that opens next to a secret that does not
        is just as broken — the request paths read every field."""
        from api.models import Credential
        other = Fernet(Fernet.generate_key())
        with factory() as s:
            s.add(Credential(user_id=1, name="half", exchange_id="kis",
                             app_key_enc=crypto.encrypt("app-key"),
                             app_secret_enc=other.encrypt(b"old-secret").decode()))
            s.add(Credential(user_id=1, name="no-app-key", exchange_id="kis",
                             account_no_enc=other.encrypt(b"old-account").decode()))
            s.commit()
        assert crypto.validate_key(factory) == 2

    def test_a_credential_is_counted_once_however_many_fields_fail(self, key, factory):
        from api.models import Credential
        other = Fernet(Fernet.generate_key())
        with factory() as s:
            s.add(Credential(user_id=1, name="all-stale", exchange_id="kis",
                             app_key_enc=other.encrypt(b"a").decode(),
                             app_secret_enc=other.encrypt(b"b").decode(),
                             hts_id_enc=other.encrypt(b"c").decode()))
            s.commit()
        assert crypto.validate_key(factory) == 1

    def test_decrypt_says_so_once_and_still_returns_none(self, key, caplog):
        stale = Fernet(Fernet.generate_key()).encrypt(b"x").decode()
        with caplog.at_level(logging.WARNING, logger="api.crypto"):
            assert crypto.decrypt(stale) is None
            assert crypto.decrypt(stale) is None
        assert sum("복호화되지 않는다" in r.getMessage() for r in caplog.records) == 1

    def test_decrypt_round_trips(self, key):
        assert crypto.decrypt(crypto.encrypt("hello")) == "hello"


def test_the_api_checks_the_key_before_it_starts_serving(monkeypatch):
    """The lifespan runs the check first, so a bad key fails the app's startup.
    Needs FastAPI — skipped in the CI jobs that do not install it (#127)."""
    pytest.importorskip("fastapi")
    import asyncio
    import os
    if not os.environ.get("JWT_SECRET_KEY"):
        monkeypatch.setenv("JWT_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("KIS_CREDENTIAL_KEY", "not-a-fernet-key")
    monkeypatch.setattr(crypto, "_fernet", None)
    from api.main import app, lifespan

    async def start():
        async with lifespan(app):
            pass

    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        asyncio.run(start())


@pytest.mark.parametrize("tables_ok, expect_check", [(True, True), (False, False)])
def test_the_stored_data_check_runs_only_once_the_database_answered(
        monkeypatch, key, tables_ok, expect_check):
    """Review finding: after ``create_tables`` failed, a second blocking connect
    would only delay startup. The check also runs off the startup path."""
    pytest.importorskip("fastapi")
    import asyncio
    import os
    import threading
    if not os.environ.get("JWT_SECRET_KEY"):
        monkeypatch.setenv("JWT_SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("QT_RECOVERY_ON_STARTUP", "false")
    import api.main as main_mod

    def tables():
        if not tables_ok:
            raise RuntimeError("db down")

    ran = threading.Event()
    real = crypto.validate_key

    def validate(session_factory=None):
        if session_factory is not None:
            ran.set()
            return 0
        return real()

    monkeypatch.setattr(main_mod, "create_tables", tables)
    monkeypatch.setattr(crypto, "validate_key", validate)

    async def start():
        async with main_mod.lifespan(main_mod.app):
            pass

    asyncio.run(start())
    assert ran.wait(2) is expect_check
