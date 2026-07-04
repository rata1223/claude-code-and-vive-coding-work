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
  4. 브로커에 없는 DB 포지션 (외부 수동 매도 등) — 미체결 주문 없을 때만 삭제
"""
import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
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

    broker_name 파라미터로 브로커를 구분해 DB 쿼리를 필터링한다.
    dry_run=True 이면 갭만 탐지하고 DB를 수정하지 않는다.

    사용 예:
        reconciler = PositionReconciler(broker, db_factory, redis_client, broker_name="kis")
        result = reconciler.reconcile("startup")
    """

    # 포지션 수량 허용 오차: 브로커와 DB가 ±1주 이내면 무시
    _QTY_TOLERANCE = 1

    # 스테일 포지션(브로커에 없는 DB 포지션) 삭제 최소 나이
    _STALE_MIN_AGE_HOURS = 1.0

    def __init__(self, broker: BrokerAdapter, db_factory: Callable,
                 redis_client=None, poller=None, broker_name: str = "kis"):
        self._broker = broker
        self._factory = db_factory
        self._redis = redis_client
        self._poller = poller  # OrderFillPoller: register re-discovered pending orders
        self._broker_name = broker_name
        self._reconcile_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def reconcile(self, trigger: str = "periodic", dry_run: bool = False) -> ReconciliationResult:
        """메인 조정 루틴. 브로커 → DB 방향으로 수리. 동시 호출은 즉시 스킵.

        dry_run=True 이면 갭만 탐지, DB 수정 없음 (ReconciliationLog는 기록).
        """
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
                    self._persist_log(result, dry_run)
                    return result

                self._reconcile_positions(broker_positions, result, dry_run)
                self._reconcile_pending_orders(result)
                result.finish()
            except Exception as e:
                result.error(f"조정 예외: {e}")
                result.finish()
                logger.exception("조정 예외")
            finally:
                self._persist_log(result, dry_run)
                self._publish_ws(result)

            logger.info(
                "조정 완료 [%s] broker=%s dry_run=%s: 갭=%d 수정=%d 오류=%d",
                trigger, self._broker_name, dry_run,
                len(result.gaps), len(result.repairs), len(result.errors),
            )
            return result
        finally:
            self._reconcile_lock.release()

    # ── Position reconciliation ────────────────────────────────────────────

    def _fetch_broker_positions(self, result: ReconciliationResult) -> Optional[dict[str, Position]]:
        try:
            positions = self._broker.get_positions()
            return {p.symbol: p for p in positions}
        except NotImplementedError:
            result.gap("broker_unimplemented", "",
                       f"{self._broker_name} get_positions() 미구현 — 포지션 조정 스킵")
            return {}  # empty dict: position reconcile skipped, order reconcile proceeds
        except Exception as e:
            result.error(f"브로커 포지션 조회 오류: {e}")
            return None  # None: abort reconciliation entirely

    def _reconcile_positions(self, broker_pos: dict[str, Position],
                             result: ReconciliationResult, dry_run: bool):
        from backend.database.models import Position as DBPosition

        with _session(self._factory) as db:
            db_rows = db.query(DBPosition).filter(
                DBPosition.broker == self._broker_name
            ).all()
            # Extract scalars before any session boundary
            db_pos_data = {
                r.symbol: {
                    "id": r.id,
                    "qty": r.qty,
                    "avg_price": r.avg_price,
                    "updated_at": r.updated_at,
                }
                for r in db_rows
            }

            # Case 1/3: broker has position
            for sym, bp in broker_pos.items():
                dp = db_pos_data.get(sym)
                if dp is None:
                    # Case 3: broker has it, DB doesn't (external buy)
                    result.gap("missing_in_db", sym,
                               f"브로커 qty={bp.qty} avg={bp.avg_price:.2f} — DB 없음")
                    if not dry_run:
                        self._audit_position_change(
                            "reconcile_insert", sym,
                            {"broker_qty": bp.qty, "broker_avg": bp.avg_price,
                             "broker_name": self._broker_name, "trigger": result.trigger},
                        )
                        new_row = DBPosition(
                            symbol=sym, qty=bp.qty, avg_price=bp.avg_price,
                            market=bp.market, broker=self._broker_name,
                        )
                        db.add(new_row)
                        result.repaired("insert_position", sym,
                                        f"DB에 포지션 추가: qty={bp.qty}")
                else:
                    qty_diff = abs(dp["qty"] - bp.qty)
                    price_changed = abs(dp["avg_price"] - bp.avg_price) > 0.01

                    if qty_diff > self._QTY_TOLERANCE:
                        if self._has_pending_order(sym, db):
                            result.gap("qty_mismatch_pending", sym,
                                       f"DB qty={dp['qty']} vs 브로커 qty={bp.qty} "
                                       f"— 미체결 주문 있음, 수정 보류")
                        else:
                            result.gap("qty_mismatch", sym,
                                       f"DB qty={dp['qty']} vs 브로커 qty={bp.qty}")
                            if not dry_run:
                                self._audit_position_change(
                                    "reconcile_fix_qty", sym,
                                    {"db_qty": dp["qty"], "broker_qty": bp.qty,
                                     "db_avg": dp["avg_price"], "broker_avg": bp.avg_price,
                                     "broker_name": self._broker_name, "trigger": result.trigger},
                                )
                                row = db.get(DBPosition, dp["id"])
                                if row:
                                    row.qty = bp.qty
                                    row.avg_price = bp.avg_price
                                    row.updated_at = datetime.utcnow()
                                result.repaired("fix_qty", sym,
                                                f"DB qty {dp['qty']}→{bp.qty}")
                    elif price_changed and qty_diff <= self._QTY_TOLERANCE:
                        # avg_price drift only — always safe to fix
                        if not dry_run:
                            self._audit_position_change(
                                "reconcile_fix_avg_price", sym,
                                {"db_avg": dp["avg_price"], "broker_avg": bp.avg_price,
                                 "broker_name": self._broker_name, "trigger": result.trigger},
                            )
                            row = db.get(DBPosition, dp["id"])
                            if row:
                                row.avg_price = bp.avg_price
                                row.updated_at = datetime.utcnow()
                            result.repaired("fix_avg_price", sym,
                                            f"avg_price {dp['avg_price']:.4f}→{bp.avg_price:.4f}")

            # Case 4: DB has it, broker doesn't (external sell / liquidation)
            for sym, dp in db_pos_data.items():
                if sym not in broker_pos:
                    age_hours = 0.0
                    if dp["updated_at"]:
                        age_hours = (datetime.utcnow() - dp["updated_at"]).total_seconds() / 3600

                    if self._has_pending_order(sym, db):
                        result.gap("stale_position_pending", sym,
                                   f"DB qty={dp['qty']} 브로커에 없음 "
                                   f"— 미체결 주문 있음, 삭제 보류")
                    elif age_hours < self._STALE_MIN_AGE_HOURS:
                        result.gap("stale_position_too_young", sym,
                                   f"DB qty={dp['qty']} 브로커에 없음 "
                                   f"— 포지션 나이 {age_hours:.1f}h < {self._STALE_MIN_AGE_HOURS}h, 삭제 보류")
                    else:
                        result.gap("stale_db_position", sym,
                                   f"DB qty={dp['qty']} — 브로커에 없음 (청산됨)")
                        if not dry_run:
                            self._audit_position_change(
                                "reconcile_delete", sym,
                                {"db_qty": dp["qty"], "age_hours": age_hours,
                                 "broker_name": self._broker_name, "trigger": result.trigger},
                            )
                            row = db.get(DBPosition, dp["id"])
                            if row:
                                db.delete(row)
                            result.repaired("delete_position", sym, "DB 스테일 포지션 삭제")

            if not dry_run:
                db.commit()

    def _has_pending_order(self, symbol: str, db) -> bool:
        """Return True if there is any open order for this symbol and broker."""
        from backend.database.models import Order as DBOrder
        _open = [OrderStatus.PENDING.value, OrderStatus.SUBMITTED.value, OrderStatus.PARTIAL_FILLED.value]
        return db.query(DBOrder).filter(
            DBOrder.symbol == symbol,
            DBOrder.broker == self._broker_name,
            DBOrder.status.in_(_open),
        ).first() is not None

    # ── Order reconciliation ────────────────────────────────────────────────

    def _reconcile_pending_orders(self, result: ReconciliationResult):
        """DB에서 open 상태인 주문을 브로커에 확인해 stale 처리."""
        from backend.database.models import Order as DBOrder

        _open = [OrderStatus.PENDING.value, OrderStatus.SUBMITTED.value,
                 OrderStatus.PARTIAL_FILLED.value, "unknown"]
        with _session(self._factory) as db:
            rows = db.query(DBOrder).filter(
                DBOrder.status.in_(_open),
                DBOrder.broker == self._broker_name,
            ).all()
            # Extract scalars before session closes (detached objects are fragile)
            open_orders = [
                {
                    "id": r.id,
                    "broker_order_id": r.broker_order_id,
                    "symbol": r.symbol,
                    "status": r.status,
                    "created_at": r.created_at,
                }
                for r in rows
            ]

        if not open_orders:
            return

        for db_order in open_orders:
            if not db_order["broker_order_id"]:
                result.gap("missing_broker_id", db_order["symbol"],
                           f"broker_order_id 없음 — 조정 스킵 (id={db_order['id']})")
                continue
            try:
                broker_order = self._broker.get_order_status(
                    db_order["broker_order_id"] or "",
                    db_order["symbol"],
                )
            except NotImplementedError:
                logger.debug("get_order_status 미구현 (%s) — 주문 조정 스킵", self._broker_name)
                break  # entire broker doesn't support it; skip all orders
            except Exception as e:
                result.error(f"주문 조회 오류 {db_order['broker_order_id']}: {e}")
                continue

            if broker_order is None:
                # KIS has no record — treat as lost/rejected if > 1h old
                age_hours = (datetime.utcnow() - db_order["created_at"]).total_seconds() / 3600
                if age_hours > 1:
                    result.gap("lost_order", db_order["symbol"],
                               f"주문 {db_order['broker_order_id']} 브로커 미조회 (나이 {age_hours:.1f}h)")
                    self._mark_order_lost(db_order["id"], result)
                continue

            # Sync broker state → DB
            new_status = broker_order.status.value
            if new_status != db_order["status"]:
                result.gap("order_status_mismatch", db_order["symbol"],
                           f"DB={db_order['status']} 브로커={new_status}")
                self._sync_order_status(db_order["id"], broker_order, result)

    def _mark_order_lost(self, db_order_id: int, result: ReconciliationResult):
        from backend.database.models import Order as DBOrder
        with _session(self._factory) as db:
            row = db.get(DBOrder, db_order_id)
            if row:
                # Attempt broker cancel with full params (US cancel requires symbol+qty+price)
                if row.broker_order_id:
                    try:
                        self._broker.cancel_order(
                            order_id=row.broker_order_id,
                            symbol=row.symbol,
                            qty=row.qty,
                            price=float(row.price or 0),
                        )
                    except Exception as e:
                        logger.warning("reconciler cancel_order 실패 %s: %s", row.broker_order_id, e)
                else:
                    logger.warning("broker_order_id 없음 — 취소 스킵 (db_id=%d)", row.id)
                row.status = OrderStatus.CANCELED.value
                row.error = "조정: 브로커 미조회 → 취소 처리"
                row.updated_at = datetime.utcnow()
                db.commit()
                result.repaired("cancel_lost_order", row.symbol,
                                f"주문 {row.broker_order_id} 취소 처리")

    def _sync_order_status(self, db_order_id: int, broker_order, result: ReconciliationResult):
        from backend.database.models import Order as DBOrder, Fill as DBFill

        # Route the broker-confirmed state through the SINGLE fill-processing pipeline
        # (OrderFillPoller) when this order is owned by a live poller entry. resync()
        # re-drives the exact same machine → tracker → PnL → DB → audit path as normal
        # polling, so the runtime (in-memory tracker + state machine + pending lock) is
        # repaired without a restart — and there is never a second fill processor here.
        routed = False
        if self._poller is not None:
            try:
                routed = self._poller.resync(broker_order)
            except Exception as e:
                logger.warning("poller resync 실패 — DB 폴백: %s", e)

        # A FILLED order routed through the pipeline had its Fill row + DBOrder advanced
        # by that single authority. Writing here too would duplicate the fill/audit.
        if routed and broker_order.status == OrderStatus.FILLED:
            result.repaired("sync_order_runtime", broker_order.symbol,
                            f"주문 {broker_order.id}: 런타임 파이프라인 경유 체결 반영")
            return

        with _session(self._factory) as db:
            row = db.get(DBOrder, db_order_id)
            if row is None:
                return
            old_status = row.status
            row.status = broker_order.status.value
            row.filled_qty = broker_order.filled_qty
            row.avg_fill_price = broker_order.avg_fill_price
            row.updated_at = datetime.utcnow()

            # Insert a Fill only on the DB-only fallback (poller did not own the order).
            # Use enum value for comparison (not raw string) so an enum rename isn't silently missed.
            # The _reconcile_lock prevents concurrent reconciler runs; this application-level
            # check is the dedup guard against a simultaneous poller callback.
            if (not routed and broker_order.status == OrderStatus.FILLED
                    and old_status != OrderStatus.FILLED.value):
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

    # ── Audit ──────────────────────────────────────────────────────────────

    def _audit_position_change(self, event_type: str, symbol: str, detail: dict) -> None:
        """Fire-and-forget AuditLog write. Failure does not abort reconciliation."""
        try:
            from backend.database.models import AuditLog
            with _session(self._factory) as db:
                db.add(AuditLog(
                    event_type=event_type,
                    symbol=symbol,
                    actor=f"reconciler:{self._broker_name}",
                    detail=json.dumps(detail, ensure_ascii=False),
                ))
                db.commit()
        except Exception as e:
            logger.warning("AuditLog 기록 실패 [%s %s]: %s", event_type, symbol, e)

    # ── Persistence / observability ────────────────────────────────────────

    def _persist_log(self, result: ReconciliationResult, dry_run: bool = False):
        from backend.database.models import ReconciliationLog
        try:
            detail = result.to_dict()
            if dry_run:
                detail["dry_run"] = True
            with _session(self._factory) as db:
                db.add(ReconciliationLog(
                    trigger=result.trigger,
                    broker=self._broker_name,
                    started_at=result.started_at,
                    completed_at=result.completed_at,
                    gaps_found=len(result.gaps),
                    repairs_made=len(result.repairs),
                    error="; ".join(result.errors) if result.errors else None,
                    detail=json.dumps(detail, ensure_ascii=False),
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
