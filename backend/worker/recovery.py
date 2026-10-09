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
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from backend.risk.halt_policy import HaltCause

_BROKER_STARTUP_TIMEOUT = int(os.environ.get("BROKER_STARTUP_TIMEOUT", "30"))
#: The startup reconcile is one position lookup plus one status lookup per open
#: order, and a single retried KIS GET is already worst-case ~32s — so it gets
#: its own budget rather than one probe's. A stop signal does not wait for this;
#: it only catches a broker that hangs with nobody asking the worker to stop.
_RECONCILE_STARTUP_TIMEOUT = int(os.environ.get("RECONCILE_STARTUP_TIMEOUT", "180"))
_RECOVERY_STALE_ORDER_HOURS = float(os.environ.get("RECOVERY_STALE_ORDER_HOURS", "24"))

#: How often a probe in flight looks up to notice a stop signal. Small enough
#: that SIGTERM is answered well inside Docker's 10s grace, large enough not to
#: spin.
_ABORT_POLL_SEC = 0.25

logger = logging.getLogger(__name__)


class RecoveryAborted(Exception):
    """A probe gave up because shutdown was requested while it was running."""


def call_with_deadline(fn, timeout: float, *, should_abort=None,
                        label: str = "broker probe"):
    """Call ``fn`` with a deadline that actually bounds this function's return.

    ``ThreadPoolExecutor`` cannot do this. Used as a context manager its
    ``__exit__`` runs ``shutdown(wait=True)``, so even after
    ``.result(timeout=...)`` raises, the ``with`` block keeps waiting for the
    call to come back — the timeout bounds *waiting for the result*, never the
    step. And ``shutdown(wait=False)`` is not a fix either: that pool's threads
    are non-daemon and joined by an ``atexit`` hook, so an abandoned call can
    hold up interpreter exit instead (issue #161).

    A plain daemon thread has neither problem. The abandoned call keeps running
    — nothing can safely interrupt a socket read mid-flight — but it can no
    longer delay this function or process exit, and these probes are read-only.

    Why this is not just belt-and-braces over the HTTP timeout: the per-request
    deadline is 10s (``kis_adapter.auth._http_timeout``) and GETs retry three
    times with a 1s pause, so *one* request is already worst-case ~32s. A
    balance probe makes two of those plus an FX lookup, and a position probe
    makes one per holding — minutes, against a 10s SIGKILL.

    ``should_abort`` is polled while waiting so a stop signal is answered
    without sitting out the full deadline.

    Raises ``TimeoutError`` on deadline, ``RecoveryAborted`` on abort, or
    whatever ``fn`` raised.
    """
    box: dict = {}
    done = threading.Event()

    def _runner():
        try:
            box["value"] = fn()
        except BaseException as exc:            # noqa: BLE001 - relayed below
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_runner, daemon=True,
                     name=f"recovery-{label}").start()

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{label} exceeded {timeout}s")
        if done.wait(min(_ABORT_POLL_SEC, remaining)):
            break
        if should_abort is not None and should_abort():
            raise RecoveryAborted(f"{label} abandoned — shutdown requested")

    if "error" in box:
        raise box["error"]
    return box["value"]


class StopGatedBroker:
    """Broker proxy whose *reads* refuse to run once ``stop()`` is true.

    ``call_with_deadline`` bounds a startup reconcile but cannot stop it: the
    abandoned ``PositionReconciler.reconcile()`` would carry on through every
    open order on its daemon thread. Passing this in its place gives that
    reconcile clean stopping points without touching the reconciler — its
    per-order loop already records a failed broker read as an error and moves
    on, so once stopped the rest of the loop drains in microseconds and the
    result comes back ``ok=False`` (fail-closed).

    Checked on both sides of the call: before, so no new request goes out;
    after, so a request that was in flight when the stop landed has its answer
    discarded instead of being acted on (a ``resync`` or DB sync).

    ``cancel_order`` is deliberately **not** gated. ``_mark_order_lost`` treats
    a failed cancel as a warning and still commits the row as CANCELED, so
    refusing the cancel would write "canceled" for an order that may still be
    live at the broker. It is only reached after a gated ``get_order_status``
    returned, so a stop can never land between deciding to cancel and sending
    it — either both the cancel and its commit happen, or neither does.
    """

    _GATED = frozenset({"get_positions", "get_order_status"})

    def __init__(self, broker, stop):
        self._broker = broker
        self._stop = stop

    def __getattr__(self, name):
        attr = getattr(self._broker, name)
        if name not in self._GATED:
            return attr

        def _gated(*args, **kwargs):
            if self._stop():
                raise RecoveryAborted(f"{name} skipped — shutdown requested")
            value = attr(*args, **kwargs)
            if self._stop():
                raise RecoveryAborted(f"{name} discarded — shutdown requested")
            return value
        return _gated


