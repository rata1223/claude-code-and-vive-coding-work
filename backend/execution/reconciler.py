"""
포지션·주문 조정(Reconciliation) 엔진.

브로커 포지션을 ground-truth로 삼아 DB 상태의 불일치를 감지·수정한다.

트리거:
  - startup   : StartupRecovery._step_reconcile() 에서 호출 (프로세스 기동 시)
  - periodic  : 스케줄러에서 매 30분 호출 (장중)
  - manual    : /api/admin/reconcile POST 에서 호출

수정 대상:
  1. DB에서 pending/submitted 인데 브로커에서 이미 filled/canceled
  2. DB 포지션 ≠ 브로커 포지션 (수량·평균단가 불일치)
  3. DB에 없는 브로커 포지션 (외부 수동 매수 등)
  4. 브로커에 없는 DB 포지션 (외부 수동 매도 등)
"""
import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Optional

from backend.brokers.base import BrokerAdapter
from backend.brokers.models import OrderStatus, Position

logger = logging.getLogger(__name__)


@contextmanager
def _session(factory):
    sess = factory()
    try:
        yield sess
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


class ReconciliationResult:
    def __init__(self, trigger: str):
        self.trigger = trigger
        self.started_at = datetime.utcnow()
        self.completed_at: Optional[datetime] = None
        self.gaps: list[dict] = []
        self.repairs: list[dict] = []
        self.errors: list[str] = []

    def gap(self, kind: str, symbol: str, detail: str):
        self.gaps.append({"kind": kind, "symbol": symbol, "detail": detail})
        logger.warning("조정 갭 [%s] %s: %s", kind, symbol, detail)

    def repaired(self, kind: str, symbol: str, detail: str):
        self.repairs.append({"kind": kind, "symbol": symbol, "detail": detail})
        logger.info("조정 수정 [%s] %s: %s", kind, symbol, detail)

    def error(self, msg: str):
        self.errors.append(msg)
        logger.error("조정 오류: %s", msg)

    def finish(self):
        self.completed_at = datetime.utcnow()

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "trigger": self.trigger,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "gaps_found": len(self.gaps),
            "repairs_made": len(self.repairs),
            "errors": self.errors,
            "gaps": self.gaps,
            "repairs": self.repairs,
        }


