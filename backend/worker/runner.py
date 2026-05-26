"""
전략 실행 Worker — API 프로세스와 분리
실행: python -m backend.worker.runner

Redis Pub/Sub:
  strategy:start    → 전략 시작
  strategy:stop     → 전략 중단
  session:kr_open   → 한국 시장 장 시작 (스케줄러에서 발행)
  session:us_open   → 미국 시장 장 시작 (스케줄러에서 발행)
재시작 시 strategy_runs 테이블에서 활성 전략 자동 복원.
"""
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime

import redis

from backend.brokers.kis import KISBroker
from backend.database.models import (
    Order as DBOrder, Position as DBPosition, StrategyRun, init_db_factory,
)
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import Fill, PositionTracker
from backend.strategy.base import StrategyBase
from backend.strategy.indicator.strategy import IndicatorStrategy

logger = logging.getLogger(__name__)

_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
_DB_URL = os.environ.get("DB_URL", "postgresql://quantdinger:quantdinger@postgres:5432/quantdinger")

_SUBSCRIBE_CHANNELS = ["strategy:start", "strategy:stop", "session:kr_open", "session:us_open"]

# Process-level sessionmaker — each call to _session() yields an isolated Session
_SessionFactory = None


def _get_session_factory():
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = init_db_factory(_DB_URL)
    return _SessionFactory


@contextmanager
def _session():
    """Yield a per-call SQLAlchemy Session; always closes on exit."""
    factory = _get_session_factory()
    sess = factory()
    try:
        yield sess
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


class WorkerSession:
    """하나의 전략 실행 세션."""

    def __init__(self, run_id: int, strategy: StrategyBase):
        self.run_id = run_id
        self.strategy = strategy
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

    def trigger_market_open(self, market: str):
        """Scheduler calls this when a market session opens."""
        try:
            self.strategy.on_market_open()
            logger.info("[run_id=%d] on_market_open 호출 완료 (market=%s)", self.run_id, market)
        except Exception as e:
            logger.exception("on_market_open 오류 run_id=%d: %s", self.run_id, e)

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
            with _session() as db:
                run = db.get(StrategyRun, self.run_id)
                if run:
                    run.is_active = False
                    run.stopped_at = datetime.utcnow()
                    db.commit()
        except Exception as e:
            logger.warning("run 상태 업데이트 실패: %s", e)


