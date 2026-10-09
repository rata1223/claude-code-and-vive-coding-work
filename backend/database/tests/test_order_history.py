"""P2-01 — every change to ``orders`` leaves an append-only ``order_events`` row.

The hook lives on the session (``backend/database/order_history.py``), so these
check it both directly and through the real writers — the runner's persistence,
the reconciler — which were not changed to log anything themselves.

SQLite in memory; no broker, no network.
"""
from __future__ import annotations

import ast
import pathlib
import re
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import AuditLog, Base, Order, OrderEvent
from backend.database.order_history import (OrderEventImmutable, history,
                                            latest_status)
from backend.database.testing import make_test_engine

REPO = pathlib.Path(__file__).resolve().parents[3]

#: Raw SQL that writes ``orders`` or ``order_events``.
RAW_SQL = re.compile(
    r"\b(?:UPDATE|DELETE\s+FROM|INSERT\s+INTO|TRUNCATE(?:\s+TABLE)?)\s+(?:orders|order_events)\b",
    re.IGNORECASE)
_MODELS = {"Order", "OrderEvent"}
#: The hook itself writes events with a Core insert — that is the point of it.
_ALLOWED = {"backend/database/order_history.py"}


def bypasses(src: str) -> list[int]:
    """Lines that write ``orders``/``order_events`` without a session's unit of
    work — a Core or bulk statement, ``Model.__table__`` DML, ``query(Model)
    .update()/.delete()`` however it is wrapped across lines, or raw SQL. The
    history hook never sees those. Model names are resolved per file from its
    imports of ``backend.database.models`` (aliases included), or written as
    ``models.Order``."""
    tree = ast.parse(src)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("database.models"):
            names |= {a.asname or a.name for a in node.names if a.name in _MODELS}

    def is_model(n) -> bool:
        return ((isinstance(n, ast.Name) and n.id in names)
                or (isinstance(n, ast.Attribute) and n.attr in _MODELS))

    def queries_model(n) -> bool:
        while isinstance(n, (ast.Call, ast.Attribute)):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "query" and n.args and is_model(n.args[0])):
                return True
            n = n.func if isinstance(n, ast.Call) else n.value
        return False

    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if RAW_SQL.search(node.value):
                hits.append(node.lineno)
            continue
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name in ("update", "delete", "insert") and node.args and is_model(node.args[0]):
            hits.append(node.lineno)                       # update(Order)
        elif name and name.startswith("bulk_") and node.args and is_model(node.args[0]):
            hits.append(node.lineno)                       # bulk_update_mappings(Order, …)
        elif (isinstance(f, ast.Attribute) and name in ("update", "delete", "insert")
              and isinstance(f.value, ast.Attribute) and f.value.attr == "__table__"
              and is_model(f.value.value)):
            hits.append(node.lineno)                       # Order.__table__.update()
        elif isinstance(f, ast.Attribute) and name in ("update", "delete") \
                and queries_model(f.value):
            hits.append(node.lineno)                       # query(Order)….update()
    return hits


@pytest.fixture()
def factory():
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _order(**kw):
    base = dict(symbol="AAPL", side="buy", qty=10, price=100.0, market="US")
    base.update(kw)
    return Order(**base)


def _trail(factory, order_id):
    sess = factory()
    try:
        return [(e.kind, e.from_status, e.to_status, e.filled_qty, e.broker_order_id)
                for e in history(sess, order_id)]
    finally:
        sess.close()


