"""
Emergency kill switch — unified, graduated trading-state machine, trigger
evaluation, order interception, append-only kill-event logging, manual
recovery flow, and extensible notification hooks.

Standalone library: it does not import or modify strategy/execution/broker/
reconciliation code, and has no side effects on them. All collaborators
(trigger thresholds, DB session factory, notifiers, validation checks) are
dependency-injected so the module is independently testable with plain fakes
— no real broker, Redis, or DB required.

`KillSwitch` is interoperable with — not a replacement for — the existing
`DailyRiskState.kill_switch`/`kill_reason` columns already written by
`LossTracker`/`PersistentLossTracker` (backend/quant/risk/engine.py) and
`WorkerWatchdog._alert_dead_worker` (backend/worker/heartbeat.py): it reads
and writes the SAME canonical DB row, using the exact
"sess.get(...) / create-if-missing / set-only-if-not-already-set" pattern
`_alert_dead_worker` already established, so all systems observe one
consistent halt state instead of racing two conflicting flags.

Wiring this into the running worker process — calling `report_*` from the
risk engine / order submission / reconciliation paths, consulting
`check_order(...)` before `place_order()` in strategy/base.py, and replacing
the ad-hoc `_fire_kill_switch_alert`/`SAFE_MODE.disable` calls in
`engine.py` with `KillSwitch` — is a deliberate follow-up task (mirrors how
idempotency.py and watchdog.py were each built standalone first).
"""
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Global Trading State
# ═════════════════════════════════════════════════════════════════════════════

class TradingState(Enum):
    RUNNING = "running"
    WARNING = "warning"
    HALTED = "halted"


_STATE_RANK = {TradingState.RUNNING: 0, TradingState.WARNING: 1, TradingState.HALTED: 2}


# ═════════════════════════════════════════════════════════════════════════════
# 2. Kill Trigger Engine
# ═════════════════════════════════════════════════════════════════════════════

class KillTriggerType(Enum):
    BROKER_FAILURE = "broker_failure"
    CONSECUTIVE_ERRORS = "consecutive_errors"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    LOSS_LIMIT_BREACH = "loss_limit_breach"
    WATCHDOG_FAILURE = "watchdog_failure"


class Severity(Enum):
    WARNING = "warning"
    CRITICAL = "critical"


_SEVERITY_RANK = {Severity.WARNING: 1, Severity.CRITICAL: 2}

_SEVERITY_TO_STATE = {Severity.WARNING: TradingState.WARNING, Severity.CRITICAL: TradingState.HALTED}


def _normalize_severity(value: Any) -> Optional[Severity]:
    """Duck-typed severity coercion — accepts this module's Severity, any
    enum-like object exposing `.value`, or a plain string. Returns None for
    anything unrecognized (caller treats that as "no trigger"), so a foreign
    severity vocabulary can never crash evaluation — it just fails to match."""
    if isinstance(value, Severity):
        return value
    raw = getattr(value, "value", value)
    try:
        raw = str(raw).strip().lower()
    except Exception:
        return None
    for sev in Severity:
        if sev.value == raw:
            return sev
    return None


@dataclass(frozen=True)
class KillTrigger:
    trigger_type: KillTriggerType
    severity: Severity
    message: str
    detail: Mapping[str, Any] = field(default_factory=dict)


