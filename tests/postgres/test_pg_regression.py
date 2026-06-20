"""
TASK P1-05B: PostgreSQL Runtime Validation

Regression tests for SQLite/PostgreSQL behavioral differences.
All tests skip automatically unless TEST_DATABASE_URL points to a real
PostgreSQL instance (enforced by conftest.pytest_collection_modifyitems).

Coverage:
  B. Unique constraint enforcement
  C. Rollback / savepoint behavior
  D. Foreign key enforcement (api/models.py)
  E. Multi-session / concurrent execution
  F. SQLite-masked behavior (datetime type, text JSON)

Alembic migration round-trip is validated via shell steps in
.github/workflows/ci-postgres.yml, not here, to avoid global-schema
state interference with the fixture-based tests.
"""
import datetime
import json
import os
import threading

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from api.models import Credential, User
from backend.database.models import (
    AuditLog,
    Command,
    DailyRiskState,
    Order,
    Position,
)

PG_URL = os.environ.get("TEST_DATABASE_URL", "")


# ── B. Unique constraints ─────────────────────────────────────────────────────

class TestUniqueConstraints:
    def test_idempotency_key_raises_integrity_error(self, pg_trading_session):
        pg_trading_session.add(Order(
            idempotency_key="KEY-001", symbol="AAPL", side="buy",
            qty=10, price=150.0, status="pending", market="US",
        ))
        pg_trading_session.flush()

        pg_trading_session.add(Order(
            idempotency_key="KEY-001", symbol="AAPL", side="sell",
            qty=10, price=160.0, status="pending", market="US",
        ))
        with pytest.raises(IntegrityError, match="uq_orders_idempotency"):
            pg_trading_session.flush()

    def test_position_symbol_broker_raises_integrity_error(self, pg_trading_session):
        pg_trading_session.add(Position(
            symbol="AAPL", qty=10, avg_price=150.0, market="US", broker="kis",
        ))
        pg_trading_session.flush()

        pg_trading_session.add(Position(
            symbol="AAPL", qty=20, avg_price=155.0, market="US", broker="kis",
        ))
        with pytest.raises(IntegrityError, match="uq_position_symbol_broker"):
            pg_trading_session.flush()

    def test_unique_constraint_released_after_rollback(self, pg_trading_session):
        pg_trading_session.add(Order(
            idempotency_key="ROLLBACK-KEY", symbol="AAPL", side="buy",
            qty=5, price=100.0, status="pending", market="US",
        ))
        pg_trading_session.flush()

        # force a dupe → rollback
        pg_trading_session.add(Order(
            idempotency_key="ROLLBACK-KEY", symbol="AAPL", side="buy",
            qty=5, price=100.0, status="pending", market="US",
        ))
        with pytest.raises(IntegrityError):
            pg_trading_session.flush()

        pg_trading_session.rollback()

        # after rollback the key is gone; can insert again
        pg_trading_session.add(Order(
            idempotency_key="ROLLBACK-KEY", symbol="AAPL", side="buy",
            qty=5, price=100.0, status="pending", market="US",
        ))
        pg_trading_session.flush()
        pg_trading_session.commit()


# ── C. Rollback / savepoint ───────────────────────────────────────────────────

class TestRollback:
    def test_full_rollback_leaves_db_unchanged(self, pg_trading_session):
        for i in range(3):
            pg_trading_session.add(AuditLog(
                event_type="test_event", actor="test_rollback",
            ))
        pg_trading_session.flush()

        pg_trading_session.rollback()

        count = pg_trading_session.query(AuditLog).filter_by(actor="test_rollback").count()
        assert count == 0

    def test_savepoint_partial_rollback(self, pg_trading_session):
        pg_trading_session.add(AuditLog(event_type="outer", actor="savepoint_test"))
        pg_trading_session.flush()

        # nested savepoint
        sp = pg_trading_session.begin_nested()
        pg_trading_session.add(AuditLog(event_type="inner", actor="savepoint_test"))
        pg_trading_session.flush()
        sp.rollback()

        # outer row survived; inner was rolled back
        rows = (
            pg_trading_session.query(AuditLog)
            .filter_by(actor="savepoint_test")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].event_type == "outer"


# ── D. Foreign keys (api/models.py) ──────────────────────────────────────────