class TestTheHook:
    def test_an_insert_is_one_created_event(self, factory):
        sess = factory()
        o = _order()
        sess.add(o)
        sess.commit()

        assert _trail(factory, o.id) == [("created", None, "pending", 0, None)]

    def test_a_status_change_records_from_and_to(self, factory):
        sess = factory()
        o = _order()
        sess.add(o)
        sess.commit()
        o.status, o.broker_order_id = "submitted", "0001"
        sess.commit()

        assert _trail(factory, o.id)[-1] == ("updated", "pending", "submitted", 0, "0001")

    def test_a_fill_is_an_event(self, factory):
        sess = factory()
        o = _order(status="submitted")
        sess.add(o)
        sess.commit()
        o.filled_qty, o.avg_fill_price = 4, 101.5
        sess.commit()

        sess2 = factory()
        last = history(sess2, o.id)[-1]
        assert (last.kind, last.from_status, last.to_status) == ("updated", "submitted", "submitted")
        assert (last.filled_qty, last.avg_fill_price) == (4, 101.5)

    def test_bookkeeping_changes_are_not_events(self, factory):
        from datetime import datetime
        sess = factory()
        o = _order()
        sess.add(o)
        sess.commit()
        o.error = "브로커 응답 지연"
        o.updated_at = datetime.utcnow()
        o.status = "pending"                     # same value
        sess.commit()

        assert len(_trail(factory, o.id)) == 1

    def test_a_rollback_leaves_neither_change_nor_event(self, factory):
        sess = factory()
        o = _order()
        sess.add(o)
        sess.commit()
        o.status = "submitted"
        sess.flush()
        sess.rollback()

        assert [k for k, *_ in _trail(factory, o.id)] == ["created"]
        check = factory()
        assert check.get(Order, o.id).status == "pending"

    def test_several_orders_in_one_flush_each_get_their_event(self, factory):
        sess = factory()
        a, b = _order(symbol="AAPL"), _order(symbol="MSFT")
        sess.add_all([a, b])
        sess.commit()
        a.status, b.status = "submitted", "rejected"
        sess.commit()

        assert latest_status(sess, a.id) == "submitted"
        assert latest_status(sess, b.id) == "rejected"

    def test_the_event_records_the_row_not_a_stale_object(self, factory):
        """Session B loaded the order before A filled it, and changes only the
        average price. B's UPDATE leaves A's status and quantity on the row —
        and the event must say so, not repeat B's stale ``submitted``."""
        sess = factory()
        o = _order(status="submitted")
        sess.add(o)
        sess.commit()
        a, b = factory(), factory()
        ra, rb = a.get(Order, o.id), b.get(Order, o.id)
        ra.status, ra.filled_qty = "filled", 10
        a.commit()

        rb.avg_fill_price = 100.4
        b.commit()

        last = history(factory(), o.id)[-1]
        assert (last.to_status, last.filled_qty, last.avg_fill_price) == ("filled", 10, 100.4)

    def test_re_setting_an_expired_field_to_its_value_is_not_an_event(self, factory):
        sess = factory()
        o = _order(status="submitted")
        sess.add(o)
        sess.commit()
        sess.expire(o)
        o.status = "submitted"
        sess.commit()

        assert len(_trail(factory, o.id)) == 1

    def test_latest_status_without_events_is_none(self, factory):
        assert latest_status(factory(), 12345) is None


class TestAppendOnly:
    def test_an_event_cannot_be_changed(self, factory):
        sess = factory()
        sess.add(_order())
        sess.commit()
        ev = sess.query(OrderEvent).one()
        ev.to_status = "filled"

        with pytest.raises(OrderEventImmutable):
            sess.commit()

    def test_an_event_cannot_be_deleted(self, factory):
        sess = factory()
        sess.add(_order())
        sess.commit()
        sess.delete(sess.query(OrderEvent).one())

        with pytest.raises(OrderEventImmutable):
            sess.commit()

    def test_no_production_code_writes_orders_around_the_session(self):
        """A Core ``update(Order)``, a bulk ``query(Order).update()`` or raw SQL
        skips the hook — and the history with it."""
        hits = []
        for root in ("backend", "api", "bot", "kis_adapter", "strategy", "scripts"):
            for path in (REPO / root).rglob("*.py"):
                rel = path.relative_to(REPO).as_posix()
                if "/tests/" in rel or rel in _ALLOWED:
                    continue
                hits += [f"{rel}:{n}" for n in bypasses(path.read_text(encoding="utf-8"))]
        assert hits == []

    @pytest.mark.parametrize("src", [
        "from backend.database.models import Order\nsess.execute(update(Order).values(status='x'))",
        "from backend.database.models import Order as DBOrder\nsess.execute(sa.delete(DBOrder))",
        "from backend.database.models import Order\nsess.execute(insert(Order), rows)",
        "from backend.database import models\nsess.execute(models.Order.__table__.update())",
        "from backend.database.models import Order\nsess.bulk_update_mappings(Order, rows)",
        "from backend.database.models import Order as DBOrder\n"
        "(db.query(DBOrder)\n   .filter(DBOrder.id == 1)\n   .update({'status': 'x'}))",
        'sess.execute(text("UPDATE orders SET status = :s"))',
        'conn.exec_driver_sql("truncate table order_events")',
    ])
    def test_the_guard_sees_each_kind_of_bypass(self, src):
        assert bypasses(src)

    @pytest.mark.parametrize("src", [
        "self._publish_order_update(order)",
        "from backend.brokers.models import Order\nupdate(Order)",   # not the table
        "from backend.database.models import Order\nrows = db.query(Order).all()\nd.update(x)",
        "from backend.database.models import Order\nsess.delete(order)",
    ])
    def test_the_guard_ignores_lookalikes(self, src):
        assert bypasses(src) == []