class PositionReconciler:
    """
    브로커와 DB 사이의 포지션·주문 상태 조정기.

    사용 예:
        reconciler = PositionReconciler(broker, db_factory, redis_client)
        result = reconciler.reconcile("startup")
    """

    # 포지션 수량 허용 오차: 브로커와 DB가 ±1주 이내면 무시
    _QTY_TOLERANCE = 1

    def __init__(self, broker: BrokerAdapter, db_factory: Callable,
                 redis_client=None, poller=None):
        self._broker = broker
        self._factory = db_factory
        self._redis = redis_client
        self._poller = poller  # OrderFillPoller: register re-discovered pending orders
        self._reconcile_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def reconcile(self, trigger: str = "periodic") -> ReconciliationResult:
        """메인 조정 루틴. 브로커 → DB 방향으로 수리. 동시 실행 차단."""
        if not self._reconcile_lock.acquire(blocking=False):
            logger.warning("조정 이미 진행 중 — 스킵: %s", trigger)
            r = ReconciliationResult(trigger)
            r.error("조정 이미 진행 중 (스킵)")
            r.finish()
            return r
        try:
            result = ReconciliationResult(trigger)
            try:
                broker_positions = self._fetch_broker_positions(result)
                if broker_positions is None:
                    result.error("브로커 포지션 조회 실패 — 조정 중단")
                    result.finish()
                    self._persist_log(result)
                    return result

                self._reconcile_positions(broker_positions, result)
                self._reconcile_pending_orders(result)
                result.finish()
            except Exception as e:
                result.error(f"조정 예외: {e}")
                result.finish()
                logger.exception("조정 예외")
            finally:
                self._persist_log(result)
                self._publish_ws(result)

            logger.info(
                "조정 완료 [%s]: 갭=%d 수정=%d 오류=%d",
                trigger, len(result.gaps), len(result.repairs), len(result.errors),
            )
            return result
        finally:
            self._reconcile_lock.release()

    # ── Position reconciliation ────────────────────────────────────────────

    def _fetch_broker_positions(self, result: ReconciliationResult) -> Optional[dict[str, Position]]:
        try:
            positions = self._broker.get_positions()
            return {p.symbol: p for p in positions}
        except Exception as e:
            result.error(f"브로커 포지션 조회 오류: {e}")
            return None

    def _reconcile_positions(self, broker_pos: dict[str, Position], result: ReconciliationResult):
        from backend.database.models import Position as DBPosition

        with _session(self._factory) as db:
            db_rows = db.query(DBPosition).all()
            db_pos = {r.symbol: r for r in db_rows}

            # Case 1: DB position ≠ broker (qty mismatch or avg_price drift)
            for sym, bp in broker_pos.items():
                dp = db_pos.get(sym)
                if dp is None:
                    # Case 3: broker has it, DB doesn't (external buy)
                    result.gap("missing_in_db", sym,
                               f"브로커 qty={bp.qty} avg={bp.avg_price:.2f} — DB 없음")
                    new_row = DBPosition(
                        symbol=sym, qty=bp.qty, avg_price=bp.avg_price,
                        market=bp.market, broker="kis",
                    )
                    db.add(new_row)
                    result.repaired("insert_position", sym,
                                    f"DB에 포지션 추가: qty={bp.qty}")
                else:
                    qty_diff = abs(dp.qty - bp.qty)
                    if qty_diff > self._QTY_TOLERANCE:
                        result.gap("qty_mismatch", sym,
                                   f"DB qty={dp.qty} vs 브로커 qty={bp.qty}")
                        dp.qty = bp.qty
                        dp.avg_price = bp.avg_price
                        dp.updated_at = datetime.utcnow()
                        result.repaired("fix_qty", sym,
                                        f"DB qty {dp.qty}→{bp.qty}")

            # Case 4: DB has it, broker doesn't (external sell / liquidation)
            for sym, dp in db_pos.items():
                if sym not in broker_pos:
                    result.gap("stale_db_position", sym,
                               f"DB qty={dp.qty} — 브로커에 없음 (청산됨)")
                    db.delete(dp)
                    result.repaired("delete_position", sym, "DB 스테일 포지션 삭제")

            db.commit()

    # ── Order reconciliation ────────────────────────────────────────────────

    def _reconcile_pending_orders(self, result: ReconciliationResult):
        """DB에서 open 상태인 주문을 브로커에 확인해 stale 처리."""
        from backend.database.models import Order as DBOrder

        with _session(self._factory) as db:
            open_orders = db.query(DBOrder).filter(
                DBOrder.status.in_(["pending", "submitted", "partial_filled"])
            ).all()

        if not open_orders:
            return

        for db_order in open_orders:
            try:
                broker_order = self._broker.get_order_status(
                    db_order.broker_order_id or "",
                    db_order.symbol,
                )
            except Exception as e:
                result.error(f"주문 조회 오류 {db_order.broker_order_id}: {e}")
                continue

            if broker_order is None:
                # KIS has no record — treat as lost/rejected if > 1h old
                age_hours = (datetime.utcnow() - db_order.created_at).total_seconds() / 3600
                if age_hours > 1:
                    result.gap("lost_order", db_order.symbol,
                               f"주문 {db_order.broker_order_id} 브로커 미조회 (나이 {age_hours:.1f}h)")
                    self._mark_order_lost(db_order.id, result)
                continue

            # Sync broker state → DB
            new_status = broker_order.status.value
            if new_status != db_order.status:
                result.gap("order_status_mismatch", db_order.symbol,
                           f"DB={db_order.status} 브로커={new_status}")
                self._sync_order_status(db_order.id, broker_order, result)

    def _mark_order_lost(self, db_order_id: int, result: ReconciliationResult):
        from backend.database.models import Order as DBOrder
        with _session(self._factory) as db:
            row = db.get(DBOrder, db_order_id)
            if row:
                # Attempt broker cancel with full params (US cancel requires symbol+qty+price)
                try:
                    self._broker.cancel_order(
                        order_id=row.broker_order_id or "",
                        symbol=row.symbol,
                        qty=row.qty,
                        price=float(row.price or 0),
                    )
                except Exception as e:
                    logger.warning("reconciler cancel_order 실패 %s: %s", row.broker_order_id, e)
                row.status = OrderStatus.CANCELED.value
                row.error = "조정: 브로커 미조회 → 취소 처리"
                row.updated_at = datetime.utcnow()
                db.commit()
                result.repaired("cancel_lost_order", row.symbol,
                                f"주문 {row.broker_order_id} 취소 처리")

    def _sync_order_status(self, db_order_id: int, broker_order, result: ReconciliationResult):
        from backend.database.models import Order as DBOrder, Fill as DBFill
        with _session(self._factory) as db:
            row = db.get(DBOrder, db_order_id)
            if row is None:
                return
            old_status = row.status
            row.status = broker_order.status.value
            row.filled_qty = broker_order.filled_qty
            row.avg_fill_price = broker_order.avg_fill_price
            row.updated_at = datetime.utcnow()

            # If newly filled: insert fill record if not already there
            if broker_order.status == OrderStatus.FILLED and old_status != "filled":
                existing_fill = db.query(DBFill).filter(DBFill.order_id == row.id).first()
                if not existing_fill:
                    db.add(DBFill(
                        order_id=row.id,
                        qty=broker_order.filled_qty,
                        price=broker_order.avg_fill_price or broker_order.price,
                    ))
            db.commit()
            result.repaired("sync_order", row.symbol,
                            f"주문 {row.broker_order_id}: {old_status}→{row.status}")

    # ── Persistence / observability ────────────────────────────────────────

    def _persist_log(self, result: ReconciliationResult):
        from backend.database.models import ReconciliationLog
        try:
            with _session(self._factory) as db:
                db.add(ReconciliationLog(
                    trigger=result.trigger,
                    started_at=result.started_at,
                    completed_at=result.completed_at,
                    gaps_found=len(result.gaps),
                    repairs_made=len(result.repairs),
                    error="; ".join(result.errors) if result.errors else None,
                    detail=json.dumps(result.to_dict(), ensure_ascii=False),
                ))
                db.commit()
        except Exception as e:
            logger.warning("조정 로그 저장 실패: %s", e)

    def _publish_ws(self, result: ReconciliationResult):
        if not result.gaps and not result.errors:
            return
        try:
            from backend.websocket.server import publish_alert
            msg = (f"조정 완료 [{result.trigger}]: "
                   f"갭 {len(result.gaps)}건 수정 {len(result.repairs)}건")
            level = "warning" if result.gaps else "info"
            if result.errors:
                level = "error"
            publish_alert(msg, level=level)
        except Exception:
            pass
