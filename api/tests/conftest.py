"""Shared fixtures for api/ HTTP-level tests.

Required env vars (JWT_SECRET_KEY, KIS_CREDENTIAL_KEY) must be set before any
`api.*` module is imported, since api/auth.py and api/crypto.py validate them
at import/first-use time.
"""
import os

os.environ.setdefault("JWT_SECRET_KEY", "test-only-jwt-secret-not-for-production-use-32c")
# Disable the Quick Trade startup recovery sweep during HTTP tests so a TestClient
# startup never spawns a background thread hitting the real DATABASE_URL. Tests that
# exercise the sweep set this explicitly via monkeypatch.
os.environ.setdefault("QT_RECOVERY_ON_STARTUP", "false")

if "KIS_CREDENTIAL_KEY" not in os.environ:
    from cryptography.fernet import Fernet

    os.environ["KIS_CREDENTIAL_KEY"] = Fernet.generate_key().decode()

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.database.testing import make_test_engine, StaticPool

from api import models  # noqa: F401 - register ORM models on Base.metadata
from api.auth import create_access_token, hash_password
from api.database import Base, get_db
from api.main import app
from api.models import User
from api.routers.auth import limiter as auth_router_limiter
from api.routers.quick_trade import get_risk_gate


@pytest.fixture()
def db_session():
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def _override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    # Default the P0-05 risk gate to ALLOW so existing HTTP tests exercise the
    # order path without a live Redis/RiskManager. Risk-specific tests remove
    # this override to run the real gate against a patched RiskManager.
    app.dependency_overrides[get_risk_gate] = lambda: (lambda: None)
    # auth.py's rate-limit decorator uses its own Limiter instance, separate
    # from app.state.limiter — both must be disabled so repeated test calls
    # to /login and /register don't hit the real 5/minute production limit.
    # Both are mutable process-wide singletons, so save/restore around the
    # test rather than leaving them disabled for the rest of the pytest run.
    _app_limiter_was_enabled = app.state.limiter.enabled
    _auth_limiter_was_enabled = auth_router_limiter.enabled
    app.state.limiter.enabled = False
    auth_router_limiter.enabled = False

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        app.dependency_overrides.clear()
        app.state.limiter.enabled = _app_limiter_was_enabled
        auth_router_limiter.enabled = _auth_limiter_was_enabled
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def client(db_session):
    return TestClient(app)


@pytest.fixture()
def seed_user(db_session):
    """Create a user with a known password and return (user, plaintext_password)."""
    plaintext_password = "correct-horse-battery-staple"
    user = User(
        email="rider@example.com",
        password_hash=hash_password(plaintext_password),
        nickname="rider",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user, plaintext_password


@pytest.fixture()
def auth_headers(seed_user):
    user, _ = seed_user
    token = create_access_token(user.id, user.email)
    return {"Authorization": f"Bearer {token}"}
