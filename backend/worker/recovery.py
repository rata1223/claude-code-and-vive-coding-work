"""
재시작 복구 시퀀스.

Worker 프로세스가 시작될 때 이 모듈의 StartupRecovery를 먼저 실행한다.
복구가 완료되기 전까지 SafeModeState.can_trade = False 이며,
전략의 매수·매도 진입을 차단한다.

8-step 순서:
  1. DB 연결 확인
  2. Redis 연결 확인
  3. 일일 리스크 상태 복원 (PersistentLossTracker)
  4. 브로커 잔고 조회 (KIS API 연결 확인)
  5. 브로커 포지션 조회
  6. DB 포지션과 브로커 포지션 대조 (reconcile)
  7. 미체결 주문 확인 → OrderFillPoller에 등록
  8. 정상 모드 진입 (can_trade = True)
"""
import concurrent.futures as _cf
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

_BROKER_STARTUP_TIMEOUT = int(os.environ.get("BROKER_STARTUP_TIMEOUT", "30"))

logger = logging.getLogger(__name__)


@dataclass
class ReconcileAction:
    symbol: str
    action: str            # "accept_broker" | "ghost_position" | "untracked_position"
    broker_qty: int = 0
    db_qty: int = 0
    note: str = ""


class SafeModeState:
    """전략 실행 허용 여부를 전역으로 관리한다."""

    def __init__(self):
        self._can_trade = False
        self._reason = "초기화 중"

    @property
    def can_trade(self) -> bool:
        return self._can_trade

    def enable(self) -> None:
        self._can_trade = True
        self._reason = "정상"
        logger.info("SafeMode 해제 — 매매 허용")

    def disable(self, reason: str) -> None:
        self._can_trade = False
        self._reason = reason
        logger.warning("SafeMode 활성화: %s", reason)

    def __repr__(self) -> str:
        return f"SafeModeState(can_trade={self._can_trade}, reason={self._reason!r})"


# Process-level safe mode gate — strategies should check this before placing orders
SAFE_MODE = SafeModeState()


