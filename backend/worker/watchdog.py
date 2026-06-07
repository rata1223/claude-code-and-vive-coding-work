"""
Worker watchdog — in-process health monitoring, dead-component detection,
and graduated recovery recommendations.

Standalone library: it does not import or modify runner.py / scheduler.py /
order_poller.py / strategy code, and has no side effects on them. All signal
sources (threads, poller health, Redis client) are dependency-injected as
duck-typed callables/objects so the module is independently testable with
plain fakes — no real threads, Redis, or DB required.

Wiring this into the running worker process — instrumenting loops to call
HeartbeatRegistry.record(), passing thread/poller providers, and connecting
RecoveryExecutor callbacks to existing safe primitives (`strategy:stop`
publish, `DailyRiskState.kill_switch`) — is a deliberate follow-up task.
See docs/WATCHDOG_SYSTEM.md.
"""
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Heartbeat Registry
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class HeartbeatRecord:
    worker_id: str
    strategy_id: str   # "" = process-level / non-strategy component
    timestamp: datetime
    status: str        # "alive" | "stopped" | "error"


class HeartbeatRegistry:
    """Thread-safe in-memory heartbeat registry, keyed by (worker_id, strategy_id).

    The in-memory dict is the source of truth — always available, no external
    dependency. Redis mirroring is best-effort observability only: every call
    is wrapped in try/except so a Redis outage never blocks recording or
    reading (same fail-open philosophy as WorkerHeartbeat._beat / IdempotencyStore).
    """

    _KEY_PREFIX = "watchdog:hb"

    def __init__(self, redis_client=None, ttl_seconds: int = 120):
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], HeartbeatRecord] = {}
        self._redis = redis_client
        self._ttl = ttl_seconds

    def record(self, worker_id: str, strategy_id: str = "", status: str = "alive",
               _now: Optional[datetime] = None) -> HeartbeatRecord:
        now = _now or datetime.now(timezone.utc)
        rec = HeartbeatRecord(worker_id=worker_id, strategy_id=strategy_id,
                              timestamp=now, status=status)
        with self._lock:
            self._records[(worker_id, strategy_id)] = rec
        self._mirror(rec)
        return rec

    def get(self, worker_id: str, strategy_id: str = "") -> Optional[HeartbeatRecord]:
        with self._lock:
            return self._records.get((worker_id, strategy_id))

    def all(self) -> list[HeartbeatRecord]:
        with self._lock:
            return list(self._records.values())

    def age_seconds(self, worker_id: str, strategy_id: str = "",
                    _now: Optional[datetime] = None) -> Optional[float]:
        rec = self.get(worker_id, strategy_id)
        if rec is None:
            return None
        now = _now or datetime.now(timezone.utc)
        return (now - rec.timestamp).total_seconds()

    def _mirror(self, rec: HeartbeatRecord) -> None:
        if self._redis is None:
            return
        try:
            key = f"{self._KEY_PREFIX}:{rec.worker_id}:{rec.strategy_id or '_proc'}"
            value = f"{rec.status}:{rec.timestamp.isoformat()}"
            self._redis.setex(key, self._ttl, value)
        except Exception as e:
            logger.debug("HeartbeatRegistry: Redis 미러링 실패 (무시): %s", e)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Health Monitor
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ComponentCheck:
    name: str
    alive: bool
    heartbeat_age_sec: Optional[float] = None
    detail: str = ""


@dataclass(frozen=True)
class HealthSnapshot:
    worker_id: str
    checked_at: datetime
    heartbeat_age_sec: Optional[float]
    redis_connected: Optional[bool]    # None = Redis not configured (not monitored)
    components: tuple[ComponentCheck, ...] = ()


