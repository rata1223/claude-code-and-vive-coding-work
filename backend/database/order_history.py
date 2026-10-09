"""Order history: every change to ``orders`` leaves an ``order_events`` row (P2-01).

``orders`` is updated in place — status, filled quantity, average fill price,
broker order number — by the runner, recovery, the reconciler and the terminal
event handler. After a crash or a reconcile nothing said how an order got where
it is. This records it **once, at the session**, rather than at each writer:

* ``after_flush`` turns every new ``Order`` and every change to a tracked field
  into an ``OrderEvent``, executed on the flush's own connection — the event
  commits or rolls back with the change it describes. PKs are assigned by then;
  attribute history still shows the values before the flush. The values
  recorded are **read back from the row** the flush just wrote, not taken from
  this session's object: a field this session did not touch may have been
  changed by another one since it loaded the order.
* ``before_flush`` refuses to update or delete an ``OrderEvent`` through the
  ORM: the log only grows. On Postgres a trigger also refuses UPDATE, DELETE
  and TRUNCATE (``ensure_db_guard``, run where the tables are created, and the
  Alembic migration ``e2f3a4b5c6d7``).

What goes around a session — a Core update statement, raw SQL — goes around
this too; a static guard keeps production code from doing that
(``backend/database/tests/test_order_history.py``).

``orders`` stays the read model: nothing reads status from here yet. Deriving
it from the log and dropping the shadow column is P6 (ROADMAP R-CRIT-03).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

#: The ``Order`` fields whose change is an event. Others (``error``,
#: ``updated_at``, ``strategy_run_id``…) are bookkeeping, not order state.
TRACKED = ("status", "filled_qty", "avg_fill_price", "broker_order_id")


class OrderEventImmutable(RuntimeError):
    """An ``order_events`` row was about to be changed or deleted."""


def _changed(obj) -> tuple[bool, object]:
    """Whether a tracked field changed in this flush, and the status before it."""
    state = inspect(obj)
    changed = False
    before = obj.status
    for name in TRACKED:
        hist = state.attrs[name].history
        if not hist.added:
            continue
        old = hist.deleted[0] if hist.deleted else None
        if old != hist.added[0]:
            changed = True
        if name == "status":
            before = old
    return changed, before


def _record(session: Session, flush_context) -> None:
    from sqlalchemy import select
    from backend.database.models import Order, OrderEvent
    events = [(o.id, "created", None) for o in session.new if isinstance(o, Order)]
    for o in session.dirty:
        if isinstance(o, Order):
            changed, before = _changed(o)
            if changed:
                events.append((o.id, "updated", before))
    if not events:
        return
    # The flush's own connection: same transaction as the change, and the row
    # as this transaction now sees it — after our UPDATE, on top of whatever
    # another session committed to the fields we did not set.
    conn = session.connection()
    t = Order.__table__
    now = {r.id: r for r in conn.execute(
        select(t.c.id, t.c.status, t.c.filled_qty, t.c.avg_fill_price, t.c.broker_order_id)
        .where(t.c.id.in_({oid for oid, _, _ in events})))}
    rows = [{
        "order_id": oid,
        "kind": kind,
        "from_status": before,
        "to_status": now[oid].status,
        "filled_qty": now[oid].filled_qty,
        "avg_fill_price": now[oid].avg_fill_price,
        "broker_order_id": now[oid].broker_order_id,
        "recorded_at": datetime.utcnow(),
    } for oid, kind, before in events]
    conn.execute(OrderEvent.__table__.insert(), rows)


def _guard(session: Session, flush_context, instances) -> None:
    from backend.database.models import OrderEvent
    for o in session.deleted:
        if isinstance(o, OrderEvent):
            raise OrderEventImmutable("order_events는 지울 수 없다 (append-only)")
    for o in session.dirty:
        if isinstance(o, OrderEvent) and session.is_modified(o):
            raise OrderEventImmutable("order_events는 고칠 수 없다 (append-only)")


def install() -> None:
    """Register the hooks on every ``Session`` (idempotent)."""
    if not event.contains(Session, "after_flush", _record):
        event.listen(Session, "after_flush", _record)
    if not event.contains(Session, "before_flush", _guard):
        event.listen(Session, "before_flush", _guard)


#: The append-only guard below the ORM. Same SQL as the Alembic migration.
_GUARD_SQL = (
    """CREATE OR REPLACE FUNCTION order_events_append_only() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION USING MESSAGE = 'order_events is append-only: ' || TG_OP;
    END;
    $$ LANGUAGE plpgsql""",
    """CREATE OR REPLACE TRIGGER order_events_append_only
    BEFORE UPDATE OR DELETE ON order_events
    FOR EACH ROW EXECUTE FUNCTION order_events_append_only()""",
    """CREATE OR REPLACE TRIGGER order_events_no_truncate
    BEFORE TRUNCATE ON order_events
    FOR EACH STATEMENT EXECUTE FUNCTION order_events_append_only()""",
)

#: Serialises two processes installing the guard at once.
_GUARD_LOCK_KEY = 2190001


def ensure_db_guard(engine) -> bool:
    """Install the Postgres trigger that refuses UPDATE, DELETE and TRUNCATE on
    ``order_events`` — idempotently. The databases this platform runs on are
    built by ``create_all``, which never runs the Alembic migration, so without
    this the guard would exist only on paper. Never raises: a missing guard is
    logged, it does not stop the process. Returns whether it is installed.
    """
    import logging
    log = logging.getLogger(__name__)
    if engine.dialect.name != "postgresql":
        return False
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(f"SELECT pg_advisory_xact_lock({_GUARD_LOCK_KEY})")
            for stmt in _GUARD_SQL:
                conn.exec_driver_sql(stmt)
        return True
    except Exception as e:
        log.error("order_events append-only 트리거 설치 실패 — ORM 가드만 동작: %s", e)
        return False


def history(sess, order_id: int) -> list:
    """The order's events, oldest first."""
    from backend.database.models import OrderEvent
    return (sess.query(OrderEvent).filter(OrderEvent.order_id == order_id)
            .order_by(OrderEvent.id).all())


def latest_status(sess, order_id: int):
    """The status the order's latest event left it in, or ``None`` with no events."""
    from backend.database.models import OrderEvent
    row = (sess.query(OrderEvent.to_status).filter(OrderEvent.order_id == order_id)
           .order_by(OrderEvent.id.desc()).first())
    return row[0] if row else None