class KillTriggerEngine:
    """Pure threshold evaluation — every check_* takes a pre-measured value
    (no broker/DB/strategy collaborators) and returns Optional[KillTrigger]:
    None when the value is below the configured warning threshold, WARNING
    when it has crossed the warning threshold, CRITICAL when it has crossed
    the critical threshold. Each check is independent — no cross-check
    coupling (mirrors DeadWorkerDetector.evaluate's per-signal independence).

    Loss-limit defaults mirror the platform's DAILY_LOSS_LIMIT_PCT=0.03 /
    MDD_LIMIT_PCT=0.15 env values, with WARNING set at the same "80% of the
    critical limit" convention LossTracker.can_buy() already uses
    (backend/quant/risk/engine.py:299).
    """

    def __init__(self, *,
                 broker_failure_warning: int = 2,
                 broker_failure_critical: int = 5,
                 consecutive_errors_warning: int = 3,
                 consecutive_errors_critical: int = 8,
                 reconciliation_mismatch_warning: int = 1,
                 reconciliation_mismatch_critical: int = 3,
                 daily_loss_critical_pct: float = 0.03,
                 daily_loss_warning_pct: Optional[float] = None,
                 mdd_critical_pct: float = 0.15,
                 mdd_warning_pct: Optional[float] = None):
        self._broker_warn = broker_failure_warning
        self._broker_crit = broker_failure_critical
        self._errors_warn = consecutive_errors_warning
        self._errors_crit = consecutive_errors_critical
        self._mismatch_warn = reconciliation_mismatch_warning
        self._mismatch_crit = reconciliation_mismatch_critical
        self._daily_loss_crit = daily_loss_critical_pct
        self._daily_loss_warn = daily_loss_warning_pct if daily_loss_warning_pct is not None \
            else daily_loss_critical_pct * 0.8
        self._mdd_crit = mdd_critical_pct
        self._mdd_warn = mdd_warning_pct if mdd_warning_pct is not None \
            else mdd_critical_pct * 0.8

    @staticmethod
    def _by_count(value: int, warn: int, crit: int, trigger_type: KillTriggerType,
                  label: str, detail: Optional[Mapping[str, Any]] = None) -> Optional[KillTrigger]:
        d = dict(detail or {})
        d["value"] = value
        if value >= crit:
            return KillTrigger(trigger_type, Severity.CRITICAL,
                               f"{label} 임계 초과 ({value} >= {crit})", d)
        if value >= warn:
            return KillTrigger(trigger_type, Severity.WARNING,
                               f"{label} 경고 수준 ({value} >= {warn})", d)
        return None

    def check_broker_failure(self, consecutive_failures: int,
                             detail: Optional[Mapping[str, Any]] = None) -> Optional[KillTrigger]:
        return self._by_count(consecutive_failures, self._broker_warn, self._broker_crit,
                              KillTriggerType.BROKER_FAILURE, "브로커 연속 실패", detail)

    def check_consecutive_errors(self, error_count: int,
                                 detail: Optional[Mapping[str, Any]] = None) -> Optional[KillTrigger]:
        return self._by_count(error_count, self._errors_warn, self._errors_crit,
                              KillTriggerType.CONSECUTIVE_ERRORS, "연속 오류", detail)

    def check_reconciliation_mismatch(self, mismatch_count: int,
                                      detail: Optional[Mapping[str, Any]] = None) -> Optional[KillTrigger]:
        return self._by_count(mismatch_count, self._mismatch_warn, self._mismatch_crit,
                              KillTriggerType.RECONCILIATION_MISMATCH, "포지션 정합성 불일치", detail)

    def check_loss_breach(self, daily_pnl_pct: float, mdd_pct: float,
                          detail: Optional[Mapping[str, Any]] = None) -> Optional[KillTrigger]:
        d = dict(detail or {})
        d["daily_pnl_pct"] = daily_pnl_pct
        d["mdd_pct"] = mdd_pct
        if daily_pnl_pct <= -self._daily_loss_crit:
            return KillTrigger(KillTriggerType.LOSS_LIMIT_BREACH, Severity.CRITICAL,
                               f"일일 손실 한도 초과 ({daily_pnl_pct:.2%})", d)
        if mdd_pct <= -self._mdd_crit:
            return KillTrigger(KillTriggerType.LOSS_LIMIT_BREACH, Severity.CRITICAL,
                               f"MDD 한도 초과 ({mdd_pct:.2%})", d)
        if daily_pnl_pct <= -self._daily_loss_warn:
            return KillTrigger(KillTriggerType.LOSS_LIMIT_BREACH, Severity.WARNING,
                               f"일일 손실 경고 수준 ({daily_pnl_pct:.2%})", d)
        if mdd_pct <= -self._mdd_warn:
            return KillTrigger(KillTriggerType.LOSS_LIMIT_BREACH, Severity.WARNING,
                               f"MDD 경고 수준 ({mdd_pct:.2%})", d)
        return None

    def check_watchdog_failure(self, severity: Any, message: str,
                               detail: Optional[Mapping[str, Any]] = None) -> Optional[KillTrigger]:
        sev = _normalize_severity(severity)
        if sev is None:
            return None
        d = dict(detail or {})
        d["source_severity"] = sev.value
        return KillTrigger(KillTriggerType.WATCHDOG_FAILURE, sev,
                           f"watchdog 보고: {message}", d)


# ═════════════════════════════════════════════════════════════════════════════
# 3. Order Interceptor
# ═════════════════════════════════════════════════════════════════════════════

class OrderIntent(Enum):
    NEW = "new"                         # new entry/exit order
    AMEND = "amend"                     # amend / replace an existing order
    CANCEL = "cancel"                   # cancel a resting order
    CLOSE_POSITION = "close_position"   # explicit position-closing order (e.g. emergency flatten)