class HealthMonitor:
    """Assembles a point-in-time HealthSnapshot from injected, duck-typed probes.

    Every probe runs in its own try/except so one failing signal can never
    mask the others — a raising thread_provider surfaces as its own
    ComponentCheck(alive=False, detail="probe error: ..."), not a crash.

    Signal sources are injected as plain callables/objects (never imported
    from runner.py/order_poller.py directly) so this stays standalone-testable:
      thread_provider()         -> {component_name: threading.Thread-like}
      poller_health_provider()  -> Optional[bool]   (duck-typed: PollingHealth.is_healthy)
      redis_client              -> object with .ping()
    """

    def __init__(self, worker_id: str, registry: HeartbeatRegistry,
                 thread_provider: Optional[Callable[[], dict]] = None,
                 poller_health_provider: Optional[Callable[[], Optional[bool]]] = None,
                 redis_client=None):
        self._worker_id = worker_id
        self._registry = registry
        self._thread_provider = thread_provider
        self._poller_health_provider = poller_health_provider
        self._redis = redis_client

    def check(self, _now: Optional[datetime] = None) -> HealthSnapshot:
        now = _now or datetime.now(timezone.utc)
        components: list[ComponentCheck] = []
        components.extend(self._check_threads(_now=now))

        poller_check = self._check_poller()
        if poller_check is not None:
            components.append(poller_check)

        return HealthSnapshot(
            worker_id=self._worker_id,
            checked_at=now,
            heartbeat_age_sec=self._age_seconds(self._worker_id, "", _now=now),
            redis_connected=self._check_redis(),
            components=tuple(components),
        )

    # ── Internal probes — each isolated so one failure can't hide the rest ──

    def _age_seconds(self, worker_id: str, strategy_id: str, _now: datetime) -> Optional[float]:
        """registry.age_seconds wrapped so a raising registry can never crash
        snapshot assembly — degrades to "age unknown" (None), not a crash."""
        try:
            return self._registry.age_seconds(worker_id, strategy_id, _now=_now)
        except Exception as e:
            logger.debug("HealthMonitor: heartbeat age 조회 실패 (%s/%s): %s", worker_id, strategy_id, e)
            return None

    def _check_threads(self, _now: datetime) -> list[ComponentCheck]:
        if self._thread_provider is None:
            return []
        try:
            threads = self._thread_provider() or {}
        except Exception as e:
            return [ComponentCheck(name="threads", alive=False, detail=f"probe error: {e}")]

        checks = []
        for name, thread in threads.items():
            try:
                alive = bool(thread is not None and thread.is_alive())
            except Exception as e:
                checks.append(ComponentCheck(name=name, alive=False, detail=f"probe error: {e}"))
                continue
            age = self._age_seconds(self._worker_id, name, _now=_now)
            checks.append(ComponentCheck(
                name=name, alive=alive, heartbeat_age_sec=age,
                detail="" if alive else "thread not alive",
            ))
        return checks

    def _check_poller(self) -> Optional[ComponentCheck]:
        if self._poller_health_provider is None:
            return None
        try:
            healthy = self._poller_health_provider()
        except Exception as e:
            return ComponentCheck(name="poller", alive=False, detail=f"probe error: {e}")
        if healthy is None:
            return None
        return ComponentCheck(name="poller", alive=bool(healthy),
                              detail="" if healthy else "poller unhealthy")

    def _check_redis(self) -> Optional[bool]:
        if self._redis is None:
            return None
        try:
            return bool(self._redis.ping())
        except Exception:
            return False


# ═════════════════════════════════════════════════════════════════════════════
# 3. Dead Worker Detector
# ═════════════════════════════════════════════════════════════════════════════

class Severity(Enum):
    WARNING = "warning"
    CRITICAL = "critical"


_SEVERITY_RANK = {Severity.WARNING: 1, Severity.CRITICAL: 2}


@dataclass(frozen=True)
class Finding:
    severity: Severity
    kind: str          # "heartbeat_missing" | "heartbeat_aging" | "thread_dead" |
                       # "thread_hung" | "redis_disconnected"
    component: str     # component / strategy thread name; "" for worker-wide
    message: str


@dataclass(frozen=True)
class Detection:
    severity: Severity
    findings: tuple[Finding, ...]
    snapshot: HealthSnapshot

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f.message for f in self.findings)