class TestTheRealWriters:
    """The writers were not touched; the trail appears anyway."""

    def test_the_runners_persistence(self, factory, monkeypatch):
        import backend.worker.runner as runner
        from backend.brokers.models import Order as BOrder, OrderStatus
        monkeypatch.setattr(runner, "_SessionFactory", factory)
        w = runner.StrategyWorker.__new__(runner.StrategyWorker)
        w._poller = None
        w._loss_tracker = None
        order = BOrder(id="KIS001", symbol="AAPL", side="buy", qty=10, price=100.0,
                       status=OrderStatus.SUBMITTED)

        w._persist_order(order)
        order.status, order.filled_qty, order.avg_fill_price = OrderStatus.FILLED, 10, 100.2
        w._persist_order(order)

        sess = factory()
        row = sess.query(Order).filter(Order.broker_order_id == "KIS001").one()
        trail = _trail(factory, row.id)
        assert [t[:3] for t in trail] == [("created", None, "submitted"),
                                          ("updated", "submitted", "filled")]
        assert trail[-1][3] == 10

    def test_the_reconcilers_lost_order_cancel(self, factory):
        from backend.execution.reconciler import PositionReconciler, ReconciliationResult
        sess = factory()
        o = _order(status="submitted", broker_order_id="0007")
        sess.add(o)
        sess.commit()
        rec = PositionReconciler(broker=MagicMock(), db_factory=factory)

        rec._mark_order_lost(o.id, ReconciliationResult("test"))

        assert _trail(factory, o.id)[-1][:3] == ("updated", "submitted", "canceled")


class TestTheBootCheck:
    def _validate(self, factory):
        from backend.worker.recovery import StartupRecovery
        StartupRecovery(db_session_factory=factory)._step_validate_state()
        sess = factory()
        try:
            return [r.detail for r in sess.query(AuditLog)
                    .filter(AuditLog.event_type == "recovery_inconsistency")]
        finally:
            sess.close()

    def test_a_write_around_the_hook_is_reported(self, factory):
        from sqlalchemy import text
        sess = factory()
        o = _order(status="filled", broker_order_id="0009")
        sess.add(o)
        sess.commit()
        sess.execute(text("UPDATE orders SET status = 'canceled' WHERE id = :i"), {"i": o.id})
        sess.commit()

        details = self._validate(factory)

        assert any("order_status_event_mismatch" in d and '"event_status": "filled"' in d
                   for d in details)

    def test_an_order_from_before_the_log_is_only_counted(self, factory, caplog):
        from sqlalchemy import text
        sess = factory()
        sess.execute(text(
            "INSERT INTO orders (symbol, side, qty, price, filled_qty, status, market, broker, "
            "created_at, updated_at) VALUES ('AAPL', 'buy', 1, 1.0, 1, 'filled', 'US', 'kis', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        sess.commit()

        with caplog.at_level("INFO", logger="backend.worker.recovery"):
            details = self._validate(factory)

        assert not any("order_status_event_mismatch" in d for d in details)
        assert "주문 이력 없는 주문 1건" in caplog.text

    def test_an_old_mismatch_is_not_re_reported_forever(self, factory):
        from datetime import datetime, timedelta
        from sqlalchemy import text
        sess = factory()
        o = _order(status="filled", broker_order_id="0011")
        sess.add(o)
        sess.commit()
        sess.execute(text("UPDATE orders SET status = 'canceled', updated_at = :t WHERE id = :i"),
                     {"i": o.id, "t": datetime.utcnow() - timedelta(days=8)})
        sess.commit()

        assert not any("order_status_event_mismatch" in d for d in self._validate(factory))

    def test_a_failing_history_check_leaves_the_other_checks(self, factory, monkeypatch):
        from backend.database.models import Position
        import backend.database.models as models
        sess = factory()
        sess.add(Position(symbol="005930", qty=0, avg_price=1.0, market="KR", broker="kis"))
        sess.commit()

        class _Gone:
            def __getattr__(self, name):
                raise RuntimeError("order_events 없음")
        monkeypatch.setattr(models, "OrderEvent", _Gone())

        details = self._validate(factory)

        assert any("position_nonpositive_qty" in d for d in details)

    def test_a_consistent_order_is_not_reported(self, factory):
        sess = factory()
        o = _order(status="submitted", broker_order_id="0010")
        sess.add(o)
        sess.commit()
        o.status = "filled"
        sess.commit()

        assert not any("order_status_event_mismatch" in d for d in self._validate(factory))