class TestForeignKeys:
    def test_credential_fk_prevents_orphan_insert(self, pg_api_session):
        pg_api_session.add(Credential(
            user_id=999999,
            name="orphan",
            exchange_id="kis",
        ))
        with pytest.raises(IntegrityError):
            pg_api_session.flush()

    def test_cascade_delete_removes_credentials(self, pg_api_session):
        user = User(
            email="cascade_test@example.com",
            password_hash="hash",
            nickname="Test",
        )
        pg_api_session.add(user)
        pg_api_session.flush()

        cred = Credential(
            user_id=user.id,
            name="main",
            exchange_id="kis",
        )
        pg_api_session.add(cred)
        pg_api_session.flush()

        pg_api_session.delete(user)
        pg_api_session.flush()

        remaining = pg_api_session.query(Credential).filter_by(user_id=user.id).count()
        assert remaining == 0


# ── E. Multi-session / concurrent execution ───────────────────────────────────

class TestMultiSession:
    def test_committed_data_visible_to_second_session(self, pg_trading_engine):
        S = sessionmaker(bind=pg_trading_engine)

        s1 = S()
        log = AuditLog(event_type="multi_session_test", actor="session_a")
        s1.add(log)
        s1.commit()
        log_id = log.id
        s1.close()

        s2 = S()
        found = s2.query(AuditLog).filter_by(id=log_id).one_or_none()
        assert found is not None
        assert found.event_type == "multi_session_test"
        s2.close()

    def test_concurrent_inserts_no_deadlock(self, pg_trading_engine):
        S = sessionmaker(bind=pg_trading_engine)
        errors: list[Exception] = []

        def _insert_batch(n: int) -> None:
            s = S()
            try:
                for i in range(n):
                    s.add(AuditLog(event_type="concurrent_test", actor=f"thread_{n}"))
                s.commit()
            except Exception as exc:
                errors.append(exc)
            finally:
                s.close()

        t1 = threading.Thread(target=_insert_batch, args=(50,))
        t2 = threading.Thread(target=_insert_batch, args=(50,))
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)

        assert not errors, f"Concurrent insert errors: {errors}"
        s = S()
        count = s.query(AuditLog).filter_by(event_type="concurrent_test").count()
        s.close()
        assert count == 100

    def test_command_status_update_across_sessions(self, pg_trading_engine):
        S = sessionmaker(bind=pg_trading_engine)

        s1 = S()
        cmd = Command(channel="strategy", payload='{"action":"stop"}', status="pending")
        s1.add(cmd)
        s1.commit()
        cmd_id = cmd.id
        s1.close()

        s2 = S()
        cmd2 = s2.query(Command).filter_by(id=cmd_id).one()
        cmd2.status = "processed"
        s2.commit()
        s2.close()

        s3 = S()
        final = s3.query(Command).filter_by(id=cmd_id).one()
        assert final.status == "processed"
        s3.close()


# ── F. SQLite-masked behavior ─────────────────────────────────────────────────

class TestSQLiteRegressions:
    def test_created_at_returns_datetime_not_string(self, pg_trading_session):
        log = AuditLog(event_type="datetime_type_test", actor="test")
        pg_trading_session.add(log)
        pg_trading_session.commit()

        row = pg_trading_session.query(AuditLog).filter_by(
            event_type="datetime_type_test"
        ).one()
        # SQLite without type coercion returns a string; PG returns datetime
        assert isinstance(row.created_at, datetime.datetime), (
            f"Expected datetime, got {type(row.created_at)}"
        )

    def test_json_text_round_trips_exactly(self, pg_trading_session):
        payload = '{"symbol": "AAPL", "side": "buy", "qty": 10}'
        log = AuditLog(event_type="json_test", actor="test", detail=payload)
        pg_trading_session.add(log)
        pg_trading_session.commit()

        row = pg_trading_session.query(AuditLog).filter_by(event_type="json_test").one()
        assert row.detail == payload
        parsed = json.loads(row.detail)
        assert parsed["symbol"] == "AAPL"

    def test_null_optional_columns_stored_and_retrieved(self, pg_trading_session):
        log = AuditLog(event_type="null_test", actor=None, symbol=None, detail=None)
        pg_trading_session.add(log)
        pg_trading_session.commit()

        row = pg_trading_session.query(AuditLog).filter_by(event_type="null_test").one()
        assert row.actor is None
        assert row.symbol is None
        assert row.detail is None

    def test_daily_risk_state_date_primary_key(self, pg_trading_session):
        today = datetime.date.today()
        state = DailyRiskState(
            trade_date=today, daily_pnl=-500.0, weekly_pnl=-1000.0,
            peak_equity=2_000_000.0, kill_switch=False,
        )
        pg_trading_session.add(state)
        pg_trading_session.commit()

        row = pg_trading_session.query(DailyRiskState).filter_by(trade_date=today).one()
        assert isinstance(row.trade_date, datetime.date)
        assert row.kill_switch is False
