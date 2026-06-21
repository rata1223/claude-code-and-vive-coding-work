"""
Shared fixtures for PostgreSQL regression tests.

All tests in this package auto-skip unless TEST_DATABASE_URL is set to a
postgresql:// URL — this keeps the suite green on developer machines and in CI
jobs that don't spin up Postgres, while still running fully in the dedicated
CI-Postgres job defined in .github/workflows/ci-postgres.yml.
"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.database import Base as ApiBase
from backend.database.models import Base as TradingBase

PG_URL = os.environ.get("TEST_DATABASE_URL", "")


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    if not PG_URL.startswith("postgresql"):
        skip = pytest.mark.skip(reason="TEST_DATABASE_URL not set to postgresql://…")
        for item in items:
            item.add_marker(skip)


# ── Trading DB (backend/database/models.py) ──────────────────────────────────

@pytest.fixture(scope="module")
def pg_trading_engine():
    engine = create_engine(PG_URL, poolclass=NullPool)
    TradingBase.metadata.create_all(engine)
    yield engine
    TradingBase.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def pg_trading_session(pg_trading_engine):
    connection = pg_trading_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    transaction.rollback()
    connection.close()


# ── API DB (api/models.py) ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_api_engine():
    engine = create_engine(PG_URL, poolclass=NullPool)
    ApiBase.metadata.create_all(engine)
    yield engine
    ApiBase.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def pg_api_session(pg_api_engine):
    connection = pg_api_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    transaction.rollback()
    connection.close()
