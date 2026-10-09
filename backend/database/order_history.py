"""Order history: every change to ``orders`` leaves an ``order_events`` row (P2-01).

``orders`` is updated in place — status, filled quantity, average fill price,
broker order number — by the runner, recovery, the reconciler and the terminal
event handler. After a crash or a reconcile nothing said how an order got where
it is. This records it **once, at the session**, rather than at each writer:

* ``after_flush`` turns every new ``Order`` and every change to a tracked field
  into an ``OrderEvent``, executed on the flush's own connection — the event
  commits or rolls back with the change it describes. PKs are assigned by then;
  attribute history still shows the values before the flush.
* ``before_flush`` refuses to update or delete an ``OrderEvent`` through the
  ORM: the log only grows. (Postgres also enforces it with a trigger, added by
  the Alembic migration ``e2f3a4b5c6d7``.)

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


def _row(obj, kind: str, from_status) -> dict:
    return {
        "order_id": obj.id,
        "kind": kind,
        "from_status": from_status,
        "to_status": obj.status,
        "filled_qty": obj.filled_qty,
        "avg_fill_price": obj.avg_fill_price,
        "broker_order_id": obj.broker_order_id,
        "recorded_at": datetime.utcnow(),
    }


def _record(session: Session, flush_context) -> None:
    from backend.database.models import Order, OrderEvent
    rows = [_row(o, "created", None) for o in session.new if isinstance(o, Order)]
    for o in session.dirty:
        if isinstance(o, Order):
            changed, before = _changed(o)
            if changed:
                rows.append(_row(o, "updated", before))
    if rows:
        # The flush's own connection: same transaction as the change.
        session.connection().execute(OrderEvent.__table__.insert(), rows)


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
