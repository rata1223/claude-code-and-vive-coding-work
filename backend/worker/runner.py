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
import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import redis
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from backend.brokers.kis import KISBroker, get_kis_broker
from backend.brokers.models import Order, OrderStatus
from backend.database.models import (
    Fill as DBFill, Order as DBOrder, Position as DBPosition,
    StrategyRun, init_db_factory,
)
from backend.execution.order_events import apply_terminal_event
from backend.execution.order_machine import FillEvent, OrderStateMachine
from backend.execution.order_poller import OrderFillPoller
from backend.execution.position_tracker import Fill, PositionTracker
from backend.execution.reconciler import PositionReconciler
from backend.worker.heartbeat import WorkerHeartbeat
from backend.worker.portfolio_feed import publish_portfolio
from backend.strategy.base import StrategyBase
from backend.strategy.indicator.strategy import IndicatorStrategy

logger = logging.getLogger(__name__)

#: Whether the equity reading the current thread just handed to the loss
#: tracker is complete (issue #178). ``record_pnl`` calls the MDD breach hook
#: synchronously on the same thread, so the hook reads the flag of the reading
#: that actually caused the breach — fills arrive on several threads at once.
_EQUITY_READING = threading.local()

_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
_DB_URL = os.environ.get("DB_URL", "postgresql://quantdinger:quantdinger@postgres:5432/quantdinger")

_SUBSCRIBE_CHANNELS = ["strategy:start", "strategy:stop", "session:kr_open", "session:us_open"]

#: Total wall-clock the teardown may spend. ``docker stop`` sends SIGTERM and
#: then SIGKILL 10 seconds later (docker-compose sets no ``stop_grace_period``
#: for kis-worker, so that default applies). Every join below draws from this one
#: budget instead of owning an independent timeout, because independent timeouts
#: add up past the grace period and the last steps never run.
_SHUTDOWN_BUDGET_SEC = 8.0

#: Ceiling on step 3 of the teardown. See the call site for why it is capped
#: rather than given whatever the budget has left.
_AUX_JOIN_CAP_SEC = 3.0

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


# ── Which DB row is this broker order? (issue #168) ─────────────────────────
#
# A KIS order number (ODNO → `Order.broker_order_id`) is unique only within one
# trading day, and it restarts every day — so yesterday's 0000117 and today's
# 0000117 are routinely two different orders. Finding "the row" by the number
# alone overwrote old orders, filed fills under them, and left new orders with
# no row at all.
#
# The date cannot tell them apart either (both attempts in PR #165 were rolled
# back): the US session crosses Seoul midnight, so one order legitimately spans
# two trading days, and an order can stay open for longer than any window.
#
# What does tell them apart is whether the order is still open. A number is
# live on at most one order at a time, so an *open* row with that number is
# this order whatever its date, and a *closed* one is some earlier order that
# happened to get the same number. The one step that must reach a row after it
# closed — `_persist_fill`, which runs after the FILLED transition has already
# been written — does not look it up at all: the fill pipeline hands it the
# primary key this worker recorded while the order was open, read before the
# symbol's pending lock is released.
#
# Every match also requires the same symbol and side. An earlier order that was
# never closed (a stuck `unknown` row, say) stays "open" forever, and without
# this it would capture the next order to draw its number. Quantity is not
# compared: KIS can omit `ord_qty`, and gating on it would drop a real fill.

#: Non-terminal statuses. `unknown` is included because the state machine and
#: the poller still treat such an order as live (`OrderStateMachine.active_orders`).
_OPEN_ORDER_STATUSES = (
    OrderStatus.PENDING.value, OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIAL_FILLED.value, OrderStatus.UNKNOWN.value,
)
_TERMINAL_NEGATIVE = (
    OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED,
)


def _same_order(row, order) -> bool:
    return row.symbol == order.symbol and row.side == order.side


def _open_order_row(db, order, broker: str = "kis"):
    """The open row for this broker order, or None.

    Never returns a closed row: a closed row with this number is a different,
    earlier order. More than one open row means an earlier order was never
    closed (see `_step_validate_state`); the newest is the one a live event can
    be about.
    """
    if not order.id:
        return None
    rows = (db.query(DBOrder)
            .filter(DBOrder.broker_order_id == order.id,
                    DBOrder.broker == broker,
                    DBOrder.symbol == order.symbol,
                    DBOrder.side == order.side,
                    DBOrder.status.in_(_OPEN_ORDER_STATUSES))
            .order_by(DBOrder.id.desc())
            .all())
    if len(rows) > 1:
        logger.warning("같은 주문번호의 미종결 행 %d개 (%s) — 최신 행(id=%d) 사용",
                       len(rows), order.id, rows[0].id)
    return rows[0] if rows else None


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


#: Waits between attempts to record a start that never ran. Short: the row is
#: written once, on the start path, and the alert below covers a lasting outage.
_NEVER_RAN_RETRY_DELAYS = (0.5, 1.0)


def _record_never_ran(run_id: int, strategy_type, reason: str) -> bool:
    """Record a run that never ran: ``is_active=False`` and ``stopped_at`` equal
    to its own ``started_at`` (zero duration), unless a stop is already recorded.

    Left active with no ``stopped_at`` the row reads as a run going since
    ``started_at`` and passes the 4-week paper gate
    (``promotion_guard.paper_run_qualifies``) without trading. Not "now": a start
    command can be handled days late (worker down, picked up from ``commands``)
    and now − started_at would count those days.

    The write is retried. If it still fails the row would be restored on the
    next boot and keep aging, and making it ineligible without a write would
    need a schema change (``create_all`` databases, #194) — so a lasting failure
    is a critical log plus an emergency alert naming the manual fix.
    """
    attempts = len(_NEVER_RAN_RETRY_DELAYS) + 1
    last_error = None
    for attempt in range(attempts):
        try:
            with _session() as db:
                run = db.get(StrategyRun, run_id)
                if run is not None and run.stopped_at is None:
                    run.is_active = False
                    run.stopped_at = run.started_at or datetime.utcnow()
                    db.commit()
            last_error = None
            break
        except Exception as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(_NEVER_RAN_RETRY_DELAYS[attempt])

    if last_error is None:
        logger.error("실행으로 치지 않음(0일 기록): run_id=%s type=%s 사유=%s",
                     run_id, strategy_type, reason)
        _audit("strategy_start_failed",
               detail={"run_id": run_id, "strategy_type": strategy_type, "reason": reason})
        return True

    fix = (f"UPDATE strategy_runs SET is_active = false, stopped_at = started_at "
           f"WHERE id = {int(run_id)} AND stopped_at IS NULL;")
    logger.critical("0일 종료를 기록하지 못함 run_id=%s (%d회 시도, 사유: %s): %s — 행이 실행 슬롯을 "
                    "계속 점유하거나, 다음 기동에 복원돼 4주 관문 기간으로 세질 수 있다. 수동 조치: %s",
                    run_id, attempts, reason, last_error, fix)
    _audit("strategy_start_failed_unrecorded",
           detail={"run_id": run_id, "strategy_type": strategy_type, "reason": reason})
    try:
        from bot.notifier import alert_emergency
        alert_emergency(f"[실행 종료 기록 불가] run_id={run_id} ({reason})\n"
                        f"실행 슬롯을 계속 점유하거나 4주 관문 기간으로 세질 수 있다.\n수동 조치: {fix}")
    except Exception as e:  # the alert is best effort; the log above is the record
        logger.warning("종료 기록 경보 전송 실패: %s", e)
    return False


#: Where sessions record run uptime for the 4-week gate (``uptime.py``). Set by
#: :func:`enable_run_uptime` once startup recovery has succeeded; until then (and
#: in tests) sessions record nothing.
_run_uptime_factory = None


#: How often the worker checks for an operator's release (P0-12).
RISK_RESUME_POLL_SEC = 60


def enable_run_uptime(db_factory) -> None:
    global _run_uptime_factory
    _run_uptime_factory = db_factory


def _start_run_uptime(run_id: int):
    """Start recording that ``run_id`` is running, or ``None`` if recording is
    off. Never raises — the record is for the gate, not for trading."""
    factory = _run_uptime_factory
    if factory is None:
        return None
    try:
        from backend.worker.uptime import UptimeRecorder
        recorder = UptimeRecorder(factory, run_id)
        recorder.start()
        return recorder
    except Exception as e:
        logger.warning("[run_id=%d] 가동 기록 시작 실패: %s", run_id, e)
        return None


