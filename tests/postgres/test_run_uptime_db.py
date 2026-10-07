"""run_uptime on Postgres: an existing database gains the table on the next
boot (``create_all`` creates missing tables), and the gate's uptime arithmetic
reads it the same way it does on SQLite."""
import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from backend.database.models import Base, Order, StrategyRun, RunUptime
from backend.worker.promotion_guard import LivePromotionGuard, uptime_by_run
from backend.worker.uptime import UPTIME_GRACE, UptimeRecorder

from .conftest import PG_URL


def test_an_existing_database_gains_the_table_without_touching_the_others():
    engine = create_engine(PG_URL, poolclass=NullPool)
    try:
        Base.metadata.drop_all(engine)
        others = [t for t in Base.metadata.sorted_tables if t.name != "run_uptime"]
        Base.metadata.create_all(engine, tables=others)     # a database from before
        with sessionmaker(bind=engine)() as db:
            db.add(StrategyRun(name="old", strategy_type="indicator", config="{}"))
            db.commit()
        assert "run_uptime" not in inspect(engine).get_table_names()

        Base.metadata.create_all(engine)                     # the next boot

        assert "run_uptime" in inspect(engine).get_table_names()
        with sessionmaker(bind=engine)() as db:
            assert db.query(StrategyRun).count() == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_the_gate_counts_recorded_uptime(pg_trading_engine):
    factory = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    now = datetime.utcnow()
    with factory() as db:
        db.query(Order).delete()
        db.query(StrategyRun).delete()
        db.query(RunUptime).delete()
        run = StrategyRun(name="paper", strategy_type="indicator",
                          config=json.dumps({"kis_env": "paper"}), is_active=True,
                          started_at=now - timedelta(days=28, hours=3))
        db.add(run)
        db.flush()
        db.add(Order(broker_order_id="pg-up-1", symbol="SPY", side="buy", qty=1, price=1.0,
                     filled_qty=1, status="filled", market="US", strategy_run_id=run.id))
        crash = run.started_at + timedelta(days=5)
        db.add_all([
            RunUptime(run_id=run.id, boot_at=run.started_at, last_beat_at=crash),
            RunUptime(run_id=run.id, boot_at=crash + timedelta(hours=6), last_beat_at=now),
        ])
        db.commit()
        rid = run.id
    try:
        with factory() as db:
            got = uptime_by_run(db, [db.get(StrategyRun, rid)], now)[rid]
        assert abs(got - (timedelta(days=28, hours=3) - timedelta(hours=6) + UPTIME_GRACE)) \
            < timedelta(seconds=5)
        assert LivePromotionGuard(factory)._check_paper_run() is False, "6 h down: not yet"
    finally:
        with factory() as db:
            db.query(Order).delete()
            db.query(StrategyRun).delete()
            db.query(RunUptime).delete()
            db.commit()


def test_the_recorder_writes_to_postgres(pg_trading_engine):
    factory = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    with factory() as db:
        db.query(RunUptime).delete()
        db.commit()
    rec = UptimeRecorder(factory, run_id=1)
    try:
        rec.beat()
        rec.beat()
        rec.stop()
        with factory() as db:
            rows = db.query(RunUptime).all()
        assert len(rows) == 1 and rows[0].ended_at is not None and rows[0].run_id == 1
        assert rows[0].boot_at <= rows[0].last_beat_at == rows[0].ended_at
    finally:
        with factory() as db:
            db.query(RunUptime).delete()
            db.commit()