class StrategyWorker:
    """Redis Pub/Sub 구독 + 전략 세션 관리."""

    def __init__(self):
        self._redis = redis.from_url(_REDIS_URL)
        self._sessions: dict[int, WorkerSession] = {}
        self._lock = threading.Lock()

    def run(self):
        self._restore_active()
        self._run_with_pubsub()

    def _run_with_pubsub(self):
        backoff = 2.0
        while True:
            try:
                pubsub = self._redis.pubsub()
                pubsub.subscribe(*_SUBSCRIBE_CHANNELS)
                logger.info("Worker 대기 중 (Redis Pub/Sub: %s)...", _SUBSCRIBE_CHANNELS)
                backoff = 2.0

                for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    channel = message["channel"].decode()
                    try:
                        data = json.loads(message["data"])
                    except Exception:
                        data = {}

                    if channel == "strategy:start":
                        self._handle_start(data)
                    elif channel == "strategy:stop":
                        self._handle_stop(data)
                    elif channel in ("session:kr_open", "session:us_open"):
                        market = "KR" if channel == "session:kr_open" else "US"
                        self._handle_market_open(market)

            except redis.ConnectionError as e:
                logger.error("Redis 연결 끊김: %s — %.1fs 후 재연결", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 64.0)
                self._enter_db_polling_mode()
            except Exception as e:
                logger.exception("Worker 예외: %s — %.1fs 후 재시작", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 64.0)

    def _enter_db_polling_mode(self):
        """Redis 불가 시 DB commands 테이블을 30초마다 폴링."""
        from backend.database.models import Command
        logger.warning("DB 폴링 모드 전환 (Redis 불가)")
        while True:
            try:
                # try Redis first — if it comes back, exit polling mode
                self._redis.ping()
                logger.info("Redis 재연결 성공 — 폴링 모드 종료")
                return
            except Exception:
                pass

            try:
                with _session() as db:
                    cmds = (db.query(Command)
                            .filter(Command.status == "pending")
                            .order_by(Command.created_at)
                            .limit(20)
                            .all())
                    for cmd in cmds:
                        try:
                            data = json.loads(cmd.payload)
                            if cmd.channel == "strategy:start":
                                self._handle_start(data)
                            elif cmd.channel == "strategy:stop":
                                self._handle_stop(data)
                            cmd.status = "processed"
                            cmd.processed_at = datetime.utcnow()
                        except Exception as e:
                            logger.warning("명령 처리 실패 id=%d: %s", cmd.id, e)
                            cmd.status = "error"
                    db.commit()
            except Exception as e:
                logger.warning("DB 폴링 오류: %s", e)

            time.sleep(30)

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

        session = WorkerSession(run_id, strategy)
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

    def _handle_market_open(self, market: str):
        """Broadcast on_market_open() to all active strategy sessions."""
        with self._lock:
            sessions = list(self._sessions.values())
        logger.info("장 시작 브로드캐스트: market=%s sessions=%d", market, len(sessions))
        for session in sessions:
            t = threading.Thread(
                target=session.trigger_market_open,
                args=(market,),
                daemon=True,
                name=f"market-open-{session.run_id}",
            )
            t.start()

    # ── 재시작 복원 ───────────────────────────────────────────────────────
    def _restore_active(self):
        try:
            with _session() as db:
                rows = db.query(StrategyRun).filter(StrategyRun.is_active == True).all()
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
        except Exception as e:
            logger.error("활성 전략 복원 전체 실패: %s", e)

    # ── 전략 팩토리 ──────────────────────────────────────────────────────
    def _build_strategy(self, data: dict) -> StrategyBase | None:
        from backend.brokers.kis import get_kis_broker
        broker = get_kis_broker()
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
            with _session() as db:
                existing = db.query(DBOrder).filter(
                    DBOrder.broker_order_id == order.id
                ).first()
                if existing:
                    existing.status = order.status.value
                    existing.filled_qty = order.filled_qty
                    existing.avg_fill_price = order.avg_fill_price or None
                    existing.updated_at = datetime.utcnow()
                else:
                    market = "US" if (len(order.symbol) < 6 or not order.symbol.isdigit()) else "KR"
                    row = DBOrder(
                        broker_order_id=order.id,
                        symbol=order.symbol,
                        side=order.side,
                        qty=order.qty,
                        price=order.price,
                        status=order.status.value,
                        market=market,
                        trade_date=datetime.utcnow().date(),
                    )
                    db.add(row)
                db.commit()
        except Exception as e:
            logger.warning("주문 DB 저장 실패: %s", e)

    def _restore_positions(self, tracker: PositionTracker, broker: str = "kis"):
        try:
            from backend.brokers.models import Position as BPosition
            with _session() as db:
                rows = db.query(DBPosition).filter(DBPosition.broker == broker).all()
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

    # ── 시작 복구 시퀀스 ───────────────────────────────────────────────────
    from backend.worker.recovery import StartupRecovery
    from backend.brokers.kis import get_kis_broker
    factory = _get_session_factory()
    r_client = redis.from_url(_REDIS_URL)
    try:
        broker = get_kis_broker()
    except Exception as e:
        logger.error("KISBroker 초기화 실패: %s — SafeMode 유지", e)
        broker = None

    recovery = StartupRecovery(db_session_factory=factory, redis_client=r_client, broker=broker)
    if not recovery.run():
        logger.critical("복구 실패 — Worker SafeMode로 계속 실행")

    from backend.worker.scheduler import build_scheduler
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("스케줄러 시작")

    worker = StrategyWorker()
    worker.run()  # blocking


if __name__ == "__main__":
    main()
