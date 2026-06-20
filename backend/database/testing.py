"""
Test database engine factory — lets the suite run on either SQLite (default,
fast, isolated per engine) or PostgreSQL (canonical CI database).

Set ``TEST_DATABASE_URL`` to a Postgres URL to validate against Postgres, e.g.::

    TEST_DATABASE_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/test pytest

When unset, behavior is identical to the previous inline
``create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})``
so existing tests are unaffected.

Postgres isolation: each ``make_test_engine()`` call provisions a fresh, uniquely
named schema and pins every connection's ``search_path`` to it. That gives each
test fixture its own clean namespace inside one shared database — the equivalent
of a fresh in-memory SQLite db — without per-test ``CREATE DATABASE``.
"""
import os
import uuid

from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import NullPool, StaticPool


def make_test_engine(*, connect_args=None, poolclass=None):
    """Return a SQLAlchemy engine for tests.

    SQLite (default): in-memory, ``check_same_thread=False``.
    Postgres (``TEST_DATABASE_URL`` set): isolated unique schema per engine.

    ``poolclass`` / ``connect_args`` override the SQLite defaults (e.g. the
    integration suite passes ``poolclass=StaticPool``); they are ignored on the
    Postgres path where they don't apply.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return create_engine(
            "sqlite:///:memory:",
            connect_args=connect_args if connect_args is not None else {"check_same_thread": False},
            poolclass=poolclass,
        )

    # Postgres: one fresh schema per engine for isolation. NullPool so each
    # short-lived test engine doesn't pin a connection (hundreds of fixtures
    # would otherwise exhaust max_connections).
    #
    # The schema name is UUID-derived ("t_" + uuid4 hex → [a-z0-9_] only), so it
    # is injection-safe; SQLAlchemy text()/DBAPI can't bind SQL *identifiers*
    # anyway, hence the f-strings. Schemas are not dropped: CI databases are
    # ephemeral, and locally the per-run schemas are tiny.
    schema = "t_" + uuid.uuid4().hex[:16]
    engine = create_engine(url, poolclass=NullPool)
    with engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.commit()

    @event.listens_for(engine, "connect")
    def _set_search_path(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute(f'SET search_path TO "{schema}"')
        cur.close()

    return engine


__all__ = ["make_test_engine", "StaticPool"]
