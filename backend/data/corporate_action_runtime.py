"""
Corporate-action runtime glue (TASK P2-02C — M4 integration).

Composes the pure, in-memory ``CorporateActionService`` with DB persistence so a
corporate action survives a restart and the trading gate stays fail-closed. This
is the object the reconciler, the position tracker, and startup recovery hold.

Ownership (per ``docs/CORPORATE_ACTION_RUNTIME_INTEGRATION.md``):
  * the **broker** is the sole authority for position value (qty/avg);
  * the **reconciler** is the sole *writer* of broker-adjusted positions;
  * this runtime **never** writes positions or re-scales price bars — it only
    **detects / classifies / records / gates**.

So no double-adjustment is possible by construction.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Optional

from backend.data.corporate_actions import (
    ActionStatus,
    ActionType,
    CorporateAction,
    CorporateActionDetector,
    CorporateActionPendingError,
    CorporateActionService,
)

logger = logging.getLogger(__name__)

# CA statuses that still block trading (i.e. should be restored on restart).
_BLOCKING_STATUSES = ("pending", "confirmed", "unknown")


class CorporateActionRuntime:
    """DB-backed detector/recorder/gate for live corporate actions.

    Stateless w.r.t. positions — it never mutates qty/avg. ``db_factory`` is a
    SQLAlchemy ``sessionmaker``; when ``None`` the runtime degrades to in-memory
    only (still classifies/gates within a process, just no persistence), so unit
    tests and dry runs work without a database.
    """

    def __init__(self, db_factory=None, broker: str = "kis",
                 service: Optional[CorporateActionService] = None,
                 detector: Optional[CorporateActionDetector] = None) -> None:
        self._db = db_factory
        self._broker = broker
        self._service = service or CorporateActionService(
            db_factory=db_factory, actor=f"corporate_actions:{broker}")
        self._detector = detector or CorporateActionDetector()

    # ── Classification ──────────────────────────────────────────────────────

    def classify_broker_jump(self, symbol: str, db_qty: float, db_avg: float,
                              broker_qty: float, broker_avg: float,
                              effective_date: Optional[date] = None) -> CorporateAction:
        """Label a broker↔DB quantity jump as a CONFIRMED split/reverse (value
        preserved + known ratio) or UNKNOWN (fail closed). Never adjusts."""
        eff = effective_date or date.today()
        return self._detector.classify_quantity_jump(
            symbol, db_qty, db_avg, broker_qty, broker_avg, eff)

    # ── Record / apply ──────────────────────────────────────────────────────

    def record(self, action: CorporateAction) -> Optional[int]:
        """Register the action in the in-memory gate (blocks the symbol) and
        upsert its DB row (idempotent on the unique key). Returns the row id."""
        self._service.register_action(action)
        return self._persist_action(action)

    def mark_applied(self, action: CorporateAction, *, qty_before: Optional[float],
                     avg_before: Optional[float], qty_after: Optional[float],
                     avg_after: Optional[float], cash_delta: float = 0.0,
                     value_preserved: bool = True, actor: Optional[str] = None) -> None:
        """Clear the gate for a CONFIRMED action whose value the broker has
        already adjusted, and persist the applied status + an append-only history
        row. Does NOT touch positions."""
        try:
            self._service.apply(action)  # clears pending + in-memory history + EVENT_APPLIED audit
        except Exception as exc:  # invalid/unsupported never reaches here for confirmed splits
            logger.warning("CA mark_applied service.apply 실패 (%s): %s", action.symbol, exc)
        self._persist_applied(action, qty_before, avg_before, qty_after, avg_after,
                              cash_delta, value_preserved, actor or f"reconciler:{self._broker}")

    # ── Gate ────────────────────────────────────────────────────────────────

    def is_blocked(self, symbol: str) -> bool:
        """True if `symbol` has a blocking (pending/confirmed-unapplied/unknown)
        corporate action. Pure read; never raises."""
        try:
            self._service.assert_tradeable(symbol)
            return False
        except CorporateActionPendingError:
            return True

    def assert_tradeable(self, symbol: str) -> None:
        self._service.assert_tradeable(symbol)

    def pending_for(self, symbol: str):
        return self._service.pending_for(symbol)

    # ── Restart recovery ────────────────────────────────────────────────────

    def restore_pending(self) -> int:
        """Reload blocking corporate actions from the DB into the in-memory gate
        so the symbol stays blocked across a restart. Returns count restored."""
        if self._db is None:
            return 0
        actions: list[CorporateAction] = []
        try:
            from backend.database.models import CorporateAction as CARow
            sess = self._db()
            try:
                rows = (sess.query(CARow)
                        .filter(CARow.broker == self._broker,
                                CARow.status.in_(_BLOCKING_STATUSES))
                        .all())
                for r in rows:
                    actions.append(self._row_to_action(r))
            finally:
                sess.close()
        except Exception as exc:
            logger.warning("CA restore_pending 실패: %s", exc)
            return 0
        n = self._service.restore_pending(actions)
        if n:
            logger.info("CA pending 복원: %d개 (broker=%s)", n, self._broker)
        return n

    # ── Persistence helpers ─────────────────────────────────────────────────

    def _persist_action(self, action: CorporateAction) -> Optional[int]:
        if self._db is None:
            return None
        try:
            from backend.database.models import CorporateAction as CARow
            sess = self._db()
            try:
                row = (sess.query(CARow)
                       .filter(CARow.broker == self._broker,
                               CARow.symbol == action.symbol,
                               CARow.effective_date == action.effective_date,
                               CARow.action_type == action.action_type.value)
                       .first())
                if row is None:
                    row = CARow(
                        broker=self._broker, symbol=action.symbol,
                        action_type=action.action_type.value,
                        effective_date=action.effective_date,
                        status=action.status.value, ratio=action.ratio,
                        cash_amount=action.cash_amount, new_symbol=action.new_symbol,
                        source=action.source, detail=action.detail,
                    )
                    sess.add(row)
                else:
                    # idempotent: keep the existing row, refresh mutable fields
                    row.status = action.status.value
                    row.ratio = action.ratio
                    row.detail = action.detail
                sess.commit()
                return row.id
            except Exception:
                sess.rollback()
                raise
            finally:
                sess.close()
        except Exception as exc:
            logger.warning("CA _persist_action 실패 (%s): %s", action.symbol, exc)
            return None

    def _persist_applied(self, action: CorporateAction, qty_before, avg_before,
                         qty_after, avg_after, cash_delta, value_preserved, actor) -> None:
        if self._db is None:
            return
        try:
            from backend.database.models import (
                CorporateAction as CARow, CorporateActionHistory as CAHist)
            sess = self._db()
            try:
                row = (sess.query(CARow)
                       .filter(CARow.broker == self._broker,
                               CARow.symbol == action.symbol,
                               CARow.effective_date == action.effective_date,
                               CARow.action_type == action.action_type.value)
                       .first())
                ca_id = None
                if row is not None:
                    row.status = "applied"
                    row.applied_at = datetime.utcnow()
                    ca_id = row.id
                sess.add(CAHist(
                    corporate_action_id=ca_id, broker=self._broker, symbol=action.symbol,
                    action_type=action.action_type.value,
                    qty_before=qty_before, avg_before=avg_before,
                    qty_after=qty_after, avg_after=avg_after,
                    cash_delta=cash_delta, value_preserved=value_preserved, actor=actor,
                ))
                sess.commit()
            except Exception:
                sess.rollback()
                raise
            finally:
                sess.close()
        except Exception as exc:
            logger.warning("CA _persist_applied 실패 (%s): %s", action.symbol, exc)

    def db_history_for(self, symbol: Optional[str] = None) -> list:
        """Read the append-only adjustment history from the DB."""
        if self._db is None:
            return []
        try:
            from backend.database.models import CorporateActionHistory as CAHist
            sess = self._db()
            try:
                q = sess.query(CAHist).filter(CAHist.broker == self._broker)
                if symbol is not None:
                    q = q.filter(CAHist.symbol == symbol)
                return q.order_by(CAHist.applied_at.asc()).all()
            finally:
                sess.close()
        except Exception as exc:
            logger.warning("CA db_history_for 실패: %s", exc)
            return []

    @staticmethod
    def _row_to_action(row) -> CorporateAction:
        return CorporateAction(
            action_type=ActionType(row.action_type), symbol=row.symbol,
            effective_date=row.effective_date, status=ActionStatus(row.status),
            ratio=row.ratio, cash_amount=row.cash_amount, new_symbol=row.new_symbol,
            source=row.source or "restored", detail=row.detail or "",
        )
