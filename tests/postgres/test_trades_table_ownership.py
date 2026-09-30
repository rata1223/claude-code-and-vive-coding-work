"""The api app and the worker no longer contend for one ``trades`` table.

``api/models.py`` and ``backend/database/models.py`` both mapped ``trades``
with incompatible columns, and api, kis-api and kis-worker share one database
(docker-compose). Both provision with a non-altering ``create_all``, so the
first service up won the name: with the worker first, every api query on
``Trade`` failed with ``UndefinedColumn: trades.strategy_id`` — the dashboard
summary, strategy detail and Quick Trade history. The api table is now
``strategy_trades``; either start order leaves both ORMs working.
"""
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

import api.models as am
import backend.database.models as bm
from tests.postgres.conftest import PG_URL


@pytest.fixture()
def fresh_schema():
    admin = create_engine(PG_URL, poolclass=NullPool)
    name = "own_" + uuid.uuid4().hex[:10]
    with admin.begin() as c:
        c.execute(text(f'CREATE SCHEMA "{name}"'))
    engine = create_engine(PG_URL, poolclass=NullPool,
                           connect_args={"options": f"-csearch_path={name}"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        admin.dispose()


def _both_orms_work(engine):
    with Session(engine) as s:
        s.add(am.User(id=1, email="a@example.com", password_hash="x"))
        s.add(am.Strategy(id=1, user_id=1, name="s", type="script"))
        s.add(am.Trade(strategy_id=1, symbol="AAPL", side="sell", qty=1,
                       price=100.0, pnl=5.0))
        s.add(bm.Trade(symbol="AAPL", side="buy", qty=1, price=100.0, market="US"))
        s.commit()
        assert s.query(am.Trade).filter(am.Trade.strategy_id == 1).count() == 1
        assert s.query(bm.Trade).count() == 1


@pytest.mark.parametrize("worker_first", [True, False])
def test_either_start_order_leaves_both_orms_working(fresh_schema, worker_first):
    first, second = (bm.Base, am.Base) if worker_first else (am.Base, bm.Base)
    first.metadata.create_all(fresh_schema)
    second.metadata.create_all(fresh_schema)

    cols = {t: {c["name"] for c in inspect(fresh_schema).get_columns(t)}
            for t in ("trades", "strategy_trades")}
    assert "strategy_run_id" in cols["trades"]          # the worker's table
    assert "strategy_id" in cols["strategy_trades"]      # the api's table
    _both_orms_work(fresh_schema)