def run_reconcile_bounded(reconciler_factory, trigger: str, timeout: float, *,
                          should_abort=None):
    """Run a startup reconcile that can neither outlive ``timeout`` nor a stop.

    ``reconciler_factory(broker_wrapper)`` builds the ``PositionReconciler``;
    it receives a function to wrap its broker with so every read goes through
    a ``StopGatedBroker`` tied to this call. When this returns — normally, on
    deadline, or on abort — the gate is closed, so an abandoned reconcile stops
    at its next broker read instead of running on through every open order.

    Raises ``TimeoutError`` or ``RecoveryAborted`` like ``call_with_deadline``.
    """
    closed = threading.Event()

    def _stop() -> bool:
        return closed.is_set() or (should_abort is not None and should_abort())

    reconciler = reconciler_factory(lambda broker: StopGatedBroker(broker, _stop))
    try:
        result = call_with_deadline(lambda: reconciler.reconcile(trigger), timeout,
                                    should_abort=should_abort,
                                    label=f"reconcile-{trigger}")
    finally:
        closed.set()
    # Once the gate trips, the rest of the reconcile turns into caught errors
    # and returns in microseconds — usually before the wait above next looks
    # at `should_abort`. Without this check that comes back as an ordinary
    # failed reconcile, and SafeMode records a broker failure for what was a
    # stop signal.
    if should_abort is not None and should_abort():
        raise RecoveryAborted(f"reconcile-{trigger} stopped — shutdown requested")
    return result


@dataclass
class ReconcileAction:
    symbol: str
    action: str            # "accept_broker" | "ghost_position" | "untracked_position"
    broker_qty: int = 0
    db_qty: int = 0
    note: str = ""


class SafeModeState:
    """전략 실행 허용 여부를 전역으로 관리한다.

    P0-07 S1: a halt now carries *why* it happened, not just that it happened.
    The cause decides whether risk-reducing exits survive the halt — see
    ``backend/risk/halt_policy``. ``disable()`` keeps its single-argument form
    for existing callers and defaults to the most restrictive cause
    (``UNTRUSTED_STATE``, which blocks exits too), so an un-migrated caller can
    never accidentally widen what is permitted.
    """

    def __init__(self):
        self._can_trade = False
        self._reason = "초기화 중"
        # Startup begins in the untrusted state: recovery has not run yet, so
        # position data cannot be relied on for an exit decision.
        self._cause: Optional[HaltCause] = HaltCause.UNTRUSTED_STATE
        #: Why a fill could not be recorded (P1-10), or ``None``. While set,
        #: nothing in this process opens the gate or softens the cause.
        self._latch: Optional[str] = None
        self._latch_lock = threading.Lock()

    @property
    def can_trade(self) -> bool:
        return self._can_trade

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def halt_cause(self) -> Optional[HaltCause]:
        """The active halt cause, or ``None`` when trading is allowed."""
        return None if self._can_trade else self._cause

    @property
    def latched(self) -> Optional[str]:
        """Why a fill could not be recorded in this process, or ``None``."""
        return self._latch

    def enable(self) -> None:
        with self._latch_lock:
            if self._latch is not None:
                # Every opener — the end of startup recovery, the kill-switch
                # resume poll — goes through here; none may reopen past this.
                logger.error("SafeMode 해제 거부 — 체결 기록 실패, 재시작 필요: %s", self._latch)
                return
            self._can_trade = True
            self._reason = "정상"
            self._cause = None
        logger.info("SafeMode 해제 — 매매 허용")

    def disable(self, reason: str,
                cause: Optional[HaltCause] = HaltCause.UNTRUSTED_STATE) -> None:
        cause = cause or HaltCause.UNTRUSTED_STATE
        with self._latch_lock:
            if self._latch is not None:
                # A later halt must not turn this into one the resume poll
                # reopens, nor loosen an untrusted state into one that allows
                # exits: untrusted wins, anything else stays RECORD_FAILURE.
                untrusted = (cause is HaltCause.UNTRUSTED_STATE
                             or (not self._can_trade
                                 and self._cause is HaltCause.UNTRUSTED_STATE))
                cause = HaltCause.UNTRUSTED_STATE if untrusted else HaltCause.RECORD_FAILURE
                if self._latch not in reason:
                    # The reason the operator reads must keep saying a fill
                    # is unrecorded, whatever halts on top of it.
                    reason = f"{reason} — {self._latch}"
            self._can_trade = False
            self._reason = reason
            self._cause = cause
        logger.warning("SafeMode 활성화 [%s]: %s", cause.value, reason)

    def latch(self, reason: str) -> bool:
        """Shut the gate for a fill that could not be recorded (P1-10) until
        the process restarts. Exits stay allowed (``RECORD_FAILURE``) unless the
        state was already untrusted. Returns whether this was the first."""
        with self._latch_lock:
            first = self._latch is None
            if first:
                self._latch = reason
            stricter = (not self._can_trade
                        and self._cause is HaltCause.UNTRUSTED_STATE)
            self._can_trade = False
            self._reason = reason
            self._cause = HaltCause.UNTRUSTED_STATE if stricter else HaltCause.RECORD_FAILURE
            cause = self._cause
        logger.warning("SafeMode 고정 [%s]: %s", cause.value, reason)
        return first

    def __repr__(self) -> str:
        return (f"SafeModeState(can_trade={self._can_trade}, "
                f"reason={self._reason!r}, cause={self._cause})")


