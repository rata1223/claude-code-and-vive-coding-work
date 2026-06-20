"""
Tests for backend/data/validator.py (OHLCVValidationService)

Covers all 7 components (NullChecker, OHLCConsistencyChecker, VolumeChecker,
TimestampChecker, PriceSpikeDetector, MissingCandleDetector, ValidationClassifier)
and the orchestrator, including all 7 required scenarios:
  NaN data, negative price, negative volume, reversed timestamp,
  duplicate candle, extreme spike, missing candle.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from backend.database.testing import make_test_engine
from sqlalchemy.orm import sessionmaker

from backend.data.validator import (
    Candle,
    DEFAULT_SEVERITY,
    InvalidCandleError,
    MissingCandleDetector,
    NullChecker,
    OHLCConsistencyChecker,
    OHLCVValidationService,
    PriceSpikeDetector,
    TimestampChecker,
    ValidationClassifier,
    ValidationIssue,
    ValidationStatus,
    VolumeChecker,
)
from backend.database.models import AuditLog, Base


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

# 2026-06-01 is a Monday
_MON = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
_TUE = datetime(2026, 6, 2, 9, 30, tzinfo=timezone.utc)
_WED = datetime(2026, 6, 3, 9, 30, tzinfo=timezone.utc)
_FRI = datetime(2026, 6, 5, 9, 30, tzinfo=timezone.utc)
_NEXT_MON = datetime(2026, 6, 8, 9, 30, tzinfo=timezone.utc)


def _bar(symbol="AAPL", ts=_MON, open=100.0, high=105.0, low=99.0, close=102.0, volume=1000.0) -> dict:
    return {"symbol": symbol, "ts": ts, "open": open, "high": high,
            "low": low, "close": close, "volume": volume}


def _candle(**kwargs) -> Candle:
    return Candle.from_bar(_bar(**kwargs))


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
# TestCandle
# ─────────────────────────────────────────────────────────────────────────────

class TestCandle:
    def test_from_bar_round_trip(self):
        c = Candle.from_bar(_bar())
        assert c.symbol == "AAPL"
        assert c.ts == _MON
        assert c.open == 100.0
        assert c.high == 105.0
        assert c.low == 99.0
        assert c.close == 102.0
        assert c.volume == 1000.0

    def test_from_bar_missing_keys_become_none(self):
        c = Candle.from_bar({"symbol": "AAPL"})
        assert c.ts is None
        assert c.open is None
        assert c.volume is None


# ─────────────────────────────────────────────────────────────────────────────
# TestNullChecker — required scenario: NaN data
# ─────────────────────────────────────────────────────────────────────────────

class TestNullChecker:
    def test_nan_close_is_null_field(self):
        bar = _bar(close=float("nan"))
        assert NullChecker.check(bar) == [ValidationIssue.NULL_FIELD]

    def test_missing_volume_is_null_field(self):
        bar = _bar()
        del bar["volume"]
        assert NullChecker.check(bar) == [ValidationIssue.NULL_FIELD]

    def test_missing_ts_is_null_field(self):
        bar = _bar()
        bar["ts"] = None
        assert NullChecker.check(bar) == [ValidationIssue.NULL_FIELD]

    def test_valid_bar_has_no_issues(self):
        assert NullChecker.check(_bar()) == []


# ─────────────────────────────────────────────────────────────────────────────
# TestOHLCConsistencyChecker — required scenario: negative price
# ─────────────────────────────────────────────────────────────────────────────

class TestOHLCConsistencyChecker:
    def test_negative_price(self):
        c = _candle(open=-10.0)
        assert ValidationIssue.NEGATIVE_PRICE in OHLCConsistencyChecker.check(c)

    def test_high_less_than_low(self):
        c = _candle(high=90.0, low=99.0)
        assert ValidationIssue.OHLC_INCONSISTENT in OHLCConsistencyChecker.check(c)

    def test_high_less_than_close(self):
        c = _candle(high=100.0, open=99.5, low=99.0, close=102.0)
        assert ValidationIssue.OHLC_INCONSISTENT in OHLCConsistencyChecker.check(c)

    def test_low_greater_than_open(self):
        c = _candle(low=101.0, open=100.0, high=105.0, close=102.0)
        assert ValidationIssue.OHLC_INCONSISTENT in OHLCConsistencyChecker.check(c)

    def test_valid_candle_has_no_issues(self):
        assert OHLCConsistencyChecker.check(_candle()) == []


# ─────────────────────────────────────────────────────────────────────────────
# TestVolumeChecker — required scenario: negative volume
# ─────────────────────────────────────────────────────────────────────────────

class TestVolumeChecker:
    def test_negative_volume(self):
        c = _candle(volume=-100.0)
        assert VolumeChecker().check(c) == [ValidationIssue.NEGATIVE_VOLUME]

    def test_zero_volume_warns_by_default(self):
        c = _candle(volume=0.0)
        assert VolumeChecker().check(c) == [ValidationIssue.ZERO_VOLUME]

    def test_zero_volume_silent_when_disabled(self):
        c = _candle(volume=0.0)
        assert VolumeChecker(warn_on_zero=False).check(c) == []

    def test_positive_volume_has_no_issues(self):
        assert VolumeChecker().check(_candle()) == []


# ─────────────────────────────────────────────────────────────────────────────
# TestTimestampChecker — required scenarios: reversed timestamp, duplicate candle
# ─────────────────────────────────────────────────────────────────────────────

class TestTimestampChecker:
    def test_first_candle_has_no_issues(self):
        assert TimestampChecker.check(_candle(), None) == []

    def test_reversed_timestamp(self):
        prev = _candle(ts=_TUE)
        cur = _candle(ts=_MON)
        assert TimestampChecker.check(cur, prev) == [ValidationIssue.REVERSED_TIMESTAMP]

    def test_duplicate_identical_candle(self):
        prev = _candle(ts=_MON)
        cur = _candle(ts=_MON)
        assert TimestampChecker.check(cur, prev) == [ValidationIssue.DUPLICATE_TIMESTAMP]

    def test_duplicate_conflicting_candle(self):
        prev = _candle(ts=_MON, close=102.0)
        cur = _candle(ts=_MON, close=103.0)
        assert TimestampChecker.check(cur, prev) == [ValidationIssue.DUPLICATE_CONFLICT]

    def test_advancing_timestamp_has_no_issues(self):
        prev = _candle(ts=_MON)
        cur = _candle(ts=_TUE)
        assert TimestampChecker.check(cur, prev) == []


# ─────────────────────────────────────────────────────────────────────────────
# TestPriceSpikeDetector — required scenario: extreme spike
# ─────────────────────────────────────────────────────────────────────────────

class TestPriceSpikeDetector:
    def test_no_prev_candle(self):
        assert PriceSpikeDetector().check(_candle(), None) == []

    def test_prev_close_zero_avoids_division(self):
        prev = _candle(close=0.0)
        cur = _candle(close=100.0)
        assert PriceSpikeDetector().check(cur, prev) == []

    def test_small_move_has_no_issues(self):
        prev = _candle(close=100.0)
        cur = _candle(close=105.0)  # +5%
        assert PriceSpikeDetector().check(cur, prev) == []

    def test_moderate_spike_warns(self):
        prev = _candle(close=100.0)
        cur = _candle(close=115.0)  # +15%
        assert PriceSpikeDetector().check(cur, prev) == [ValidationIssue.PRICE_SPIKE]

    def test_extreme_spike_invalid(self):
        prev = _candle(close=100.0)
        cur = _candle(close=160.0)  # +60%
        assert PriceSpikeDetector().check(cur, prev) == [ValidationIssue.EXTREME_PRICE_SPIKE]


# ─────────────────────────────────────────────────────────────────────────────
# TestMissingCandleDetector — required scenario: missing candle
# ─────────────────────────────────────────────────────────────────────────────

class TestMissingCandleDetector:
    def test_no_prev_candle(self):
        assert MissingCandleDetector().check(_candle(), None, timedelta(days=1)) == []

    def test_weekday_gap_is_missing_candle(self):
        # Monday -> Wednesday skips Tuesday, a weekday
        prev = _candle(ts=_MON)
        cur = _candle(ts=_WED)
        assert MissingCandleDetector().check(cur, prev, timedelta(days=1)) == [ValidationIssue.MISSING_CANDLE]

    def test_weekend_gap_not_flagged(self):
        # Friday -> next Monday skips Sat/Sun only
        prev = _candle(ts=_FRI)
        cur = _candle(ts=_NEXT_MON)
        assert MissingCandleDetector().check(cur, prev, timedelta(days=1)) == []

    def test_consecutive_days_no_gap(self):
        prev = _candle(ts=_MON)
        cur = _candle(ts=_TUE)
        assert MissingCandleDetector().check(cur, prev, timedelta(days=1)) == []

    def test_injected_trading_day_check_treats_weekday_as_holiday(self):
        def is_trading_day(d: date) -> bool:
            if d == date(2026, 6, 2):  # Tuesday is a holiday
                return False
            return d.weekday() < 5

        prev = _candle(ts=_MON)
        cur = _candle(ts=_WED)
        detector = MissingCandleDetector(trading_day_check=is_trading_day)
        assert detector.check(cur, prev, timedelta(days=1)) == []

    def test_intraday_gap_exceeds_tolerance(self):
        prev = _candle(ts=datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc))
        cur = _candle(ts=datetime(2026, 6, 1, 9, 33, tzinfo=timezone.utc))  # 3 min gap
        detector = MissingCandleDetector()
        assert detector.check(cur, prev, timedelta(minutes=1)) == [ValidationIssue.MISSING_CANDLE]

    def test_intraday_gap_within_tolerance(self):
        prev = _candle(ts=datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc))
        cur = _candle(ts=datetime(2026, 6, 1, 9, 31, 30, tzinfo=timezone.utc))  # 1.5 min gap
        detector = MissingCandleDetector()
        assert detector.check(cur, prev, timedelta(minutes=1)) == []


# ─────────────────────────────────────────────────────────────────────────────
# TestValidationClassifier
# ─────────────────────────────────────────────────────────────────────────────

class TestValidationClassifier:
    def test_no_issues_is_valid(self):
        assert ValidationClassifier().classify([]) == ValidationStatus.VALID

    def test_single_warning_issue(self):
        assert ValidationClassifier().classify([ValidationIssue.ZERO_VOLUME]) == ValidationStatus.WARNING

    def test_single_invalid_issue(self):
        assert ValidationClassifier().classify([ValidationIssue.NEGATIVE_PRICE]) == ValidationStatus.INVALID

    def test_invalid_wins_over_warning(self):
        issues = [ValidationIssue.ZERO_VOLUME, ValidationIssue.NEGATIVE_PRICE]
        assert ValidationClassifier().classify(issues) == ValidationStatus.INVALID

    def test_custom_severity_map(self):
        custom = dict(DEFAULT_SEVERITY)
        custom[ValidationIssue.ZERO_VOLUME] = ValidationStatus.INVALID
        classifier = ValidationClassifier(severity=custom)
        assert classifier.classify([ValidationIssue.ZERO_VOLUME]) == ValidationStatus.INVALID


# ─────────────────────────────────────────────────────────────────────────────
# TestOHLCVValidationService — orchestrator + all 7 required scenarios
# ─────────────────────────────────────────────────────────────────────────────

class TestOHLCVValidationService:
    # --- Required scenarios ---

    def test_nan_data_is_invalid(self):
        svc = OHLCVValidationService()
        result = svc.validate(_bar(close=float("nan")))
        assert result.status == ValidationStatus.INVALID
        assert ValidationIssue.NULL_FIELD in result.issues
        assert svc._get_last("AAPL") is None  # state not updated for NULL_FIELD candles

    def test_negative_price_is_invalid(self):
        svc = OHLCVValidationService()
        result = svc.validate(_bar(open=-10.0))
        assert result.status == ValidationStatus.INVALID
        assert ValidationIssue.NEGATIVE_PRICE in result.issues

    def test_negative_volume_is_invalid(self):
        svc = OHLCVValidationService()
        result = svc.validate(_bar(volume=-1.0))
        assert result.status == ValidationStatus.INVALID
        assert ValidationIssue.NEGATIVE_VOLUME in result.issues

    def test_reversed_timestamp_is_invalid(self):
        svc = OHLCVValidationService()
        svc.validate(_bar(ts=_TUE))
        result = svc.validate(_bar(ts=_MON))
        assert result.status == ValidationStatus.INVALID
        assert ValidationIssue.REVERSED_TIMESTAMP in result.issues

    def test_duplicate_identical_candle_is_warning(self):
        svc = OHLCVValidationService()
        svc.validate(_bar(ts=_MON))
        result = svc.validate(_bar(ts=_MON))
        assert result.status == ValidationStatus.WARNING
        assert ValidationIssue.DUPLICATE_TIMESTAMP in result.issues

    def test_duplicate_conflicting_candle_is_invalid(self):
        svc = OHLCVValidationService()
        svc.validate(_bar(ts=_MON, close=102.0))
        result = svc.validate(_bar(ts=_MON, close=103.0))
        assert result.status == ValidationStatus.INVALID
        assert ValidationIssue.DUPLICATE_CONFLICT in result.issues
        assert ValidationIssue.OHLC_INCONSISTENT not in result.issues

    def test_extreme_spike_is_invalid(self):
        svc = OHLCVValidationService()
        svc.validate(_bar(ts=_MON, open=99, high=101, low=98, close=100.0))
        result = svc.validate(_bar(ts=_TUE, open=159, high=161, low=158, close=160.0))
        assert result.status == ValidationStatus.INVALID
        assert ValidationIssue.EXTREME_PRICE_SPIKE in result.issues

    def test_missing_candle_is_warning(self):
        svc = OHLCVValidationService()
        svc.validate(_bar(ts=_MON))
        result = svc.validate(_bar(ts=_WED))  # skips Tuesday, a weekday
        assert result.status == ValidationStatus.WARNING
        assert ValidationIssue.MISSING_CANDLE in result.issues

    # --- Other coverage ---

    def test_valid_bar_is_valid(self):
        svc = OHLCVValidationService()
        result = svc.validate(_bar())
        assert result.status == ValidationStatus.VALID
        assert result.issues == []

    def test_assert_valid_raises_on_invalid(self):
        svc = OHLCVValidationService()
        with pytest.raises(InvalidCandleError) as exc_info:
            svc.assert_valid(_bar(open=-10.0))
        assert exc_info.value.result.status == ValidationStatus.INVALID

    def test_invalid_candle_error_is_not_runtime_error(self):
        svc = OHLCVValidationService()
        try:
            svc.assert_valid(_bar(open=-10.0))
            pytest.fail("expected InvalidCandleError")
        except InvalidCandleError as exc:
            assert not isinstance(exc, RuntimeError)

    def test_assert_valid_returns_result_when_not_invalid(self):
        svc = OHLCVValidationService()
        result = svc.assert_valid(_bar())
        assert result.status == ValidationStatus.VALID

    def test_reset_symbol_clears_state(self):
        svc = OHLCVValidationService()
        svc.validate(_bar(symbol="AAPL", ts=_MON))
        svc.reset("AAPL")
        result = svc.validate(_bar(symbol="AAPL", ts=_WED))
        assert ValidationIssue.MISSING_CANDLE not in result.issues

    def test_reset_all_clears_every_symbol(self):
        svc = OHLCVValidationService()
        svc.validate(_bar(symbol="AAPL", ts=_MON))
        svc.validate(_bar(symbol="MSFT", ts=_MON))
        svc.reset()
        assert svc._get_last("AAPL") is None
        assert svc._get_last("MSFT") is None

    def test_multi_symbol_state_independence(self):
        svc = OHLCVValidationService()
        svc.validate(_bar(symbol="AAPL", ts=_MON, open=99, high=101, low=98, close=100.0))
        svc.validate(_bar(symbol="MSFT", ts=_MON, open=299, high=301, low=298, close=300.0))
        result = svc.validate(_bar(symbol="MSFT", ts=_TUE, open=304, high=306, low=303, close=305.0))
        assert result.status == ValidationStatus.VALID

    def test_db_factory_none_does_not_raise(self):
        svc = OHLCVValidationService(db_factory=None)
        result = svc.validate(_bar(open=-10.0))
        assert result.status == ValidationStatus.INVALID

    def test_invalid_persists_to_audit_log(self):
        factory = _sqlite_factory()
        svc = OHLCVValidationService(db_factory=factory)
        svc.validate(_bar(open=-10.0))
        assert _count_audit(factory, "ohlcv_validation_invalid") == 1

    def test_warning_persists_to_audit_log(self):
        factory = _sqlite_factory()
        svc = OHLCVValidationService(db_factory=factory)
        svc.validate(_bar(volume=0.0))
        assert _count_audit(factory, "ohlcv_validation_warning") == 1

    def test_valid_does_not_persist_to_audit_log(self):
        factory = _sqlite_factory()
        svc = OHLCVValidationService(db_factory=factory)
        svc.validate(_bar())
        assert _count_audit(factory, "ohlcv_validation_invalid") == 0
        assert _count_audit(factory, "ohlcv_validation_warning") == 0

    def test_check_error_is_invalid(self):
        svc = OHLCVValidationService()
        svc.validate(_bar(symbol="AAPL", ts=_MON))
        # naive datetime vs the previously stored aware datetime -> TypeError -> CHECK_ERROR
        naive_bar = _bar(symbol="AAPL", ts=datetime(2026, 6, 2, 9, 30))
        result = svc.validate(naive_bar)
        assert result.status == ValidationStatus.INVALID
        assert ValidationIssue.CHECK_ERROR in result.issues
