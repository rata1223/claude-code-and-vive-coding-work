"""
Tests for backend/data/stale_detector.py (StaleDataDetectionService)

Covers all 5 components (FreshnessChecker, DataSourceHealthTracker,
StalenessClassifier, TradingGate, RecoveryHook) and the orchestrator,
including all 7 required scenarios:
  normal refresh, delayed data, update stop, websocket disconnect,
  polling stop, unknown source, threshold breach.
"""
from datetime import datetime, timedelta, timezone

import pytest
from backend.database.testing import make_test_engine
from sqlalchemy.orm import sessionmaker

from backend.data.stale_detector import (
    DataSourceHealthTracker,
    FeedStatus,
    FreshnessChecker,
    RecoveryHook,
    StaleDataDetectionService,
    StaleFeedError,
    StalenessClassifier,
    StalenessResult,
    StaleState,
    SourceHealth,
    TradingGate,
)
from backend.database.models import AuditLog, Base


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

_T0 = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)


def _sqlite_factory():
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _count_audit(factory, event_type: str) -> int:
    sess = factory()
    try:
        return sess.query(AuditLog).filter(AuditLog.event_type == event_type).count()
    finally:
        sess.close()


# ─────────────────────────────────────────────────────────────────────────────
# TestFreshnessChecker
# ─────────────────────────────────────────────────────────────────────────────

class TestFreshnessChecker:
    def test_no_last_update_is_unknown(self):
        checker = FreshnessChecker(300, 600)
        assert checker.classify(None, now=_T0) == StaleState.UNKNOWN

    def test_age_within_warn_is_fresh(self):
        checker = FreshnessChecker(300, 600)
        result = checker.classify(_T0, now=_T0 + timedelta(seconds=299))
        assert result == StaleState.FRESH

    def test_age_between_warn_and_stale_is_warning(self):
        checker = FreshnessChecker(300, 600)
        result = checker.classify(_T0, now=_T0 + timedelta(seconds=400))
        assert result == StaleState.WARNING

    def test_age_beyond_stale_is_stale(self):
        checker = FreshnessChecker(300, 600)
        result = checker.classify(_T0, now=_T0 + timedelta(seconds=601))
        assert result == StaleState.STALE

    def test_naive_datetimes_treated_as_utc(self):
        checker = FreshnessChecker(300, 600)
        naive_last_update = datetime(2026, 6, 1, 9, 30)
        naive_now = datetime(2026, 6, 1, 9, 35)
        assert checker.classify(naive_last_update, now=naive_now) == StaleState.FRESH

    def test_age_seconds_none_when_no_last_update(self):
        assert FreshnessChecker.age_seconds(None, now=_T0) is None

    def test_age_seconds_computes_difference(self):
        age = FreshnessChecker.age_seconds(_T0, now=_T0 + timedelta(seconds=42))
        assert age == 42.0


# ─────────────────────────────────────────────────────────────────────────────
# TestDataSourceHealthTracker
# ─────────────────────────────────────────────────────────────────────────────

class TestDataSourceHealthTracker:
    def test_unseen_key_returns_default_health(self):
        tracker = DataSourceHealthTracker()
        health = tracker.get("AAPL")
        assert health.status == FeedStatus.UNKNOWN
        assert health.last_update is None
        assert health.consecutive_failures == 0

    def test_record_update_marks_connected_and_resets_failures(self):
        tracker = DataSourceHealthTracker(failure_threshold=3)
        tracker.record_failure("AAPL")
        tracker.record_failure("AAPL")
        health = tracker.record_update("AAPL", ts=_T0)
        assert health.status == FeedStatus.CONNECTED
        assert health.last_update == _T0
        assert health.consecutive_failures == 0

    def test_failure_threshold_boundary(self):
        tracker = DataSourceHealthTracker(failure_threshold=3)
        tracker.record_failure("AAPL")
        below = tracker.record_failure("AAPL")
        assert below.consecutive_failures == 2
        assert below.status != FeedStatus.DISCONNECTED

        at = tracker.record_failure("AAPL")
        assert at.consecutive_failures == 3
        assert at.status == FeedStatus.DISCONNECTED

    def test_record_disconnect_marks_disconnected_immediately(self):
        tracker = DataSourceHealthTracker()
        tracker.record_update("AAPL", ts=_T0)
        health = tracker.record_disconnect("AAPL")
        assert health.status == FeedStatus.DISCONNECTED

    def test_reset_single_key(self):
        tracker = DataSourceHealthTracker()
        tracker.record_update("AAPL", ts=_T0)
        tracker.record_update("MSFT", ts=_T0)
        tracker.reset("AAPL")
        assert tracker.get("AAPL").status == FeedStatus.UNKNOWN
        assert tracker.get("MSFT").status == FeedStatus.CONNECTED

    def test_reset_all(self):
        tracker = DataSourceHealthTracker()
        tracker.record_update("AAPL", ts=_T0)
        tracker.record_update("MSFT", ts=_T0)
        tracker.reset()
        assert tracker.get("AAPL").status == FeedStatus.UNKNOWN
        assert tracker.get("MSFT").status == FeedStatus.UNKNOWN

    def test_get_returns_mutation_safe_copy(self):
        tracker = DataSourceHealthTracker()
        tracker.record_update("AAPL", ts=_T0)
        copy1 = tracker.get("AAPL")
        copy1.status = FeedStatus.DISCONNECTED
        copy1.consecutive_failures = 99
        assert tracker.get("AAPL").status == FeedStatus.CONNECTED
        assert tracker.get("AAPL").consecutive_failures == 0


