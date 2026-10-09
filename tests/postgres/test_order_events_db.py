"""Order history on Postgres (P2-01): the event commits with the change, two
sessions both get theirs, and the Alembic migration's trigger keeps the table
append-only below the ORM."""
import os
import subprocess
import sys
import threading
from datetime import datetime
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from backend.database.models import Order, OrderEvent
from backend.database.order_history import history

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def factory(pg_trading_engine):
    f = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    yield f
    with f() as db:
        # The events stay: they are append-only, and order ids are never reused
        # within this module's tables.
        db.query(Order).filter(Order.symbol.like("P201%")).delete(synchronize_session=False)
        db.commit()


def _order(sym="P201A", **kw):
    return Order(symbol=sym, side="buy", qty=10, price=100.0, market="US", **kw)


def test_the_event_commits_with_the_change(factory):
    with factory() as s:
        o = _order()
        s.add(o)
        s.commit()
        o.status = "submitted"
        s.flush()
        s.rollback()
        o = s.get(Order, o.id)
        o.status = "filled"
        s.commit()

    with factory() as s:
        assert [(e.kind, e.to_status) for e in history(s, o.id)] == [
            ("created", "pending"), ("updated", "filled")]


def test_two_sessions_both_get_their_events(factory):
    with factory() as s:
        a, b = _order("P201A"), _order("P201B")
        s.add_all([a, b])
        s.commit()
    barrier = threading.Barrier(2)
    errors = []

    def move(oid, status):
        try:
            with factory() as s:
                row = s.get(Order, oid)
                row.status = status
                barrier.wait(5)
                s.commit()
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=move, args=(a.id, "submitted")),
               threading.Thread(target=move, args=(b.id, "rejected"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert errors == []
    with factory() as s:
        assert history(s, a.id)[-1].to_status == "submitted"
        assert history(s, b.id)[-1].to_status == "rejected"


@pytest.fixture()
def scratch_db_url():
    """A throwaway database to run the migrations against."""
    base = os.environ["TEST_DATABASE_URL"]
    admin = sa.create_engine(base, isolation_level="AUTOCOMMIT")
    name = f"p201_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = sa.engine.make_url(base).set(database=name)
    yield url.render_as_string(hide_password=False)
    with admin.connect() as c:
        c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.dispose()


def _alembic(url, *args):
    env = dict(os.environ, DB_URL=url)
    r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


def _assert_append_only(url):
    eng = sa.create_engine(url)
    try:
        with eng.begin() as c:
            c.execute(OrderEvent.__table__.insert(), {
                "order_id": 1, "kind": "created", "to_status": "pending",
                "recorded_at": datetime.utcnow()})
        for stmt in ("UPDATE order_events SET to_status = 'filled'",
                     "DELETE FROM order_events",
                     "TRUNCATE order_events"):
            with pytest.raises(sa.exc.DBAPIError, match="append-only"):
                with eng.begin() as c:
                    c.exec_driver_sql(stmt)
        with eng.connect() as c:
            assert c.exec_driver_sql("SELECT count(*) FROM order_events").scalar() == 1
    finally:
        eng.dispose()


def test_the_worker_installs_the_guard_on_a_create_all_database(scratch_db_url):
    """Production databases are built by create_all, never by the migration —
    the guard has to come from where the worker and kis-api open the database."""
    from backend.database.models import init_db_factory
    init_db_factory(scratch_db_url).kw["bind"].dispose()
    init_db_factory(scratch_db_url).kw["bind"].dispose()      # idempotent

    _assert_append_only(scratch_db_url)


def test_the_migration_accepts_a_table_create_all_already_made(scratch_db_url):
    from backend.database.models import Base
    _alembic(scratch_db_url, "upgrade", "d1e2f3a4b5c6")
    eng = sa.create_engine(scratch_db_url)
    Base.metadata.tables["order_events"].create(eng)
    eng.dispose()

    _alembic(scratch_db_url, "upgrade", "head")

    _assert_append_only(scratch_db_url)


def test_the_migration_makes_the_table_append_only(scratch_db_url):
    _alembic(scratch_db_url, "upgrade", "head")
    _assert_append_only(scratch_db_url)

    _alembic(scratch_db_url, "downgrade", "d1e2f3a4b5c6")
    eng = sa.create_engine(scratch_db_url)
    try:
        with eng.connect() as c:
            assert not sa.inspect(c).has_table("order_events")
            assert c.exec_driver_sql(
                "SELECT count(*) FROM pg_proc WHERE proname = 'order_events_append_only'"
            ).scalar() == 0
    finally:
        eng.dispose()
