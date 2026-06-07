"""
Tests for backend/worker/watchdog.py — TASK 2-4A.

  Unit:      TestHeartbeatRegistry, TestHealthMonitor, TestDeadWorkerDetector,
             TestRecoveryExecutor, TestAlertSystem, TestWatchdogMetrics
  Required:  TestWorkerKill, TestThreadStop, TestRedisDisconnect, TestInfiniteLoop,
             TestSchedulerStop, TestPollerStop, TestProcessRestart
  Lifecycle: TestWatchdogLoop
"""
import threading
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.models import AuditLog, Base
from backend.worker.watchdog import (
    Alert,
    AlertSystem,
    ComponentCheck,
    DeadWorkerDetector,
    Detection,
    HealthMonitor,
    HealthSnapshot,
    HeartbeatRecord,
    HeartbeatRegistry,
    RecoveryAction,
    RecoveryActionType,
    RecoveryExecutor,
    Severity,
    Watchdog,
    WatchdogMetrics,
    WatchdogMetricsSnapshot,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

_FIXED_NOW = datetime(2026, 6, 6, 9, 37, 0, tzinfo=timezone.utc)
_WORKER_ID = "kis-worker"


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _count_audit(factory, event_type: str) -> int:
    sess = factory()
    try:
        return sess.query(AuditLog).filter(AuditLog.event_type == event_type).count()
    finally:
        sess.close()


class FakeRedis:
    """Thread-safe in-memory Redis stub — only the subset watchdog.py uses."""

    def __init__(self, alive: bool = True):
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()
        self._alive = alive

    def setex(self, key, ttl, value):
        with self._lock:
            self._data[key] = value

    def get(self, key):
        with self._lock:
            val = self._data.get(key)
            return val.encode() if isinstance(val, str) else val

    def ping(self):
        if not self._alive:
            raise ConnectionError("Redis down")
        return True


class BrokenRedis:
    """Every call raises — used to prove mirroring is best-effort."""

    def setex(self, *a, **kw):
        raise ConnectionError("Redis down")

    def get(self, *a, **kw):
        raise ConnectionError("Redis down")

    def ping(self):
        raise ConnectionError("Redis down")


class FakeThread:
    """Controllable stand-in for threading.Thread — only is_alive() is consulted."""

    def __init__(self, alive: bool = True):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive

    def kill(self) -> None:
        self._alive = False


class FailingCommitSession:
    """Session stub whose commit() raises — records whether rollback/close ran,
    so tests can prove the established sess.rollback()-before-close() convention
    (heartbeat.py / reconciler.py / runner.py / persistence.py / emergency.py /
    risk/engine.py) is honored on a failed write."""

    def __init__(self):
        self.rolled_back = False
        self.closed = False

    def add(self, *a, **kw):
        pass

    def commit(self):
        raise RuntimeError("commit failed")

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _snapshot(heartbeat_age=None, redis_connected=None, components=()) -> HealthSnapshot:
    return HealthSnapshot(worker_id=_WORKER_ID, checked_at=_FIXED_NOW,
                          heartbeat_age_sec=heartbeat_age, redis_connected=redis_connected,
                          components=tuple(components))


def _action(action_type=RecoveryActionType.EMERGENCY_STOP, target="") -> RecoveryAction:
    return RecoveryAction(action_type=action_type, target=target, reason="test",
                          requested_at=_FIXED_NOW)


def _watchdog(registry=None, threads=None, poller_healthy=None, redis_client=None,
              on_restart=None, on_disable=None, on_emergency=None,
              heartbeat_warning_sec=120, heartbeat_critical_sec=300, thread_hang_sec=180,
              db_factory=None, notifier=None, check_interval_sec=60):
    """Wires a fully-injected Watchdog. Returns (watchdog, registry, alerts, metrics)."""
    registry = registry if registry is not None else HeartbeatRegistry()
    monitor = HealthMonitor(
        _WORKER_ID, registry,
        thread_provider=(lambda: threads) if threads is not None else None,
        poller_health_provider=(lambda: poller_healthy) if poller_healthy is not None else None,
        redis_client=redis_client,
    )
    detector = DeadWorkerDetector(heartbeat_warning_sec=heartbeat_warning_sec,
                                  heartbeat_critical_sec=heartbeat_critical_sec,
                                  thread_hang_sec=thread_hang_sec)
    executor = RecoveryExecutor(db_factory=db_factory, on_restart_worker=on_restart,
                                on_disable_strategy=on_disable, on_emergency_stop=on_emergency)
    alerts = AlertSystem(db_factory=db_factory, notifier=notifier)
    metrics = WatchdogMetrics(_now=_FIXED_NOW)
    wd = Watchdog(_WORKER_ID, registry, monitor, detector, executor, alerts, metrics,
                  check_interval_sec=check_interval_sec)
    return wd, registry, alerts, metrics


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests — Heartbeat Registry
# ══════════════════════════════════════════════════════════════════════════════

class TestHeartbeatRegistry:

    def test_record_and_get(self):
        reg = HeartbeatRegistry()
        rec = reg.record(_WORKER_ID, "order-poller", status="alive", _now=_FIXED_NOW)
        assert isinstance(rec, HeartbeatRecord)
        assert rec.worker_id == _WORKER_ID
        assert rec.strategy_id == "order-poller"
        assert rec.status == "alive"
        assert reg.get(_WORKER_ID, "order-poller") == rec

    def test_unknown_component_returns_none(self):
        reg = HeartbeatRegistry()
        assert reg.get(_WORKER_ID, "nonexistent") is None
        assert reg.age_seconds(_WORKER_ID, "nonexistent") is None

    def test_age_seconds_computation(self):
        reg = HeartbeatRegistry()
        reg.record(_WORKER_ID, "", status="alive", _now=_FIXED_NOW)
        later = _FIXED_NOW + timedelta(seconds=45)
        assert reg.age_seconds(_WORKER_ID, "", _now=later) == pytest.approx(45.0)

    def test_all_returns_every_record(self):
        reg = HeartbeatRegistry()
        reg.record(_WORKER_ID, "", _now=_FIXED_NOW)
        reg.record(_WORKER_ID, "order-poller", _now=_FIXED_NOW)
        reg.record(_WORKER_ID, "strategy-1", _now=_FIXED_NOW)
        assert len(reg.all()) == 3

    def test_redis_mirror_best_effort_survives_outage(self):
        reg = HeartbeatRegistry(redis_client=BrokenRedis())
        rec = reg.record(_WORKER_ID, "order-poller", _now=_FIXED_NOW)  # must not raise
        assert reg.get(_WORKER_ID, "order-poller") == rec

    def test_redis_mirror_writes_setex_with_ttl(self):
        r = FakeRedis()
        reg = HeartbeatRegistry(redis_client=r, ttl_seconds=99)
        reg.record(_WORKER_ID, "order-poller", status="alive", _now=_FIXED_NOW)
        key = f"watchdog:hb:{_WORKER_ID}:order-poller"
        assert key in r._data
        assert r._data[key].startswith("alive:")


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests — Health Monitor
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthMonitor:

    def test_snapshot_includes_heartbeat_age(self):
        reg = HeartbeatRegistry()
        reg.record(_WORKER_ID, "", _now=_FIXED_NOW)
        mon = HealthMonitor(_WORKER_ID, reg)
        snap = mon.check(_now=_FIXED_NOW + timedelta(seconds=10))
        assert snap.heartbeat_age_sec == pytest.approx(10.0)

    def test_no_heartbeat_recorded_yields_none_age(self):
        mon = HealthMonitor(_WORKER_ID, HeartbeatRegistry())
        assert mon.check(_now=_FIXED_NOW).heartbeat_age_sec is None

    def test_thread_alive_and_dead_reported(self):
        reg = HeartbeatRegistry()
        threads = {"order-poller": FakeThread(alive=True), "strategy-1": FakeThread(alive=False)}
        mon = HealthMonitor(_WORKER_ID, reg, thread_provider=lambda: threads)
        snap = mon.check(_now=_FIXED_NOW)
        by_name = {c.name: c for c in snap.components}
        assert by_name["order-poller"].alive is True
        assert by_name["strategy-1"].alive is False
        assert "not alive" in by_name["strategy-1"].detail

    def test_thread_is_alive_raising_is_reported_individually(self):
        class BadThread:
            def is_alive(self):
                raise RuntimeError("thread inspection failed")

        reg = HeartbeatRegistry()
        threads = {"weird": BadThread(), "order-poller": FakeThread(alive=True)}
        mon = HealthMonitor(_WORKER_ID, reg, thread_provider=lambda: threads)
        snap = mon.check(_now=_FIXED_NOW)
        by_name = {c.name: c for c in snap.components}
        assert by_name["weird"].alive is False
        assert "probe error" in by_name["weird"].detail
        assert by_name["order-poller"].alive is True

    def test_failing_thread_provider_does_not_mask_other_probes(self):
        reg = HeartbeatRegistry()
        reg.record(_WORKER_ID, "", _now=_FIXED_NOW)

        def bad_provider():
            raise RuntimeError("boom")

        mon = HealthMonitor(_WORKER_ID, reg, thread_provider=bad_provider,
                            poller_health_provider=lambda: True, redis_client=FakeRedis())
        snap = mon.check(_now=_FIXED_NOW)
        assert snap.heartbeat_age_sec is not None
        names = [c.name for c in snap.components]
        assert "threads" in names   # the provider's own failure surfaces as a component
        assert "poller" in names
        assert snap.redis_connected is True

    def test_poller_health_provider_included_when_present(self):
        mon = HealthMonitor(_WORKER_ID, HeartbeatRegistry(), poller_health_provider=lambda: False)
        snap = mon.check(_now=_FIXED_NOW)
        poller = next(c for c in snap.components if c.name == "poller")
        assert poller.alive is False

    def test_poller_health_provider_returning_none_is_excluded(self):
        mon = HealthMonitor(_WORKER_ID, HeartbeatRegistry(), poller_health_provider=lambda: None)
        snap = mon.check(_now=_FIXED_NOW)
        assert all(c.name != "poller" for c in snap.components)

    def test_redis_ping_success_and_failure(self):
        reg = HeartbeatRegistry()
        ok = HealthMonitor(_WORKER_ID, reg, redis_client=FakeRedis(alive=True))
        bad = HealthMonitor(_WORKER_ID, reg, redis_client=FakeRedis(alive=False))
        assert ok.check(_now=_FIXED_NOW).redis_connected is True
        assert bad.check(_now=_FIXED_NOW).redis_connected is False

    def test_no_redis_client_reports_none_not_disconnected(self):
        mon = HealthMonitor(_WORKER_ID, HeartbeatRegistry())
        assert mon.check(_now=_FIXED_NOW).redis_connected is None

    def test_component_heartbeat_age_populated_from_registry(self):
        reg = HeartbeatRegistry()
        reg.record(_WORKER_ID, "strategy-1", _now=_FIXED_NOW)
        threads = {"strategy-1": FakeThread(alive=True)}
        mon = HealthMonitor(_WORKER_ID, reg, thread_provider=lambda: threads)
        snap = mon.check(_now=_FIXED_NOW + timedelta(seconds=30))
        comp = next(c for c in snap.components if c.name == "strategy-1")
        assert comp.heartbeat_age_sec == pytest.approx(30.0)


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests — Dead Worker Detector
# ══════════════════════════════════════════════════════════════════════════════

class TestDeadWorkerDetector:

    def test_healthy_snapshot_returns_none(self):
        det = DeadWorkerDetector()
        snap = _snapshot(heartbeat_age=5.0, redis_connected=True,
                         components=[ComponentCheck("order-poller", alive=True, heartbeat_age_sec=5.0)])
        assert det.evaluate(snap) is None

    def test_heartbeat_aging_is_warning(self):
        det = DeadWorkerDetector(heartbeat_warning_sec=100, heartbeat_critical_sec=300)
        d = det.evaluate(_snapshot(heartbeat_age=150.0))
        assert d.severity is Severity.WARNING
        assert any(f.kind == "heartbeat_aging" for f in d.findings)

    def test_heartbeat_missing_is_critical(self):
        det = DeadWorkerDetector(heartbeat_warning_sec=100, heartbeat_critical_sec=300)
        d = det.evaluate(_snapshot(heartbeat_age=400.0))
        assert d.severity is Severity.CRITICAL
        assert any(f.kind == "heartbeat_missing" for f in d.findings)

    def test_thread_dead_is_critical(self):
        det = DeadWorkerDetector()
        snap = _snapshot(heartbeat_age=5.0, components=[ComponentCheck("scheduler", alive=False)])
        d = det.evaluate(snap)
        assert d.severity is Severity.CRITICAL
        assert d.findings[0].kind == "thread_dead"
        assert d.findings[0].component == "scheduler"

    def test_alive_thread_with_stale_progress_heartbeat_is_hung(self):
        det = DeadWorkerDetector(thread_hang_sec=120)
        snap = _snapshot(heartbeat_age=5.0,
                         components=[ComponentCheck("strategy-3", alive=True, heartbeat_age_sec=200.0)])
        d = det.evaluate(snap)
        assert d.severity is Severity.CRITICAL
        assert d.findings[0].kind == "thread_hung"
        assert d.findings[0].component == "strategy-3"

    def test_alive_thread_with_fresh_heartbeat_is_healthy(self):
        det = DeadWorkerDetector(thread_hang_sec=120)
        snap = _snapshot(heartbeat_age=5.0,
                         components=[ComponentCheck("strategy-3", alive=True, heartbeat_age_sec=10.0)])
        assert det.evaluate(snap) is None

    def test_redis_disconnected_is_warning_not_critical(self):
        det = DeadWorkerDetector()
        d = det.evaluate(_snapshot(heartbeat_age=5.0, redis_connected=False))
        assert d.severity is Severity.WARNING
        assert d.findings[0].kind == "redis_disconnected"

    def test_redis_not_configured_is_not_flagged(self):
        det = DeadWorkerDetector()
        assert det.evaluate(_snapshot(heartbeat_age=5.0, redis_connected=None)) is None

    def test_mixed_findings_escalate_to_critical_and_bundle_reasons(self):
        det = DeadWorkerDetector()
        snap = _snapshot(heartbeat_age=5.0, redis_connected=False,
                         components=[ComponentCheck("scheduler", alive=False)])
        d = det.evaluate(snap)
        assert d.severity is Severity.CRITICAL   # highest severity wins
        assert {f.kind for f in d.findings} == {"redis_disconnected", "thread_dead"}
        assert len(d.reasons) == 2


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests — Recovery Action / Executor
# ══════════════════════════════════════════════════════════════════════════════

class TestRecoveryExecutor:

    def test_no_callback_recommends_only_and_returns_false(self):
        assert RecoveryExecutor().execute(_action()) is False

    def test_callback_invoked_returns_true(self):
        calls = []
        ex = RecoveryExecutor(on_emergency_stop=lambda a: calls.append(a))
        assert ex.execute(_action(RecoveryActionType.EMERGENCY_STOP)) is True
        assert len(calls) == 1
        assert calls[0].action_type is RecoveryActionType.EMERGENCY_STOP

    def test_callback_exception_does_not_raise_and_returns_false(self):
        def boom(a):
            raise RuntimeError("callback exploded")
        ex = RecoveryExecutor(on_restart_worker=boom)
        assert ex.execute(_action(RecoveryActionType.RESTART_WORKER)) is False

    def test_only_matching_action_type_callback_is_invoked(self):
        restart_calls, disable_calls = [], []
        ex = RecoveryExecutor(on_restart_worker=lambda a: restart_calls.append(a),
                              on_disable_strategy=lambda a: disable_calls.append(a))
        ex.execute(_action(RecoveryActionType.DISABLE_STRATEGY, target="7"))
        assert disable_calls and not restart_calls

    def test_audits_every_action_type(self):
        factory = _db()
        ex = RecoveryExecutor(db_factory=factory)
        for t in RecoveryActionType:
            ex.execute(_action(t))
        assert _count_audit(factory, "watchdog_recovery_restart_worker") == 1
        assert _count_audit(factory, "watchdog_recovery_disable_strategy") == 1
        assert _count_audit(factory, "watchdog_recovery_emergency_stop") == 1

    def test_audits_even_when_callback_is_wired(self):
        factory = _db()
        ex = RecoveryExecutor(db_factory=factory, on_emergency_stop=lambda a: None)
        ex.execute(_action(RecoveryActionType.EMERGENCY_STOP))
        assert _count_audit(factory, "watchdog_recovery_emergency_stop") == 1

    def test_no_db_factory_does_not_raise(self):
        ex = RecoveryExecutor(db_factory=None, on_emergency_stop=lambda a: None)
        assert ex.execute(_action()) is True

    def test_db_failure_does_not_raise(self):
        bad_factory = MagicMock(side_effect=RuntimeError("db down"))
        ex = RecoveryExecutor(db_factory=bad_factory, on_emergency_stop=lambda a: None)
        assert ex.execute(_action()) is True

    def test_audit_rolls_back_session_on_commit_failure(self):
        sess = FailingCommitSession()
        ex = RecoveryExecutor(db_factory=lambda: sess, on_emergency_stop=lambda a: None)
        assert ex.execute(_action()) is True
        assert sess.rolled_back is True
        assert sess.closed is True


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests — Alert System
# ══════════════════════════════════════════════════════════════════════════════

class TestAlertSystem:

    def test_record_returns_alert_with_all_fields(self):
        sys_ = AlertSystem()
        alert = sys_.record(Severity.CRITICAL, "worker dead", error="ConnectionError",
                            stacktrace="Traceback...", last_heartbeat=_FIXED_NOW, _now=_FIXED_NOW)
        assert isinstance(alert, Alert)
        assert alert.severity is Severity.CRITICAL
        assert alert.message == "worker dead"
        assert alert.error == "ConnectionError"
        assert alert.stacktrace == "Traceback..."
        assert alert.last_heartbeat == _FIXED_NOW
        assert alert.recorded_at == _FIXED_NOW

    def test_history_is_bounded_ring_buffer(self):
        sys_ = AlertSystem(max_history=3)
        for i in range(5):
            sys_.record(Severity.WARNING, f"alert-{i}", _now=_FIXED_NOW)
        hist = sys_.history()
        assert len(hist) == 3
        assert [a.message for a in hist] == ["alert-2", "alert-3", "alert-4"]

    def test_persists_to_audit_log(self):
        factory = _db()
        AlertSystem(db_factory=factory).record(Severity.CRITICAL, "scheduler dead", _now=_FIXED_NOW)
        assert _count_audit(factory, "watchdog_alert") == 1

    def test_notifier_invoked_with_severity_and_message(self):
        seen = []
        AlertSystem(notifier=lambda sev, msg: seen.append((sev, msg))).record(
            Severity.WARNING, "redis disconnected", _now=_FIXED_NOW)
        assert seen == [(Severity.WARNING, "redis disconnected")]

    def test_notifier_exception_does_not_raise(self):
        def boom(sev, msg):
            raise RuntimeError("telegram down")
        AlertSystem(notifier=boom).record(Severity.CRITICAL, "x", _now=_FIXED_NOW)

    def test_db_failure_does_not_raise(self):
        bad_factory = MagicMock(side_effect=RuntimeError("db down"))
        AlertSystem(db_factory=bad_factory).record(Severity.CRITICAL, "x", _now=_FIXED_NOW)

    def test_persist_rolls_back_session_on_commit_failure(self):
        sess = FailingCommitSession()
        AlertSystem(db_factory=lambda: sess).record(Severity.CRITICAL, "x", _now=_FIXED_NOW)
        assert sess.rolled_back is True
        assert sess.closed is True


# ══════════════════════════════════════════════════════════════════════════════
# Unit tests — Metrics
# ══════════════════════════════════════════════════════════════════════════════

class TestWatchdogMetrics:

    def test_uptime_and_interval_in_snapshot(self):
        m = WatchdogMetrics(heartbeat_interval_sec=30.0, _now=_FIXED_NOW)
        snap = m.snapshot(_now=_FIXED_NOW + timedelta(seconds=90))
        assert isinstance(snap, WatchdogMetricsSnapshot)
        assert snap.uptime_seconds == pytest.approx(90.0)
        assert snap.heartbeat_interval_sec == 30.0

    def test_restart_and_failure_counts_increment(self):
        m = WatchdogMetrics(_now=_FIXED_NOW)
        m.record_restart()
        m.record_restart()
        m.record_failure()
        snap = m.snapshot(_now=_FIXED_NOW)
        assert snap.restart_count == 2
        assert snap.failure_count == 1

    def test_record_check_updates_last_check_at(self):
        m = WatchdogMetrics(_now=_FIXED_NOW)
        assert m.snapshot(_now=_FIXED_NOW).last_check_at is None
        later = _FIXED_NOW + timedelta(seconds=10)
        m.record_check(_now=later)
        assert m.snapshot(_now=later).last_check_at == later

    def test_snapshot_is_an_independent_copy(self):
        m = WatchdogMetrics(_now=_FIXED_NOW)
        snap1 = m.snapshot(_now=_FIXED_NOW)
        m.record_restart()
        snap2 = m.snapshot(_now=_FIXED_NOW)
        assert snap1.restart_count == 0
        assert snap2.restart_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# Required scenario 1 — worker kill
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkerKill:

    def test_dead_heartbeat_triggers_critical_and_restart_recommendation(self):
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", status="alive", _now=_FIXED_NOW)

        restart_calls = []
        wd, _, alerts, metrics = _watchdog(registry=registry,
                                           on_restart=lambda a: restart_calls.append(a),
                                           heartbeat_warning_sec=60, heartbeat_critical_sec=120)

        # 5 minutes pass with no further heartbeat -> the worker process is presumed killed
        later = _FIXED_NOW + timedelta(minutes=5)
        detection = wd.check_once(_now=later)

        assert isinstance(detection, Detection)
        assert detection.severity is Severity.CRITICAL
        assert any(f.kind == "heartbeat_missing" for f in detection.findings)
        assert len(restart_calls) == 1
        assert restart_calls[0].action_type is RecoveryActionType.RESTART_WORKER
        assert restart_calls[0].target == _WORKER_ID
        assert metrics.snapshot(_now=later).restart_count == 1
        assert len(alerts.history()) == 1 and alerts.history()[0].severity is Severity.CRITICAL


# ══════════════════════════════════════════════════════════════════════════════
# Required scenario 2 — thread stop
# ══════════════════════════════════════════════════════════════════════════════

class TestThreadStop:

    def test_strategy_thread_death_triggers_disable_strategy(self):
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", _now=_FIXED_NOW)
        threads = {"strategy-7": FakeThread(alive=False), "order-poller": FakeThread(alive=True)}

        disable_calls = []
        wd, _, _, _ = _watchdog(registry=registry, threads=threads,
                                on_disable=lambda a: disable_calls.append(a))

        detection = wd.check_once(_now=_FIXED_NOW)
        assert detection is not None and detection.severity is Severity.CRITICAL
        assert any(f.kind == "thread_dead" and f.component == "strategy-7" for f in detection.findings)
        assert len(disable_calls) == 1
        assert disable_calls[0].action_type is RecoveryActionType.DISABLE_STRATEGY
        assert disable_calls[0].target == "7"   # "strategy-" prefix stripped to bare run_id

    def test_malformed_strategy_thread_name_with_no_run_id_routes_to_emergency_stop(self):
        """A thread literally named "strategy-" (no run_id suffix) must never produce
        DISABLE_STRATEGY(target="") — a wired callback could misread "" as "disable
        every strategy". Route it to EMERGENCY_STOP like any other process component."""
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", _now=_FIXED_NOW)
        threads = {"strategy-": FakeThread(alive=False)}

        disable_calls, emergency_calls = [], []
        wd, _, _, _ = _watchdog(registry=registry, threads=threads,
                                on_disable=lambda a: disable_calls.append(a),
                                on_emergency=lambda a: emergency_calls.append(a))

        detection = wd.check_once(_now=_FIXED_NOW)
        assert detection is not None and detection.severity is Severity.CRITICAL
        assert disable_calls == []
        assert len(emergency_calls) == 1
        assert emergency_calls[0].action_type is RecoveryActionType.EMERGENCY_STOP
        assert emergency_calls[0].target == ""


# ══════════════════════════════════════════════════════════════════════════════
# Required scenario 3 — redis disconnect
# ══════════════════════════════════════════════════════════════════════════════

class TestRedisDisconnect:

    def test_redis_ping_failure_is_warning_and_never_triggers_recovery(self):
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", _now=_FIXED_NOW)

        recovery_calls = []
        wd, _, alerts, _ = _watchdog(registry=registry, redis_client=FakeRedis(alive=False),
                                     on_emergency=lambda a: recovery_calls.append(a),
                                     on_restart=lambda a: recovery_calls.append(a),
                                     on_disable=lambda a: recovery_calls.append(a))

        detection = wd.check_once(_now=_FIXED_NOW)
        assert detection is not None
        assert detection.severity is Severity.WARNING
        assert any(f.kind == "redis_disconnected" for f in detection.findings)
        assert recovery_calls == []   # WARNING is alert-only — fail-open, no recovery dispatched
        assert len(alerts.history()) == 1
        assert alerts.history()[0].severity is Severity.WARNING


# ══════════════════════════════════════════════════════════════════════════════
# Required scenario 4 — infinite loop (alive thread, no progress)
# ══════════════════════════════════════════════════════════════════════════════

class TestInfiniteLoop:

    def test_alive_thread_with_stale_progress_heartbeat_is_flagged_hung(self):
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", _now=_FIXED_NOW)
        registry.record(_WORKER_ID, "strategy-3", status="alive", _now=_FIXED_NOW)
        threads = {"strategy-3": FakeThread(alive=True)}   # OS thread is alive — but stuck

        disable_calls = []
        wd, _, _, _ = _watchdog(registry=registry, threads=threads, thread_hang_sec=120,
                                on_disable=lambda a: disable_calls.append(a))

        later = _FIXED_NOW + timedelta(minutes=10)   # 600s with no progress beat > 120s threshold
        detection = wd.check_once(_now=later)

        assert detection is not None and detection.severity is Severity.CRITICAL
        hung = [f for f in detection.findings if f.kind == "thread_hung"]
        assert len(hung) == 1 and hung[0].component == "strategy-3"
        assert len(disable_calls) == 1 and disable_calls[0].target == "3"

    def test_alive_thread_with_fresh_progress_heartbeat_is_not_flagged(self):
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", _now=_FIXED_NOW)
        threads = {"strategy-3": FakeThread(alive=True)}
        wd, _, _, _ = _watchdog(registry=registry, threads=threads, thread_hang_sec=120)

        progress_at = _FIXED_NOW + timedelta(seconds=30)
        registry.record(_WORKER_ID, "strategy-3", status="alive", _now=progress_at)
        assert wd.check_once(_now=progress_at + timedelta(seconds=5)) is None


# ══════════════════════════════════════════════════════════════════════════════
# Required scenario 5 — scheduler stop
# ══════════════════════════════════════════════════════════════════════════════

class TestSchedulerStop:

    def test_scheduler_thread_death_triggers_emergency_stop(self):
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", _now=_FIXED_NOW)
        threads = {"scheduler": FakeThread(alive=False), "order-poller": FakeThread(alive=True)}

        emergency_calls = []
        wd, _, _, _ = _watchdog(registry=registry, threads=threads,
                                on_emergency=lambda a: emergency_calls.append(a))

        detection = wd.check_once(_now=_FIXED_NOW)
        assert detection is not None and detection.severity is Severity.CRITICAL
        assert any(f.kind == "thread_dead" and f.component == "scheduler" for f in detection.findings)
        assert len(emergency_calls) == 1
        assert emergency_calls[0].action_type is RecoveryActionType.EMERGENCY_STOP
        assert emergency_calls[0].target == ""   # worker-wide halt — not a specific strategy


# ══════════════════════════════════════════════════════════════════════════════
# Required scenario 6 — poller stop
# ══════════════════════════════════════════════════════════════════════════════

class TestPollerStop:

    def test_poller_unhealthy_triggers_emergency_stop(self):
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", _now=_FIXED_NOW)

        emergency_calls = []
        wd, _, _, _ = _watchdog(registry=registry, poller_healthy=False,
                                on_emergency=lambda a: emergency_calls.append(a))

        detection = wd.check_once(_now=_FIXED_NOW)
        assert detection is not None and detection.severity is Severity.CRITICAL
        assert any(f.kind == "thread_dead" and f.component == "poller" for f in detection.findings)
        assert len(emergency_calls) == 1
        assert emergency_calls[0].action_type is RecoveryActionType.EMERGENCY_STOP

    def test_poller_thread_death_is_also_detected_via_thread_provider(self):
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", _now=_FIXED_NOW)
        threads = {"order-poller": FakeThread(alive=False)}
        wd, _, _, _ = _watchdog(registry=registry, threads=threads)

        detection = wd.check_once(_now=_FIXED_NOW)
        assert detection is not None
        assert any(f.kind == "thread_dead" and f.component == "order-poller" for f in detection.findings)


# ══════════════════════════════════════════════════════════════════════════════
# Required scenario 7 — process restart
# ══════════════════════════════════════════════════════════════════════════════

class TestProcessRestart:

    def test_restart_count_only_increments_when_recovery_actually_executes(self):
        later = _FIXED_NOW + timedelta(minutes=5)

        # No restart callback wired -> RecoveryExecutor recommends only -> not counted
        reg_a = HeartbeatRegistry()
        reg_a.record(_WORKER_ID, "", _now=_FIXED_NOW)
        wd_a, _, _, metrics_a = _watchdog(registry=reg_a, heartbeat_warning_sec=60,
                                          heartbeat_critical_sec=120)
        wd_a.check_once(_now=later)
        assert metrics_a.snapshot(_now=later).restart_count == 0

        # Restart callback wired and succeeds -> counted exactly once
        reg_b = HeartbeatRegistry()
        reg_b.record(_WORKER_ID, "", _now=_FIXED_NOW)
        restart_calls = []
        wd_b, _, _, metrics_b = _watchdog(registry=reg_b, on_restart=lambda a: restart_calls.append(a),
                                          heartbeat_warning_sec=60, heartbeat_critical_sec=120)
        wd_b.check_once(_now=later)
        assert len(restart_calls) == 1
        assert metrics_b.snapshot(_now=later).restart_count == 1


# ══════════════════════════════════════════════════════════════════════════════
# Watchdog loop lifecycle
# ══════════════════════════════════════════════════════════════════════════════

class TestWatchdogLoop:

    def test_start_and_stop_lifecycle(self):
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", _now=datetime.now(timezone.utc))
        wd, _, _, _ = _watchdog(registry=registry, check_interval_sec=0.05)

        wd.start()
        try:
            time.sleep(0.2)
            assert wd._thread is not None
            assert wd._thread.is_alive()
        finally:
            wd.stop()
            wd._thread.join(timeout=2)
        assert not wd._thread.is_alive()

    def test_check_once_is_directly_callable_without_starting_a_thread(self):
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", _now=_FIXED_NOW)
        wd, _, _, _ = _watchdog(registry=registry)

        assert wd.check_once(_now=_FIXED_NOW) is None   # healthy snapshot -> no detection
        assert wd._thread is None                       # never started a loop thread

    def test_loop_survives_check_once_raising(self):
        """A watchdog whose entire job is to keep watching must never die from
        an unexpected collaborator exception — mirrors WorkerHeartbeat._loop's
        guarantee that _beat() failures never kill the heartbeat thread."""
        registry = HeartbeatRegistry()
        registry.record(_WORKER_ID, "", _now=datetime.now(timezone.utc))
        wd, _, _, _ = _watchdog(registry=registry, check_interval_sec=0.05)
        wd._monitor.check = MagicMock(side_effect=RuntimeError("boom"))

        wd.start()
        try:
            time.sleep(0.2)
            assert wd._thread.is_alive()          # survived >= one raising tick
            wd._monitor.check.assert_called()
        finally:
            wd.stop()
            wd._thread.join(timeout=2)
        assert not wd._thread.is_alive()