class InterceptDecision(Enum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True)
class InterceptOutcome:
    decision: InterceptDecision
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision is InterceptDecision.ALLOW


class OrderInterceptor:
    """Pure, stateless decision function of (state, intent) — holds no
    per-order memory, so repeated calls with identical inputs always return
    identical outcomes. This is a deliberate safety property: the gate cannot
    "forget" it already blocked something, and cannot be confused by retries
    or duplicated submission attempts (see TestDuplicateOrderDetection)."""

    def __init__(self, allow_close_position_when_halted: bool = False):
        self._allow_close = allow_close_position_when_halted

    def evaluate(self, state: TradingState, intent: OrderIntent) -> InterceptOutcome:
        if state in (TradingState.RUNNING, TradingState.WARNING):
            return InterceptOutcome(InterceptDecision.ALLOW, f"state={state.value}: 거래 허용")

        # state is HALTED
        if intent is OrderIntent.CANCEL:
            return InterceptOutcome(InterceptDecision.ALLOW,
                                    "HALTED 상태에서도 주문 취소는 항상 허용")
        if intent is OrderIntent.CLOSE_POSITION:
            if self._allow_close:
                return InterceptOutcome(InterceptDecision.ALLOW,
                                        "HALTED 상태 — 포지션 청산 허용 설정됨")
            return InterceptOutcome(InterceptDecision.BLOCK,
                                    "HALTED 상태 — 포지션 청산 차단 (설정상 비허용)")
        # NEW / AMEND
        return InterceptOutcome(InterceptDecision.BLOCK,
                                f"HALTED 상태 — {intent.value} 주문 차단")


# ═════════════════════════════════════════════════════════════════════════════
# 4. Kill Reason Logging
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class KillEvent:
    timestamp: datetime
    trigger_type: Optional[KillTriggerType]   # None for resume / manual events
    severity: Severity
    from_state: TradingState
    to_state: TradingState                    # == from_state when no transition occurred
    message: str
    detail: Mapping[str, Any]
    actor: str                                  # "kill_switch" | "operator" | injected name

    @property
    def is_transition(self) -> bool:
        return self.from_state is not self.to_state


class KillReasonLog:
    """Append-only kill-event log — record() is the only write method (no
    update/delete), satisfying 'preserve append-only auditability'.

    Three independent layers, mirroring watchdog.AlertSystem
    (backend/worker/watchdog.py:403-476):
      - bounded in-memory ring buffer (deque(maxlen=...))  — always available
      - append-only AuditLog persistence (event_type='kill_switch_event')
      - (notification is a SEPARATE component — see NotificationHook; this
        class only logs, it never dispatches externally)

    DB persistence uses the identical sess.rollback()-on-commit-failure
    convention as watchdog.AlertSystem._persist / RecoveryExecutor._audit —
    failure here is logged and swallowed; it can never raise into the caller
    or block the in-memory record.
    """

    _DB_EVENT_TYPE = "kill_switch_event"

    def __init__(self, db_factory=None, max_history: int = 200):
        self._db = db_factory
        self._lock = threading.Lock()
        self._history: deque = deque(maxlen=max_history)

    def record(self, event: KillEvent) -> KillEvent:
        with self._lock:
            self._history.append(event)
        self._persist(event)
        return event

    def history(self) -> list:
        with self._lock:
            return list(self._history)

    def _persist(self, event: KillEvent) -> None:
        if self._db is None:
            return
        try:
            from backend.database.models import AuditLog
            sess = self._db()
            try:
                sess.add(AuditLog(
                    event_type=self._DB_EVENT_TYPE,
                    actor=event.actor,
                    detail=json.dumps({
                        "trigger_type": event.trigger_type.value if event.trigger_type else None,
                        "severity": event.severity.value,
                        "from_state": event.from_state.value,
                        "to_state": event.to_state.value,
                        "message": event.message,
                        "detail": dict(event.detail),
                        "timestamp": event.timestamp.isoformat(),
                    }, ensure_ascii=False, default=str),
                ))
                sess.commit()
            except Exception:
                try:
                    sess.rollback()
                except Exception:
                    pass
                raise
            finally:
                sess.close()
        except Exception as e:
            logger.warning("KillEvent 감사 로그 실패: %s", e)


# ═════════════════════════════════════════════════════════════════════════════
# 5. Recovery Flow
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RecoveryOutcome:
    approved: bool
    new_state: Optional[TradingState]
    reason: str


