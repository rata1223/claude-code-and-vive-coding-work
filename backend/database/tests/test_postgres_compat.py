"""
PostgreSQL runtime-compatibility regression tests (P1-05B).

These pin the behaviors that differ between SQLite (dev default) and PostgreSQL
(canonical CI database) and that the audit surfaced:

  - UNIQUE constraints are enforced and a session recovers after the violation
  - PostgreSQL is strict about column type / declared length (SQLite silently
    coerces) — guards against latent over-length / wrong-type writes
  - multi-session concurrent upsert of the single-PK DailyRiskState row is race
    safe (exactly one row, no exception escapes)

Run on whichever backend ``make_test_engine`` selects. Tests that assert
PostgreSQL-only strictness or true multi-connection concurrency skip on SQLite
(in-memory SQLite gives each thread its own database, so it cannot model them).
"""
import datetime as dt
import threading

import pytest
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.database.models import Base, DailyRiskState, Order, Position
from backend.database.testing import make_test_engine


@pytest.fixture()
def factory():
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _is_pg(factory) -> bool:
    sess = factory()
    try:
        return sess.bind.dialect.name == "postgresql"
    finally:
        sess.close()


def test_unique_constraint_enforced_and_session_recovers(factory):
    s = factory()
    s.add(Position(symbol="005930", qty=10, avg_price=1.0, market="KR", broker="kis"))
    s.commit()
    s.add(Position(symbol="005930", qty=5, avg_price=2.0, market="KR", broker="kis"))
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    # session is usable again after rollback (PG aborts the whole tx otherwise)
    s.add(Position(symbol="000660", qty=1, avg_price=1.0, market="KR", broker="kis"))
    s.commit()
    assert s.query(Position).count() == 2
    s.close()


def test_multiple_null_idempotency_keys_allowed(factory):
    s = factory()
    s.add(Order(symbol="A", side="buy", qty=1, price=1.0, market="KR"))
    s.add(Order(symbol="B", side="buy", qty=1, price=1.0, market="KR"))
    s.commit()  # NULL != NULL for UNIQUE on both SQLite and PG
    assert s.query(Order).count() == 2
    s.close()


def test_string_length_is_enforced_on_postgres(factory):
    if not _is_pg(factory):
        pytest.skip("SQLite does not enforce String(n) length")
    s = factory()
    s.add(DailyRiskState(trade_date=dt.date.today(), kill_reason="x" * 250))  # > String(200)
    with pytest.raises(DataError):
        s.commit()
    s.rollback()
    s.close()


def test_integer_type_is_strict_on_postgres(factory):
    if not _is_pg(factory):
        pytest.skip("SQLite coerces non-integer values into INTEGER columns")
    s = factory()
    s.add(Order(symbol="X", side="buy", qty="not-an-int", price=1.0, market="KR"))
    with pytest.raises((DataError, IntegrityError)):
        s.commit()
    s.rollback()
    s.close()


def test_concurrent_risk_state_upsert_is_race_safe(factory):
    """Multi-session: many threads upsert the same single-PK row concurrently.

    On PostgreSQL the losers hit a duplicate-PK IntegrityError; the production
    upsert (PersistentLossTracker._write_db) swallows it with a rollback, so no
    exception must escape and exactly one row must exist.
    """
    if not _is_pg(factory):
        pytest.skip("in-memory SQLite gives each thread a separate DB")
    from backend.quant.risk.engine import PersistentLossTracker, RiskConfig

    tracker = PersistentLossTracker(RiskConfig(), redis_client=None, db_factory=factory)
    errors = []

    def hammer(i):
        try:
            tracker.record_pnl(float(i), 1_000_000.0)
        except Exception as e:  # must not happen — _write_db is fail-safe
            errors.append(repr(e))

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent upsert raised: {errors}"
    s = factory()
    assert s.query(DailyRiskState).count() == 1  # single PK row, no duplicates
    s.close()