class StartupRecovery:
    """
    Worker 시작 시 8단계 복구 시퀀스 실행.
    완료 후 SAFE_MODE.enable() 호출.
    """

    def __init__(self, db_session_factory, redis_client=None, broker=None, poller=None):
        self._factory = db_session_factory
        self._redis = redis_client
        self._broker = broker
        self._shared_poller = poller  # Worker's poller — avoid creating a second one
        self._actions: list[ReconcileAction] = []

    def run(self) -> bool:
        """복구 실행. 성공 시 True, 치명적 오류 시 False."""
        steps = [
            ("DB 연결 확인", self._step_db),
            ("Redis 연결 확인", self._step_redis),
            ("일일 리스크 상태 복원", self._step_risk),
            ("브로커 잔고 조회", self._step_balance),
            ("브로커 포지션 조회", self._step_positions),
            ("포지션 대조 (reconcile)", self._step_reconcile),
            ("미체결 주문 확인", self._step_pending_orders),
            ("정상 모드 진입", self._step_enable_trading),
        ]
        for i, (name, fn) in enumerate(steps, 1):
            logger.info("[복구 %d/%d] %s", i, len(steps), name)
            try:
                ok = fn()
                if not ok:
                    logger.error("[복구 %d/%d] 실패: %s — SafeMode 유지", i, len(steps), name)
                    SAFE_MODE.disable(f"복구 실패: {name}")
                    return False
            except Exception as e:
                logger.exception("[복구 %d/%d] 예외: %s — %s", i, len(steps), name, e)
                SAFE_MODE.disable(f"복구 예외: {name}: {e}")
                return False
        return True

    def reconcile_actions(self) -> list[ReconcileAction]:
        return list(self._actions)

    # ── Steps ──────────────────────────────────────────────────────────────

    def _step_db(self) -> bool:
        try:
            from sqlalchemy import text
            db = self._factory()
            db.execute(text("SELECT 1"))
            db.close()
            return True
        except Exception as e:
            logger.error("DB 연결 실패: %s", e)
            return False

    def _step_redis(self) -> bool:
        if self._redis is None:
            logger.warning("Redis 클라이언트 없음 — Redis 단계 스킵")
            return True
        try:
            self._redis.ping()
            return True
        except Exception as e:
            logger.warning("Redis 연결 실패: %s — Redis 없이 계속 진행", e)
            return True  # non-fatal; worker can operate in polling mode

    def _step_risk(self) -> bool:
        try:
            from backend.quant.risk.engine import PersistentLossTracker, RiskConfig
            tracker = PersistentLossTracker(
                config=RiskConfig(),
                redis_client=self._redis,
                db_factory=self._factory,
            )
            if tracker.kill_switch:
                logger.warning("킬스위치 복원됨: %s — 매매 차단 유지", tracker.kill_reason)
                self._kill_switch_active = True
                self._kill_reason = tracker.kill_reason
            return True
        except Exception as e:
            logger.warning("리스크 상태 복원 실패: %s — 기본값 사용", e)
            return True  # non-fatal

    def _step_balance(self) -> bool:
        if self._broker is None:
            logger.warning("브로커 없음 — 잔고 단계 스킵")
            return True
        try:
            with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                bal = ex.submit(self._broker.get_balance).result(timeout=_BROKER_STARTUP_TIMEOUT)
            logger.info("잔고 확인: 총평가 %.0f원", bal.total_eval_krw)
            return True
        except _cf.TimeoutError:
            logger.error("잔고 조회 타임아웃 (%ds) — KIS API 응답 없음", _BROKER_STARTUP_TIMEOUT)
            return False
        except Exception as e:
            logger.error("잔고 조회 실패: %s", e)
            return False

    def _step_positions(self) -> bool:
        if self._broker is None:
            return True
        try:
            with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                positions = ex.submit(self._broker.get_positions).result(timeout=_BROKER_STARTUP_TIMEOUT)
            logger.info("브로커 포지션: %d개 %s",
                        len(positions), [p.symbol for p in positions])
            return True
        except _cf.TimeoutError:
            logger.error("포지션 조회 타임아웃 (%ds) — KIS API 응답 없음", _BROKER_STARTUP_TIMEOUT)
            return False
        except Exception as e:
            logger.error("포지션 조회 실패: %s", e)
            return False

    def _step_reconcile(self) -> bool:
        if self._broker is None:
            return True
        try:
            import os
            import redis as _redis
            from backend.execution.reconciler import PositionReconciler
            r = None
            try:
                r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
            except Exception:
                pass
            result = PositionReconciler(
                broker=self._broker,
                db_factory=self._factory,
                redis_client=r,
                broker_name="kis",
            ).reconcile("startup")
            # Populate _actions from reconcile result for observability
            for gap in result.gaps:
                self._actions.append(ReconcileAction(
                    symbol=gap["symbol"],
                    action=gap["kind"],
                    note=gap["detail"],
                ))
            for repair in result.repairs:
                logger.debug("스타트업 조정 수정: %s", repair)
            logger.info("스타트업 조정 완료: 갭=%d 수정=%d", len(result.gaps), len(result.repairs))
            return True
        except Exception as e:
            logger.warning("Reconcile 실패: %s — 스킵", e)
            return True  # non-fatal

    def _step_pending_orders(self) -> bool:
        if self._broker is None:
            return True
        try:
            from backend.database.models import Order as DBOrder, Fill as DBFill
            from backend.execution.order_poller import OrderFillPoller
            from backend.brokers.models import Order as BOrder, OrderStatus
            db = self._factory()
            pending = (db.query(DBOrder)
                       .filter(DBOrder.status.in_(["pending", "submitted", "partial_filled"]))
                       .filter(DBOrder.broker_order_id.isnot(None))
                       .all())
            db.close()
            if pending:
                logger.info("미체결 주문 %d개 발견 — OrderFillPoller에 재등록", len(pending))
                # Reuse Worker's shared poller to avoid duplicate polling threads
                if self._shared_poller is not None:
                    poller = self._shared_poller
                else:
                    poller = OrderFillPoller(self._broker)
                    poller.start()

                def _make_recovery_fill_cb(db_order_pk: int, broker_order_id: str):
                    """Persist fill to DB on recovery; in-memory state updated when strategies restart."""
                    def on_filled(order: BOrder):
                        sess = self._factory()
                        try:
                            row = sess.get(DBOrder, db_order_pk)
                            if row:
                                row.status = order.status.value
                                row.filled_qty = order.filled_qty or order.qty
                                row.avg_fill_price = order.avg_fill_price or order.price
                                fill = DBFill(order_id=db_order_pk,
                                              qty=order.filled_qty or order.qty,
                                              price=order.avg_fill_price or order.price)
                                sess.add(fill)
                                sess.commit()
                                logger.info("복구 체결 DB 업데이트: %s → FILLED", broker_order_id)
                        except Exception as e:
                            logger.warning("복구 체결 DB 저장 실패: %s", e)
                        finally:
                            sess.close()
                    return on_filled

                for row in pending:
                    try:
                        status = OrderStatus(row.status)
                    except ValueError:
                        status = OrderStatus.SUBMITTED
                    border = BOrder(
                        id=row.broker_order_id,
                        symbol=row.symbol,
                        side=row.side,
                        qty=row.qty,
                        price=row.price or 0,
                        status=status,
                    )
                    poller.register(
                        border,
                        on_filled=_make_recovery_fill_cb(row.id, row.broker_order_id),
                    )

                # After all pending orders are processed, schedule a post-recovery
                # reconcile to sync positions once fills start arriving.
                import threading, os
                def _post_recovery_reconcile():
                    try:
                        import redis as _redis
                        from backend.execution.reconciler import PositionReconciler
                        r = None
                        try:
                            r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
                        except Exception:
                            pass
                        PositionReconciler(
                            broker=self._broker,
                            db_factory=self._factory,
                            redis_client=r,
                            broker_name="kis",
                        ).reconcile("post_recovery")
                    except Exception as ex:
                        logger.warning("post-recovery 포지션 조정 실패: %s", ex)
                threading.Thread(
                    target=_post_recovery_reconcile,
                    daemon=True,
                    name="post-recovery-reconcile",
                ).start()
        except Exception as e:
            logger.warning("미체결 주문 복원 실패: %s", e)
        return True

    def _step_enable_trading(self) -> bool:
        import os
        if os.environ.get("KIS_ENV") == "real":
            try:
                from backend.worker.promotion_guard import LivePromotionGuard
                ok, failed = LivePromotionGuard(self._factory, self._redis).check()
                if not ok:
                    logger.critical("실전 매매 프로모션 체크 실패: %s — SafeMode 유지", failed)
                    SAFE_MODE.disable(f"실전 프로모션 미완: {failed}")
                    return False
                logger.info("실전 매매 프로모션 체크 통과")
            except Exception as e:
                logger.warning("LivePromotionGuard 로드 실패: %s — 실전 차단", e)
                SAFE_MODE.disable("LivePromotionGuard 오류")
                return False

        # Block trading if kill-switch was active from the previous session
        if getattr(self, "_kill_switch_active", False):
            reason = getattr(self, "_kill_reason", "알 수 없음")
            SAFE_MODE.disable(f"킬스위치 복원: {reason}")
            logger.critical("킬스위치 복원 — 매매 차단. 수동 해제 후 재시작 필요.")
            try:
                from bot.notifier import alert_emergency
                alert_emergency(
                    f"[킬스위치 복원] 재시작 후에도 매매 차단 중\n사유: {reason}\n수동 해제 필요"
                )
            except Exception as e:
                logger.warning("킬스위치 재시작 Telegram 알림 실패: %s", e)
            return False

        SAFE_MODE.enable()
        logger.info("복구 완료 — 매매 허용. reconcile actions=%d", len(self._actions))
        return True

