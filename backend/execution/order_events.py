"""
Shared terminal broker-event application (TASK P3-02B).

A broker order can end in CANCELLED / REJECTED / EXPIRED. The OrderFillPoller
observes these and invokes terminal callbacks, but the runtime must then
actually converge: transition the OrderStateMachine, release the
PositionTracker pending lock, and write an audit row. Before P3-02B the live
worker wired only on_filled/on_timeout, so a terminal broker event never
reached the runtime — the machine/DB order stayed SUBMITTED and the pending
lock was held until its 30-min TTL (see docs/PAPER_TRADING_REMEDIATION_AUDIT
findings C1/H1).

Both the live worker (runner + strategy) and the paper-trading harness route
their terminal callbacks here so the behaviour is identical and covered by one
set of tests.

Idempotent by design: a repeated or duplicate terminal event for the same
order (poller re-delivery, reconcile overlap) is safe — a second call finds the
order already terminal, applies no transition, and only re-asserts the
(idempotent) pending-lock release.

Dependency-light on purpose: it imports only brokers.models and duck-types
`machine`/`tracker`, so it carries no pandas/redis import and is unit-testable
without the full worker.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from backend.brokers.models import Order, OrderStatus

logger = logging.getLogger(__name__)

_TERMINAL = (OrderStatus.FILLED, OrderStatus.CANCELED,
             OrderStatus.REJECTED, OrderStatus.EXPIRED)
_TERMINAL_NEGATIVE = (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED)


def apply_terminal_event(machine, tracker, order: Order, *,
                         target_status: Optional[OrderStatus] = None,
                         db_factory=None, actor: str = "runtime") -> bool:
    """Converge runtime state to a terminal (negative) broker event.

    - Transitions the OrderStateMachine to the terminal status (if the order is
      known and not already terminal).
    - Releases the PositionTracker pending lock for the symbol (always,
      idempotent) so a terminal order never blocks re-entry until the TTL.
    - Writes an audit row when db_factory is provided.

    A partially-filled order reported as REJECTED is converged to CANCELLED
    instead: the already-filled quantity is real and cannot be wholesale
    rejected, so only the unfilled remainder is voided (this is also the one
    transition the state machine forbids: PARTIAL_FILLED → REJECTED).

    Returns True iff a state-machine transition was applied.
    """
    status = target_status or order.status
    if status not in _TERMINAL_NEGATIVE:
        logger.warning("apply_terminal_event: 비(非)터미널 상태 무시 %s (order=%s)",
                       getattr(status, "value", status), order.id)
        return False

    transitioned = False
    o = machine.get(order.id) if machine is not None else None
    if o is not None and o.status not in _TERMINAL:
        # PARTIAL_FILLED can't be rejected wholesale — void the remainder.
        if status is OrderStatus.REJECTED and o.status is OrderStatus.PARTIAL_FILLED:
            status = OrderStatus.CANCELED
        try:
            machine.transition(order.id, status)
            transitioned = True
        except Exception as e:  # invalid transition / unknown — never raise into poller
            logger.warning("terminal transition 실패 %s → %s: %s",
                           order.id, status.value, e)

    # Always release the pending lock (idempotent), even when the order was
    # unknown to the machine — an orphaned pending lock is the worse failure.
    if tracker is not None:
        try:
            tracker.unmark_pending(order.symbol)
        except Exception as e:
            logger.warning("unmark_pending 실패 %s: %s", order.symbol, e)

    _audit(db_factory, order, status, transitioned, actor)
    return transitioned


def _audit(db_factory, order: Order, status: OrderStatus,
           transitioned: bool, actor: str) -> None:
    """Fire-and-forget AuditLog write. Never raises into the caller."""
    if db_factory is None:
        return
    try:
        from backend.database.models import AuditLog
        sess = db_factory()
        try:
            sess.add(AuditLog(
                event_type=f"runtime_terminal_{status.value}",
                symbol=order.symbol,
                order_id=order.id,
                actor=actor,
                detail=json.dumps({"status": status.value,
                                   "transitioned": transitioned}, ensure_ascii=False),
            ))
            sess.commit()
        finally:
            sess.close()
    except Exception as e:
        logger.warning("terminal 이벤트 감사 로그 실패: %s", e)