# Process-level safe mode gate — strategies should check this before placing orders
SAFE_MODE = SafeModeState()


#: Set when a ``fill_write_failed`` audit could not be written; later failures
#: in this process only log and latch.
_audit_down = False


def report_fill_write_failure(order_id, symbol, qty, price, error,
                              session_factory=None) -> None:
    """A real fill could not be written to ``fills``/``orders`` (P1-10).

    Latches ``SAFE_MODE`` until a restart, whose recovery reconciles orders,
    fills and positions with the broker: entries stop, exits stay possible
    (the in-memory tracker has the fill), and nothing in this process reopens
    it — see ``SafeModeState.latch``. The operator is alerted once per process
    (a database outage fails every fill), and the failure is audited as
    ``fill_write_failed`` when the database allows. Never raises.
    """
    reason = f"체결 기록 실패: order={order_id} {symbol} {qty}주 @ {price} — {error}"
    logger.error(reason)
    first = SAFE_MODE.latch(reason)
    global _audit_down
    if session_factory is not None and not _audit_down:
        try:
            from backend.database.models import AuditLog
            sess = session_factory()
            try:
                sess.add(AuditLog(event_type="fill_write_failed", symbol=symbol,
                                  order_id=str(order_id), actor="worker",
                                  detail=json.dumps({"qty": qty, "price": price,
                                                     "error": str(error)},
                                                    ensure_ascii=False)))
                sess.commit()
            finally:
                sess.close()
        except Exception as e:
            # The database is likely what failed: don't spend another connect
            # timeout on the poller thread for every further fill.
            _audit_down = True
            logger.warning("fill_write_failed 감사 기록 실패 — 이후 감사 생략: %s", e)
    if not first:
        return
    try:
        from bot.notifier import alert_emergency
        alert_emergency(f"[체결 기록 실패] 신규 매매 차단(청산은 허용) — 재시작 필요(기동 복구가 브로커와 맞춘다)\n"
                        f"주문: {order_id} {symbol} {qty}주 @ {price}\n오류: {error}")
    except Exception as e:
        logger.warning("체결 기록 실패 Telegram 알림 실패: %s", e)