class DeadWorkerDetector:
    """Evaluates a HealthSnapshot against configurable thresholds.

    Returns the highest-severity Detection bundling every triggered finding,
    or None when everything looks healthy. Distinguishes:
      - "thread_dead"  : thread.is_alive() == False              (worker/thread/process killed)
      - "thread_hung"  : is_alive() == True but heartbeat stale  (infinite loop / deadlock)
      - "heartbeat_*"  : process-level heartbeat aging/missing
      - "redis_disconnected" : Redis ping failed (WARNING — fail-open, not fatal)
    """

    def __init__(self, heartbeat_warning_sec: float = 120.0,
                 heartbeat_critical_sec: float = 300.0,
                 thread_hang_sec: float = 180.0):
        self._warning_sec = heartbeat_warning_sec
        self._critical_sec = heartbeat_critical_sec
        self._hang_sec = thread_hang_sec

    def evaluate(self, snapshot: HealthSnapshot) -> Optional[Detection]:
        findings: list[Finding] = []

        age = snapshot.heartbeat_age_sec
        if age is not None:
            if age > self._critical_sec:
                findings.append(Finding(Severity.CRITICAL, "heartbeat_missing", snapshot.worker_id,
                                        f"heartbeat missing ({age:.0f}s old)"))
            elif age > self._warning_sec:
                findings.append(Finding(Severity.WARNING, "heartbeat_aging", snapshot.worker_id,
                                        f"heartbeat aging ({age:.0f}s old)"))

        for comp in snapshot.components:
            if not comp.alive:
                findings.append(Finding(Severity.CRITICAL, "thread_dead", comp.name,
                                        f"thread dead: {comp.name}"))
            elif comp.heartbeat_age_sec is not None and comp.heartbeat_age_sec > self._hang_sec:
                findings.append(Finding(
                    Severity.CRITICAL, "thread_hung", comp.name,
                    f"thread hung: {comp.name} ({comp.heartbeat_age_sec:.0f}s no progress)",
                ))

        if snapshot.redis_connected is False:
            findings.append(Finding(Severity.WARNING, "redis_disconnected", "",
                                    "redis disconnected"))

        if not findings:
            return None
        severity = max((f.severity for f in findings), key=lambda s: _SEVERITY_RANK[s])
        return Detection(severity=severity, findings=tuple(findings), snapshot=snapshot)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Recovery Action
# ═════════════════════════════════════════════════════════════════════════════

class RecoveryActionType(Enum):
    RESTART_WORKER = "restart_worker"
    DISABLE_STRATEGY = "disable_strategy"
    EMERGENCY_STOP = "emergency_stop"   # halt NEW trading (kill-switch); never flattens positions


@dataclass(frozen=True)
class RecoveryAction:
    action_type: RecoveryActionType
    target: str       # worker_id or strategy thread name; "" for worker-wide
    reason: str
    requested_at: datetime


