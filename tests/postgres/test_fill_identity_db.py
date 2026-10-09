"""P2-03 on Postgres: the fill check runs under the order row's lock.

`_persist_fill` reads how far the order's fills already reach, then writes.
Without the lock, a session that read before another committed the same fill
would write it again. Here session A holds the row and has written the fill
but not committed; the worker's `_persist_fill` for the same total must wait
for A, then see A's fill and skip.
"""
import threading
import time
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

import backend.worker.runner as runner
from backend.brokers.models import Order as BOrder, OrderStatus
from backend.database.models import Fill as DBFill, Order as DBOrder
from backend.execution.position_tracker import Fill

ODNO = "P203LOCK"


@pytest.fixture()
def factory(pg_trading_engine, monkeypatch):
    f = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    monkeypatch.setattr(runner, "_SessionFactory", f)
    yield f
    with f() as s:
        ids = [i for (i,) in s.query(DBOrder.id).filter(DBOrder.broker_order_id == ODNO)]
        s.query(DBFill).filter(DBFill.order_id.in_(ids)).delete(synchronize_session=False)
        s.query(DBOrder).filter(DBOrder.id.in_(ids)).delete(synchronize_session=False)
        s.commit()


def test_a_concurrent_copy_of_the_same_fill_waits_and_is_skipped(factory):
    with factory() as s:
        row = DBOrder(broker_order_id=ODNO, symbol="005930", side="buy", qty=10,
                      price=70000.0, filled_qty=0, status="submitted",
                      market="KR", broker="kis", created_at=datetime.utcnow())
        s.add(row)
        s.commit()
        row_id = row.id

    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    order = BOrder(id=ODNO, symbol="005930", side="buy", qty=10, price=70000.0,
                   status=OrderStatus.PARTIAL_FILLED, filled_qty=5, avg_fill_price=70000.0)
    fill = Fill(order_id=ODNO, symbol="005930", side="buy", qty=5, price=70000.0, market="KR")

    a = factory()
    try:
        a.query(DBOrder).filter(DBOrder.id == row_id).with_for_update().one()
        a.add(DBFill(order_id=row_id, qty=5, price=70000.0))
        a.flush()

        t = threading.Thread(target=w._persist_fill, args=(fill, order),
                             kwargs={"row_id": row_id, "cumulative": 5})
        t.start()
        time.sleep(0.5)
        assert t.is_alive(), "_persist_fill did not wait for the row lock"
        a.commit()
    finally:
        a.close()
    t.join(10)
    assert not t.is_alive()

    with factory() as s:
        assert s.query(DBFill).filter(DBFill.order_id == row_id).count() == 1