# ─────────────────────────────────────────────────────────────────────────────
# TestStalenessClassifier
# ─────────────────────────────────────────────────────────────────────────────

class TestStalenessClassifier:
    def test_disconnected_is_stale_regardless_of_freshness(self):
        classifier = StalenessClassifier(FreshnessChecker(300, 600))
        health = SourceHealth(status=FeedStatus.DISCONNECTED, last_update=_T0)
        result = classifier.classify(health, now=_T0)
        assert result == StaleState.STALE

    def test_connected_recent_is_fresh(self):
        classifier = StalenessClassifier(FreshnessChecker(300, 600))
        health = SourceHealth(status=FeedStatus.CONNECTED, last_update=_T0)
        result = classifier.classify(health, now=_T0 + timedelta(seconds=10))
        assert result == StaleState.FRESH

    def test_connected_old_is_stale_via_freshness(self):
        classifier = StalenessClassifier(FreshnessChecker(300, 600))
        health = SourceHealth(status=FeedStatus.CONNECTED, last_update=_T0)
        result = classifier.classify(health, now=_T0 + timedelta(seconds=601))
        assert result == StaleState.STALE

    def test_unknown_status_no_last_update_is_unknown(self):
        classifier = StalenessClassifier(FreshnessChecker(300, 600))
        health = SourceHealth()
        result = classifier.classify(health, now=_T0)
        assert result == StaleState.UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# TestTradingGate
# ─────────────────────────────────────────────────────────────────────────────

class TestTradingGate:
    def _result(self, state, key="AAPL"):
        return StalenessResult(
            state=state, key=key, age_seconds=0.0, status=FeedStatus.CONNECTED,
            consecutive_failures=0, detail="x",
        )

    def test_fresh_is_not_blocking(self):
        gate = TradingGate()
        result = self._result(StaleState.FRESH)
        assert gate.is_blocking(result) is False
        assert gate.assert_fresh(result) is result

    def test_warning_is_not_blocking(self):
        gate = TradingGate()
        result = self._result(StaleState.WARNING)
        assert gate.is_blocking(result) is False
        assert gate.assert_fresh(result) is result

    def test_stale_is_blocking_and_raises(self):
        gate = TradingGate()
        result = self._result(StaleState.STALE)
        assert gate.is_blocking(result) is True
        with pytest.raises(StaleFeedError):
            gate.assert_fresh(result)

    def test_unknown_blocks_by_default(self):
        gate = TradingGate()
        result = self._result(StaleState.UNKNOWN)
        assert gate.is_blocking(result) is True
        with pytest.raises(StaleFeedError):
            gate.assert_fresh(result)

    def test_unknown_not_blocking_when_configured(self):
        gate = TradingGate(block_on_unknown=False)
        result = self._result(StaleState.UNKNOWN)
        assert gate.is_blocking(result) is False
        assert gate.assert_fresh(result) is result

    def test_stale_feed_error_is_not_runtime_error(self):
        gate = TradingGate()
        result = self._result(StaleState.STALE)
        try:
            gate.assert_fresh(result)
        except StaleFeedError as exc:
            assert not isinstance(exc, RuntimeError)
            assert exc.result is result
        else:
            pytest.fail("StaleFeedError was not raised")


# ─────────────────────────────────────────────────────────────────────────────
# TestRecoveryHook
# ─────────────────────────────────────────────────────────────────────────────