class WorkerSession:
    """하나의 전략 실행 세션."""

    def __init__(self, run_id: int, strategy: StrategyBase, new_start: bool = False):
        self.run_id = run_id
        self.strategy = strategy
        #: A start command (not a boot restore). If ``strategy.start()`` raises,
        #: such a run never ran and is recorded with zero duration
        #: (``_record_never_ran``); a restore keeps ``_mark_stopped``.
        self._new_start = new_start
        self._strategy_type = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        #: Whether ``_run``'s exit should mark ``strategy_runs.is_active = False``.
        #: See ``stop()``.
        self._deactivate_on_exit = True
        #: ``on_market_open`` calls in flight. They run on their own threads, so
        #: the session thread ending does not mean the strategy has stopped
        #: acting; ``_run`` waits for this to reach 0 before it releases the
        #: slot (``stopped_at``). Checked together with ``_stop_event`` under the
        #: condition, so none can start once the drain has begun.
        self._callbacks = 0
        self._callbacks_cv = threading.Condition()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"strategy-{self.run_id}")
        self._thread.start()
        logger.info("Worker 세션 시작: run_id=%d", self.run_id)

    def stop(self, *, deactivate: bool = True):
        """Ask the strategy thread to end.

        ``deactivate=False`` is the **process shutdown** path. ``_run``'s
        ``finally`` normally flips ``strategy_runs.is_active`` to ``False``, and
        ``StrategyWorker._restore_active()`` only restores rows where that is
        ``True`` — so tearing sessions down the ordinary way on the way out would
        silently switch every running strategy off on every deploy, and nobody
        would find out until a bar that never traded.

        The default stays ``True`` because an operator's ``strategy:stop`` *does*
        mean "leave it off".

        **This returns immediately.** ``strategy.stop()`` runs in ``_run``'s
        cleanup, on the session's own thread, not here — see there for why.
        """
        self._deactivate_on_exit = deactivate
        self._stop_event.set()
        logger.info("Worker 세션 중단 요청: run_id=%d (deactivate=%s)",
                    self.run_id, deactivate)

    def join(self, timeout: float) -> bool:
        """Wait for the strategy thread. ``True`` if it ended within ``timeout``."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def is_alive(self) -> bool:
        """Whether the session thread is still running (its exit writes the
        run's end, see ``_run``)."""
        return self._thread is not None and self._thread.is_alive()

    def trigger_market_open(self, market: str):
        """Scheduler calls this when a market session opens."""
        with self._callbacks_cv:
            if self._stop_event.is_set():
                logger.info("[run_id=%d] 세션 중단 — on_market_open 스킵 (market=%s)", self.run_id, market)
                return
            self._callbacks += 1
        try:
            self.strategy.on_market_open()
            logger.info("[run_id=%d] on_market_open 호출 완료 (market=%s)", self.run_id, market)
        except Exception as e:
            logger.exception("on_market_open 오류 run_id=%d: %s", self.run_id, e)
        finally:
            with self._callbacks_cv:
                self._callbacks -= 1
                self._callbacks_cv.notify_all()

    def _drain_callbacks(self):
        """Wait for in-flight ``on_market_open`` calls. No timeout: until they
        return the old run can still be acting on the account, and releasing the
        slot would let a new run start beside it. A hung one keeps the slot
        occupied (fail-closed) and is logged."""
        with self._callbacks_cv:
            while self._callbacks:
                logger.warning("[run_id=%d] 진행 중인 on_market_open %d건 대기 — 끝나야 슬롯을 푼다",
                               self.run_id, self._callbacks)
                self._callbacks_cv.wait(30)

    def _run(self):
        started = False
        uptime = None
        try:
            self.strategy.start()
            started = True
            # The 4-week gate counts the time this run was running — from here,
            # not from a restore that never got this far.
            uptime = _start_run_uptime(self.run_id)
            #: Recording off at the start — the worker came up halted. A release
            #: turns it on later (P0-12), and this run then counts from there.
            recording_off = _run_uptime_factory is None
            while not self._stop_event.is_set():
                if recording_off and _run_uptime_factory is not None:
                    recording_off = False
                    uptime = _start_run_uptime(self.run_id)
                time.sleep(1)
        except Exception as e:
            logger.exception("전략 실행 오류 run_id=%d: %s", self.run_id, e)
        finally:
            # No new on_market_open from here on (also when start() raised and
            # nobody called stop()).
            with self._callbacks_cv:
                self._stop_event.set()
            # The run stops counting as running now. Bounded: a stalled DB must
            # not hold up the cleanup below.
            if uptime is not None:
                uptime.stop()
            # Order matters. ``is_active = False`` is the durable record that the
            # operator switched this strategy off, and ``_restore_active()``
            # reads it on the next boot — it must not be held hostage by user
            # cleanup that may never return, so it is written first.
            # ``stopped_at`` is different: kis-api counts a run without it as
            # holding the single strategy slot, so it is written last — once no
            # on_market_open is still in flight and ``strategy.stop()`` has
            # returned. A hung callback or stop hook keeps the slot (fail-closed).
            if self._deactivate_on_exit:
                self._mark_stopped(release_slot=False)
                self._drain_callbacks()

            # ``StrategyBase.stop()`` calls the overridable ``on_stop()``;
            # ``ScriptStrategy`` runs a sandboxed *user script* there. Running it
            # here rather than in ``stop()`` puts it on this thread, inside the
            # deadline ``StrategyWorker._shutdown_strategies()`` applies with
            # ``join()`` — and keeps it off the pub/sub loop thread, which is
            # what ``_handle_stop()`` calls ``stop()`` from.
            try:
                self.strategy.stop()
            except Exception as e:
                logger.exception("전략 on_stop 오류 run_id=%d: %s", self.run_id, e)

            if self._deactivate_on_exit:
                if not started and self._new_start:
                    _record_never_ran(self.run_id, self._strategy_type,
                                      "strategy.start() 실패")
                else:
                    self._mark_stopped()

    def _mark_stopped(self, release_slot: bool = True):
        """``is_active = False`` (not restored on the next boot); with
        ``release_slot`` also ``stopped_at`` (frees kis-api's strategy slot)."""
        try:
            with _session() as db:
                run = db.get(StrategyRun, self.run_id)
                if run:
                    run.is_active = False
                    if release_slot:
                        run.stopped_at = datetime.utcnow()
                    db.commit()
        except Exception as e:
            logger.warning("run 상태 업데이트 실패: %s", e)


class StrategyWorker:
    """Redis Pub/Sub 구독 + 전략 세션 관리."""

    def __init__(self):
        self._redis = redis.from_url(_REDIS_URL)
        self._sessions: dict[int, WorkerSession] = {}
        #: Sessions a stop has removed from ``_sessions`` whose thread has not
        #: ended yet (it writes the run's end, see ``WorkerSession._run``). A
        #: repeated stop for one of them must not record the run as ended.
        self._stopping: dict[int, WorkerSession] = {}
        self._lock = threading.Lock()
        self._last_market_open: dict[str, float] = {}  # market → monotonic ts; dedup gate

        # ── graceful shutdown (P0-10) ────────────────────────────────────────
        self._shutdown = threading.Event()
        self._shutdown_done = False
        #: monotonic timestamp of the signal, not of shutdown() being entered.
        #: The 10s SIGKILL clock starts at the signal, so the budget must too.
        self._shutdown_at: float | None = None
        self._scheduler = None          # set by main() via attach_scheduler()
        #: Set by main() once startup recovery succeeded, or failed only on a
        #: restored risk halt. Until then the resume poll never opens the gate:
        #: a worker whose recovery failed keeps a later risk halt's cause too.
        self._risk_resume_allowed = False
        #: One-shot threads spawned per market open / periodic reconcile. They do
        #: broker I/O and DB writes, so they are tracked in order to be joined on
        #: the way out rather than SIGKILLed mid-flight.
        self._aux_threads: list[threading.Thread] = []
        #: Publish positions/equity for the operator screen after fills (see
        #: ``_publish_portfolio_soon``).
        self._portfolio_feed = True
        #: broker order number → DB primary key, for orders this process has seen
        #: open. See `_open_order_row` for why the number alone is not enough.
        self._order_row_ids: dict[str, int] = {}
        self._order_row_lock = threading.Lock()

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
        #: ``(amount, equity_verified)`` of that reading, as **one** value: fills
        #: run on several threads, and two attributes could pair a new amount
        #: with the previous reading's flag (#178). A tuple is swapped whole.
        self._last_equity_reading: tuple[float | None, bool] = (None, True)
        try:
            from backend.quant.risk.engine import PersistentLossTracker, RiskConfig
            self._loss_tracker = PersistentLossTracker(
                config=RiskConfig(),
                redis_client=self._redis,
                db_factory=_get_session_factory(),  # per-op sessions, not long-lived
            )
            # P0-03: an MDD breach liquidates. Wired after construction, so the
            # halt restored from the DB in __init__ cannot fire it.
            self._loss_tracker.on_mdd_breach = self._on_mdd_breach
            logger.info("PersistentLossTracker 초기화 완료 (kill_switch=%s)", self._loss_tracker.kill_switch)

            # Seed equity from a live balance fetch at startup. This always sets
            # _last_known_equity (so the first fill's MDD check has a baseline even
            # if the balance API is briefly down later) and bootstraps peak_equity
            # on a cold start (peak_equity == 0).
            try:
                _bal = get_kis_broker().get_balance()
                _eq = _bal.total_eval_krw
                if _eq > 0:
                    self._last_equity_reading = (_eq, getattr(_bal, "equity_verified", True))
                    # A release applied at boot rebases MDD on this reading.
                    self._loss_tracker.seed_equity(_eq)
                    if self._loss_tracker.peak_equity == 0:
                        self._loss_tracker.peak_equity = _eq
                        self._loss_tracker._persist()
                        logger.info("peak_equity 초기화: %.0f원", _eq)
            except Exception as _e:
                logger.warning("기준 잔고 시드 실패 — 첫 체결 MDD 평가가 스킵될 수 있음: %s", _e)
            # A halt restored from an older row goes on today's row now, before
            # the operator can release it: written later, at the first fill or
            # resume poll, the carry would overwrite that release (P0-12).
            self._loss_tracker.write_pending()

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
        try:
            # A SIGTERM during startup used to be deferred until the whole boot
            # sequence finished — restore + a broker-backed reconcile, which can
            # run for seconds — and the teardown then began against a clock that
            # was already most of the way to SIGKILL. Each step is a checkpoint.
            if self._shutdown.is_set():
                logger.info("기동 중 shutdown 요청 — 전략 복원 생략")
                return
            self._restore_active()

            if self._shutdown.is_set():
                logger.info("기동 중 shutdown 요청 — 시작 조정 생략")
                return
            # Startup reconciliation: broker is ground truth on boot
            if not self._startup_reconcile():
                logger.info("기동 중 shutdown 요청 — 시작 조정 중단")
                return

            if self._shutdown.is_set():
                return
            self._run_with_pubsub()
        finally:
            # Reached on a SIGTERM, on a clean loop exit, and on the way out of an
            # exception. It is the only place that guarantees the checkpoint and
            # the shutdown record happen at all.
            self.shutdown()

    def _startup_reconcile(self) -> bool:
        """Boot-time reconcile. Returns False only when shutdown cut it short.

        Bounded and abortable (issue #170): one ``get_order_status`` per open
        order, at up to ~32s each, used to hold a SIGTERM here past Docker's
        SIGKILL. It runs on its own reconciler with a gated broker rather than
        on ``self._reconciler``, so when this gives up the abandoned run stops
        at its next broker read instead of carrying on into the teardown — see
        ``StopGatedBroker``. Any other failure is logged and boot continues, as
        before.
        """
        from backend.worker.recovery import (
            _RECONCILE_STARTUP_TIMEOUT, RecoveryAborted, run_reconcile_bounded,
        )
        try:
            run_reconcile_bounded(
                lambda gate: PositionReconciler(
                    broker=gate(get_kis_broker()),
                    db_factory=_get_session_factory(),
                    redis_client=self._redis,
                    poller=self._poller,
                    ca_runtime=self._ca_runtime,
                ),
                "startup", _RECONCILE_STARTUP_TIMEOUT,
                should_abort=self._shutdown.is_set,
            )
        except RecoveryAborted:
            return False
        except Exception as e:
            logger.warning("시작 조정 실패 (계속 진행): %s", e)
        return True

    # ── graceful shutdown (P0-10) ────────────────────────────────────────────

    @property
    def shutdown_requested(self) -> bool:
        """Whether a stop has been asked for. Read by the loops and by ``main()``."""
        return self._shutdown.is_set()

    def request_shutdown(self) -> None:
        """Raise the shutdown flag. Safe to call from a signal handler.

        A handler runs in the main thread between two bytecodes, which may be
        anywhere — inside ``self._lock``, inside a SQLAlchemy commit, inside the
        poller's registration path. Running the teardown from there would try to
        take locks the interrupted frame already holds. So the handler sets this
        and returns; the loops poll it and call ``shutdown()`` on their way out.
        """
        if self._shutdown_at is None:
            self._shutdown_at = time.monotonic()
        self._shutdown.set()

    def attach_scheduler(self, scheduler) -> None:
        """Hand the APScheduler instance over so the teardown can stop it first,
        and add the jobs that need this worker."""
        self._scheduler = scheduler
        scheduler.add_job(self._resume_if_released, "interval",
                          seconds=RISK_RESUME_POLL_SEC, id="risk_resume",
                          coalesce=True, max_instances=1, replace_existing=True)

    def allow_risk_resume(self, allowed: bool) -> None:
        self._risk_resume_allowed = bool(allowed)

    def _resume_if_released(self) -> bool:
        """Reopen trading once the operator has released a risk halt (P0-12).

        A release (``POST /api/risk/kill-switch/reset``) clears the rows, but
        the order gate is this process's ``SAFE_MODE``, and adopting the clear
        on a write deliberately leaves it shut. Trading stayed closed until a
        restart. This polls instead, and reopens only when all of these hold:

        * recovery succeeded, or failed only on a restored risk halt
          (``allow_risk_resume``);
        * the gate is shut for a risk limit (``RISK_BREACH``) — untrusted state
          still needs a restart;
        * no row is halted, whatever its date — a failed read is no;
        * the tracker, settled with the row now, is not halted. That write
          adopts the release and sets its baseline *before* trading reopens.

        The check and the reopening run under the tracker's lock, so a halt a
        fill decides meanwhile is not reopened over. Returns True if it
        reopened. Never raises — a failed poll leaves the gate shut.
        """
        try:
            from backend.database.models import DailyRiskState
            from backend.risk.halt_policy import HaltCause
            from backend.worker.recovery import SAFE_MODE
            tracker = self._loss_tracker
            if (not self._risk_resume_allowed or tracker is None
                    or self.shutdown_requested
                    or SAFE_MODE.halt_cause is not HaltCause.RISK_BREACH):
                return False
            sess = _get_session_factory()()
            try:
                # Any halted row is a live halt (#216) — one query answers it.
                if (sess.query(DailyRiskState.trade_date)
                        .filter(DailyRiskState.kill_switch.is_(True)).first()
                        is not None):
                    return False
            finally:
                sess.close()
            if tracker.refresh_from_db(self._last_known_equity):
                return False

            def _reopen():
                # Re-read: something else may have shut it since, for a reason
                # this poll must not override.
                if SAFE_MODE.halt_cause is HaltCause.RISK_BREACH:
                    SAFE_MODE.enable()
            if not tracker.if_clear(_reopen) or not SAFE_MODE.can_trade:
                return False
        except Exception as e:
            logger.warning("킬스위치 해제 확인 실패 — 매매 차단 유지: %s", e)
            return False

        logger.warning("킬스위치 해제 확인 — 매매 재개 (재시작 없이)")
        if _run_uptime_factory is None:
            # Recovery stopped at the restored halt; it had passed everything
            # else, so from here the run's time counts as for any running worker.
            # Sessions already running pick it up (``WorkerSession._run``).
            enable_run_uptime(_get_session_factory())
        try:
            from bot.notifier import alert_emergency
            alert_emergency("킬스위치 해제 확인 — 매매 재개\n"
                            "일·주 한도는 해제 시점보다 1% 더 잃으면 다시 정지, "
                            "MDD는 해제 시점 자산 기준")
        except Exception as e:
            logger.warning("매매 재개 알림 실패: %s", e)
        return True

    def shutdown(self) -> None:
        """Stop everything this process owns, in order, inside the grace period.

        Every step is best-effort and isolated: one wedged component must not
        cost the others their cleanup, and the audit record is written whatever
        happened, because "was this a clean stop or a crash?" is the question the
        next boot needs answered.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._shutdown.set()

        # From the signal, not from here: SIGKILL is 10s after the signal, and
        # anything the boot sequence spent before reaching this point is time the
        # teardown no longer has. When the flag was raised long ago every step
        # gets a zero timeout, which is the correct answer — do what can be done
        # without blocking, and still write the record.
        t0 = self._shutdown_at if self._shutdown_at is not None else time.monotonic()

        def _left(reserve: float = 0.0) -> float:
            """Budget still unspent, minus what later steps are reserved."""
            return max(0.0, _SHUTDOWN_BUDGET_SEC - (time.monotonic() - t0) - reserve)

        done: list[str] = []
        failed: list[str] = []

        def _step(name: str, fn) -> None:
            """Run one teardown step; record it and carry on if it raises."""
            try:
                fn()
                done.append(name)
            except Exception as e:
                logger.warning("종료 단계 실패 (%s): %s", name, e)
                failed.append(name)

        logger.info("graceful shutdown 시작 (예산 %.0fs)", _SHUTDOWN_BUDGET_SEC)

        # 1. Scheduler first — a job that fires mid-teardown starts work on the
        #    components the steps below are about to take away.
        _step("scheduler", self._shutdown_scheduler)
        # 2. Strategies. deactivate=False: see WorkerSession.stop().
        _step("strategies", lambda: self._shutdown_strategies(_left(reserve=3.0)))
        # 3. The one-shot threads — market-open broadcasts and periodic
        #    reconciles, which do broker I/O and DB writes. Capped separately:
        #    ``IndicatorStrategy._scan_and_trade`` never checks ``is_running()``,
        #    so a scan already in flight keeps submitting orders and stopping its
        #    strategy in step 2 does not end it. Waiting it out would spend the
        #    poller's budget too. Orders it did submit are persisted and
        #    re-registered with the poller by ``StartupRecovery`` on the next boot
        #    (backend/worker/recovery.py:333), so abandoning the thread costs
        #    tracking until restart, not the order.
        seen: set = set()

        def _join_first() -> None:
            seen.update(self._join_aux_threads(min(_AUX_JOIN_CAP_SEC, _left(reserve=2.0))))

        _step("aux-threads", _join_first)
        # 4. Poller — the "drain active polls" half of P0-10.
        _step("poller", lambda: self._shutdown_poller(_left(reserve=1.0)))
        # 4b. Threads the drain started. A fill it delivers can breach MDD, and
        #     the flatten that triggers runs on a new aux thread (P0-03) that the
        #     pass above never saw — a daemon, so the process would exit under
        #     it. Only new threads: ones already waited on in step 3 are not
        #     waited on twice.
        _step("aux-threads-late",
              lambda: self._join_aux_threads(
                  min(_AUX_JOIN_CAP_SEC, _left(reserve=1.0)), skip=frozenset(seen)))
        # 5. Checkpoint equity to the DB.
        _step("equity-checkpoint", self._checkpoint_equity)
        # 6. Heartbeat LAST, so the API-side watchdog does not see a dead worker
        #    while the teardown is still running.
        _step("heartbeat", self._shutdown_heartbeat)

        elapsed = time.monotonic() - t0
        logger.info("graceful shutdown 완료 (%.1fs) — 완료=%s 실패=%s",
                    elapsed, done, failed or "없음")
        _audit("worker_shutdown", actor="worker", detail={
            "steps_ok": done,
            "steps_failed": failed,
            "elapsed_sec": round(elapsed, 2),
            "within_budget": elapsed <= _SHUTDOWN_BUDGET_SEC,
        })

    def _shutdown_scheduler(self) -> None:
        """Stop dispatching scheduled jobs. First, so nothing new starts."""
        if self._scheduler is None:
            return
        # wait=False: a job already running keeps its thread, but no new ones are
        # dispatched. Waiting could spend the whole grace period inside a job that
        # is polling a market.
        self._scheduler.shutdown(wait=False)

    def _shutdown_strategies(self, budget: float) -> None:
        """Ask every session to end, then wait for them within one deadline.

        The deadline is taken **before** the stop loop. ``stop()`` only sets an
        event now, so the loop is cheap — but taking the deadline first means a
        change that makes it expensive again cannot silently spend the budget
        the later teardown steps need.
        """
        deadline = time.monotonic() + budget
        with self._lock:
            sessions = [s for s in self._sessions.values() if s is not None]
            # Already stopping (an operator stop): joined, but not stopped again —
            # deactivate=False would undo that stop.
            stopping = list(getattr(self, "_stopping", {}).values())
        for session in sessions:
            try:
                session.stop(deactivate=False)
            except Exception as e:
                logger.warning("전략 중단 실패 run_id=%s: %s", session.run_id, e)
        stuck = [s.run_id for s in sessions + stopping
                 if not s.join(max(0.0, deadline - time.monotonic()))]
        if stuck:
            logger.warning("전략 스레드 미종료 run_id=%s — 데몬이라 프로세스와 함께 끝난다",
                           stuck)

    def _join_aux_threads(self, budget: float, skip=frozenset()) -> set:
        """Wait, briefly, for tracked one-shot threads to finish their I/O.

        Returns every thread it looked at, so a later pass can ``skip`` them:
        ``shutdown()`` joins once before the poller drain and again after it,
        and the second pass is only for threads the drain itself started.
        """
        with self._lock:
            threads = [t for t in self._aux_threads if t.is_alive() and t not in skip]
        deadline = time.monotonic() + budget
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        stuck = [t.name for t in threads if t.is_alive()]
        if stuck:
            logger.warning("보조 스레드 미종료: %s", stuck)
        return set(skip) | set(threads)

    def _shutdown_poller(self, budget: float) -> None:
        """Stop the fill poller and wait for its current cycle — the drain."""
        if self._poller is None:
            return
        self._poller.stop()
        # stop() only sets the poller's event, and its thread is a daemon — so
        # without this join the interpreter can tear it down between reading a
        # fill from the broker and writing it. Reaching for the private handle is
        # deliberate: widening backend/execution's API is not this change's job.
        thread = getattr(self._poller, "_thread", None)
        if thread is None:
            return
        thread.join(budget)
        if thread.is_alive():
            logger.warning("OrderFillPoller 스레드가 %.1fs 안에 끝나지 않음", budget)

    def _checkpoint_equity(self) -> None:
        """Write daily/weekly PnL and peak equity. **Never** the halt flag.

        Deliberately narrow: only the three equity columns, never the halt flag.
        ``_persist()`` would do more than this step needs — a Redis write, and a
        pass over ``kill_switch`` — and a shutdown checkpoint has no business
        deciding a halt either way.

        (This used to be load-bearing: ``_write_db`` overwrote the flag from
        memory, so calling ``_persist()`` here could erase a halt the API-side
        watchdog had set while this worker was alive believing nothing was wrong.
        Issue #158 fixed that at the source — the tracker now re-reads the row —
        so this is a matter of scope rather than safety.)

        ``record_pnl()`` persists these same columns on every call, so this is a
        retry for a write that failed earlier rather than the only copy.
        """
        tracker = self._loss_tracker
        if tracker is None:
            return
        from backend.database.models import lock_risk_row, trading_day

        # The tracker's own mutex (P0-05) — read the four values consistently.
        # Resolved once and rolled to first: between 07:00 and the day's first
        # fill the tracker still holds the closed day, and writing that onto the
        # new day's row made a restart count it twice (#166).
        day = trading_day()
        with tracker._lock:
            tracker.roll_over(day)
            daily_pnl = tracker.daily_pnl
            weekly_pnl = tracker.weekly_pnl
            peak_equity = tracker.peak_equity
            halted = tracker.kill_switch
            halt_reason = tracker.kill_reason or None

        with _session() as db:
            # Locked like every other writer of this row (#164).
            row, is_new = lock_risk_row(db, day)
            row.daily_pnl = daily_pnl
            row.weekly_pnl = weekly_pnl
            row.peak_equity = peak_equity
            if is_new and halted:
                # Only on a row this call creates, and only to *set* the flag.
                #
                # "Never writes the halt flag" is about not clobbering an external
                # one, and on a brand-new row there is nothing external to clobber
                # — while `kill_switch` would otherwise default to False, which is
                # writing a clear by omission. Shutting down at 07:30 KST while
                # halted would leave the new day's row reading "not halted".
                #
                # Same reasoning as `_write_db`'s `is_new` path (issue #158).
                row.kill_switch = True
                row.kill_reason = halt_reason
            db.commit()

    def _shutdown_heartbeat(self) -> None:
        """Stop publishing the liveness beat. Last, and the key is left alone."""
        self._heartbeat.stop()
        # Deliberately NOT deleting `worker:heartbeat`. WorkerWatchdog runs in the
        # API process and sets DailyRiskState.kill_switch the moment the key is
        # missing (backend/worker/heartbeat.py:_alert_dead_worker). Tidying it up
        # here would halt trading on every deploy; letting the 90s TTL lapse is
        # what gives a restart room to come back unnoticed.

    @property
    def _last_known_equity(self) -> float | None:
        """The last good equity amount — the half of ``_last_equity_reading``
        most callers want."""
        return getattr(self, "_last_equity_reading", (None, True))[0]

    @_last_known_equity.setter
    def _last_known_equity(self, value: float | None) -> None:
        self._last_equity_reading = (value, True)

    def _on_mdd_breach(self, reason: str) -> None:
        """The loss tracker measured an MDD breach: liquidate (P0-03).

        Called under the tracker's lock from a fill thread, so the flatten —
        quotes and orders for every position — runs on a tracked thread that
        ``shutdown()`` waits for. The tracker has already closed ``SAFE_MODE``
        to new entries; the flatten sells straight to the broker, which is the
        one path a halt does not block.
        """
        breach_verified = getattr(_EQUITY_READING, "verified", True)
        self._spawn_aux(self._emergency_flatten, args=(reason, breach_verified),
                        name="emergency-flatten")

    def _emergency_flatten(self, reason: str, breach_verified: bool = True) -> None:
        """Run the flatten; if it sent nothing and failed, let the next fill retry.

        The tracker requests one flatten per breach. Without a retry, a broker
        that was down at that moment left the book invested through the whole
        drawdown. The retry is deliberately narrow: only when **no order went
        out**. Once any sell is resting, a second run would ask for the same
        shares again — the unknown-sellable fallback sells the held quantity —
        so a partial result is left to the alert and the operator.
        """
        from backend.worker.emergency import EmergencyFlattenManager, auto_flatten_dry_run
        dry_run = auto_flatten_dry_run()
        held = not dry_run and not self._equity_verified_for_flatten(breach_verified)
        if held:
            # Nothing is sold, so the next fill — with a fresh reading — may ask
            # again. Without this the held breach could never flatten at all.
            dry_run = True
            tracker = self._loss_tracker
            if tracker is not None:
                with tracker._lock:
                    tracker._rearm_mdd_flatten()
        retry = False
        try:
            mgr = EmergencyFlattenManager(
                get_kis_broker(),
                db_factory=_get_session_factory(),
                dry_run=dry_run,
            )
            mgr.flatten_all(reason=f"MDD 킬스위치: {reason}")
            retry = (mgr.last_status is None and mgr.last_submitted == 0
                     and mgr.last_failed_count > 0)
        except Exception as e:  # noqa: BLE001 - logged; the halt already stands
            logger.error("MDD 비상청산 실패: %s", e)
            retry = True
        if dry_run or not retry:
            return   # a dry run sells nothing either way — nothing to retry
        # Loud, because the retry may never come: it rides on the next fill, and
        # with entries halted and no sell resting there may be none. The early
        # failure paths inside flatten_all() send no alert of their own.
        msg = ("MDD 비상청산 실패 — 주문 0건. 포지션이 남아 있다. "
               "수동 청산 필요(/api/admin/flatten). 다음 체결 시 자동 재시도")
        logger.critical(msg)
        try:
            from bot.notifier import alert_emergency
            alert_emergency(msg)
        except Exception:
            pass
        tracker = self._loss_tracker
        if tracker is not None:
            with tracker._lock:
                tracker._rearm_mdd_flatten()

    def _equity_verified_for_flatten(self, breach_verified: bool = True) -> bool:
        """Whether the equity behind this breach can be trusted enough to sell.

        The MDD that asked for the flatten is computed from
        ``get_balance().total_eval_krw``, which can read low (issue #178): a
        missing field reads as 0, and USD cash is left out. A low reading looks
        like a drawdown. The halt stands either way — entries stay closed — but
        liquidating the book on a number the broker adapter itself flags as
        incomplete is the one step this refuses, and says so.

        Both readings must pass: the one that **caused** the breach
        (``breach_verified`` — possibly the cached last-known value), and a
        fresh one now. A fresh call that *fails* counts as unverified: selling
        is the one thing this gate exists to refuse without both readings. The
        hold re-arms, so the next fill tries again.
        """
        verified = breach_verified
        if verified:
            try:
                bal = get_kis_broker().get_balance()
                verified = getattr(bal, "equity_verified", True)
            except Exception as e:  # noqa: BLE001 - see docstring
                logger.warning("비상청산 전 잔고 확인 실패 — 청산 보류: %s", e)
                verified = False
        if verified:
            return True
        msg = ("MDD 비상청산 보류 — 총자산 판독이 불완전하다(#178). 매매 정지는 유지된다. "
               "포지션·잔고를 직접 확인하고 필요하면 수동 청산(/api/admin/flatten)")
        logger.critical(msg)
        try:
            from bot.notifier import alert_emergency
            alert_emergency(msg)
        except Exception:
            pass
        return False

    def _spawn_aux(self, target, args=(), name=None) -> threading.Thread:
        """Start a tracked one-shot thread so ``shutdown()`` can wait for it."""
        t = threading.Thread(target=target, args=args, daemon=True, name=name)
        with self._lock:
            self._aux_threads = [x for x in self._aux_threads if x.is_alive()]
            self._aux_threads.append(t)
        t.start()
        return t

    def _run_with_pubsub(self):
        backoff = 2.0
        while not self._shutdown.is_set():
            pubsub = None
            try:
                pubsub = self._redis.pubsub()
                pubsub.subscribe(*_SUBSCRIBE_CHANNELS)
                logger.info("Worker 대기 중 (Redis Pub/Sub: %s)...", _SUBSCRIBE_CHANNELS)
                backoff = 2.0

                # get_message(timeout=) rather than listen(). listen() blocks in a
                # socket read, and PEP 475 resumes that read once a signal handler
                # returns — so a flag raised by SIGTERM would never be looked at
                # and the process would sit there until SIGKILL. A 1s poll bounds
                # how long shutdown waits for this loop to notice.
                while not self._shutdown.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if message is None or message["type"] != "message":
                        continue
                    channel = message["channel"]
                    if isinstance(channel, bytes):
                        channel = channel.decode()
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
                if self._shutdown.is_set():
                    break
                logger.error("Redis 연결 끊김: %s — %.1fs 후 재연결", e, backoff)
                self._shutdown.wait(backoff)
                backoff = min(backoff * 2, 64.0)
                self._enter_db_polling_mode()
            except Exception as e:
                if self._shutdown.is_set():
                    break
                logger.exception("Worker 예외: %s — %.1fs 후 재시작", e, backoff)
                self._shutdown.wait(backoff)
                backoff = min(backoff * 2, 64.0)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass

        logger.info("Pub/Sub 루프 종료 (shutdown 요청)")

    def _enter_db_polling_mode(self):
        """Redis 불가 시 DB commands 테이블을 30초마다 폴링."""
        from backend.database.models import Command
        logger.warning("DB 폴링 모드 전환 (Redis 불가)")
        _audit("redis_failover", actor="worker", detail={"mode": "db_polling"})
        # Same shutdown check as the pub/sub loop: if only that one learned to
        # exit, a SIGTERM during a Redis outage still hangs until SIGKILL.
        while not self._shutdown.is_set():
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

            self._shutdown.wait(30)

    # ── 이벤트 핸들러 ─────────────────────────────────────────────────────
    def _handle_start(self, data: dict, restoring: bool = False):
        run_id = data["run_id"]
        with self._lock:
            if run_id in self._sessions:
                logger.warning("이미 실행 중: run_id=%d", run_id)
                return
            # Reserve slot under lock to prevent a concurrent duplicate start
            self._sessions[run_id] = None

        if not self._run_still_wanted(run_id):
            with self._lock:
                self._sessions.pop(run_id, None)
            return

        if not self._env_matches(data, restoring):
            with self._lock:
                self._sessions.pop(run_id, None)
            return
        self._stamp_order_mode(run_id)

        strategy = self._build_strategy(data)
        if strategy is None:
            with self._lock:
                self._sessions.pop(run_id, None)  # release reservation
            if not restoring:
                self._mark_start_failed(run_id, data.get("strategy_type"))
            return

        session = WorkerSession(run_id, strategy, new_start=not restoring)
        session._strategy_type = data.get("strategy_type")
        with self._lock:
            # A stop handled while the strategy was being built popped the
            # reservation (``_handle_stop``) and recorded the run as stopped;
            # starting it now would trade under a row that says it is off.
            if run_id not in self._sessions:
                logger.warning("시작 취소 — 생성 중에 중지 요청: run_id=%s", run_id)
                return
            self._sessions[run_id] = session
        session.start()
        _audit("strategy_start", detail={"run_id": run_id, "strategy_type": data.get("strategy_type")})

    def _run_still_wanted(self, run_id) -> bool:
        """Start only a run whose row is still active with no recorded stop.

        A start command can arrive twice: Redis delivers it, and its
        ``commands`` row stays ``pending`` until the DB-polling fallback
        replays it. By then the run may have been stopped by the operator or
        recorded as never having run (``_record_never_ran``); starting it again
        would trade under a row that says it is off — and the next boot would
        not restore it. Unreadable state is treated as "do not start".
        """
        try:
            with _session() as db:
                run = db.get(StrategyRun, run_id)
                if run is None:
                    logger.warning("시작 요청 무시 — 실행 행 없음: run_id=%s", run_id)
                    return False
                if not run.is_active or run.stopped_at is not None:
                    logger.warning("시작 요청 무시 — 이미 중지된 실행: run_id=%s", run_id)
                    return False
                return True
        except Exception as e:
            logger.warning("시작 요청 보류 — 실행 상태를 읽지 못함 run_id=%s: %s", run_id, e)
            return False

    def _env_matches(self, data: dict, restoring: bool) -> bool:
        """Run only a row stamped for this worker's ``KIS_ENV``.

        kis-api stamps each run with the environment it was started in
        (``config["kis_env"]``). A row stamped for another environment is not run
        here: its days and fills belong to that environment, and the 4-week gate
        counts only paper runs.

        - New start: recorded as never having run (zero days).
        - Restore (``KIS_ENV`` changed and the worker restarted): ended as of now —
          it did run, in its own environment, until this boot. A run under the
          new environment is started explicitly.
        - No stamp (a row from before stamping): runs as before, with a warning;
          the gate treats its environment as unknown and never counts it.
        """
        from backend.worker.promotion_guard import current_kis_env, run_kis_env
        run_id = data["run_id"]
        stamped = run_kis_env(SimpleNamespace(config=data.get("config")))
        here = current_kis_env()
        if stamped is None:
            logger.warning("환경 기록 없는 실행 — 그대로 실행하지만 4주 관문에 세지 않는다: run_id=%s",
                           run_id)
            return True
        if stamped == here:
            return True
        reason = f"환경 불일치(실행={stamped}, 워커={here})"
        if restoring:
            logger.warning("복원하지 않음 — %s, 지금 시각으로 종료 기록: run_id=%s", reason, run_id)
            try:
                with _session() as db:
                    run = db.get(StrategyRun, run_id)
                    if run is not None and run.stopped_at is None:
                        run.is_active = False
                        run.stopped_at = datetime.utcnow()
                        db.commit()
            except Exception as e:
                logger.error("환경 불일치 실행의 종료 기록 실패 run_id=%s: %s — 다음 기동에 다시 시도",
                             run_id, e)
            _audit("strategy_env_mismatch",
                   detail={"run_id": run_id, "stamped": stamped, "worker": here, "restore": True})
        else:
            _record_never_ran(run_id, data.get("strategy_type"), reason)
        return False

    def _stamp_order_mode(self, run_id) -> None:
        """Record on the run whether this worker submits orders
        (``ENABLE_LIVE_TRADING``), as of this start or restore. A shadow run
        never fills; the operator screen says so instead of only "no fills".
        Best effort: the gate looks at fills, not at this."""
        from backend.worker.promotion_guard import RUN_ORDERS_KEY, orders_enabled
        try:
            with _session() as db:
                run = db.get(StrategyRun, run_id)
                if run is None:
                    return
                try:
                    cfg = json.loads(run.config or "{}")
                except (TypeError, ValueError):
                    cfg = {}
                if not isinstance(cfg, dict):
                    cfg = {}
                cfg[RUN_ORDERS_KEY] = orders_enabled()
                run.config = json.dumps(cfg)
                db.commit()
        except Exception as e:
            logger.warning("주문 제출 여부 기록 실패 run_id=%s: %s", run_id, e)

    def _mark_start_failed(self, run_id: int, strategy_type):
        """The strategy for a start command could not be built, so it never ran.

        Only for new starts. A restore at boot that fails (e.g. the broker is
        briefly unreachable) keeps the row active so the next boot retries it,
        rather than ending a running strategy over a transient outage.
        """
        _record_never_ran(run_id, strategy_type, "전략 생성 실패")

    def _handle_stop(self, data: dict):
        run_id = data["run_id"]
        with self._lock:
            # getattr: workers built with ``__new__`` (the test suites) have none yet.
            self._stopping = {k: s for k, s in getattr(self, "_stopping", {}).items()
                              if s.is_alive()}
            session = self._sessions.pop(run_id, None)
            still_stopping = self._stopping.get(run_id) if session is None else None
            if session is not None:
                self._stopping[run_id] = session
        if session:
            session.stop()
            _audit("strategy_stop", detail={"run_id": run_id})
        elif still_stopping is not None:
            # Stopped already and still ending: its thread writes the end once
            # in-flight callbacks are done. Recording it here would free the slot
            # while the old run may still be acting on the account.
            logger.warning("이미 중단 중인 세션 — 종료되면 기록된다: run_id=%s", run_id)
            still_stopping.stop()
        else:
            # Nothing runs here for this run (stopped before it was built, or the
            # stop is replayed after a boot that did not restore the inactive
            # row), so no session will ever write ``stopped_at`` — and kis-api
            # counts a row without it as still holding the single strategy slot.
            # Record the end now. Zero duration: how long it actually ran, if at
            # all, is not known here, and the 4-week paper gate must not credit
            # days nobody saw it run.
            logger.warning("중단할 세션 없음 — 종료를 0일로 기록: run_id=%s", run_id)
            _record_never_ran(run_id, None, "중지 요청 시 실행 중인 세션 없음")

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

        # Periodic reconciliation on each market open (catches overnight drift).
        # Tracked so a shutdown waits for it — it does broker I/O and DB writes.
        self._spawn_aux(self._reconciler.reconcile, ("periodic",),
                        name=f"reconcile-{market}")

        logger.info("장 시작 브로드캐스트: market=%s sessions=%d", market, len(sessions))
        for session in sessions:
            self._spawn_aux(session.trigger_market_open, (market,),
                            name=f"market-open-{session.run_id}")

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
                        self._handle_start(data, restoring=True)
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

        run_id = data.get("run_id", 0)
        # Orders this run's machine records are attributed to it
        # (``orders.strategy_run_id``) — the 4-week gate needs a fill from the run.
        machine = OrderStateMachine(
            on_state_change=lambda o: self._persist_order(o, run_id=run_id))
        tracker = PositionTracker(machine, corporate_action_runtime=self._ca_runtime)

        on_filled_cb = self._make_fill_callback(tracker, machine, run_id)

        # P3-02B: terminal broker events (CANCELLED/REJECTED/EXPIRED) observed by
        # the poller must converge the runtime — transition the state machine and
        # release the pending lock — via the shared, idempotent handler.
        def on_terminal_cb(o):
            apply_terminal_event(machine, tracker, o, actor="poller",
                                 db_factory=_get_session_factory())

        # P3-02B (review Finding 1): durable audit for an untrackable order (no
        # broker id). The strategy fails closed (keeps the pending lock); this
        # records the orphan so reconcile/an operator can act.
        def on_orphan_cb(symbol, o):
            _audit("orphan_order_no_id", symbol=symbol, order_id=getattr(o, "id", "") or "",
                   detail={"side": getattr(o, "side", None), "qty": getattr(o, "qty", None),
                           "price": float(getattr(o, "price", 0) or 0)})

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
                # Converge the machine to CANCELLED and release the lock (was:
                # unmark only, which left the order stuck SUBMITTED — P3-02B H1).
                apply_terminal_event(machine, tracker, o,
                                     target_status=OrderStatus.CANCELED,
                                     db_factory=_get_session_factory())

        self._restore_positions(tracker, broker=data.get("broker", "kis"))
        self._restore_pending_to_tracker(
            tracker, broker=data.get("broker", "kis"),
            on_filled_cb=on_filled_cb, on_timeout_cb=on_timeout_cb,
            on_terminal_cb=on_terminal_cb, machine=machine,
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
                    on_terminal_cb=on_terminal_cb,
                    on_orphan_cb=on_orphan_cb,
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
            #
            # The poller hands this callback the *increment* (`filled_qty` is
            # replaced with what is new since its watermark), and the machine
            # turns it into the running total, which `_persist_order` writes to
            # the row. Step 4 must then set that total, not add the increment on
            # top — adding counted every fill twice (issue #172). Only when the
            # machine did not take the fill is there no total, and step 4 adds.
            filled_total = None
            try:
                if machine.get(order.id) is not None:
                    event = FillEvent(
                        order_id=order.id,
                        filled_qty=fill.qty,
                        fill_price=fill.price,
                    )
                    filled_total = machine.process_fill(event).filled_qty
            except Exception as e:
                logger.warning("machine.process_fill 오류: %s", e)

            # Which DB row this fill belongs to — settled *here*, before step 2
            # releases the symbol's pending lock (issue #168). Until then no other
            # order for this symbol can exist, so the recorded row can only be
            # this order's. After it, a new order could draw the same number
            # across a KIS day boundary and replace the record while step 3 waits
            # on the broker; step 4 then gets the row explicitly, not by lookup.
            row_id = self._remembered_order_row(order.id) if order.id else None
            closed = machine.get(order.id)
            if order.id and (closed.status if closed is not None
                             else order.status) == OrderStatus.FILLED:
                self._forget_order_row(order.id, row_id)

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
                    # Never fall back to peak_equity: MDD = (peak - peak)/peak = 0% masks drawdown.
                    # Use last-known-good equity; skip MDD evaluation if none available.
                    try:
                        _bal = get_kis_broker().get_balance()
                        current_equity = _bal.total_eval_krw
                        equity_verified = getattr(_bal, "equity_verified", True)
                        self._last_equity_reading = (current_equity, equity_verified)
                    except Exception as _be:
                        # The cached reading, amount and completeness together.
                        current_equity, equity_verified = self._last_equity_reading
                        if current_equity is None:
                            logger.warning("잔고 조회 실패, 기준 잔고 없음 — MDD 평가 스킵: %s", _be)
                            _audit("balance_fetch_failed", symbol=fill.symbol,
                                   detail={"reason": str(_be), "realized_pnl": realized_pnl})
                        else:
                            logger.warning("잔고 조회 실패 — 마지막 확인 잔고(%.0f원) 사용: %s",
                                           current_equity, _be)
                    if current_equity is not None:
                        # record_pnl() reports what *this call* did, decided under
                        # the tracker's own lock. Fills arrive concurrently on
                        # poller threads, so reading kill_switch (or a decision
                        # counter) before and after would let one fill claim
                        # another's breach and file it against the wrong symbol
                        # and P&L.
                        _EQUITY_READING.verified = equity_verified
                        try:
                            outcome = self._loss_tracker.record_pnl(
                                realized_pnl, current_equity)
                        finally:
                            _EQUITY_READING.verified = True
                        logger.info("손익 기록: %s %.0f원 (entry=%s fill=%.4f qty=%d)",
                                    fill.symbol, realized_pnl,
                                    f"{entry_price:.4f}" if entry_price is not None else "n/a",
                                    fill.price, fill.qty)
                        if outcome == "triggered":
                            _audit("kill_switch_triggered", symbol=fill.symbol,
                                   detail={"reason": self._loss_tracker.kill_reason,
                                           "realized_pnl": realized_pnl})
                        elif outcome == "adopted":
                            # Set outside this process (typically the API-side
                            # WorkerWatchdog) and only noticed here. Recording it
                            # against this symbol and P&L would invent a cause for
                            # the next person reading the trail.
                            _audit("kill_switch_adopted",
                                   detail={"reason": self._loss_tracker.kill_reason,
                                           "noticed_on_fill": fill.symbol})
                except Exception as e:
                    logger.warning("P&L 기록 실패: %s", e)

            # 4. Persist fill + update order status
            self._persist_fill(fill, order, row_id=row_id, filled_total=filled_total,
                              cumulative=order.cumulative_filled_qty)

            # 5. Upsert position in DB to reflect fill
            self._upsert_position_db(fill.symbol, fill.market, tracker.get_position(fill.symbol))

            # 6. WebSocket push — the order, then (off this path) the account's
            # positions and equity, which the fill just changed.
            self._publish_order_update(order)
            self._publish_portfolio_soon()
            logger.info("체결 파이프라인 완료: %s %s qty=%d @ %.4f",
                        order.id, order.symbol, fill.qty, fill.price)

        return on_filled

    # ── DB 연동 ───────────────────────────────────────────────────────────
    def _row_ids(self) -> tuple[dict, threading.Lock]:
        # setdefault, not a plain attribute read: workers built with
        # ``StrategyWorker.__new__`` (the test suites do) never ran __init__.
        return (self.__dict__.setdefault("_order_row_ids", {}),
                self.__dict__.setdefault("_order_row_lock", threading.Lock()))

    def _remember_order_row(self, broker_order_id: str, row_id: int) -> None:
        ids, lock = self._row_ids()
        with lock:
            ids[broker_order_id] = row_id

    def _forget_order_row(self, broker_order_id: str, row_id: int | None = None) -> None:
        """Drop the record for this number — only if it still names ``row_id``.

        With ``row_id`` given this is compare-and-delete: an order closing must
        not erase the record of a newer order that has since drawn the same
        number. Without it the record is dropped whatever it holds, which is
        for discarding a leftover before a new order's row is inserted.
        """
        ids, lock = self._row_ids()
        with lock:
            if row_id is None or ids.get(broker_order_id) == row_id:
                ids.pop(broker_order_id, None)

    def _remembered_order_row(self, broker_order_id: str) -> int | None:
        ids, lock = self._row_ids()
        with lock:
            return ids.get(broker_order_id)

    def _persist_order(self, order: Order, run_id: int | None = None):
        # Derive a deterministic idempotency key from broker order id + date.
        # KIS ODNO is unique per trading day per account, so this composite key
        # prevents duplicate DB rows when the same order is processed twice.
        # The day must be KIS's, i.e. the Seoul calendar date — not the 07:00
        # risk day (`trading_day()`, #166): ODNO restarts at Seoul midnight, so a
        # risk-day key could give two different orders sharing a reused number
        # (23:00 and 01:00) the same key. On the UTC date the key rolled over at
        # 09:00 KST — the Korean market open — so one order seen either side of
        # the open produced two keys and two rows (issue #160).
        # Resolved once: two calls could straddle Seoul midnight and put one
        # date in the key and the next in `trade_date` on the same row.
        from backend.database.models import seoul_date
        day = seoul_date()
        idem_key = (
            f"{order.id}:{order.symbol}:{order.side}:{day.isoformat()}"
            if order.id else None
        )
        try:
            with _session() as db:
                # Only ever an *open* row — see `_open_order_row` (issue #168).
                # Every call here is a state transition of a live order: terminal
                # states have no outgoing transitions, so an order never comes
                # back through this after closing, and a closed row with this
                # number is an earlier order that must not be overwritten. With no
                # open row this is a new order and gets its own row — including
                # the overnight case, where the row is still open until the fill
                # that closes it, so 23:50-submitted / 00:10-filled stays one row.
                existing = None
                known = self._remembered_order_row(order.id) if order.id else None
                if known is not None:
                    row = db.get(DBOrder, known)
                    if (row is not None and row.status in _OPEN_ORDER_STATUSES
                            and _same_order(row, order)):
                        existing = row
                if existing is None:
                    existing = _open_order_row(db, order)
                if existing:
                    existing.status = order.status.value
                    existing.filled_qty = order.filled_qty
                    existing.avg_fill_price = order.avg_fill_price or None
                    existing.updated_at = datetime.utcnow()
                    row_id = existing.id
                else:
                    # A leftover mapping points at a closed earlier order. Drop it
                    # before inserting, so a failed insert cannot leave the next
                    # fill resolving to that old row.
                    if order.id:
                        self._forget_order_row(order.id)
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
                        # The same `day` as the idempotency key — a row that
                        # says one thing in its key and another in its column is
                        # how the next person copies the wrong one. Nothing reads
                        # this column today, so this is a coherence fix, not a
                        # behaviour change.
                        trade_date=day,
                        # Set on insert only: an existing row keeps whatever run
                        # it was first recorded under.
                        strategy_run_id=run_id or None,
                    )
                    db.add(row)
                    db.flush()
                    row_id = row.id
                db.commit()
                if order.id:
                    # Kept past FILLED: the fill pipeline reads it right after
                    # this transition, while the symbol is still locked, to know
                    # which closed row its fill belongs to — and drops it there.
                    # A cancel/reject/expire has no fill to follow, so it is
                    # dropped here.
                    if order.status in _TERMINAL_NEGATIVE:
                        self._forget_order_row(order.id, row_id)
                    else:
                        self._remember_order_row(order.id, row_id)
        except IntegrityError:
            # Unique-constraint violation on idempotency_key — another path persisted the
            # same order concurrently (crash-replay or duplicate event). Treat as a duplicate
            # and skip; the existing row is authoritative.
            logger.warning("중복 주문 감지 (IntegrityError, idempotency_key=%s) — 저장 스킵", idem_key)
        except Exception as e:
            logger.warning("주문 DB 저장 실패: %s", e)

    def _persist_fill(self, fill: Fill, order: Order, row_id: int | None = None,
                      filled_total: int | None = None, cumulative: int | None = None):
        """File one fill under its order's row.

        ``row_id`` is the row the fill pipeline settled on while the symbol was
        still locked (see `on_filled`). It is needed because the FILLED
        transition was written a moment ago, so the row is already closed — and
        a closed row is exactly what an earlier order holding a recycled number
        looks like (issue #168). Without it (the machine skipped this order, or
        its write failed) the row is still open and found as such. A closed row
        is never guessed at, and the shared record is never read here: by now
        the lock is released and a newer order may own that entry.

        ``filled_total`` is the order's cumulative filled quantity from the
        state machine, when it processed this fill. The row already holds it
        (`_persist_order` wrote it in step 1), so it is set, never added to.
        Without it — the machine skipped this order — ``fill.qty`` is the
        increment and is added, which is the only write in that case.

        ``cumulative`` is the order's broker-reported filled total once this
        fill is in (``Order.cumulative_filled_qty``, set by the poller). It is
        the fill's identity: a redelivery of this fill carries the same total,
        a second fill of the same size a larger one (P2-03). The fills already
        on file add up to how far the order has been recorded, so the fill is
        a duplicate exactly when they already reach ``cumulative``. Without it
        nothing tells the two apart, and only the invariant is checked: fills
        never add up to more than the order's quantity.
        """
        try:
            with _session() as db:
                db_order = None
                if row_id is not None:
                    row = db.get(DBOrder, row_id)
                    # Checked, not trusted: the record it came from is dropped
                    # only when an order closes, so a run of failed writes could
                    # leave it pointing at an earlier order with this number.
                    if row is not None and _same_order(row, order):
                        db_order = row
                if db_order is None:
                    db_order = _open_order_row(db, order)
                if db_order is None:
                    logger.warning("체결 DB 저장 스킵: 미등록 주문 %s", order.id)
                    return
                # Idempotency. The poller's watermark is the first line: it hands
                # each increment over once. This is the second, for a fill that
                # reaches here twice anyway. Not by (qty, price): two real fills
                # of 5 at the limit price share that key, and the second was
                # dropped. The row is locked, and re-read under the lock, first:
                # two sessions cannot both pass the check, and the totals below
                # start from what the other one committed (no-op on SQLite).
                db.refresh(db_order, with_for_update=True)
                recorded = int(db.query(func.coalesce(func.sum(DBFill.qty), 0))
                               .filter(DBFill.order_id == db_order.id).scalar() or 0)
                qty = fill.qty
                if cumulative is not None:
                    if recorded >= cumulative:
                        logger.info("중복 체결 감지 — Fill 삽입 스킵: order=%s qty=%d 누적=%d (기록=%d)",
                                    order.id, fill.qty, cumulative, recorded)
                        return
                    if recorded + qty != cumulative:
                        # The fills on file and the poller's watermark disagree:
                        # a row from before this check or a fallback write (more
                        # on file), or an earlier fill write that failed (less).
                        # File what the broker's total says is missing, so the
                        # fills add up to the `filled_qty` written below.
                        qty = cumulative - recorded
                        logger.warning("체결 기록이 브로커 누적과 어긋남 — %d주로 기록: order=%s "
                                       "기록=%d + 증분 %d ≠ 누적 %d",
                                       qty, order.id, recorded, fill.qty, cumulative)
                elif db_order.qty and recorded + fill.qty > db_order.qty:
                    order_qty = db_order.qty
                    db.rollback()  # release the row lock before auditing
                    logger.error("과체결 체결 거부: order=%s 기록=%d + %d > 주문 %d",
                                 order.id, recorded, fill.qty, order_qty)
                    _audit("fill_overfill_rejected", symbol=fill.symbol, order_id=order.id,
                           detail={"recorded": recorded, "qty": fill.qty, "price": fill.price,
                                   "order_qty": order_qty})
                    return
                row = DBFill(order_id=db_order.id, qty=qty, price=fill.price)
                db.add(row)
                db_order.status = order.status.value
                if filled_total is not None:
                    db_order.filled_qty = filled_total
                elif cumulative is not None:
                    db_order.filled_qty = cumulative     # the broker's own total
                else:
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
                            "qty": qty,
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
                                    on_filled_cb=None, on_timeout_cb=None, on_terminal_cb=None,
                                    machine=None):
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
                ).order_by(DBOrder.id.desc()).all()
                # Extract scalars before the session closes (avoid DetachedInstanceError).
                # One row per order number, the newest: the poller and the state
                # machine are keyed by the number, so a second open row with it
                # would silently replace the first there anyway — just without
                # saying which. Only an earlier order that was never closed can
                # produce one; `_step_validate_state` reports it (issue #168).
                pending, seen = [], set()
                for r in rows:
                    if r.broker_order_id in seen:
                        logger.warning("같은 주문번호의 미종결 행 중복 — 옛 행(id=%d) 복원 제외: %s",
                                       r.id, r.broker_order_id)
                        continue
                    seen.add(r.broker_order_id)
                    pending.append(
                        {"row_id": r.id, "symbol": r.symbol, "order_id": r.broker_order_id,
                         "side": r.side, "qty": r.qty, "price": r.price or 0.0,
                         "status": r.status, "filled_qty": r.filled_qty or 0})
            for p in pending:
                self._remember_order_row(p["order_id"], p["row_id"])
                tracker.mark_pending(p["symbol"], p["order_id"])
                if self._poller is not None and on_filled_cb is not None:
                    self._register_recovered_order(p, tracker, on_filled_cb, on_timeout_cb,
                                                   on_terminal_cb, machine=machine)
                _audit("recovery_restore_pending", symbol=p["symbol"], order_id=p["order_id"],
                       detail={"broker": broker, "status": p["status"]})
            if pending:
                logger.info("미체결 주문 tracker 복원: %d개 %s",
                            len(pending), [p["symbol"] for p in pending])
        except Exception as e:
            logger.warning("pending tracker 복원 실패: %s", e)

    def _register_recovered_order(self, p: dict, tracker: PositionTracker,
                                  on_filled_cb, on_timeout_cb, on_terminal_cb=None,
                                  machine=None):
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
            filled_qty=p.get("filled_qty", 0),
        )

        row_id = p.get("row_id")

        def _guarded_on_filled(order: Order):
            try:
                with _session() as db:
                    # By primary key: the row was known when the order was
                    # restored. By number, an earlier FILLED order that held the
                    # same number could answer instead and suppress this live
                    # order's whole fill pipeline (issue #168).
                    row = (db.get(DBOrder, row_id) if row_id is not None
                           else _open_order_row(db, order))
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

        # Register the recovered order in the state machine so terminal broker
        # events (cancel/reject/expire) observed by the poller after restart can
        # transition it — otherwise apply_terminal_event has no machine entry to
        # converge (CodeRabbit Finding B). register() unconditionally (re)writes
        # the entry, so guard on get() to genuinely skip a duplicate; the except
        # only covers an unrelated persistence-callback failure.
        if machine is not None and machine.get(border.id) is None:
            try:
                machine.register(border)
            except Exception as e:
                logger.debug("복구 주문 machine.register 실패: %s", e)
        self._poller.register(border, on_filled=_guarded_on_filled, on_timeout=on_timeout_cb,
                              on_canceled=on_terminal_cb, on_rejected=on_terminal_cb,
                              on_expired=on_terminal_cb,
                              # Seed the poller watermark from the DB-persisted filled_qty so a
                              # recovered PARTIAL order never re-reports its pre-crash fill (the
                              # fill pipeline has no other dedup for partials).
                              initial_reported_qty=p.get("filled_qty", 0) or 0)

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
    def _publish_portfolio_soon(self) -> None:
        """Publish positions and equity on a tracked aux thread (``shutdown()``
        joins it), off the fill path. Only a worker built by ``__init__`` does:
        the publish reads the broker, and workers the test suites build with
        ``__new__`` must never reach a real one."""
        if not getattr(self, "_portfolio_feed", False):
            return
        try:
            self._spawn_aux(publish_portfolio, name="portfolio-feed")
        except Exception as e:  # a view; never let it disturb the fill pipeline
            logger.warning("포트폴리오 발행 시작 실패: %s", e)

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


def install_signal_handlers(worker: "StrategyWorker") -> None:
    """Route SIGTERM/SIGINT into the worker's shutdown flag.

    SIGTERM is what ``docker stop`` sends; SIGINT is Ctrl-C in a foreground
    container. Python installs no handler for SIGTERM by default, so until this
    ran the process simply vanished — the poller could die between reading a fill
    and writing it, and nothing recorded that the stop was deliberate.

    The handler does one thing. Signal handlers run in the main thread between
    two bytecodes, i.e. possibly inside a lock or a commit; doing the teardown
    here would deadlock on locks the interrupted frame holds. A second signal is
    read as "stop waiting" and exits without unwinding, since the graceful path
    is evidently not finishing.
    """
    # Counts signals, not ``worker.shutdown_requested``. ``shutdown()`` raises
    # that flag itself, so keying off it would turn the operator's *first* Ctrl-C
    # during a teardown reached from ``run()``'s ``finally`` into an immediate
    # ``_exit`` — losing the equity checkpoint and the shutdown record.
    seen = {"count": 0}

    def _handler(signum, _frame):
        """Raise the flag on the first signal; give up and exit on the second."""
        name = signal.Signals(signum).name
        seen["count"] += 1
        if seen["count"] > 1:
            logger.warning("%s 재수신 — graceful shutdown 포기, 즉시 종료", name)
            os._exit(1)
        logger.info("%s 수신 — graceful shutdown 시작", name)
        worker.request_shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _handler)
    logger.info("SIGTERM/SIGINT 핸들러 등록 (종료 예산 %.0fs)", _SHUTDOWN_BUDGET_SEC)


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
        # The 4-week paper run: orders go to the KIS paper account. The paper gate
        # (promotion_guard.paper_gate_status) needs a fill, so this is the setting
        # that can pass it.
        logger.info("KIS_ENV=paper + ENABLE_LIVE_TRADING=true — 모의투자 계좌로 주문을 보낸다")
    if _kis_env == "paper" and not _live_enabled:
        logger.warning(
            "섀도 모드(KIS_ENV=paper, ENABLE_LIVE_TRADING=false) — 주문이 나가지 않는다. "
            "체결이 없으므로 이 실행은 4주 모의투자 관문에 세지 않는다. "
            "모의투자는 ENABLE_LIVE_TRADING=true로."
        )

    # Create Worker first so its single poller can be shared with recovery
    # (prevents dual-poller situation where recovery creates its own poller)
    worker = StrategyWorker()

    # Installed before the recovery sequence, and the sequence is handed the flag
    # below — otherwise a SIGTERM during startup is only noticed once the whole
    # boot has finished, which is well past the 10s SIGKILL deadline.
    install_signal_handlers(worker)

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
        should_abort=lambda: worker.shutdown_requested,
    )
    recovered = recovery.run()

    if worker.shutdown_requested:
        # Stopped during boot. Starting the scheduler here would mean starting a
        # component only to tear it down, and firing jobs into a process that is
        # already on its way out.
        logger.info("기동 중 종료 요청 — 스케줄러를 시작하지 않고 종료 절차로 넘어간다")
        worker.shutdown()
        return

    from backend.worker.recovery import SAFE_MODE
    if not recovered and not recovery.halted_by_risk:
        logger.critical("복구 실패 — Worker SafeMode로 계속 실행")
        # Nothing reopens an untrusted halt but a restart — not the resume poll,
        # and since P0-12 not the 07:01 job either (it reopened any cause, so a
        # worker that never reconciled began trading). Say so, loudly.
        try:
            from bot.notifier import alert_emergency
            alert_emergency(f"[워커 복구 실패] 매매 차단 중 — 원인 확인 후 재시작 필요\n"
                            f"사유: {SAFE_MODE.reason}")
        except Exception as e:
            logger.warning("복구 실패 알림 전송 실패: %s", e)
    elif recovered:
        # The 4-week gate counts run uptime from here: a worker that is still
        # recovering, or stuck in SafeMode because recovery failed, cannot trade,
        # so its time is not paper-run time. Sessions record their own runs.
        enable_run_uptime(factory)

    # A restored risk halt is the one recovery failure a release can resume.
    worker.allow_risk_resume(recovered or recovery.halted_by_risk)

    from backend.worker.scheduler import build_scheduler
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("스케줄러 시작")
    worker.attach_scheduler(scheduler)  # so shutdown() can stop it first
    # The operator screen's account card; the scheduler refreshes it from here.
    worker._publish_portfolio_soon()

    worker.run()  # blocking until SIGTERM/SIGINT; tears down on the way out


if __name__ == "__main__":
    main()
