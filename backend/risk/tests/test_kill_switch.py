"""
Tests for backend/risk/kill_switch.py — TASK 2-5B.

  Unit:      TestTradingState, TestKillTriggerEngine, TestOrderInterceptor,
             TestKillReasonLog, TestRecoveryManager, TestNotificationHook,
             TestKillSwitch
  Required:  TestBrokerTimeout, TestApiConsecutiveFailure, TestExcessiveLoss,
             TestReconciliationMismatch, TestWatchdogFailure,
             TestDuplicateOrderDetection, TestStaleData
  Recovery:  TestKillSwitchResumeFlow
"""
import threading
from datetime import datetime, timezone, timedelta
from enum import Enum

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.models import AuditLog, Base, DailyRiskState
from backend.risk.kill_switch import (
    InterceptDecision,
    InterceptOutcome,
    KillEvent,
    KillReasonLog,
    KillSwitch,
    KillTrigger,
    KillTriggerEngine,
    KillTriggerType,
    NotificationHook,
    OrderInterceptor,
    OrderIntent,
    RecoveryManager,
    RecoveryOutcome,
    Severity,
    TradingState,
    _STATE_RANK,
    _normalize_severity,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

_FIXED_NOW = datetime(2026, 6, 6, 9, 37, 0, tzinfo=timezone.utc)


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


def _get_risk_row(factory):
    from datetime import date
    sess = factory()
    try:
        return sess.get(DailyRiskState, date.today())
    finally:
        sess.close()


class FailingCommitSession:
    """Session stub whose commit() raises — records whether rollback/close ran,
    so tests can prove the established sess.rollback()-before-close() convention
    (heartbeat.py / reconciler.py / runner.py / persistence.py / emergency.py /
    risk/engine.py / watchdog.py) is honored on a failed write."""

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


class _ForeignSeverity(Enum):
    """Stand-in for an unrelated module's severity enum (e.g. watchdog.Severity)
    — same .value vocabulary, different class, proving duck-typed bridging."""
    WARNING = "warning"
    CRITICAL = "critical"


def _kill_switch(*, trigger_engine=None, interceptor=None, db_factory=None,
                 notifiers=None, cooldown_seconds=300.0, validation_checks=None,
                 max_history=200, _now=None):
    """Wires a fully-injected KillSwitch with controllable collaborators.
    Returns (kill_switch, reason_log, notify, recovery)."""
    reason_log = KillReasonLog(db_factory=db_factory, max_history=max_history)
    notify = NotificationHook(notifiers=notifiers)
    recovery = RecoveryManager(cooldown_seconds=cooldown_seconds, validation_checks=validation_checks)
    ks = KillSwitch(trigger_engine=trigger_engine, interceptor=interceptor,
                    reason_log=reason_log, recovery_manager=recovery,
                    notification_hook=notify, db_factory=db_factory, _now=_now)
    return ks, reason_log, notify, recovery


# ═════════════════════════════════════════════════════════════════════════════
# 1. Global Trading State
# ═════════════════════════════════════════════════════════════════════════════

class TestTradingState:
    def test_rank_ordering_running_lowest_halted_highest(self):
        assert _STATE_RANK[TradingState.RUNNING] < _STATE_RANK[TradingState.WARNING]
        assert _STATE_RANK[TradingState.WARNING] < _STATE_RANK[TradingState.HALTED]

    def test_three_distinct_states(self):
        assert {TradingState.RUNNING, TradingState.WARNING, TradingState.HALTED} == set(TradingState)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Kill Trigger Engine
# ═════════════════════════════════════════════════════════════════════════════

class TestKillTriggerEngine:
    def test_broker_failure_below_warning_returns_none(self):
        eng = KillTriggerEngine(broker_failure_warning=2, broker_failure_critical=5)
        assert eng.check_broker_failure(1) is None

    def test_broker_failure_at_warning_returns_warning(self):
        eng = KillTriggerEngine(broker_failure_warning=2, broker_failure_critical=5)
        trig = eng.check_broker_failure(2)
        assert trig is not None
        assert trig.severity is Severity.WARNING
        assert trig.trigger_type is KillTriggerType.BROKER_FAILURE

    def test_broker_failure_at_critical_returns_critical(self):
        eng = KillTriggerEngine(broker_failure_warning=2, broker_failure_critical=5)
        trig = eng.check_broker_failure(5)
        assert trig is not None
        assert trig.severity is Severity.CRITICAL

    def test_consecutive_errors_thresholds(self):
        eng = KillTriggerEngine(consecutive_errors_warning=3, consecutive_errors_critical=8)
        assert eng.check_consecutive_errors(2) is None
        assert eng.check_consecutive_errors(3).severity is Severity.WARNING
        assert eng.check_consecutive_errors(8).severity is Severity.CRITICAL

    def test_reconciliation_mismatch_thresholds(self):
        eng = KillTriggerEngine(reconciliation_mismatch_warning=1, reconciliation_mismatch_critical=3)
        assert eng.check_reconciliation_mismatch(0) is None
        assert eng.check_reconciliation_mismatch(1).severity is Severity.WARNING
        assert eng.check_reconciliation_mismatch(3).severity is Severity.CRITICAL

    def test_loss_breach_below_thresholds_returns_none(self):
        eng = KillTriggerEngine(daily_loss_critical_pct=0.03, mdd_critical_pct=0.15)
        assert eng.check_loss_breach(daily_pnl_pct=-0.01, mdd_pct=-0.05) is None

    def test_loss_breach_daily_warning(self):
        eng = KillTriggerEngine(daily_loss_critical_pct=0.03, daily_loss_warning_pct=0.024,
                                mdd_critical_pct=0.15, mdd_warning_pct=0.12)
        trig = eng.check_loss_breach(daily_pnl_pct=-0.025, mdd_pct=0.0)
        assert trig is not None and trig.severity is Severity.WARNING
        assert trig.trigger_type is KillTriggerType.LOSS_LIMIT_BREACH

    def test_loss_breach_daily_critical(self):
        eng = KillTriggerEngine(daily_loss_critical_pct=0.03)
        trig = eng.check_loss_breach(daily_pnl_pct=-0.04, mdd_pct=0.0)
        assert trig is not None and trig.severity is Severity.CRITICAL

    def test_loss_breach_mdd_critical(self):
        eng = KillTriggerEngine(mdd_critical_pct=0.15)
        trig = eng.check_loss_breach(daily_pnl_pct=0.0, mdd_pct=-0.20)
        assert trig is not None and trig.severity is Severity.CRITICAL
        assert "MDD" in trig.message

    def test_loss_breach_mdd_warning(self):
        eng = KillTriggerEngine(mdd_critical_pct=0.15, mdd_warning_pct=0.12,
                                daily_loss_critical_pct=0.03, daily_loss_warning_pct=0.024)
        trig = eng.check_loss_breach(daily_pnl_pct=0.0, mdd_pct=-0.13)
        assert trig is not None and trig.severity is Severity.WARNING

    def test_default_warning_thresholds_derive_from_critical_at_80_percent(self):
        eng = KillTriggerEngine(daily_loss_critical_pct=0.03, mdd_critical_pct=0.15)
        assert eng._daily_loss_warn == pytest.approx(0.024)
        assert eng._mdd_warn == pytest.approx(0.12)


class TestNormalizeSeverity:
    def test_accepts_native_severity(self):
        assert _normalize_severity(Severity.CRITICAL) is Severity.CRITICAL

    def test_accepts_foreign_enum_with_matching_value(self):
        assert _normalize_severity(_ForeignSeverity.WARNING) is Severity.WARNING

    def test_accepts_plain_string(self):
        assert _normalize_severity("critical") is Severity.CRITICAL
        assert _normalize_severity("WARNING") is Severity.WARNING

    def test_unknown_value_returns_none(self):
        assert _normalize_severity("info") is None
        assert _normalize_severity(12345) is None
        assert _normalize_severity(None) is None


class TestKillTriggerEngineWatchdogFailure:
    def test_normalizes_and_wraps_severity(self):
        eng = KillTriggerEngine()
        trig = eng.check_watchdog_failure(_ForeignSeverity.CRITICAL, "thread dead: strategy-7")
        assert trig is not None
        assert trig.severity is Severity.CRITICAL
        assert trig.trigger_type is KillTriggerType.WATCHDOG_FAILURE
        assert "strategy-7" in trig.message

    def test_unrecognized_severity_returns_none(self):
        eng = KillTriggerEngine()
        assert eng.check_watchdog_failure("info", "noise") is None


# ═════════════════════════════════════════════════════════════════════════════
# 3. Order Interceptor
# ═════════════════════════════════════════════════════════════════════════════

class TestOrderInterceptor:
    @pytest.mark.parametrize("state", [TradingState.RUNNING, TradingState.WARNING])
    @pytest.mark.parametrize("intent", list(OrderIntent))
    def test_running_and_warning_allow_everything(self, state, intent):
        interceptor = OrderInterceptor()
        outcome = interceptor.evaluate(state, intent)
        assert outcome.decision is InterceptDecision.ALLOW
        assert outcome.allowed is True

    def test_halted_blocks_new_orders(self):
        interceptor = OrderInterceptor()
        outcome = interceptor.evaluate(TradingState.HALTED, OrderIntent.NEW)
        assert outcome.decision is InterceptDecision.BLOCK

    def test_halted_blocks_amend(self):
        interceptor = OrderInterceptor()
        outcome = interceptor.evaluate(TradingState.HALTED, OrderIntent.AMEND)
        assert outcome.decision is InterceptDecision.BLOCK

    def test_halted_always_allows_cancel(self):
        interceptor = OrderInterceptor(allow_close_position_when_halted=False)
        outcome = interceptor.evaluate(TradingState.HALTED, OrderIntent.CANCEL)
        assert outcome.decision is InterceptDecision.ALLOW

    def test_halted_blocks_close_position_by_default(self):
        interceptor = OrderInterceptor()
        outcome = interceptor.evaluate(TradingState.HALTED, OrderIntent.CLOSE_POSITION)
        assert outcome.decision is InterceptDecision.BLOCK

    def test_halted_allows_close_position_when_configured(self):
        interceptor = OrderInterceptor(allow_close_position_when_halted=True)
        outcome = interceptor.evaluate(TradingState.HALTED, OrderIntent.CLOSE_POSITION)
        assert outcome.decision is InterceptDecision.ALLOW


# ═════════════════════════════════════════════════════════════════════════════
# 4. Kill Reason Logging
# ═════════════════════════════════════════════════════════════════════════════

def _event(trigger_type=KillTriggerType.BROKER_FAILURE, severity=Severity.CRITICAL,
           from_state=TradingState.RUNNING, to_state=TradingState.HALTED,
           message="test", detail=None, actor="kill_switch", _now=None):
    return KillEvent(timestamp=_now or _FIXED_NOW, trigger_type=trigger_type, severity=severity,
                     from_state=from_state, to_state=to_state, message=message,
                     detail=detail or {}, actor=actor)


class TestKillReasonLog:
    def test_record_appends_to_history(self):
        log = KillReasonLog()
        log.record(_event(message="first"))
        log.record(_event(message="second"))
        msgs = [e.message for e in log.history()]
        assert msgs == ["first", "second"]

    def test_history_returns_independent_copy(self):
        log = KillReasonLog()
        log.record(_event())
        snap = log.history()
        snap.append("mutated")
        assert len(log.history()) == 1

    def test_ring_buffer_is_bounded(self):
        log = KillReasonLog(max_history=3)
        for i in range(5):
            log.record(_event(message=f"e{i}"))
        msgs = [e.message for e in log.history()]
        assert msgs == ["e2", "e3", "e4"]

    def test_persists_to_audit_log(self):
        factory = _db()
        log = KillReasonLog(db_factory=factory)
        log.record(_event(message="halt triggered"))
        assert _count_audit(factory, "kill_switch_event") == 1

    def test_no_db_factory_does_not_raise(self):
        log = KillReasonLog(db_factory=None)
        log.record(_event())
        assert len(log.history()) == 1

    def test_db_failure_does_not_raise(self):
        from unittest.mock import MagicMock
        bad_factory = MagicMock(side_effect=RuntimeError("db down"))
        log = KillReasonLog(db_factory=bad_factory)
        log.record(_event())
        assert len(log.history()) == 1

    def test_persist_rolls_back_session_on_commit_failure(self):
        sess = FailingCommitSession()
        log = KillReasonLog(db_factory=lambda: sess)
        log.record(_event())
        assert sess.rolled_back is True
        assert sess.closed is True


# ═════════════════════════════════════════════════════════════════════════════
# 5. Recovery Flow
# ═════════════════════════════════════════════════════════════════════════════

class TestRecoveryManager:
    def test_denies_during_cooldown(self):
        mgr = RecoveryManager(cooldown_seconds=300.0)
        halted_at = _FIXED_NOW - timedelta(seconds=60)
        outcome = mgr.evaluate_resume(halted_at, "operator", _now=_FIXED_NOW)
        assert outcome.approved is False
        assert outcome.new_state is None
        assert "cooldown" in outcome.reason

    def test_approves_after_cooldown_elapses(self):
        mgr = RecoveryManager(cooldown_seconds=300.0)
        halted_at = _FIXED_NOW - timedelta(seconds=600)
        outcome = mgr.evaluate_resume(halted_at, "operator", _now=_FIXED_NOW)
        assert outcome.approved is True
        assert outcome.new_state is TradingState.RUNNING

    def test_halted_at_none_skips_cooldown_check(self):
        mgr = RecoveryManager(cooldown_seconds=999999.0)
        outcome = mgr.evaluate_resume(None, "operator", _now=_FIXED_NOW)
        assert outcome.approved is True

    def test_validation_failure_denies_with_fail_closed_default(self):
        mgr = RecoveryManager(cooldown_seconds=0.0,
                              validation_checks=[lambda: (False, "positions still open")])
        outcome = mgr.evaluate_resume(None, "operator", _now=_FIXED_NOW)
        assert outcome.approved is False
        assert "positions still open" in outcome.reason

    def test_validation_exception_denies(self):
        def boom():
            raise RuntimeError("probe failed")
        mgr = RecoveryManager(cooldown_seconds=0.0, validation_checks=[boom])
        outcome = mgr.evaluate_resume(None, "operator", _now=_FIXED_NOW)
        assert outcome.approved is False
        assert "probe failed" in outcome.reason

    def test_first_failing_check_wins(self):
        calls = []

        def first():
            calls.append("first")
            return False, "first failed"

        def second():
            calls.append("second")
            return True, "ok"

        mgr = RecoveryManager(cooldown_seconds=0.0, validation_checks=[first, second])
        outcome = mgr.evaluate_resume(None, "operator", _now=_FIXED_NOW)
        assert outcome.approved is False
        assert "first failed" in outcome.reason
        assert calls == ["first"]

    def test_all_checks_pass_approves(self):
        mgr = RecoveryManager(cooldown_seconds=0.0,
                              validation_checks=[lambda: (True, "ok"), lambda: (True, "ok")])
        outcome = mgr.evaluate_resume(None, "operator", _now=_FIXED_NOW)
        assert outcome.approved is True
        assert outcome.new_state is TradingState.RUNNING


# ═════════════════════════════════════════════════════════════════════════════
# 6. Notification Hook
# ═════════════════════════════════════════════════════════════════════════════

class TestNotificationHook:
    def test_dispatch_calls_all_registered_notifiers(self):
        seen_a, seen_b = [], []
        hook = NotificationHook(notifiers=[
            lambda sev, msg, det: seen_a.append((sev, msg, det)),
            lambda sev, msg, det: seen_b.append((sev, msg, det)),
        ])
        hook.dispatch(Severity.CRITICAL, "halt", {"k": "v"})
        assert seen_a == [(Severity.CRITICAL, "halt", {"k": "v"})]
        assert seen_b == [(Severity.CRITICAL, "halt", {"k": "v"})]

    def test_register_adds_a_channel(self):
        seen = []
        hook = NotificationHook()
        hook.register(lambda sev, msg, det: seen.append(msg))
        hook.dispatch(Severity.WARNING, "warn", {})
        assert seen == ["warn"]

    def test_one_failing_notifier_does_not_block_others(self):
        seen = []

        def broken(sev, msg, det):
            raise RuntimeError("telegram down")

        hook = NotificationHook(notifiers=[broken, lambda sev, msg, det: seen.append(msg)])
        hook.dispatch(Severity.CRITICAL, "halt", {})
        assert seen == ["halt"]

    def test_no_notifiers_does_not_raise(self):
        hook = NotificationHook()
        hook.dispatch(Severity.CRITICAL, "halt", {})  # must not raise


# ═════════════════════════════════════════════════════════════════════════════
# 7. KillSwitch orchestration
# ═════════════════════════════════════════════════════════════════════════════

class TestKillSwitch:
    def test_starts_running_with_no_db(self):
        ks, _, _, _ = _kill_switch(_now=_FIXED_NOW)
        assert ks.state is TradingState.RUNNING
        assert ks.halted_at is None

    def test_restores_halted_state_from_db(self):
        from datetime import date
        factory = _db()
        sess = factory()
        sess.add(DailyRiskState(trade_date=date.today(), kill_switch=True, kill_reason="이전 세션 중단"))
        sess.commit()
        sess.close()

        ks, _, _, _ = _kill_switch(db_factory=factory, _now=_FIXED_NOW)
        assert ks.state is TradingState.HALTED
        assert ks.halted_at is not None

    def test_restore_failure_starts_running(self):
        from unittest.mock import MagicMock
        bad_factory = MagicMock(side_effect=RuntimeError("db down"))
        ks, _, _, _ = _kill_switch(db_factory=bad_factory, _now=_FIXED_NOW)
        assert ks.state is TradingState.RUNNING

    def test_state_only_escalates_automatically_never_de_escalates(self):
        eng = KillTriggerEngine(consecutive_errors_warning=2, consecutive_errors_critical=10)
        ks, _, _, _ = _kill_switch(trigger_engine=eng, _now=_FIXED_NOW)
        ks.report_consecutive_errors(2, _now=_FIXED_NOW)   # -> WARNING
        assert ks.state is TradingState.WARNING
        ks.report_consecutive_errors(2, _now=_FIXED_NOW)   # still WARNING-level — must not go back to RUNNING
        assert ks.state is TradingState.WARNING

    def test_history_delegates_to_reason_log(self):
        eng = KillTriggerEngine(broker_failure_warning=1, broker_failure_critical=99)
        ks, log, _, _ = _kill_switch(trigger_engine=eng, _now=_FIXED_NOW)
        ks.report_broker_failure(1, _now=_FIXED_NOW)
        assert ks.history() == log.history()
        assert len(ks.history()) == 1

    def test_concurrent_reports_transition_exactly_once(self):
        eng = KillTriggerEngine(broker_failure_warning=1, broker_failure_critical=2)
        notified = []
        ks, log, notify, _ = _kill_switch(
            trigger_engine=eng,
            notifiers=[lambda sev, msg, det: notified.append((sev, msg))],
            _now=_FIXED_NOW,
        )
        barrier = threading.Barrier(8)

        def hammer():
            barrier.wait(timeout=5)
            ks.report_broker_failure(5, _now=_FIXED_NOW)  # always CRITICAL -> HALTED

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert ks.state is TradingState.HALTED
        # every fired trigger logged...
        assert len(log.history()) == 8
        # ...but the transition (and therefore the notification) happened exactly once
        assert len(notified) == 1
        assert sum(1 for e in log.history() if e.is_transition) == 1


# ═════════════════════════════════════════════════════════════════════════════
# Required scenario: broker timeout
# ═════════════════════════════════════════════════════════════════════════════

class TestBrokerTimeout:
    def test_consecutive_broker_failures_escalate_to_halt_and_block_new_orders(self):
        eng = KillTriggerEngine(broker_failure_warning=2, broker_failure_critical=5)
        ks, _, notified, _ = _kill_switch(trigger_engine=eng, _now=_FIXED_NOW)
        notify_seen = []
        notified.register(lambda sev, msg, det: notify_seen.append(sev))

        ev1 = ks.report_broker_failure(2, _now=_FIXED_NOW)
        assert ev1.to_state is TradingState.WARNING
        assert ks.state is TradingState.WARNING
        assert ks.check_order(OrderIntent.NEW).allowed is True   # WARNING does not block

        ev2 = ks.report_broker_failure(5, _now=_FIXED_NOW)
        assert ev2.to_state is TradingState.HALTED
        assert ks.state is TradingState.HALTED
        assert ks.check_order(OrderIntent.NEW).decision is InterceptDecision.BLOCK
        assert notify_seen == [Severity.WARNING, Severity.CRITICAL]


# ═════════════════════════════════════════════════════════════════════════════
# Required scenario: API consecutive failure
# ═════════════════════════════════════════════════════════════════════════════

class TestApiConsecutiveFailure:
    def test_consecutive_errors_escalate_through_warning_to_halt(self):
        eng = KillTriggerEngine(consecutive_errors_warning=3, consecutive_errors_critical=8)
        ks, _, _, _ = _kill_switch(trigger_engine=eng, _now=_FIXED_NOW)

        ks.report_consecutive_errors(1, _now=_FIXED_NOW)
        assert ks.state is TradingState.RUNNING

        ks.report_consecutive_errors(3, _now=_FIXED_NOW)
        assert ks.state is TradingState.WARNING

        ks.report_consecutive_errors(8, _now=_FIXED_NOW)
        assert ks.state is TradingState.HALTED


# ═════════════════════════════════════════════════════════════════════════════
# Required scenario: excessive loss
# ═════════════════════════════════════════════════════════════════════════════

class TestExcessiveLoss:
    def test_daily_loss_breach_halts_and_persists_to_daily_risk_state(self):
        factory = _db()
        eng = KillTriggerEngine(daily_loss_critical_pct=0.03, mdd_critical_pct=0.15)
        ks, _, _, _ = _kill_switch(trigger_engine=eng, db_factory=factory, _now=_FIXED_NOW)

        event = ks.report_loss_breach(daily_pnl_pct=-0.04, mdd_pct=-0.02, _now=_FIXED_NOW)
        assert event.to_state is TradingState.HALTED
        assert ks.state is TradingState.HALTED

        row = _get_risk_row(factory)
        assert row is not None
        assert row.kill_switch is True
        assert "손실" in row.kill_reason

    def test_mdd_breach_halts(self):
        eng = KillTriggerEngine(daily_loss_critical_pct=0.03, mdd_critical_pct=0.15)
        ks, _, _, _ = _kill_switch(trigger_engine=eng, _now=_FIXED_NOW)
        event = ks.report_loss_breach(daily_pnl_pct=0.0, mdd_pct=-0.20, _now=_FIXED_NOW)
        assert event.to_state is TradingState.HALTED

    def test_first_halt_reason_is_not_clobbered_by_a_later_trigger(self):
        """Mirrors WorkerWatchdog._alert_dead_worker's `if not row.kill_switch` guard —
        once halted, a later (different) trigger must not overwrite the original
        root-cause reason an operator needs for postmortem analysis."""
        factory = _db()
        eng = KillTriggerEngine(daily_loss_critical_pct=0.03, broker_failure_critical=2)
        ks, _, _, _ = _kill_switch(trigger_engine=eng, db_factory=factory, _now=_FIXED_NOW)

        ks.report_loss_breach(daily_pnl_pct=-0.05, mdd_pct=0.0, _now=_FIXED_NOW)
        first_reason = _get_risk_row(factory).kill_reason

        ks.report_broker_failure(10, _now=_FIXED_NOW)  # also CRITICAL, fires after halt
        assert _get_risk_row(factory).kill_reason == first_reason


# ═════════════════════════════════════════════════════════════════════════════
# Required scenario: reconciliation mismatch
# ═════════════════════════════════════════════════════════════════════════════

class TestReconciliationMismatch:
    def test_mismatch_count_escalates_warning_then_halt(self):
        eng = KillTriggerEngine(reconciliation_mismatch_warning=1, reconciliation_mismatch_critical=3)
        ks, _, _, _ = _kill_switch(trigger_engine=eng, _now=_FIXED_NOW)

        ev1 = ks.report_reconciliation_mismatch(1, detail={"symbol": "AAPL"}, _now=_FIXED_NOW)
        assert ev1.to_state is TradingState.WARNING
        assert ev1.detail.get("symbol") == "AAPL"

        ev2 = ks.report_reconciliation_mismatch(3, _now=_FIXED_NOW)
        assert ev2.to_state is TradingState.HALTED


# ═════════════════════════════════════════════════════════════════════════════
# Required scenario: watchdog failure
# ═════════════════════════════════════════════════════════════════════════════

class TestWatchdogFailure:
    def test_native_severity_drives_halt(self):
        ks, _, _, _ = _kill_switch(_now=_FIXED_NOW)
        event = ks.report_watchdog_failure(Severity.CRITICAL, "thread dead: strategy-7", _now=_FIXED_NOW)
        assert event is not None and event.to_state is TradingState.HALTED

    def test_foreign_enum_with_matching_value_bridges_correctly(self):
        """Proves composability with backend.worker.watchdog.Detection.severity
        (a different Severity class with the same WARNING/CRITICAL .value
        vocabulary) WITHOUT this module importing watchdog.py."""
        ks, _, _, _ = _kill_switch(_now=_FIXED_NOW)
        event = ks.report_watchdog_failure(_ForeignSeverity.WARNING, "heartbeat aging", _now=_FIXED_NOW)
        assert event is not None and event.to_state is TradingState.WARNING

    def test_plain_string_severity_bridges_correctly(self):
        ks, _, _, _ = _kill_switch(_now=_FIXED_NOW)
        event = ks.report_watchdog_failure("critical", "redis disconnected", _now=_FIXED_NOW)
        assert event is not None and event.to_state is TradingState.HALTED

    def test_unrecognized_severity_produces_no_event(self):
        ks, _, _, _ = _kill_switch(_now=_FIXED_NOW)
        event = ks.report_watchdog_failure("info", "fyi", _now=_FIXED_NOW)
        assert event is None
        assert ks.state is TradingState.RUNNING


# ═════════════════════════════════════════════════════════════════════════════
# Required scenario: duplicate order detection
# ═════════════════════════════════════════════════════════════════════════════

class TestDuplicateOrderDetection:
    def test_repeated_new_order_checks_while_halted_consistently_block(self):
        """The interceptor holds no per-order memory — repeated/duplicated NEW
        order check calls (e.g. a retry loop hammering the gate after a broker
        timeout) must return identically BLOCK every single time. No flip-flop,
        no "I already blocked this one so let the next one through" gap."""
        eng = KillTriggerEngine(broker_failure_critical=1)
        ks, _, _, _ = _kill_switch(trigger_engine=eng, _now=_FIXED_NOW)
        ks.report_broker_failure(1, _now=_FIXED_NOW)
        assert ks.state is TradingState.HALTED

        outcomes = [ks.check_order(OrderIntent.NEW) for _ in range(10)]
        assert all(o.decision is InterceptDecision.BLOCK for o in outcomes)
        # identical outcome shape every time — proves pure-function determinism
        assert len({o.reason for o in outcomes}) == 1

    def test_cancel_of_the_same_intent_remains_allowed_throughout(self):
        eng = KillTriggerEngine(broker_failure_critical=1)
        ks, _, _, _ = _kill_switch(trigger_engine=eng, _now=_FIXED_NOW)
        ks.report_broker_failure(1, _now=_FIXED_NOW)

        for _ in range(5):
            assert ks.check_order(OrderIntent.CANCEL).decision is InterceptDecision.ALLOW
            assert ks.check_order(OrderIntent.NEW).decision is InterceptDecision.BLOCK


# ═════════════════════════════════════════════════════════════════════════════
# Required scenario: stale data
# ═════════════════════════════════════════════════════════════════════════════

class TestStaleData:
    def test_stale_market_data_reported_via_watchdog_channel_drives_halt_and_blocks_orders(self):
        """The unified FreshnessGate (backend/data/freshness_gate.py) escalates
        CRITICAL staleness through the SAME generic report_watchdog_failure
        channel as any other watchdog-style detection — no bespoke 'stale data'
        trigger type needed, keeping the trigger engine's surface bounded."""
        ks, _, _, _ = _kill_switch(_now=_FIXED_NOW)

        warn = ks.report_watchdog_failure(Severity.WARNING,
                                           "OHLCV stale: AAPL 320s old", _now=_FIXED_NOW)
        assert warn.to_state is TradingState.WARNING
        assert ks.check_order(OrderIntent.NEW).allowed is True

        crit = ks.report_watchdog_failure(Severity.CRITICAL,
                                           "OHLCV stale: AAPL 900s old — feed dead",
                                           _now=_FIXED_NOW)
        assert crit.to_state is TradingState.HALTED
        assert ks.check_order(OrderIntent.NEW).decision is InterceptDecision.BLOCK
        assert ks.check_order(OrderIntent.CANCEL).decision is InterceptDecision.ALLOW


# ═════════════════════════════════════════════════════════════════════════════
# Recovery flow (end-to-end through KillSwitch.resume)
# ═════════════════════════════════════════════════════════════════════════════

class TestKillSwitchResumeFlow:
    def test_resume_denied_during_cooldown_logs_attempt_without_transition(self):
        eng = KillTriggerEngine(broker_failure_critical=1)
        ks, log, notify, _ = _kill_switch(trigger_engine=eng, cooldown_seconds=300.0, _now=_FIXED_NOW)
        ks.report_broker_failure(1, _now=_FIXED_NOW)
        before = len(log.history())

        outcome = ks.resume("operator", _now=_FIXED_NOW + timedelta(seconds=10))
        assert outcome.approved is False
        assert ks.state is TradingState.HALTED
        assert len(log.history()) == before + 1
        assert log.history()[-1].is_transition is False
        assert "거부" in log.history()[-1].message or "denied" in log.history()[-1].message.lower()

    def test_resume_denied_by_validation_keeps_halted(self):
        eng = KillTriggerEngine(broker_failure_critical=1)
        ks, _, _, _ = _kill_switch(trigger_engine=eng, cooldown_seconds=0.0,
                                   validation_checks=[lambda: (False, "포지션 미정리")],
                                   _now=_FIXED_NOW)
        ks.report_broker_failure(1, _now=_FIXED_NOW)

        outcome = ks.resume("operator", _now=_FIXED_NOW + timedelta(seconds=10))
        assert outcome.approved is False
        assert ks.state is TradingState.HALTED

    def test_resume_approved_transitions_clears_db_and_notifies(self):
        factory = _db()
        eng = KillTriggerEngine(broker_failure_critical=1)
        notified = []
        ks, log, notify, _ = _kill_switch(
            trigger_engine=eng, db_factory=factory, cooldown_seconds=0.0,
            validation_checks=[lambda: (True, "ok")],
            notifiers=[lambda sev, msg, det: notified.append((sev, msg))],
            _now=_FIXED_NOW,
        )
        ks.report_broker_failure(1, _now=_FIXED_NOW)
        assert _get_risk_row(factory).kill_switch is True

        outcome = ks.resume("operator", _now=_FIXED_NOW + timedelta(seconds=10))
        assert outcome.approved is True
        assert ks.state is TradingState.RUNNING
        assert ks.halted_at is None
        assert ks.check_order(OrderIntent.NEW).allowed is True

        row = _get_risk_row(factory)
        assert row.kill_switch is False
        assert row.kill_reason is None

        assert notified[-1][0] is Severity.WARNING
        assert log.history()[-1].is_transition is True
        assert log.history()[-1].to_state is TradingState.RUNNING

    def test_resume_always_logs_the_attempt_regardless_of_outcome(self):
        eng = KillTriggerEngine(broker_failure_critical=1)
        ks, log, _, _ = _kill_switch(trigger_engine=eng, cooldown_seconds=99999.0, _now=_FIXED_NOW)
        ks.report_broker_failure(1, _now=_FIXED_NOW)
        n_before = len(log.history())

        ks.resume("operator-a", _now=_FIXED_NOW)
        ks.resume("operator-b", _now=_FIXED_NOW)

        assert len(log.history()) == n_before + 2
        assert log.history()[-1].actor == "operator-b"
        assert log.history()[-2].actor == "operator-a"
