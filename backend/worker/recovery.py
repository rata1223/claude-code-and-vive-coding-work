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

    @property
    def can_trade(self) -> bool:
        return self._can_trade

    @property
    def halt_cause(self) -> Optional[HaltCause]:
        """The active halt cause, or ``None`` when trading is allowed."""
        return None if self._can_trade else self._cause

    def enable(self) -> None:
        self._can_trade = True
        self._reason = "정상"
        self._cause = None
        logger.info("SafeMode 해제 — 매매 허용")

    def disable(self, reason: str,
                cause: Optional[HaltCause] = HaltCause.UNTRUSTED_STATE) -> None:
        self._can_trade = False
        self._reason = reason
        self._cause = cause or HaltCause.UNTRUSTED_STATE
        logger.warning("SafeMode 활성화 [%s]: %s", self._cause.value, reason)

    def __repr__(self) -> str:
        return (f"SafeModeState(can_trade={self._can_trade}, "
                f"reason={self._reason!r}, cause={self._cause})")


# Process-level safe mode gate — strategies should check this before placing orders
SAFE_MODE = SafeModeState()


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
                    """Persist fill + update positions table so restored strategies see correct state."""
                    def on_filled(order: BOrder):
                        sess = self._factory()
                        try:
                            row = sess.get(DBOrder, db_order_pk)
                            if row:
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
        """Upsert or reduce position in DB for a recovery fill (B1/F1 fix).
        Uses the provided session; caller is responsible for commit.
        """
        from backend.database.models import Position as DBPosition
        try:
            row = sess.query(DBPosition).filter(
                DBPosition.symbol == symbol,
                DBPosition.broker == "kis",
            ).first()
            if side == "sell":
                if row is not None:
                    row.qty = max(0, row.qty - fill_qty)
                    if row.qty <= 0:
                        sess.delete(row)
                    else:
                        row.updated_at = datetime.utcnow()
            else:  # buy
                market = "KR" if (len(symbol) == 6 and symbol.isdigit()) else "US"
                if row is None:
                    sess.add(DBPosition(
                        symbol=symbol, qty=fill_qty, avg_price=fill_price,
                        market=market, broker="kis",
                    ))
                else:
                    prev_val = row.avg_price * row.qty
                    new_val = fill_price * fill_qty
                    total_qty = row.qty + fill_qty
                    row.avg_price = (prev_val + new_val) / total_qty
                    row.qty = total_qty
                    row.updated_at = datetime.utcnow()
        except Exception as e:
            logger.warning("복구 포지션 DB 갱신 실패 (%s): %s", symbol, e)

    def _audit_inconsistency(self, kind: str, detail: dict) -> None:
        """Append-only AuditLog write for a detected recovery inconsistency. Never raises."""
        try:
            from backend.database.models import AuditLog
            db = self._factory()
            try:
                db.add(AuditLog(
                    event_type="recovery_inconsistency",
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
        """
        try:
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
                stale = (db.query(DBOrder)
                         .filter(DBOrder.status.in_(["pending", "submitted", "partial_filled"]),
                                 DBOrder.broker_order_id.isnot(None),
                                 DBOrder.created_at < cutoff)
                         .all())
                bad_pos_data = [{"symbol": p.symbol, "qty": p.qty, "broker": p.broker}
                                for p in bad_positions]
                orphaned_data = [{"order_id": str(o.id), "symbol": o.symbol, "status": o.status}
                                 for o in orphaned]
                stale_data = [{"order_id": o.broker_order_id or str(o.id), "symbol": o.symbol,
                               "status": o.status, "created_at": o.created_at.isoformat()
                               if o.created_at else None}
                              for o in stale]
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

            if issues:
                logger.warning("복구 상태 일관성 검증: %d개 이슈 감지 — AuditLog 기록 완료", issues)
            else:
                logger.info("복구 상태 일관성 검증: 이슈 없음")
            return True
        except Exception as e:
            logger.warning("일관성 검증 실패 (계속 진행): %s", e)
            return True  # non-fatal — observability only

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