class RecoveryExecutor:
    """Dispatches recovery recommendations to optionally-injected callbacks.

    This class never touches broker/execution/position code — it cannot place,
    cancel, or modify orders or positions, and never auto-trades.

      Allowed : restart worker · disable strategy · emergency stop (halt new trading)
      Never   : reissue orders · modify positions · auto-trade · flatten positions

    Default mode (no callback wired for an action type) is "recommend-only":
    log CRITICAL + write an append-only AuditLog row that an operator — or a
    future wiring task — can act on. Injected callbacks let a caller opt into
    real execution, e.g. wiring DISABLE_STRATEGY to the existing `strategy:stop`
    Redis publish (runner.py `_handle_stop`), or EMERGENCY_STOP to
    `DailyRiskState.kill_switch` — the exact DB write
    `WorkerWatchdog._alert_dead_worker` already performs in heartbeat.py.
    """

    _DB_EVENT_PREFIX = "watchdog_recovery_"

    def __init__(self, db_factory=None,
                 on_restart_worker: Optional[Callable[[RecoveryAction], None]] = None,
                 on_disable_strategy: Optional[Callable[[RecoveryAction], None]] = None,
                 on_emergency_stop: Optional[Callable[[RecoveryAction], None]] = None):
        self._db = db_factory
        self._callbacks = {
            RecoveryActionType.RESTART_WORKER: on_restart_worker,
            RecoveryActionType.DISABLE_STRATEGY: on_disable_strategy,
            RecoveryActionType.EMERGENCY_STOP: on_emergency_stop,
        }

    def execute(self, action: RecoveryAction) -> bool:
        """Always audits. Calls the matching callback if provided (never raises).
        Returns True iff a callback ran without error — i.e. recovery actually happened."""
        self._audit(action)
        callback = self._callbacks.get(action.action_type)
        if callback is None:
            logger.critical(
                "RecoveryAction 추천 (콜백 미설정 — 운영자 조치 필요): %s target=%r reason=%s",
                action.action_type.value, action.target, action.reason,
            )
            return False
        try:
            callback(action)
            return True
        except Exception as e:
            logger.error("RecoveryAction 콜백 실행 실패 %s: %s", action.action_type.value, e)
            return False

    def _audit(self, action: RecoveryAction) -> None:
        if self._db is None:
            return
        try:
            from backend.database.models import AuditLog
            sess = self._db()
            try:
                sess.add(AuditLog(
                    event_type=f"{self._DB_EVENT_PREFIX}{action.action_type.value}",
                    actor="watchdog",
                    detail=json.dumps({
                        "target": action.target,
                        "reason": action.reason,
                        "requested_at": action.requested_at.isoformat(),
                    }, ensure_ascii=False),
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
            logger.warning("RecoveryAction 감사 로그 실패: %s", e)


# ═════════════════════════════════════════════════════════════════════════════
# 5. Alert System
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Alert:
    severity: Severity
    message: str
    error: Optional[str] = None
    stacktrace: Optional[str] = None
    last_heartbeat: Optional[datetime] = None
    recorded_at: Optional[datetime] = None


class AlertSystem:
    """Records structured alerts (error / stacktrace / last_heartbeat).

    Three layers, each independent of the others:
      - bounded in-memory ring buffer (deque)  — always available, inspectable in tests
      - append-only AuditLog persistence       — survives process restarts
      - optional notifier callback              — kept injectable so tests never reach Telegram;
                                                   a real caller would wire severity -> alert_error/alert_emergency
    """

    _DB_EVENT_TYPE = "watchdog_alert"

    def __init__(self, db_factory=None,
                 notifier: Optional[Callable[[Severity, str], None]] = None,
                 max_history: int = 200):
        self._db = db_factory
        self._notifier = notifier
        self._lock = threading.Lock()
        self._history: deque[Alert] = deque(maxlen=max_history)

    def record(self, severity: Severity, message: str, error: Optional[str] = None,
               stacktrace: Optional[str] = None, last_heartbeat: Optional[datetime] = None,
               _now: Optional[datetime] = None) -> Alert:
        alert = Alert(
            severity=severity, message=message, error=error, stacktrace=stacktrace,
            last_heartbeat=last_heartbeat, recorded_at=_now or datetime.now(timezone.utc),
        )
        with self._lock:
            self._history.append(alert)
        self._persist(alert)
        self._notify(alert)
        return alert

    def history(self) -> list[Alert]:
        with self._lock:
            return list(self._history)

    def _persist(self, alert: Alert) -> None:
        if self._db is None:
            return
        try:
            from backend.database.models import AuditLog
            sess = self._db()
            try:
                sess.add(AuditLog(
                    event_type=self._DB_EVENT_TYPE,
                    actor="watchdog",
                    detail=json.dumps({
                        "severity": alert.severity.value,
                        "message": alert.message,
                        "error": alert.error,
                        "stacktrace": alert.stacktrace,
                        "last_heartbeat": alert.last_heartbeat.isoformat() if alert.last_heartbeat else None,
                    }, ensure_ascii=False),
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
            logger.warning("Alert 감사 로그 실패: %s", e)

    def _notify(self, alert: Alert) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier(alert.severity, alert.message)
        except Exception as e:
            logger.warning("Alert 알림 전송 실패: %s", e)


# ═════════════════════════════════════════════════════════════════════════════
# 6. Metrics
# ═════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class WatchdogMetricsSnapshot:
    started_at: datetime
    uptime_seconds: float
    heartbeat_interval_sec: float
    restart_count: int
    failure_count: int
    last_check_at: Optional[datetime]


class WatchdogMetrics:
    """Thread-safe mutable accumulator + immutable snapshot — same shape as
    PollingHealth/PollingHealthMonitor (backend/execution/order_poller.py)."""

    def __init__(self, heartbeat_interval_sec: float = 30.0,
                 _now: Optional[datetime] = None):
        self._lock = threading.Lock()
        self._started_at = _now or datetime.now(timezone.utc)
        self._heartbeat_interval = heartbeat_interval_sec
        self._restart_count = 0
        self._failure_count = 0
        self._last_check_at: Optional[datetime] = None

    def record_check(self, _now: Optional[datetime] = None) -> None:
        with self._lock:
            self._last_check_at = _now or datetime.now(timezone.utc)

    def record_restart(self) -> None:
        with self._lock:
            self._restart_count += 1

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1

    def snapshot(self, _now: Optional[datetime] = None) -> WatchdogMetricsSnapshot:
        now = _now or datetime.now(timezone.utc)
        with self._lock:
            return WatchdogMetricsSnapshot(
                started_at=self._started_at,
                uptime_seconds=(now - self._started_at).total_seconds(),
                heartbeat_interval_sec=self._heartbeat_interval,
                restart_count=self._restart_count,
                failure_count=self._failure_count,
                last_check_at=self._last_check_at,
            )


# ═════════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═════════════════════════════════════════════════════════════════════════════

class Watchdog:
    """Periodic in-process health-check loop tying every component together.

    Each tick: build a HealthSnapshot, evaluate it, and on a finding — record
    metrics, raise a structured alert, and (CRITICAL findings only) dispatch a
    graduated RecoveryAction. WARNING findings are alert-only and never trigger
    a recovery action — a graduated response that avoids overreacting to
    transient blips (a slow heartbeat, a Redis hiccup).

    Named `Watchdog` (not `WorkerWatchdog`) to avoid confusion with the
    existing cross-process `backend.worker.heartbeat.WorkerWatchdog`, which
    polls a single Redis heartbeat key from the API process. This one runs
    *inside* the worker and inspects its own threads/poller/Redis connection.
    """

    def __init__(self, worker_id: str,
                 registry: HeartbeatRegistry,
                 monitor: HealthMonitor,
                 detector: DeadWorkerDetector,
                 executor: RecoveryExecutor,
                 alerts: AlertSystem,
                 metrics: WatchdogMetrics,
                 check_interval_sec: float = 60.0,
                 strategy_thread_prefix: str = "strategy-"):
        self._worker_id = worker_id
        self._registry = registry
        self._monitor = monitor
        self._detector = detector
        self._executor = executor
        self._alerts = alerts
        self._metrics = metrics
        self._interval = check_interval_sec
        self._strategy_prefix = strategy_thread_prefix
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="watchdog-loop")
        self._thread.start()
        logger.info("Watchdog 시작 (interval=%.0fs)", self._interval)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # check_once() is intentionally left free to raise (so a direct call from
        # tests/debugging surfaces wiring bugs loudly) — but the daemon loop itself
        # must never die: a watchdog that silently stops watching is the worst
        # possible failure mode for a fail-safe component (mirrors WorkerHeartbeat._loop
        # / _beat, which fully swallows so the heartbeat thread never dies).
        while not self._stop.wait(self._interval):
            try:
                self.check_once()
            except Exception as e:
                logger.error("Watchdog 점검 루프 오류 (계속 감시): %s", e)

    def check_once(self, _now: Optional[datetime] = None) -> Optional[Detection]:
        """Runs a single check/evaluate/respond cycle. Exposed directly so
        tests can drive the watchdog deterministically without a real thread."""
        now = _now or datetime.now(timezone.utc)
        snapshot = self._monitor.check(_now=now)
        self._metrics.record_check(_now=now)

        detection = self._detector.evaluate(snapshot)
        if detection is None:
            return None

        self._metrics.record_failure()
        last_hb = self._registry.get(self._worker_id)
        self._alerts.record(
            severity=detection.severity,
            message="; ".join(detection.reasons),
            last_heartbeat=last_hb.timestamp if last_hb else None,
            _now=now,
        )

        if detection.severity is Severity.CRITICAL:
            for action in self._map_to_actions(detection, _now=now):
                executed = self._executor.execute(action)
                if executed and action.action_type is RecoveryActionType.RESTART_WORKER:
                    self._metrics.record_restart()

        return detection

    def _map_to_actions(self, detection: Detection, _now: datetime) -> list[RecoveryAction]:
        """Maps each CRITICAL finding to exactly one graduated RecoveryAction —
        structured (Finding.kind/component), never string-parsed from messages."""
        actions: list[RecoveryAction] = []
        for finding in detection.findings:
            if finding.severity is not Severity.CRITICAL:
                continue
            if finding.kind == "heartbeat_missing":
                actions.append(RecoveryAction(RecoveryActionType.RESTART_WORKER,
                                               finding.component or self._worker_id,
                                               finding.message, _now))
            elif finding.kind in ("thread_dead", "thread_hung"):
                target = finding.component[len(self._strategy_prefix):] \
                    if finding.component.startswith(self._strategy_prefix) else ""
                if target:
                    # Non-empty run_id after stripping the prefix — a genuine strategy thread.
                    actions.append(RecoveryAction(RecoveryActionType.DISABLE_STRATEGY,
                                                   target, finding.message, _now))
                else:
                    # Either a process-component thread (poller/scheduler), or a
                    # malformed strategy-thread name with no run_id suffix — never
                    # emit DISABLE_STRATEGY with an empty target (a future callback
                    # could misread "" as "disable every strategy").
                    actions.append(RecoveryAction(RecoveryActionType.EMERGENCY_STOP,
                                                   "", finding.message, _now))
        return actions
