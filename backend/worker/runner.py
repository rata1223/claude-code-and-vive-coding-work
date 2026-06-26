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
from sqlalchemy.exc import IntegrityError

from backend.brokers.kis import KISBroker, get_kis_broker
from backend.brokers.models import Order, OrderStatus
from backend.database.models import (
    Fill as DBFill, Order as DBOrder, Position as DBPosition,
    StrategyRun, init_db_factory,
)
from backend.execution.order_machine import FillEvent, OrderStateMachine
from backend.execution.order_poller import OrderFillPoller
from backend.execution.position_tracker import Fill, PositionTracker
from backend.execution.reconciler import PositionReconciler
from backend.worker.heartbeat import WorkerHeartbeat
from backend.strategy.base import StrategyBase
from backend.strategy.indicator.strategy import IndicatorStrategy

logger = logging.getLogger(__name__)

_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
_DB_URL = os.environ.get("DB_URL", "postgresql://quantdinger:quantdinger@postgres:5432/quantdinger")

_SUBSCRIBE_CHANNELS = ["strategy:start", "strategy:stop", "session:kr_open", "session:us_open"]

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


def _audit(event_type: str, symbol: str = None, order_id: str = None,
           actor: str = "worker", detail: dict = None):
    """Fire-and-forget append-only audit log write. Never raises."""
    try:
        import json
        from backend.database.models import AuditLog
        with _session() as db:
            db.add(AuditLog(
                event_type=event_type,
                symbol=symbol,
                order_id=order_id,
                actor=actor,
                detail=json.dumps(detail, ensure_ascii=False) if detail else None,
            ))
            db.commit()
    except Exception as e:
        logger.warning("감사 로그 실패 (event=%s): %s", event_type, e)


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
        if self._stop_event.is_set():
            logger.info("[run_id=%d] 세션 중단 — on_market_open 스킵 (market=%s)", self.run_id, market)
            return
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
        self._last_market_open: dict[str, float] = {}  # market → monotonic ts; dedup gate

        # Process-level OrderFillPoller — shared across all strategy sessions
        try:
            from backend.brokers.semantic_mapper import BrokerSemanticMapper
            _kis = get_kis_broker()
            self._poller = OrderFillPoller(
                broker=_kis,
                db_factory=_get_session_factory(),
                semantic_mapper=BrokerSemanticMapper(_kis.capabilities),
            )
            self._poller.start()
            logger.info("OrderFillPoller 시작")
        except Exception as e:
            logger.warning("OrderFillPoller 초기화 실패: %s — 폴링 비활성화", e)
            self._poller = None

        # Process-level PersistentLossTracker — uses db_factory for per-op sessions (P1-3 fix)
        self._loss_tracker = None
        # Last successfully fetched live equity — used as the kill-switch fallback
        # when the broker balance API is temporarily unavailable (prevents MDD = 0%
        # masking). Seeded below from a real startup balance fetch.
        self._last_known_equity: float | None = None
        try:
            from backend.quant.risk.engine import PersistentLossTracker, RiskConfig
            self._loss_tracker = PersistentLossTracker(
                config=RiskConfig(),
                redis_client=self._redis,
                db_factory=_get_session_factory(),  # per-op sessions, not long-lived
            )
            logger.info("PersistentLossTracker 초기화 완료 (kill_switch=%s)", self._loss_tracker.kill_switch)

            # Seed equity from a live balance fetch at startup. This always sets
            # _last_known_equity (so the first fill's MDD check has a baseline even
            # if the balance API is briefly down later) and bootstraps peak_equity
            # on a cold start (peak_equity == 0).
            try:
                _eq = get_kis_broker().get_balance().total_eval_krw
                if _eq > 0:
                    self._last_known_equity = _eq
                    if self._loss_tracker.peak_equity == 0:
                        self._loss_tracker.peak_equity = _eq
                        self._loss_tracker._persist()
                        logger.info("peak_equity 초기화: %.0f원", _eq)
            except Exception as _e:
                logger.warning("기준 잔고 시드 실패 — 첫 체결 MDD 평가가 스킵될 수 있음: %s", _e)

        except Exception as e:
            logger.warning("PersistentLossTracker 초기화 실패: %s", e)

        # Heartbeat — lets watchdog / monitoring know the worker is alive
        self._heartbeat = WorkerHeartbeat(self._redis)
        self._heartbeat.start()

        # Corporate-action runtime — one detector/recorder/gate shared across the
        # reconciler, the per-strategy position trackers, and startup recovery, so
        # there is exactly one corporate-action gate per worker (P2-02C).
        from backend.data.corporate_action_runtime import CorporateActionRuntime
        self._ca_runtime = CorporateActionRuntime(
            db_factory=_get_session_factory(), broker="kis")

        # Reconciler — startup + periodic position/order reconciliation
        self._reconciler = PositionReconciler(
            broker=get_kis_broker(),
            db_factory=_get_session_factory(),
            redis_client=self._redis,
            poller=self._poller,
            ca_runtime=self._ca_runtime,
        )

    def run(self):
        self._restore_active()
        # Startup reconciliation: broker is ground truth on boot
        try:
            self._reconciler.reconcile("startup")
        except Exception as e:
            logger.warning("시작 조정 실패 (계속 진행): %s", e)
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
        _audit("redis_failover", actor="worker", detail={"mode": "db_polling"})
        while True:
            try:
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
                            elif cmd.channel in ("session:kr_open", "session:us_open"):
                                market = "KR" if cmd.channel == "session:kr_open" else "US"
                                self._handle_market_open(market)
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
            # Reserve slot under lock to prevent a concurrent duplicate start
            self._sessions[run_id] = None

        strategy = self._build_strategy(data)
        if strategy is None:
            with self._lock:
                self._sessions.pop(run_id, None)  # release reservation
            return

        session = WorkerSession(run_id, strategy)
        with self._lock:
            self._sessions[run_id] = session
        session.start()
        _audit("strategy_start", detail={"run_id": run_id, "strategy_type": data.get("strategy_type")})

    def _handle_stop(self, data: dict):
        run_id = data["run_id"]
        with self._lock:
            session = self._sessions.pop(run_id, None)
        if session:
            session.stop()
            _audit("strategy_stop", detail={"run_id": run_id})
        else:
            logger.warning("중단할 세션 없음: run_id=%d", run_id)

    def _handle_market_open(self, market: str):
        """Broadcast on_market_open() to all active strategy sessions with dedup."""
        now = time.monotonic()
        # F5: dedup check and sessions snapshot must share the same lock acquisition
        # to prevent two threads both passing the 5-minute guard and double-broadcasting.
        with self._lock:
            last = self._last_market_open.get(market, 0.0)
            if now - last < 300:  # 5-minute dedup window
                logger.warning("중복 market_open 무시: %s (마지막 %.0fs 전)", market, now - last)
                return
            self._last_market_open[market] = now
            sessions = [s for s in self._sessions.values() if s is not None]

        # Periodic reconciliation on each market open (catches overnight drift)
        threading.Thread(
            target=self._reconciler.reconcile,
            args=("periodic",),
            daemon=True,
            name=f"reconcile-{market}",
        ).start()

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
        try:
            broker = get_kis_broker()
        except Exception as e:
            logger.error("KISBroker 획득 실패: %s", e)
            return None

        machine = OrderStateMachine(on_state_change=lambda o: self._persist_order(o))
        tracker = PositionTracker(machine, corporate_action_runtime=self._ca_runtime)
        run_id = data.get("run_id", 0)

        on_filled_cb = self._make_fill_callback(tracker, machine, run_id)

        def on_timeout_cb(o):
            logger.warning("주문 타임아웃 — 브로커 취소 시도: %s %s %s", o.id, o.side, o.symbol)
            try:
                broker.cancel_order(
                    order_id=o.id,
                    symbol=o.symbol,
                    qty=o.qty,
                    price=float(o.price or 0),
                )
            except Exception as _e:
                logger.error("타임아웃 취소 예외 %s: %s", o.id, _e)
            finally:
                tracker.unmark_pending(o.symbol)

        self._restore_positions(tracker, broker=data.get("broker", "kis"))
        self._restore_pending_to_tracker(
            tracker, broker=data.get("broker", "kis"),
            on_filled_cb=on_filled_cb, on_timeout_cb=on_timeout_cb,
        )

        stype = data.get("strategy_type", "indicator")
        config = data.get("config", {})
        name = data.get("name", "unnamed")

        try:
            if stype == "indicator":
                return IndicatorStrategy(
                    broker=broker, tracker=tracker, machine=machine,
                    name=name, config=config,
                    poller=self._poller,
                    on_filled_cb=on_filled_cb,
                    on_timeout_cb=on_timeout_cb,
                )
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

    # ── Fill 파이프라인 ───────────────────────────────────────────────────
    def _make_fill_callback(self, tracker: PositionTracker,
                             machine: OrderStateMachine, run_id: int):
        """
        Returns a callback: Order → (machine → tracker → P&L → DB → WebSocket).
        This is the single integration point that closes the fill lifecycle loop.
        """
        def on_filled(order: Order):
            is_kr = len(order.symbol) == 6 and order.symbol.isdigit()
            fill = Fill(
                order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                qty=order.filled_qty or order.qty,
                price=order.avg_fill_price or order.price,
                market="KR" if is_kr else "US",
            )
            # Capture avg entry price before tracker modifies the position (needed for P&L)
            entry_price = None
            if fill.side == "sell":
                pos = tracker.get_position(fill.symbol)
                if pos is not None:
                    entry_price = pos.avg_price

            # 1. State machine
            try:
                if machine.get(order.id) is not None:
                    event = FillEvent(
                        order_id=order.id,
                        filled_qty=fill.qty,
                        fill_price=fill.price,
                    )
                    machine.process_fill(event)
            except Exception as e:
                logger.warning("machine.process_fill 오류: %s", e)

            # 2. Position tracker
            try:
                tracker.on_fill(fill)
            except Exception as e:
                logger.warning("tracker.on_fill 오류: %s", e)

            # 3. Record realized P&L for sell fills → feeds kill-switch evaluation
            if fill.side == "sell" and self._loss_tracker is not None:
                if entry_price is not None:
                    realized_pnl = (fill.price - entry_price) * fill.qty
                else:
                    # Sell with no tracked position: we cannot compute realized
                    # P&L, but we MUST still refresh equity so MDD/kill-switch
                    # evaluation runs (a desync must not silently disable risk).
                    realized_pnl = 0.0
                    logger.error("매도 체결이지만 진입가 미상 — 손익 0 처리, MDD만 평가: %s",
                                 fill.symbol)
                    _audit("sell_without_entry_price", symbol=fill.symbol,
                           detail={"fill_price": fill.price, "qty": fill.qty})
                try:
                    ks_before = self._loss_tracker.kill_switch
                    # Never fall back to peak_equity: MDD = (peak - peak)/peak = 0% masks drawdown.
                    # Use last-known-good equity; skip MDD evaluation if none available.
                    try:
                        current_equity = get_kis_broker().get_balance().total_eval_krw
                        self._last_known_equity = current_equity
                    except Exception as _be:
                        current_equity = self._last_known_equity
                        if current_equity is None:
                            logger.warning("잔고 조회 실패, 기준 잔고 없음 — MDD 평가 스킵: %s", _be)
                            _audit("balance_fetch_failed", symbol=fill.symbol,
                                   detail={"reason": str(_be), "realized_pnl": realized_pnl})
                        else:
                            logger.warning("잔고 조회 실패 — 마지막 확인 잔고(%.0f원) 사용: %s",
                                           current_equity, _be)
                    if current_equity is not None:
                        self._loss_tracker.record_pnl(realized_pnl, current_equity)
                        logger.info("손익 기록: %s %.0f원 (entry=%s fill=%.4f qty=%d)",
                                    fill.symbol, realized_pnl,
                                    f"{entry_price:.4f}" if entry_price is not None else "n/a",
                                    fill.price, fill.qty)
                        if not ks_before and self._loss_tracker.kill_switch:
                            _audit("kill_switch_triggered", symbol=fill.symbol,
                                   detail={"reason": self._loss_tracker.kill_reason,
                                           "realized_pnl": realized_pnl})
                except Exception as e:
                    logger.warning("P&L 기록 실패: %s", e)

            # 4. Persist fill + update order status
            self._persist_fill(fill, order)

            # 5. Upsert position in DB to reflect fill
            self._upsert_position_db(fill.symbol, fill.market, tracker.get_position(fill.symbol))

            # 6. WebSocket push
            self._publish_order_update(order)
            logger.info("체결 파이프라인 완료: %s %s qty=%d @ %.4f",
                        order.id, order.symbol, fill.qty, fill.price)

        return on_filled

    # ── DB 연동 ───────────────────────────────────────────────────────────
    def _persist_order(self, order: Order):
        # Derive a deterministic idempotency key from broker order id + date.
        # KIS ODNO is unique per trading day per account, so this composite key
        # prevents duplicate DB rows when the same order is processed twice.
        from datetime import date as _date
        idem_key = (
            f"{order.id}:{order.symbol}:{order.side}:{_date.today().isoformat()}"
            if order.id else None
        )
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
                    if idem_key:
                        dup = db.query(DBOrder).filter(
                            DBOrder.idempotency_key == idem_key
                        ).first()
                        if dup:
                            logger.warning("중복 주문 감지 (idempotency_key=%s) — 저장 스킵", idem_key)
                            return
                    market = "US" if (len(order.symbol) < 6 or not order.symbol.isdigit()) else "KR"
                    row = DBOrder(
                        broker_order_id=order.id,
                        idempotency_key=idem_key,
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
        except IntegrityError:
            # Unique-constraint violation on idempotency_key — another path persisted the
            # same order concurrently (crash-replay or duplicate event). Treat as a duplicate
            # and skip; the existing row is authoritative.
            logger.warning("중복 주문 감지 (IntegrityError, idempotency_key=%s) — 저장 스킵", idem_key)
        except Exception as e:
            logger.warning("주문 DB 저장 실패: %s", e)

    def _persist_fill(self, fill: Fill, order: Order):
        try:
            with _session() as db:
                db_order = db.query(DBOrder).filter(
                    DBOrder.broker_order_id == order.id
                ).first()
                if db_order is None:
                    logger.warning("체결 DB 저장 스킵: 미등록 주문 %s", order.id)
                    return
                # Idempotency: skip if a matching fill row already exists for this order.
                # Keeps the append-only fill history correct if the same fill is delivered
                # twice (e.g. recovery callback + re-registered poller callback racing).
                dup = db.query(DBFill).filter(
                    DBFill.order_id == db_order.id,
                    DBFill.qty == fill.qty,
                    DBFill.price == fill.price,
                ).first()
                if dup is not None:
                    logger.info("중복 체결 감지 — Fill 삽입 스킵: order=%s qty=%d", order.id, fill.qty)
                    return
                row = DBFill(order_id=db_order.id, qty=fill.qty, price=fill.price)
                db.add(row)
                db_order.status = order.status.value
                db_order.filled_qty = (db_order.filled_qty or 0) + fill.qty
                db_order.avg_fill_price = order.avg_fill_price or fill.price
                db.commit()

                # Immutable audit trail for fill events
                try:
                    from backend.database.models import AuditLog
                    db.add(AuditLog(
                        event_type="fill",
                        symbol=fill.symbol,
                        order_id=order.id,
                        actor="worker",
                        detail=json.dumps({
                            "side": fill.side,
                            "qty": fill.qty,
                            "price": fill.price,
                            "market": fill.market,
                        }),
                    ))
                    db.commit()
                except Exception as _ae:
                    logger.warning("AuditLog 체결 기록 실패: %s", _ae)
        except Exception as e:
            logger.warning("체결 DB 저장 실패: %s", e)

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

    def _restore_pending_to_tracker(self, tracker: PositionTracker, broker: str = "kis",
                                    on_filled_cb=None, on_timeout_cb=None):
        """Re-mark pending orders in tracker so duplicate orders are blocked after restart.

        Also re-registers each still-open order with the shared poller using the strategy's
        full fill callback (overwriting the DB-only recovery stub registered at startup), so
        that a post-restart fill flows through the normal pipeline and releases the pending
        lock instead of leaving the symbol stuck until the 30-min TTL.

        Queries are scoped to `broker` so a KIS strategy never restores another broker's
        pending orders (and vice versa).
        """
        try:
            with _session() as db:
                rows = db.query(DBOrder).filter(
                    DBOrder.broker == broker,
                    DBOrder.status.in_(["pending", "submitted", "partial_filled"]),
                    DBOrder.broker_order_id.isnot(None),
                ).all()
                # Extract scalars before the session closes (avoid DetachedInstanceError)
                pending = [
                    {"symbol": r.symbol, "order_id": r.broker_order_id, "side": r.side,
                     "qty": r.qty, "price": r.price or 0.0, "status": r.status}
                    for r in rows
                ]
            for p in pending:
                tracker.mark_pending(p["symbol"], p["order_id"])
                if self._poller is not None and on_filled_cb is not None:
                    self._register_recovered_order(p, tracker, on_filled_cb, on_timeout_cb)
                _audit("recovery_restore_pending", symbol=p["symbol"], order_id=p["order_id"],
                       detail={"broker": broker, "status": p["status"]})
            if pending:
                logger.info("미체결 주문 tracker 복원: %d개 %s",
                            len(pending), [p["symbol"] for p in pending])
        except Exception as e:
            logger.warning("pending tracker 복원 실패: %s", e)

    def _register_recovered_order(self, p: dict, tracker: PositionTracker,
                                  on_filled_cb, on_timeout_cb):
        """Register a recovered pending order with the shared poller under the full pipeline.

        Wrapped in a guard that skips processing if the order already reached a terminal
        FILLED state in the DB (the startup DB-only recovery callback may have fired in the
        narrow window between the pending query and this re-registration). In that case the
        pending lock is simply released to avoid a stuck symbol.
        """
        try:
            status = OrderStatus(p["status"])
        except ValueError:
            status = OrderStatus.SUBMITTED
        border = Order(
            id=p["order_id"], symbol=p["symbol"], side=p["side"],
            qty=p["qty"], price=p["price"], status=status,
        )

        def _guarded_on_filled(order: Order):
            try:
                with _session() as db:
                    row = db.query(DBOrder).filter(
                        DBOrder.broker_order_id == order.id
                    ).first()
                    already_done = row is not None and row.status == OrderStatus.FILLED.value
            except Exception as e:
                # Can't confirm whether the startup recovery callback already processed this
                # fill. Prioritise duplicate-execution safety: skip the full pipeline (which
                # would double-record P&L) and only release the lock. The periodic / post-
                # recovery reconcile (broker = ground truth) repairs position drift.
                logger.warning("복구 체결 가드 조회 실패 (%s) — 중복 방지 위해 파이프라인 스킵, 락만 해제: %s",
                               order.id, e)
                tracker.unmark_pending(order.symbol)
                return
            if already_done:
                tracker.unmark_pending(order.symbol)
                logger.info("복구 주문 이미 체결 처리됨 — 락 해제만 수행: %s", order.id)
                return
            on_filled_cb(order)

        self._poller.register(border, on_filled=_guarded_on_filled, on_timeout=on_timeout_cb)

    def _upsert_position_db(self, symbol: str, market: str, pos):
        """Upsert or delete position row in DB after a fill."""
        try:
            with _session() as db:
                row = db.query(DBPosition).filter(
                    DBPosition.symbol == symbol,
                    DBPosition.broker == "kis",
                ).first()
                if pos is None or pos.qty <= 0:
                    if row is not None:
                        db.delete(row)
                else:
                    if row is not None:
                        row.qty = pos.qty
                        row.avg_price = pos.avg_price
                        row.updated_at = datetime.utcnow()
                    else:
                        db.add(DBPosition(
                            symbol=symbol, qty=pos.qty,
                            avg_price=pos.avg_price, market=market, broker="kis",
                        ))
                db.commit()
        except Exception as e:
            logger.warning("포지션 DB 갱신 실패 (%s): %s", symbol, e)

    # ── WebSocket 발행 ────────────────────────────────────────────────────
    def _publish_order_update(self, order: Order):
        try:
            from backend.websocket.server import publish_order_update
            publish_order_update({
                "id": order.id,
                "symbol": order.symbol,
                "side": order.side,
                "status": order.status.value,
                "qty": order.qty,
                "filled_qty": order.filled_qty,
                "price": order.price,
                "avg_fill_price": order.avg_fill_price,
            })
        except Exception as e:
            logger.debug("WebSocket 주문 발행 실패: %s", e)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # Validate KIS_ENV / ENABLE_LIVE_TRADING consistency before anything starts.
    # KIS_ENV routes TR_IDs (paper vs real); ENABLE_LIVE_TRADING gates order submission.
    # A mismatch means orders are either silently blocked or routed to the wrong API.
    import sys as _sys
    _kis_env = os.environ.get("KIS_ENV", "paper")
    _live_enabled = os.environ.get("ENABLE_LIVE_TRADING", "false").lower() == "true"
    if _kis_env == "real" and not _live_enabled:
        logger.critical(
            "설정 불일치: KIS_ENV=real이지만 ENABLE_LIVE_TRADING=false — "
            "실전 TR_ID 사용 중 주문이 차단됩니다. 시작 거부."
        )
        _sys.exit(1)
    if _kis_env == "paper" and _live_enabled:
        logger.warning(
            "KIS_ENV=paper이지만 ENABLE_LIVE_TRADING=true — "
            "모의투자 TR_ID로 주문이 전송됩니다. 의도한 설정인지 확인하세요."
        )

    # Create Worker first so its single poller can be shared with recovery
    # (prevents dual-poller situation where recovery creates its own poller)
    worker = StrategyWorker()

    # ── 시작 복구 시퀀스 ───────────────────────────────────────────────────
    from backend.worker.recovery import StartupRecovery
    factory = _get_session_factory()
    r_client = redis.from_url(_REDIS_URL)
    try:
        broker = get_kis_broker()
    except Exception as e:
        logger.error("KISBroker 초기화 실패: %s — SafeMode 유지", e)
        broker = None

    recovery = StartupRecovery(
        db_session_factory=factory,
        redis_client=r_client,
        broker=broker,
        poller=worker._poller,
        ca_runtime=worker._ca_runtime,  # P2-02C: same gate the worker uses
    )
    if not recovery.run():
        logger.critical("복구 실패 — Worker SafeMode로 계속 실행")

    from backend.worker.scheduler import build_scheduler
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("스케줄러 시작")

    worker.run()  # blocking


if __name__ == "__main__":
    main()