class RecoveryManager:
    """Manual-resume gate only — never mutates trading state itself (KillSwitch
    performs the actual transition+log+notify on approval, keeping every state
    mutation in one auditable place — 'do not silently mutate trading state').

    Two fail-closed checks, evaluated in order, first failure wins:
      1. cooldown   — now - halted_at must exceed cooldown_seconds
                      (skipped when halted_at is None — e.g. resuming from
                      WARNING, which never records a halt timestamp)
      2. validation — every injected `() -> (bool, str)` check must pass;
                      duck-typed so this module never imports broker/DB/
                      reconciliation code to perform them
    """

    def __init__(self, cooldown_seconds: float = 300.0,
                 validation_checks: Optional[Sequence[Callable[[], Any]]] = None):
        self._cooldown = cooldown_seconds
        self._checks = list(validation_checks or [])

    def evaluate_resume(self, halted_at: Optional[datetime], requested_by: str,
                        _now: Optional[datetime] = None) -> RecoveryOutcome:
        now = _now or datetime.now(timezone.utc)

        if halted_at is not None:
            elapsed = (now - halted_at).total_seconds()
            if elapsed < self._cooldown:
                remaining = self._cooldown - elapsed
                return RecoveryOutcome(False, None,
                                       f"cooldown 활성 — {remaining:.0f}초 남음")

        for check in self._checks:
            try:
                ok, detail = check()
            except Exception as e:
                return RecoveryOutcome(False, None, f"검증 오류: {e}")
            if not ok:
                return RecoveryOutcome(False, None, f"검증 실패: {detail}")

        return RecoveryOutcome(True, TradingState.RUNNING,
                               f"재개 승인 (요청자={requested_by})")


# ═════════════════════════════════════════════════════════════════════════════
# 6. Notification Hook
# ═════════════════════════════════════════════════════════════════════════════

NotifierCallable = Callable[[Severity, str, Mapping[str, Any]], None]


class NotificationHook:
    """Extensible multi-channel notification dispatcher — register() lets a
    caller plug in Telegram / Discord / Push / anything else as a plain
    callable `(severity, message, detail) -> None`. Each dispatch is
    independently try/except-isolated so one broken channel can never block
    another or raise into KillSwitch (mirrors AlertSystem._notify fail-safety,
    backend/worker/watchdog.py:471-476).

    No default channels are wired — tests never reach Telegram; a future
    wiring task registers e.g. `bot.notifier.alert_emergency`.
    """

    def __init__(self, notifiers: Optional[Iterable[NotifierCallable]] = None):
        self._lock = threading.Lock()
        self._notifiers: list = list(notifiers or [])

    def register(self, notifier: NotifierCallable) -> None:
        with self._lock:
            self._notifiers.append(notifier)

    def dispatch(self, severity: Severity, message: str,
                 detail: Optional[Mapping[str, Any]] = None) -> None:
        with self._lock:
            notifiers = list(self._notifiers)
        d = detail or {}
        for notifier in notifiers:
            try:
                notifier(severity, message, d)
            except Exception as e:
                logger.warning("KillSwitch 알림 채널 실패 (다른 채널은 계속 진행): %s", e)


# ═════════════════════════════════════════════════════════════════════════════
# Orchestrator: KillSwitch
# ═════════════════════════════════════════════════════════════════════════════