class StartupRecovery:
    """
    Worker 시작 시 8단계 복구 시퀀스 실행.
    완료 후 SAFE_MODE.enable() 호출.
    """

    def __init__(self, db_session_factory, redis_client=None, broker=None, poller=None,
                 ca_runtime=None, should_abort=None):
        self._factory = db_session_factory
        self._redis = redis_client
        self._broker = broker
        self._shared_poller = poller  # Worker's poller — avoid creating a second one
        #: True when every check passed and only a restored risk halt keeps
        #: trading closed. The worker's resume poll may then reopen it once the
        #: operator releases the halt (P0-12); any other failure needs a restart.
        self.halted_by_risk = False
        self._ca_runtime = ca_runtime  # P2-02C: CorporateActionRuntime (optional)
        self._actions: list[ReconcileAction] = []
        #: Optional ``() -> bool``. Checked before each step; True stops the
        #: sequence. The worker passes its SIGTERM flag, so a stop signal during
        #: startup is noticed at the next step boundary instead of after the
        #: whole boot. Optional so existing constructions are unaffected.
        self._should_abort = should_abort

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
            ("복구 상태 일관성 검증", self._step_validate_state),
            ("정상 모드 진입", self._step_enable_trading),
        ]
        for i, (name, fn) in enumerate(steps, 1):
            # Checked per step. **Steps 4–6 additionally** poll it while their
            # broker work is in flight (`call_with_deadline`), so a stop signal
            # during a probe or the reconcile no longer waits it out — those used
            # to run past Docker's 10s SIGKILL on their own (issues #161, #170).
            #
            # Bounded, not interruptible: nothing can safely cut off a socket read
            # mid-flight, so an abandoned call runs on to its own end on a daemon
            # thread. What changed is that it cannot hold up this loop or exit.
            # Step 6 also writes (`cancel_order`), so its abandoned reconcile is
            # additionally gated to stop at its next broker read — never between
            # a cancel and its commit (`StopGatedBroker`).
            if self._should_abort is not None and self._should_abort():
                logger.warning("[복구 %d/%d] 종료 요청 — %s 이전에 복구 중단",
                               i, len(steps), name)
                SAFE_MODE.disable("기동 중 종료 요청 — 복구 중단")
                return False
            logger.info("[복구 %d/%d] %s", i, len(steps), name)
            try:
                ok = fn()
                if not ok:
                    if self.halted_by_risk:
                        # Recovery itself succeeded; the gate stays closed with
                        # its RISK_BREACH cause, which the resume poll reads.
                        # Overwriting it as untrusted would need a restart for
                        # what a release should resume.
                        logger.warning("[복구 %d/%d] 킬스위치로 매매 차단 — 해제되면 재개",
                                       i, len(steps))
                        return False
                    logger.error("[복구 %d/%d] 실패: %s — SafeMode 유지", i, len(steps), name)
                    SAFE_MODE.disable(f"복구 실패: {name}")
                    return False
            except RecoveryAborted as e:
                # Same outcome as the boundary check above, and deliberately the
                # same recorded reason: this was a stop signal, not a broker
                # failure, and the next operator reading SafeMode should be able
                # to tell those apart.
                logger.warning("[복구 %d/%d] 종료 요청 — %s 중 복구 중단: %s",
                               i, len(steps), name, e)
                SAFE_MODE.disable("기동 중 종료 요청 — 복구 중단")
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
                #: A halt read off a row — a risk halt. The failure branch below
                #: is not: there the risk state itself is unknown.
                self._kill_switch_from_row = True
            return True
        except Exception as e:
            # Fail-closed: if risk state cannot be verified, assume the
            # kill-switch is active rather than enabling trading on unknown state.
            logger.error("리스크 상태 복원 실패 — 안전을 위해 매매 차단: %s", e)
            self._kill_switch_active = True
            self._kill_reason = f"리스크 상태 복원 실패: {e}"
            return True

    def _step_balance(self) -> bool:
        if self._broker is None:
            logger.warning("브로커 없음 — 잔고 단계 스킵")
            return True
        try:
            bal = call_with_deadline(
                self._broker.get_balance, _BROKER_STARTUP_TIMEOUT,
                should_abort=self._should_abort, label="잔고 조회")
            logger.info("잔고 확인: 총평가 %.0f원", bal.total_eval_krw)
            return True
        except RecoveryAborted:
            # Relayed to run(), which records the deliberate-shutdown reason.
            # Swallowing it here would file a stop signal as a KIS outage.
            raise
        except TimeoutError:
            logger.error("잔고 조회 타임아웃 (%ds) — KIS API 응답 없음", _BROKER_STARTUP_TIMEOUT)
            return False
        except Exception as e:
            logger.error("잔고 조회 실패: %s", e)
            return False

    def _step_positions(self) -> bool:
        if self._broker is None:
            return True
        try:
            positions = call_with_deadline(
                self._broker.get_positions, _BROKER_STARTUP_TIMEOUT,
                should_abort=self._should_abort, label="포지션 조회")
            logger.info("브로커 포지션: %d개 %s",
                        len(positions), [p.symbol for p in positions])
            return True
        except RecoveryAborted:
            # Relayed to run(), which records the deliberate-shutdown reason.
            # Swallowing it here would file a stop signal as a KIS outage.
            raise
        except TimeoutError:
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
            try:
                result = run_reconcile_bounded(
                    lambda gate: PositionReconciler(
                        broker=gate(self._broker),
                        db_factory=self._factory,
                        redis_client=r,
                        broker_name="kis",
                        ca_runtime=self._ca_runtime,  # P2-02C: classify splits during startup reconcile
                    ),
                    "startup", _RECONCILE_STARTUP_TIMEOUT,
                    should_abort=self._should_abort,
                )
            except TimeoutError as e:
                logger.error("스타트업 조정 시간 초과 — 매매 차단 유지: %s", e)
                return False
            # P2-02C: rebuild the corporate-action gate from the DB so a pending/UNKNOWN
            # action persisted before the restart still blocks trading (fail-closed).
            # If the restore cannot be confirmed, keep SafeMode disabled (return False)
            # rather than booting with an empty gate that could re-enable trading.
            if self._ca_runtime is not None:
                try:
                    n = self._ca_runtime.restore_pending()
                    if n:
                        logger.info("기업행위 게이트 복원: %d개", n)
                except Exception as exc:
                    logger.error("기업행위 게이트 복원 실패 — 안전을 위해 매매 차단: %s", exc)
                    return False
            # Populate _actions from reconcile result for observability
            for gap in result.gaps:
                self._actions.append(ReconcileAction(
                    symbol=gap["symbol"],
                    action=gap["kind"],
                    note=gap["detail"],
                ))
            for repair in result.repairs:
                logger.debug("스타트업 조정 수정: %s", repair)
            logger.info("스타트업 조정 완료: 갭=%d 수정=%d 오류=%d",
                        len(result.gaps), len(result.repairs), len(result.errors))
            # Fail-closed: reconcile *errors* (broker/DB failures) mean positions
            # are unverified — keep SafeMode disabled. Gaps are normal (repaired).
            if not result.ok:
                logger.error("스타트업 조정 오류 — 매매 차단 유지: %s", result.errors)
            return result.ok
        except RecoveryAborted:
            raise
        except Exception as e:
            logger.error("Reconcile 실패 — 안전을 위해 매매 차단: %s", e)
            return False  # fail-closed

    def _step_pending_orders(self) -> bool:
        if self._broker is None:
            return True
        try:
            from backend.database.models import Order as DBOrder, Fill as DBFill
            from backend.execution.order_poller import OrderFillPoller
            from backend.brokers.models import Order as BOrder, OrderStatus
            db = self._factory()
            rows = (db.query(DBOrder)
                    .filter(DBOrder.status.in_(["pending", "submitted", "partial_filled"]))
                    .filter(DBOrder.broker_order_id.isnot(None))
                    .order_by(DBOrder.id.desc())
                    .all())
            db.close()
            # One registration per order number, the newest row. The poller is
            # keyed by the number, so a second open row with it would replace the
            # first there anyway, without saying which. A KIS number restarts
            # every day, so two open rows sharing one means an earlier order was
            # never closed — `_step_validate_state` reports it (issue #168).
            pending, seen = [], set()
            for row in rows:
                if row.broker_order_id in seen:
                    logger.warning("같은 주문번호의 미종결 행 중복 — 옛 행(id=%d) 재등록 제외: %s",
                                   row.id, row.broker_order_id)
                    continue
                seen.add(row.broker_order_id)
                pending.append(row)
            if pending:
                logger.info("미체결 주문 %d개 발견 — OrderFillPoller에 재등록", len(pending))
                # Reuse Worker's shared poller to avoid duplicate polling threads
                if self._shared_poller is not None:
                    poller = self._shared_poller
                else:
                    poller = OrderFillPoller(self._broker)
                    poller.start()

                def _make_recovery_fill_cb(db_order_pk: int, broker_order_id: str):
                    """Persist fill + update positions table so restored strategies see correct state."""
                    def on_filled(order: BOrder):
                        failure = None
                        sess = self._factory()
                        try:
                            row = sess.get(DBOrder, db_order_pk)
                            if row is None:
                                failure = "주문 행 없음"
                            else:
                                fill_qty = order.filled_qty or order.qty
                                fill_price = order.avg_fill_price or order.price
                                # P3-02C-D F2: the poller delivers INCREMENTAL fill
                                # quantities and its watermark (seeded from filled_qty at
                                # register — F1) is now the single dedup, so ACCUMULATE
                                # rather than overwrite, and do not re-dedup on
                                # (qty, price) here — that key is not unique and would drop
                                # a legitimate equal-sized second leg (audit I-1).
                                row.status = order.status.value
                                row.filled_qty = (row.filled_qty or 0) + fill_qty
                                row.avg_fill_price = fill_price
                                sess.add(DBFill(order_id=db_order_pk,
                                               qty=fill_qty,
                                               price=fill_price))
                                # F1: update positions table so _restore_positions() picks up the fill
                                self._apply_fill_to_position_db(
                                    sess, row.symbol, row.side, fill_qty, fill_price,
                                )
                                sess.commit()
                                logger.info("복구 체결 DB 업데이트: %s → FILLED", broker_order_id)
                        except Exception as e:
                            failure = e
                        finally:
                            sess.close()
                        if failure is not None:
                            report_fill_write_failure(
                                broker_order_id, order.symbol, order.filled_qty or order.qty,
                                order.avg_fill_price or order.price, failure,
                                session_factory=self._factory)
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
                        # P3-02C-D F1: seed the replay watermark from the DB-persisted
                        # filled_qty so a recovered PARTIAL is not re-reported from 0 by
                        # the poll loop or a reconciler resync() (matches the runner.py
                        # recovery seam). The poller watermark is now the single fill
                        # dedup for this order.
                        initial_reported_qty=(row.filled_qty or 0),
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
                            ca_runtime=self._ca_runtime,  # P2-02C: same CA gate as startup reconcile
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

    def _apply_fill_to_position_db(self, sess, symbol: str, side: str,
                                    fill_qty: int, fill_price: float) -> None:
        """Apply a recovery fill to the position row (B1/F1 fix) as a delta.
        Uses the provided session; caller is responsible for commit.

        P0-09: the row is locked before it is read (``lock_position``), and a
        first buy creates it with ``INSERT … ON CONFLICT DO NOTHING`` — another
        writer between the read and the commit can no longer lose this delta or
        turn it into a duplicate-key error.
        """
        from backend.database.models import insert_position_if_missing, lock_position
        try:
            if side == "sell":
                row = lock_position(sess, symbol, "kis")
                if row is not None:
                    row.qty = max(0, row.qty - fill_qty)
                    if row.qty <= 0:
                        sess.delete(row)
                    else:
                        row.updated_at = datetime.utcnow()
            else:  # buy
                market = "KR" if (len(symbol) == 6 and symbol.isdigit()) else "US"
                if insert_position_if_missing(sess, symbol=symbol, broker="kis",
                                              qty=fill_qty, avg_price=fill_price,
                                              market=market):
                    return
                row = lock_position(sess, symbol, "kis")
                prev_val = row.avg_price * row.qty
                new_val = fill_price * fill_qty
                total_qty = row.qty + fill_qty
                row.avg_price = (prev_val + new_val) / total_qty
                row.qty = total_qty
                row.updated_at = datetime.utcnow()
        except Exception as e:
            logger.warning("복구 포지션 DB 갱신 실패 (%s): %s", symbol, e)

    def _audit_inconsistency(self, kind: str, detail: dict,
                             event_type: str = "recovery_inconsistency") -> None:
        """Append-only AuditLog write for a detected recovery inconsistency. Never raises."""
        try:
            from backend.database.models import AuditLog
            db = self._factory()
            try:
                db.add(AuditLog(
                    event_type=event_type,
                    symbol=detail.get("symbol"),
                    order_id=detail.get("order_id"),
                    actor="recovery",
                    detail=json.dumps({"kind": kind, **detail}, ensure_ascii=False),
                ))
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning("일관성 감사 로그 기록 실패 [%s]: %s", kind, e)

    def _step_validate_state(self) -> bool:
        """Non-mutating consistency validation of persisted recovery state.

        Detects inconsistent states that recovery should never silently tolerate and records
        each to the append-only AuditLog. This step NEVER mutates orders or positions — it is
        observability only — and is non-fatal (returns True) so an otherwise-recoverable
        worker still starts; SAFE_MODE / kill-switch handle trade blocking separately.

        Checks:
          1. positions with qty <= 0 (should have been deleted on flat/close)
          2. pending orders with NULL broker_order_id (orphaned intent — never confirmed)
          3. pending orders older than RECOVERY_STALE_ORDER_HOURS with non-terminal status
          4. two or more open rows sharing one broker order number — a KIS number
             restarts every day, so this means an earlier order was never closed
             and is now indistinguishable, by number, from a live one (issue #168)
          5. an order updated in the last week whose status disagrees with its
             latest ``order_events`` row — a write that went around the session
             hook (P2-01). Orders with no events predate the log and are only
             counted. Checked separately (``_check_order_history``), so a
             failure here does not cost checks 1–4.

        Checks 3 and 4 count `unknown` as open, as the worker's order matching
        does: nothing re-polls such a row, so this report is how anyone learns
        it is stuck. Check 2 keeps its original three statuses.
        """
        try:
            from sqlalchemy import func
            from backend.database.models import Order as DBOrder, Position as DBPosition
            issues = 0
            cutoff = datetime.utcnow() - timedelta(hours=_RECOVERY_STALE_ORDER_HOURS)
            db = self._factory()
            try:
                bad_positions = db.query(DBPosition).filter(DBPosition.qty <= 0).all()
                orphaned = (db.query(DBOrder)
                            .filter(DBOrder.status.in_(["pending", "submitted", "partial_filled"]),
                                    DBOrder.broker_order_id.is_(None))
                            .all())
                # Exclude orphaned (NULL broker_order_id) rows — they are already reported
                # by the orphaned check above; avoid double-counting the same row.
                open_statuses = ["pending", "submitted", "partial_filled", "unknown"]
                stale = (db.query(DBOrder)
                         .filter(DBOrder.status.in_(open_statuses),
                                 DBOrder.broker_order_id.isnot(None),
                                 DBOrder.created_at < cutoff)
                         .all())
                shared = (db.query(DBOrder.broker_order_id, func.count(DBOrder.id))
                          .filter(DBOrder.status.in_(open_statuses),
                                  DBOrder.broker_order_id.isnot(None))
                          .group_by(DBOrder.broker_order_id)
                          .having(func.count(DBOrder.id) > 1)
                          .all())
                bad_pos_data = [{"symbol": p.symbol, "qty": p.qty, "broker": p.broker}
                                for p in bad_positions]
                orphaned_data = [{"order_id": str(o.id), "symbol": o.symbol, "status": o.status}
                                 for o in orphaned]
                stale_data = [{"order_id": o.broker_order_id or str(o.id), "symbol": o.symbol,
                               "status": o.status, "created_at": o.created_at.isoformat()
                               if o.created_at else None}
                              for o in stale]
                shared_data = [{"order_id": oid, "open_rows": n} for oid, n in shared]
            finally:
                db.close()

            for d in bad_pos_data:
                logger.warning("일관성 경고 — 비정상 수량 포지션: %s qty=%d", d["symbol"], d["qty"])
                self._audit_inconsistency("position_nonpositive_qty", d)
                issues += 1
            for d in orphaned_data:
                logger.warning("일관성 경고 — 미확인(orphaned) 주문: id=%s %s", d["order_id"], d["symbol"])
                self._audit_inconsistency("orphaned_pending_order", d)
                issues += 1
            for d in stale_data:
                logger.warning("일관성 경고 — 오래된 미체결 주문(>%.0fh): %s %s",
                               _RECOVERY_STALE_ORDER_HOURS, d["order_id"], d["symbol"])
                self._audit_inconsistency("stale_open_order", d)
                issues += 1
            for d in shared_data:
                logger.warning("일관성 경고 — 같은 주문번호의 미종결 행 %d개: %s",
                               d["open_rows"], d["order_id"])
                self._audit_inconsistency("duplicate_open_broker_order_id", d)
                issues += 1
            issues += self._check_order_history()

            if issues:
                logger.warning("복구 상태 일관성 검증: %d개 이슈 감지 — AuditLog 기록 완료", issues)
            else:
                logger.info("복구 상태 일관성 검증: 이슈 없음")
            return True
        except Exception as e:
            logger.warning("일관성 검증 실패 (계속 진행): %s", e)
            return True  # non-fatal — observability only

    #: How far back the boot check compares orders with their history (P2-01).
    #: Bounded so the scan does not grow with the log, and so one old mismatch
    #: stops being re-reported every boot.
    ORDER_HISTORY_CHECK_DAYS = 7

    def _check_order_history(self) -> int:
        """Check 5 of ``_step_validate_state``; returns the issues found. Never
        raises — its own failure is logged and leaves checks 1–4 standing."""
        try:
            from sqlalchemy import func
            from backend.database.models import Order as DBOrder, OrderEvent
            since = datetime.utcnow() - timedelta(days=self.ORDER_HISTORY_CHECK_DAYS)
            db = self._factory()
            try:
                recent = DBOrder.updated_at >= since
                latest = (db.query(OrderEvent.order_id, func.max(OrderEvent.id).label("eid"))
                          .join(DBOrder, DBOrder.id == OrderEvent.order_id)
                          .filter(recent)
                          .group_by(OrderEvent.order_id).subquery())
                mismatched = (db.query(DBOrder, OrderEvent.to_status)
                              .join(latest, latest.c.order_id == DBOrder.id)
                              .join(OrderEvent, OrderEvent.id == latest.c.eid)
                              .filter(OrderEvent.to_status != DBOrder.status)
                              .all())
                data = [{"order_id": str(o.id), "symbol": o.symbol,
                         "status": o.status, "event_status": ev}
                        for o, ev in mismatched]
                unlogged = (db.query(func.count(DBOrder.id))
                            .filter(recent, ~db.query(OrderEvent.id)
                                    .filter(OrderEvent.order_id == DBOrder.id).exists())
                            .scalar()) or 0
            finally:
                db.close()
        except Exception as e:
            logger.warning("주문 이력 검증 실패 (계속 진행): %s", e)
            return 0
        for d in data:
            logger.warning("일관성 경고 — 주문 상태가 이력과 다름: id=%s %s (%s, 이력 %s)",
                           d["order_id"], d["symbol"], d["status"], d["event_status"])
            self._audit_inconsistency("order_status_event_mismatch", d)
        if unlogged:
            logger.info("주문 이력 없는 주문 %d건 — 이력 기록(P2-01) 이전 행", unlogged)
        self._check_history_guard()
        return len(data)

    def _check_history_guard(self) -> None:
        """On Postgres, whether ``order_events`` is append-only below the ORM
        (``order_history.ensure_db_guard``). Its install never stops a process;
        this is where a missing guard is reported — as its own audit event, not
        a ``recovery_inconsistency``: it is about the database, not about the
        orders and positions those rows describe."""
        try:
            from backend.database.order_history import guard_installed
            db = self._factory()
            try:
                if db.get_bind().dialect.name != "postgresql" or guard_installed(db):
                    return
            finally:
                db.close()
        except Exception as e:
            logger.warning("주문 이력 트리거 확인 실패 (계속 진행): %s", e)
            return
        logger.error("order_events append-only 트리거 없음: DB 수준에서 이력을 "
                     "고치거나 지울 수 있다 (ORM 가드만 동작)")
        self._audit_inconsistency("order_events_guard_missing", {"table": "order_events"},
                                  event_type="order_events_guard_missing")

    def _step_enable_trading(self) -> bool:
        import os
        if SAFE_MODE.latched is not None:
            # A recovered order's fill could not be recorded while recovery ran.
            # Before every other branch: none of them may count this as a halt
            # a release can lift.
            logger.critical("복구 중 체결 기록 실패 — 매매 차단 유지, 재시작 필요")
            return False
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
            if getattr(self, "_kill_switch_from_row", False):
                # A risk halt, like the one the tracker closes the gate with:
                # every other check passed, so once the operator releases it the
                # worker's resume poll reopens trading (P0-12). It used to be
                # recorded as untrusted state, which only a restart cleared.
                SAFE_MODE.disable(f"킬스위치 복원: {reason}", cause=HaltCause.RISK_BREACH)
                self.halted_by_risk = True
                hint = "앱에서 해제하면 1분 안에 재개"
            else:
                SAFE_MODE.disable(f"킬스위치 복원: {reason}")
                hint = "리스크 상태를 읽지 못함 — 원인 확인 후 재시작 필요"
            logger.critical("킬스위치 복원 — 매매 차단. %s", hint)
            try:
                from bot.notifier import alert_emergency
                alert_emergency(
                    f"[킬스위치 복원] 재시작 후에도 매매 차단 중\n사유: {reason}\n{hint}"
                )
            except Exception as e:
                logger.warning("킬스위치 재시작 Telegram 알림 실패: %s", e)
            return False

        SAFE_MODE.enable()
        logger.info("복구 완료 — 매매 허용. reconcile actions=%d", len(self._actions))
        return True

