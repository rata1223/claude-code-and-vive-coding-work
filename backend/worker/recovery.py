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
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

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

    def __init__(self, db_session_factory, redis_client=None, broker=None):
        self._factory = db_session_factory
        self._redis = redis_client
        self._broker = broker
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
            db = self._factory()
            tracker = PersistentLossTracker(
                config=RiskConfig(),
                redis_client=self._redis,
                db_session=db,
            )
            db.close()
            if tracker.kill_switch:
                logger.warning("킬스위치 복원됨: %s", tracker.kill_reason)
            return True
        except Exception as e:
            logger.warning("리스크 상태 복원 실패: %s — 기본값 사용", e)
            return True  # non-fatal

    def _step_balance(self) -> bool:
        if self._broker is None:
            logger.warning("브로커 없음 — 잔고 단계 스킵")
            return True
        try:
            bal = self._broker.get_balance()
            logger.info("잔고 확인: 총평가 %.0f원", bal.total_eval_krw)
            return True
        except Exception as e:
            logger.error("잔고 조회 실패: %s", e)
            return False

    def _step_positions(self) -> bool:
        if self._broker is None:
            return True
        try:
            positions = self._broker.get_positions()
            self._broker_positions = {p.symbol: p for p in positions}
            logger.info("브로커 포지션: %d개 %s",
                        len(positions), [p.symbol for p in positions])
            return True
        except Exception as e:
            logger.error("포지션 조회 실패: %s", e)
            return False

    def _step_reconcile(self) -> bool:
        if self._broker is None or not hasattr(self, "_broker_positions"):
            return True
        try:
            from backend.database.models import Position as DBPosition
            db = self._factory()
            db_rows = db.query(DBPosition).filter(DBPosition.broker == "kis").all()
            db.close()
            db_positions = {r.symbol: r for r in db_rows}

            broker_syms = set(self._broker_positions)
            db_syms = set(db_positions)

            for sym in broker_syms - db_syms:
                action = ReconcileAction(
                    symbol=sym,
                    action="untracked_position",
                    broker_qty=self._broker_positions[sym].qty,
                    db_qty=0,
                    note="브로커에는 있지만 DB에 없음 — DB에 추가",
                )
                self._actions.append(action)
                logger.warning("미추적 포지션 발견: %s qty=%d", sym, action.broker_qty)
                self._write_position_to_db(self._broker_positions[sym])

            for sym in db_syms - broker_syms:
                action = ReconcileAction(
                    symbol=sym,
                    action="ghost_position",
                    broker_qty=0,
                    db_qty=db_positions[sym].qty,
                    note="DB에는 있지만 브로커에 없음 — DB에서 제거",
                )
                self._actions.append(action)
                logger.warning("유령 포지션 제거: %s qty=%d", sym, action.db_qty)
                self._remove_position_from_db(sym)

            for sym in broker_syms & db_syms:
                b_qty = self._broker_positions[sym].qty
                d_qty = db_positions[sym].qty
                if b_qty != d_qty:
                    action = ReconcileAction(
                        symbol=sym, action="accept_broker",
                        broker_qty=b_qty, db_qty=d_qty,
                        note=f"수량 불일치 → 브로커 기준으로 DB 갱신",
                    )
                    self._actions.append(action)
                    logger.warning("수량 불일치 %s: broker=%d db=%d", sym, b_qty, d_qty)
                    self._update_position_in_db(self._broker_positions[sym])

            return True
        except Exception as e:
            logger.warning("Reconcile 실패: %s — 스킵", e)
            return True  # non-fatal

    def _step_pending_orders(self) -> bool:
        if self._broker is None:
            return True
        try:
            from backend.database.models import Order as DBOrder
            from backend.execution.order_poller import OrderFillPoller
            db = self._factory()
            pending = (db.query(DBOrder)
                       .filter(DBOrder.status.in_(["pending", "submitted", "partial_filled"]))
                       .filter(DBOrder.broker_order_id.isnot(None))
                       .all())
            db.close()
            if pending:
                logger.info("미체결 주문 %d개 발견 — OrderFillPoller에 재등록", len(pending))
                poller = OrderFillPoller(self._broker)
                poller.start()
                from backend.brokers.models import Order as BOrder, OrderStatus
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
                    poller.register(border, on_filled=lambda o: None)
        except Exception as e:
            logger.warning("미체결 주문 복원 실패: %s", e)
        return True

    def _step_enable_trading(self) -> bool:
        SAFE_MODE.enable()
        logger.info("복구 완료 — 매매 허용. reconcile actions=%d", len(self._actions))
        return True

    # ── DB helpers ─────────────────────────────────────────────────────────

    def _write_position_to_db(self, pos) -> None:
        try:
            from backend.database.models import Position as DBPosition
            db = self._factory()
            row = DBPosition(symbol=pos.symbol, qty=pos.qty,
                             avg_price=pos.avg_price, market=pos.market, broker="kis")
            db.merge(row)
            db.commit()
            db.close()
        except Exception as e:
            logger.warning("포지션 DB 추가 실패: %s", e)

    def _remove_position_from_db(self, symbol: str) -> None:
        try:
            from backend.database.models import Position as DBPosition
            db = self._factory()
            db.query(DBPosition).filter(
                DBPosition.symbol == symbol, DBPosition.broker == "kis"
            ).delete()
            db.commit()
            db.close()
        except Exception as e:
            logger.warning("유령 포지션 DB 제거 실패: %s", e)

    def _update_position_in_db(self, pos) -> None:
        try:
            from backend.database.models import Position as DBPosition
            from datetime import datetime
            db = self._factory()
            row = db.query(DBPosition).filter(
                DBPosition.symbol == pos.symbol, DBPosition.broker == "kis"
            ).first()
            if row:
                row.qty = pos.qty
                row.avg_price = pos.avg_price
                row.updated_at = datetime.utcnow()
            db.commit()
            db.close()
        except Exception as e:
            logger.warning("포지션 DB 갱신 실패: %s", e)