class TestRecoveryHook:
    def _result(self, key="AAPL"):
        return StalenessResult(
            state=StaleState.FRESH, key=key, age_seconds=0.0, status=FeedStatus.CONNECTED,
            consecutive_failures=0, detail="x",
        )

    def test_fire_calls_registered_callbacks(self):
        received = []
        hook = RecoveryHook()
        hook.register(lambda r: received.append(r.key))
        hook.register(lambda r: received.append(r.key.lower()))
        hook.fire(self._result("AAPL"))
        assert received == ["AAPL", "aapl"]

    def test_exception_in_one_callback_does_not_block_others(self):
        received = []

        def bad_callback(_result):
            raise ValueError("boom")

        hook = RecoveryHook()
        hook.register(bad_callback)
        hook.register(lambda r: received.append(r.key))
        hook.fire(self._result("AAPL"))
        assert received == ["AAPL"]


# ─────────────────────────────────────────────────────────────────────────────
# TestStaleDataDetectionService — required scenarios
# ─────────────────────────────────────────────────────────────────────────────

class TestStaleDataDetectionService:
    # --- normal refresh ---
    def test_normal_refresh_is_fresh(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        result = svc.check("AAPL", now=_T0 + timedelta(seconds=5))
        assert result.state == StaleState.FRESH

    # --- delayed data ---
    def test_delayed_data_is_stale(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        result = svc.check("AAPL", now=_T0 + timedelta(seconds=900))
        assert result.state == StaleState.STALE

    # --- update stop ---
    def test_update_stop_transitions_to_stale_over_time(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        assert svc.check("AAPL", now=_T0 + timedelta(seconds=60)).state == StaleState.FRESH
        result = svc.check("AAPL", now=_T0 + timedelta(seconds=601))
        assert result.state == StaleState.STALE

    # --- websocket disconnect ---
    def test_websocket_disconnect_is_immediately_stale(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        result = svc.record_disconnect("AAPL", now=_T0)
        assert result.state == StaleState.STALE
        assert result.status == FeedStatus.DISCONNECTED

    # --- polling stop ---
    def test_polling_stop_marks_disconnected_after_threshold(self):
        svc = StaleDataDetectionService(
            freshness_checker=FreshnessChecker(300, 600),
            health_tracker=DataSourceHealthTracker(failure_threshold=3),
        )
        svc.record_update("AAPL", ts=_T0, now=_T0)
        svc.record_failure("AAPL", now=_T0)
        svc.record_failure("AAPL", now=_T0)
        result = svc.record_failure("AAPL", now=_T0)
        assert result.state == StaleState.STALE
        assert result.status == FeedStatus.DISCONNECTED

    # --- unknown source ---
    def test_unknown_source_blocks_by_default(self):
        svc = StaleDataDetectionService()
        result = svc.check("NEVERSEEN", now=_T0)
        assert result.state == StaleState.UNKNOWN
        with pytest.raises(StaleFeedError):
            svc.assert_fresh("NEVERSEEN", now=_T0)

    def test_unknown_source_allowed_when_configured(self):
        svc = StaleDataDetectionService(gate=TradingGate(block_on_unknown=False))
        result = svc.assert_fresh("NEVERSEEN", now=_T0)
        assert result.state == StaleState.UNKNOWN

    # --- threshold breach (boundary) ---
    def test_threshold_breach_boundary(self):
        svc = StaleDataDetectionService(health_tracker=DataSourceHealthTracker(failure_threshold=3))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        svc.record_failure("AAPL", now=_T0)
        below = svc.record_failure("AAPL", now=_T0)
        assert below.status != FeedStatus.DISCONNECTED
        assert below.state != StaleState.STALE

        at = svc.record_failure("AAPL", now=_T0)
        assert at.status == FeedStatus.DISCONNECTED
        assert at.state == StaleState.STALE


# ─────────────────────────────────────────────────────────────────────────────
# TestStaleDataDetectionService — other coverage
# ─────────────────────────────────────────────────────────────────────────────

class TestStaleDataDetectionServiceOther:
    def test_assert_fresh_raises_on_stale(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        with pytest.raises(StaleFeedError):
            svc.assert_fresh("AAPL", now=_T0 + timedelta(seconds=601))

    def test_assert_fresh_returns_result_on_fresh(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        result = svc.assert_fresh("AAPL", now=_T0 + timedelta(seconds=5))
        assert result.state == StaleState.FRESH

    def test_assert_fresh_returns_result_on_warning(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        result = svc.assert_fresh("AAPL", now=_T0 + timedelta(seconds=400))
        assert result.state == StaleState.WARNING

    def test_recovery_hook_fires_on_transition_back_to_fresh(self):
        recovered = []
        hook = RecoveryHook()
        hook.register(lambda r: recovered.append(r.key))
        svc = StaleDataDetectionService(
            freshness_checker=FreshnessChecker(300, 600), recovery_hook=hook,
        )
        svc.record_update("AAPL", ts=_T0, now=_T0)
        svc.record_disconnect("AAPL", now=_T0)
        assert recovered == []
        svc.record_update("AAPL", ts=_T0 + timedelta(seconds=601), now=_T0 + timedelta(seconds=601))
        assert recovered == ["AAPL"]

    def test_reset_single_key(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        svc.record_update("MSFT", ts=_T0, now=_T0)
        svc.reset("AAPL")
        assert svc.check("AAPL", now=_T0).state == StaleState.UNKNOWN
        assert svc.check("MSFT", now=_T0).state == StaleState.FRESH

    def test_reset_all(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        svc.record_update("MSFT", ts=_T0, now=_T0)
        svc.reset()
        assert svc.check("AAPL", now=_T0).state == StaleState.UNKNOWN
        assert svc.check("MSFT", now=_T0).state == StaleState.UNKNOWN

    def test_multi_key_independence(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        svc.record_disconnect("MSFT", now=_T0)
        assert svc.check("AAPL", now=_T0).state == StaleState.FRESH
        assert svc.check("MSFT", now=_T0).state == StaleState.STALE

    def test_record_bar_reads_symbol_and_ts(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        bar = {"symbol": "AAPL", "ts": _T0, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
        result = svc.record_bar(bar, now=_T0 + timedelta(seconds=5))
        assert result.key == "AAPL"
        assert result.state == StaleState.FRESH


# ─────────────────────────────────────────────────────────────────────────────
# TestStaleDataDetectionServicePersistence — AuditLog integration
# ─────────────────────────────────────────────────────────────────────────────

class TestStaleDataDetectionServicePersistence:
    def test_warning_transition_persists_audit_log(self):
        factory = _sqlite_factory()
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600), db_factory=factory)
        svc.record_update("AAPL", ts=_T0, now=_T0)
        svc.check("AAPL", now=_T0 + timedelta(seconds=400))
        assert _count_audit(factory, "stale_data_warning") == 1

    def test_stale_transition_persists_audit_log(self):
        factory = _sqlite_factory()
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600), db_factory=factory)
        svc.record_update("AAPL", ts=_T0, now=_T0)
        svc.check("AAPL", now=_T0 + timedelta(seconds=601))
        assert _count_audit(factory, "stale_data_stale") == 1

    def test_unknown_transition_persists_audit_log(self):
        factory = _sqlite_factory()
        svc = StaleDataDetectionService(db_factory=factory)
        svc.check("NEVERSEEN", now=_T0)
        assert _count_audit(factory, "stale_data_unknown") == 1

    def test_recovery_persists_audit_log(self):
        factory = _sqlite_factory()
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600), db_factory=factory)
        svc.record_update("AAPL", ts=_T0, now=_T0)
        svc.record_disconnect("AAPL", now=_T0)
        svc.record_update("AAPL", ts=_T0 + timedelta(seconds=601), now=_T0 + timedelta(seconds=601))
        assert _count_audit(factory, "stale_data_recovered") == 1

    def test_fresh_does_not_persist(self):
        factory = _sqlite_factory()
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600), db_factory=factory)
        svc.record_update("AAPL", ts=_T0, now=_T0)
        svc.check("AAPL", now=_T0 + timedelta(seconds=5))
        assert _count_audit(factory, "stale_data_warning") == 0
        assert _count_audit(factory, "stale_data_stale") == 0
        assert _count_audit(factory, "stale_data_unknown") == 0
        assert _count_audit(factory, "stale_data_recovered") == 0

    def test_repeated_checks_in_same_state_do_not_duplicate_persistence(self):
        factory = _sqlite_factory()
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600), db_factory=factory)
        svc.record_update("AAPL", ts=_T0, now=_T0)
        svc.check("AAPL", now=_T0 + timedelta(seconds=601))
        svc.check("AAPL", now=_T0 + timedelta(seconds=602))
        svc.check("AAPL", now=_T0 + timedelta(seconds=603))
        assert _count_audit(factory, "stale_data_stale") == 1

    def test_no_db_factory_does_not_raise(self):
        svc = StaleDataDetectionService(freshness_checker=FreshnessChecker(300, 600))
        svc.record_update("AAPL", ts=_T0, now=_T0)
        result = svc.check("AAPL", now=_T0 + timedelta(seconds=601))
        assert result.state == StaleState.STALE