class KillSwitch:
    """Thread-safe synchronous trading-state gate.

    Unlike Watchdog (which polls health on an interval), KillSwitch is a
    push-driven reactive gate: callers report events the instant they're
    observed (`report_broker_failure(...)`, `report_loss_breach(...)`, ...)
    and the resulting state is immediate — never delayed up to a poll cycle.
    This matters because a halt that arrives late is a halt that arrives
    after damage is already done.

    State only ESCALATES automatically (RUNNING -> WARNING -> HALTED) in
    response to triggers; de-escalation back to RUNNING always requires an
    explicit `resume()` call — the concrete mechanism behind "do not
    silently mutate trading state".

    `db_factory`, if given, is used for two purposes against the SAME
    canonical `DailyRiskState` row that LossTracker/WorkerWatchdog already
    read and write (no schema change, no parallel/conflicting flag):
      - read-only restore of the initial state at construction time
        (mirrors StartupRecovery._step_risk, backend/worker/recovery.py:137-152)
      - write-through on HALTED-entry / RUNNING-resume, using the exact
        "sess.get / create-if-missing / set-only-if-not-already-set" pattern
        WorkerWatchdog._alert_dead_worker already established
        (backend/worker/heartbeat.py:139-149) — first-reason-wins, never
        clobbers an existing halt reason from another subsystem.
    """

    def __init__(self, *,
                 trigger_engine: Optional[KillTriggerEngine] = None,
                 interceptor: Optional[OrderInterceptor] = None,
                 reason_log: Optional[KillReasonLog] = None,
                 recovery_manager: Optional[RecoveryManager] = None,
                 notification_hook: Optional[NotificationHook] = None,
                 db_factory=None,
                 worker_id: str = "",
                 _now: Optional[datetime] = None):
        self._trigger_engine = trigger_engine or KillTriggerEngine()
        self._interceptor = interceptor or OrderInterceptor()
        self._reason_log = reason_log or KillReasonLog(db_factory=db_factory)
        self._recovery = recovery_manager or RecoveryManager()
        self._notify = notification_hook or NotificationHook()
        self._db = db_factory
        self._worker_id = worker_id

        self._lock = threading.Lock()
        self._state, self._halted_at = self._load_initial_state(_now or datetime.now(timezone.utc))

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def state(self) -> TradingState:
        with self._lock:
            return self._state

    @property
    def halted_at(self) -> Optional[datetime]:
        with self._lock:
            return self._halted_at

    def history(self) -> list:
        return self._reason_log.history()

    def _load_initial_state(self, now: datetime):
        """Read-only restore from DailyRiskState — mirrors
        StartupRecovery._step_risk. Never writes; a missing/unreadable row
        simply starts the switch RUNNING (fail-open at construction —
        the trigger engine will re-detect any real ongoing problem)."""
        if self._db is None:
            return TradingState.RUNNING, None
        try:
            from datetime import date
            from backend.database.models import DailyRiskState
            sess = self._db()
            try:
                row = sess.get(DailyRiskState, date.today())
                if row is not None and row.kill_switch:
                    return TradingState.HALTED, now
            finally:
                sess.close()
        except Exception as e:
            logger.warning("KillSwitch: 초기 상태 복원 실패 (RUNNING으로 시작): %s", e)
        return TradingState.RUNNING, None

    # ── trigger reporting ────────────────────────────────────────────────────

    def report_broker_failure(self, consecutive_failures: int,
                              detail: Optional[Mapping[str, Any]] = None,
                              _now: Optional[datetime] = None) -> Optional[KillEvent]:
        trigger = self._trigger_engine.check_broker_failure(consecutive_failures, detail)
        return self._handle_trigger(trigger, _now)

    def report_consecutive_errors(self, error_count: int,
                                  detail: Optional[Mapping[str, Any]] = None,
                                  _now: Optional[datetime] = None) -> Optional[KillEvent]:
        trigger = self._trigger_engine.check_consecutive_errors(error_count, detail)
        return self._handle_trigger(trigger, _now)

    def report_reconciliation_mismatch(self, mismatch_count: int,
                                       detail: Optional[Mapping[str, Any]] = None,
                                       _now: Optional[datetime] = None) -> Optional[KillEvent]:
        trigger = self._trigger_engine.check_reconciliation_mismatch(mismatch_count, detail)
        return self._handle_trigger(trigger, _now)

    def report_loss_breach(self, daily_pnl_pct: float, mdd_pct: float,
                           detail: Optional[Mapping[str, Any]] = None,
                           _now: Optional[datetime] = None) -> Optional[KillEvent]:
        trigger = self._trigger_engine.check_loss_breach(daily_pnl_pct, mdd_pct, detail)
        return self._handle_trigger(trigger, _now)

    def report_watchdog_failure(self, severity: Any, message: str,
                                detail: Optional[Mapping[str, Any]] = None,
                                _now: Optional[datetime] = None) -> Optional[KillEvent]:
        trigger = self._trigger_engine.check_watchdog_failure(severity, message, detail)
        return self._handle_trigger(trigger, _now)

    def _handle_trigger(self, trigger: Optional[KillTrigger],
                        _now: Optional[datetime]) -> Optional[KillEvent]:
        if trigger is None:
            return None
        now = _now or datetime.now(timezone.utc)
        target_state = _SEVERITY_TO_STATE[trigger.severity]

        with self._lock:
            current = self._state
            to_state = target_state if _STATE_RANK[target_state] > _STATE_RANK[current] else current
            transitioned = to_state is not current
            if transitioned:
                self._state = to_state
                if to_state is TradingState.HALTED:
                    self._halted_at = now

        event = KillEvent(
            timestamp=now,
            trigger_type=trigger.trigger_type,
            severity=trigger.severity,
            from_state=current,
            to_state=to_state,
            message=trigger.message,
            detail=trigger.detail,
            actor="kill_switch",
        )
        # Every fired trigger is logged — full audit trail regardless of
        # whether it actually changed the state (operators reconstructing an
        # incident need the complete signal history, not just the moments
        # the state changed).
        self._reason_log.record(event)

        if transitioned:
            if to_state is TradingState.HALTED:
                self._write_halt_to_db(trigger.message, now)
            # Notification is dispatched ONLY on actual transitions — a
            # deliberate debounce that avoids the alert-storm gap discovered
            # live while building Watchdog (docs/WATCHDOG_SYSTEM.md Risk #1):
            # log everything, but only notify humans when something changed.
            self._notify.dispatch(trigger.severity, event.message, event.detail)

        return event

    def _write_halt_to_db(self, reason: str, now: datetime) -> None:
        """Writes DailyRiskState.kill_switch=True using the EXACT
        sess.get / create-if-missing / set-only-if-not-already-set pattern
        WorkerWatchdog._alert_dead_worker uses (heartbeat.py:139-149) —
        first-reason-wins, so a later trigger never clobbers an earlier
        subsystem's halt reason. Failure is logged and swallowed; the
        in-memory state transition has already happened and is authoritative
        for this process regardless of DB outcome (fail-open persistence,
        same philosophy as HeartbeatRegistry._mirror)."""
        if self._db is None:
            return
        try:
            from datetime import date
            from backend.database.models import DailyRiskState
            sess = self._db()
            try:
                today = date.today()
                row = sess.get(DailyRiskState, today)
                if row is None:
                    row = DailyRiskState(trade_date=today)
                    sess.add(row)
                if not row.kill_switch:
                    row.kill_switch = True
                    row.kill_reason = reason
                    sess.commit()
                else:
                    sess.rollback()
            except Exception:
                try:
                    sess.rollback()
                except Exception:
                    pass
                raise
            finally:
                sess.close()
        except Exception as e:
            logger.warning("KillSwitch: DB kill_switch 기록 실패: %s", e)

    def _clear_halt_in_db(self, now: datetime) -> None:
        if self._db is None:
            return
        try:
            from datetime import date
            from backend.database.models import DailyRiskState
            sess = self._db()
            try:
                today = date.today()
                row = sess.get(DailyRiskState, today)
                if row is not None and row.kill_switch:
                    row.kill_switch = False
                    row.kill_reason = None
                    sess.commit()
                else:
                    sess.rollback()
            except Exception:
                try:
                    sess.rollback()
                except Exception:
                    pass
                raise
            finally:
                sess.close()
        except Exception as e:
            logger.warning("KillSwitch: DB kill_switch 해제 실패: %s", e)

    # ── order interception ───────────────────────────────────────────────────

    def check_order(self, intent: OrderIntent) -> InterceptOutcome:
        return self._interceptor.evaluate(self.state, intent)

    # ── recovery ─────────────────────────────────────────────────────────────

    def resume(self, requested_by: str, _now: Optional[datetime] = None) -> RecoveryOutcome:
        now = _now or datetime.now(timezone.utc)
        with self._lock:
            current = self._state
            halted_at = self._halted_at

        outcome = self._recovery.evaluate_resume(halted_at, requested_by, _now=now)

        # Always log the attempt — approved or denied — mirrors
        # RecoveryExecutor "always audits": an operator's resume attempt is
        # operationally significant regardless of outcome (e.g. "operator
        # tried to resume, denied: cooldown 120s remaining").
        if outcome.approved:
            with self._lock:
                self._state = TradingState.RUNNING
                self._halted_at = None
            event = KillEvent(
                timestamp=now, trigger_type=None, severity=Severity.WARNING,
                from_state=current, to_state=TradingState.RUNNING,
                message=f"수동 재개 승인: {outcome.reason}",
                detail={"requested_by": requested_by}, actor=requested_by,
            )
            self._reason_log.record(event)
            self._clear_halt_in_db(now)
            self._notify.dispatch(Severity.WARNING, event.message, event.detail)
        else:
            event = KillEvent(
                timestamp=now, trigger_type=None, severity=Severity.WARNING,
                from_state=current, to_state=current,
                message=f"수동 재개 거부: {outcome.reason}",
                detail={"requested_by": requested_by}, actor=requested_by,
            )
            self._reason_log.record(event)

        return outcome
