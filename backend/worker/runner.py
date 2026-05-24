"""
전략 실행 Worker — API 프로세스와 분리
실행: python -m backend.worker.runner

Redis Pub/Sub:
  strategy:start  → 전략 시작
  strategy:stop   → 전략 중단
재시작 시 strategy_runs 테이블에서 활성 전략 자동 복원.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime

import redis

from backend.brokers.kis import KISBroker
from backend.database.models import Order as DBOrder, Position as DBPosition, StrategyRun, init_db
from backend.execution.order_machine import FillEvent, OrderStateMachine
from backend.execution.position_tracker import Fill, PositionTracker
from backend.strategy.base import StrategyBase
from backend.strategy.indicator.strategy import IndicatorStrategy

logger = logging.getLogger(__name__)

_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
_DB_URL = os.environ.get("DB_URL", "postgresql://quantdinger:quantdinger123@postgres:5432/quantdinger")


class WorkerSession:
    """하나의 전략 실행 세션."""

    def __init__(self, run_id: int, strategy: StrategyBase, db_session):
        self.run_id = run_id
        self.strategy = strategy
        self._db = db_session
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"strategy-{self.run_id}")
        self._thread.start()
        logger.info("Worker 세션 시작: run_id=%d", self.run_id)

    def stop(self):
        self._stop_event.set()
        self.strategy.stop()
        logger.info("Worker 세션 중단 요청: run_id=%d", self.run_id)

    def _run(self):
        try:
            self.strategy.start()
            while not self._stop_event.is_set():
                time.sleep(1)
        except Exception as e:
            logger.exception("전략 실행 오류 run_id=%d: %s", self.run_id, e)
        finally:
            self._mark_stopped()

    def _mark_stopped(self):
        try:
            run = self._db.get(StrategyRun, self.run_id)
            if run:
                run.is_active = False
                run.stopped_at = datetime.utcnow()
                self._db.commit()
        except Exception as e:
            logger.warning("run 상태 업데이트 실패: %s", e)


class StrategyWorker:
    """Redis Pub/Sub 구독 + 전략 세션 관리."""

    def __init__(self):
        self._redis = redis.from_url(_REDIS_URL)
        self._db = init_db(_DB_URL)
        self._sessions: dict[int, WorkerSession] = {}
        self._lock = threading.Lock()

    def run(self):
        self._restore_active()
        pubsub = self._redis.pubsub()
        pubsub.subscribe("strategy:start", "strategy:stop")
        logger.info("Worker 대기 중 (Redis Pub/Sub)...")

        for message in pubsub.listen():
            if message["type"] != "message":
                continue
            channel = message["channel"].decode()
            data = json.loads(message["data"])

            if channel == "strategy:start":
                self._handle_start(data)
            elif channel == "strategy:stop":
                self._handle_stop(data)

    # ── 이벤트 핸들러 ─────────────────────────────────────────────────────
    def _handle_start(self, data: dict):
        run_id = data["run_id"]
        with self._lock:
            if run_id in self._sessions:
                logger.warning("이미 실행 중: run_id=%d", run_id)
                return

        strategy = self._build_strategy(data)
        if strategy is None:
            return

        session = WorkerSession(run_id, strategy, self._db)
        with self._lock:
            self._sessions[run_id] = session
        session.start()

    def _handle_stop(self, data: dict):
        run_id = data["run_id"]
        with self._lock:
            session = self._sessions.pop(run_id, None)
        if session:
            session.stop()
        else:
            logger.warning("중단할 세션 없음: run_id=%d", run_id)

    # ── 재시작 복원 ───────────────────────────────────────────────────────
    def _restore_active(self):
        rows = self._db.query(StrategyRun).filter(StrategyRun.is_active == True).all()
        logger.info("활성 전략 복원: %d개", len(rows))
        for row in rows:
            try:
                config = json.loads(row.config or "{}")
                data = {
                    "run_id": row.id,
                    "name": row.name,
                    "strategy_type": row.strategy_type,
                    "config": config,
                    "broker": row.broker,
                }
                self._handle_start(data)
            except Exception as e:
                logger.warning("복원 실패 run_id=%d: %s", row.id, e)

    # ── 전략 팩토리 ──────────────────────────────────────────────────────
    def _build_strategy(self, data: dict) -> StrategyBase | None:
        broker = KISBroker()
        machine = OrderStateMachine(on_state_change=lambda o: self._persist_order(o))
        tracker = PositionTracker(machine)
        self._restore_positions(tracker, broker=data.get("broker", "kis"))

        stype = data.get("strategy_type", "indicator")
        config = data.get("config", {})
        name = data.get("name", "unnamed")

        try:
            if stype == "indicator":
                return IndicatorStrategy(broker=broker, tracker=tracker,
                                         machine=machine, name=name, config=config)
            elif stype == "script":
                from backend.strategy.script.strategy import ScriptStrategy
                script_src = config.get("script", "")
                return ScriptStrategy(broker=broker, tracker=tracker,
                                      name=name, script=script_src, config=config)
            else:
                logger.warning("알 수 없는 전략 유형: %s", stype)
                return None
        except Exception as e:
            logger.error("전략 생성 실패: %s", e)
            return None

    # ── DB 연동 ───────────────────────────────────────────────────────────
    def _persist_order(self, order):
        try:
            existing = self._db.query(DBOrder).filter(
                DBOrder.broker_order_id == order.id
            ).first()
            if existing:
                existing.status = order.status.value
                existing.qty = order.filled_qty
            else:
                row = DBOrder(
                    broker_order_id=order.id,
                    symbol=order.symbol,
                    side=order.side,
                    qty=order.qty,
                    price=order.price,
                    status=order.status.value,
                    market="US" if len(order.symbol) < 6 else "KR",
                )
                self._db.add(row)
            self._db.commit()
        except Exception as e:
            logger.warning("주문 DB 저장 실패: %s", e)

    def _restore_positions(self, tracker: PositionTracker, broker: str = "kis"):
        try:
            from backend.brokers.models import Position as BPosition
            rows = self._db.query(DBPosition).filter(DBPosition.broker == broker).all()
            positions = [
                BPosition(symbol=r.symbol, qty=r.qty, avg_price=r.avg_price, market=r.market)
                for r in rows
            ]
            tracker.restore_positions(positions)
        except Exception as e:
            logger.warning("포지션 복원 실패: %s", e)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from backend.worker.scheduler import build_scheduler
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("스케줄러 시작")

    worker = StrategyWorker()
    worker.run()  # blocking


if __name__ == "__main__":
    main()
