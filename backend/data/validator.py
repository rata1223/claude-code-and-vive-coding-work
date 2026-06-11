"""
OHLCV candle validator — structural and statistical sanity checks.

Classifies each candle as VALID / WARNING / INVALID before it reaches strategy
code. Fail-closed: any unexpected error during validation is treated as INVALID.

Usage:
    from backend.data.validator import OHLCVValidationService
    svc = OHLCVValidationService()
    result = svc.validate(bar)        # bar matches StrategyBase.on_bar's dict shape
    if result.is_blocking:
        ...  # drop the candle, do not call on_bar
    svc.assert_valid(bar)             # raises InvalidCandleError if INVALID
"""

from __future__ import annotations

import json
import logging
import math
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# 1. Enums & dataclasses
# ─────────────────────────────────────────────────────────────────

class ValidationStatus(str, Enum):
    VALID = "valid"
    WARNING = "warning"
    INVALID = "invalid"


class ValidationIssue(str, Enum):
    NULL_FIELD = "null_field"
    NEGATIVE_PRICE = "negative_price"
    OHLC_INCONSISTENT = "ohlc_inconsistent"
    NEGATIVE_VOLUME = "negative_volume"
    ZERO_VOLUME = "zero_volume"
    REVERSED_TIMESTAMP = "reversed_timestamp"
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"
    DUPLICATE_CONFLICT = "duplicate_conflict"
    PRICE_SPIKE = "price_spike"
    EXTREME_PRICE_SPIKE = "extreme_price_spike"
    MISSING_CANDLE = "missing_candle"
    CHECK_ERROR = "check_error"


@dataclass(frozen=True)
class Candle:
    symbol: Optional[str]
    ts: Optional[datetime]
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    volume: Optional[float]

    @classmethod
    def from_bar(cls, bar: dict) -> "Candle":
        return cls(
            symbol=bar.get("symbol"),
            ts=bar.get("ts"),
            open=bar.get("open"),
            high=bar.get("high"),
            low=bar.get("low"),
            close=bar.get("close"),
            volume=bar.get("volume"),
        )


@dataclass
class ValidationResult:
    status: ValidationStatus
    issues: list[ValidationIssue]
    symbol: Optional[str]
    ts: Optional[datetime]
    detail: str

    @property
    def is_blocking(self) -> bool:
        return self.status == ValidationStatus.INVALID


class InvalidCandleError(Exception):
    """Raised by assert_valid() when a candle is INVALID.

    NOT a subclass of RuntimeError — intentional, mirrors MarketClosedError
    (backend/data/calendar.py) so ConsecutiveFailureBreaker does not count
    data-quality rejections as broker failures.
    """

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__(result.detail)


# ─────────────────────────────────────────────────────────────────
# 2. NullChecker — gates all other checks
# ─────────────────────────────────────────────────────────────────

class NullChecker:
    """Detects None/NaN in required fields. Pure, stateless."""

    _NUMERIC_FIELDS = ("open", "high", "low", "close", "volume")

    @staticmethod
    def check(bar: dict) -> list[ValidationIssue]:
        if bar.get("ts") is None:
            return [ValidationIssue.NULL_FIELD]
        for field in NullChecker._NUMERIC_FIELDS:
            value = bar.get(field)
            if value is None:
                return [ValidationIssue.NULL_FIELD]
            if isinstance(value, float) and math.isnan(value):
                return [ValidationIssue.NULL_FIELD]
        return []


# ─────────────────────────────────────────────────────────────────
# 3. OHLCConsistencyChecker
# ─────────────────────────────────────────────────────────────────

class OHLCConsistencyChecker:
    """Negative prices and high/low/open/close range violations. Pure, stateless."""

    @staticmethod
    def check(c: Candle) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        if any(v < 0 for v in (c.open, c.high, c.low, c.close)):
            issues.append(ValidationIssue.NEGATIVE_PRICE)
        if (c.high < c.low or c.high < c.open or c.high < c.close
                or c.low > c.open or c.low > c.close):
            issues.append(ValidationIssue.OHLC_INCONSISTENT)
        return issues


# ─────────────────────────────────────────────────────────────────
# 4. VolumeChecker
# ─────────────────────────────────────────────────────────────────

class VolumeChecker:
    """Negative volume (INVALID) and zero volume (WARNING, configurable)."""

    def __init__(self, warn_on_zero: bool = True) -> None:
        self._warn_on_zero = warn_on_zero

    def check(self, c: Candle) -> list[ValidationIssue]:
        if c.volume < 0:
            return [ValidationIssue.NEGATIVE_VOLUME]
        if c.volume == 0 and self._warn_on_zero:
            return [ValidationIssue.ZERO_VOLUME]
        return []


# ─────────────────────────────────────────────────────────────────
# 5. TimestampChecker
# ─────────────────────────────────────────────────────────────────

class TimestampChecker:
    """Reversed/duplicate timestamps relative to the previous candle. Pure, stateless."""

    @staticmethod
    def check(c: Candle, prev: Optional[Candle]) -> list[ValidationIssue]:
        if prev is None:
            return []
        if c.ts < prev.ts:
            return [ValidationIssue.REVERSED_TIMESTAMP]
        if c.ts == prev.ts:
            same = (c.open, c.high, c.low, c.close, c.volume) == \
                   (prev.open, prev.high, prev.low, prev.close, prev.volume)
            return [ValidationIssue.DUPLICATE_TIMESTAMP if same
                    else ValidationIssue.DUPLICATE_CONFLICT]
        return []


# ─────────────────────────────────────────────────────────────────
# 6. PriceSpikeDetector
# ─────────────────────────────────────────────────────────────────

class PriceSpikeDetector:
    """Flags abnormal close-to-close moves vs the previous candle."""

    def __init__(self, warn_pct: float = 0.10, invalid_pct: float = 0.50) -> None:
        self._warn = warn_pct
        self._invalid = invalid_pct

    def check(self, c: Candle, prev: Optional[Candle]) -> list[ValidationIssue]:
        if prev is None or prev.close == 0:
            return []
        pct = abs(c.close - prev.close) / abs(prev.close)
        if pct >= self._invalid:
            return [ValidationIssue.EXTREME_PRICE_SPIKE]
        if pct >= self._warn:
            return [ValidationIssue.PRICE_SPIKE]
        return []


# ─────────────────────────────────────────────────────────────────
# 7. MissingCandleDetector
# ─────────────────────────────────────────────────────────────────

class MissingCandleDetector:
    """Flags gaps in the candle sequence vs the expected interval.

    Daily+ intervals: walks calendar days between prev and current candle and
    flags a gap if any skipped day is a trading day per `trading_day_check`
    (default: Mon-Fri, no holiday awareness — inject e.g.
    `calendar_service.is_trading_day(Market.NYSE, d)` for holiday-aware checks).

    Intraday intervals: flags a gap if it exceeds `max_gap_multiplier * interval`.
    """

    def __init__(self,
                 trading_day_check: Optional[Callable[[date], bool]] = None,
                 max_gap_multiplier: float = 1.5) -> None:
        self._is_trading_day = trading_day_check or (lambda d: d.weekday() < 5)
        self._max_gap = max_gap_multiplier

    def check(self, c: Candle, prev: Optional[Candle], interval: timedelta) -> list[ValidationIssue]:
        if prev is None or c.ts <= prev.ts:
            return []
        if interval >= timedelta(days=1):
            d = prev.ts.date() + timedelta(days=1)
            while d < c.ts.date():
                if self._is_trading_day(d):
                    return [ValidationIssue.MISSING_CANDLE]
                d += timedelta(days=1)
            return []
        if (c.ts - prev.ts) > interval * self._max_gap:
            return [ValidationIssue.MISSING_CANDLE]
        return []


# ─────────────────────────────────────────────────────────────────
# 8. ValidationClassifier
# ─────────────────────────────────────────────────────────────────

DEFAULT_SEVERITY: dict[ValidationIssue, ValidationStatus] = {
    ValidationIssue.NULL_FIELD: ValidationStatus.INVALID,
    ValidationIssue.NEGATIVE_PRICE: ValidationStatus.INVALID,
    ValidationIssue.OHLC_INCONSISTENT: ValidationStatus.INVALID,
    ValidationIssue.NEGATIVE_VOLUME: ValidationStatus.INVALID,
    ValidationIssue.ZERO_VOLUME: ValidationStatus.WARNING,
    ValidationIssue.REVERSED_TIMESTAMP: ValidationStatus.INVALID,
    ValidationIssue.DUPLICATE_TIMESTAMP: ValidationStatus.WARNING,
    ValidationIssue.DUPLICATE_CONFLICT: ValidationStatus.INVALID,
    ValidationIssue.PRICE_SPIKE: ValidationStatus.WARNING,
    ValidationIssue.EXTREME_PRICE_SPIKE: ValidationStatus.INVALID,
    ValidationIssue.MISSING_CANDLE: ValidationStatus.WARNING,
    ValidationIssue.CHECK_ERROR: ValidationStatus.INVALID,
}


class ValidationClassifier:
    """Maps a set of issues to an overall ValidationStatus. INVALID > WARNING > VALID."""

    def __init__(self, severity: Optional[dict[ValidationIssue, ValidationStatus]] = None) -> None:
        self._severity = severity or DEFAULT_SEVERITY

    def classify(self, issues: list[ValidationIssue]) -> ValidationStatus:
        statuses = {self._severity.get(i, ValidationStatus.INVALID) for i in issues}
        if ValidationStatus.INVALID in statuses:
            return ValidationStatus.INVALID
        if ValidationStatus.WARNING in statuses:
            return ValidationStatus.WARNING
        return ValidationStatus.VALID


# ─────────────────────────────────────────────────────────────────
# 9. OHLCVValidationService — orchestrator
# ─────────────────────────────────────────────────────────────────

class OHLCVValidationService:
    """Single entry point: validate a bar dict, classify, persist, track per-symbol state."""

    _EVENT_INVALID = "ohlcv_validation_invalid"
    _EVENT_WARNING = "ohlcv_validation_warning"

    def __init__(self, *,
                 volume_checker: Optional[VolumeChecker] = None,
                 spike_detector: Optional[PriceSpikeDetector] = None,
                 gap_detector: Optional[MissingCandleDetector] = None,
                 classifier: Optional[ValidationClassifier] = None,
                 default_interval: timedelta = timedelta(days=1),
                 db_factory=None,
                 actor: str = "validator") -> None:
        self._volume = volume_checker or VolumeChecker()
        self._spike = spike_detector or PriceSpikeDetector()
        self._gap = gap_detector or MissingCandleDetector()
        self._classifier = classifier or ValidationClassifier()
        self._default_interval = default_interval
        self._db = db_factory
        self._actor = actor
        self._lock = threading.Lock()
        self._last: dict[Optional[str], Candle] = {}

    def validate(self, bar: dict, *, interval: Optional[timedelta] = None) -> ValidationResult:
        """Run all checks against `bar`. Fail-closed: unexpected errors -> INVALID."""
        candle = Candle.from_bar(bar)
        try:
            issues = NullChecker.check(bar)
            if not issues:
                prev = self._get_last(candle.symbol)
                issues = (
                    OHLCConsistencyChecker.check(candle)
                    + self._volume.check(candle)
                    + TimestampChecker.check(candle, prev)
                    + self._spike.check(candle, prev)
                    + self._gap.check(candle, prev, interval or self._default_interval)
                )
        except Exception as exc:
            logger.warning("OHLCV validation error for %s: %s", candle.symbol, exc)
            issues = [ValidationIssue.CHECK_ERROR]

        status = self._classifier.classify(issues)
        result = ValidationResult(
            status=status,
            issues=issues,
            symbol=candle.symbol,
            ts=candle.ts,
            detail=", ".join(i.value for i in issues) or "ok",
        )

        if ValidationIssue.NULL_FIELD not in issues:
            self._update_last(candle)
        if status != ValidationStatus.VALID:
            self._persist(result)
        return result

    def assert_valid(self, bar: dict, *, interval: Optional[timedelta] = None) -> ValidationResult:
        """validate() and raise InvalidCandleError if the result is INVALID."""
        result = self.validate(bar, interval=interval)
        if result.is_blocking:
            raise InvalidCandleError(result)
        return result

    def reset(self, symbol: Optional[str] = None) -> None:
        """Clear tracked per-symbol state (all symbols if `symbol` is None)."""
        with self._lock:
            if symbol is None:
                self._last.clear()
            else:
                self._last.pop(symbol, None)

    def _get_last(self, symbol: Optional[str]) -> Optional[Candle]:
        with self._lock:
            return self._last.get(symbol)

    def _update_last(self, candle: Candle) -> None:
        with self._lock:
            self._last[candle.symbol] = candle

    def _persist(self, result: ValidationResult) -> None:
        if self._db is None:
            return
        event_type = (
            self._EVENT_INVALID if result.status == ValidationStatus.INVALID
            else self._EVENT_WARNING
        )
        try:
            from backend.database.models import AuditLog
            sess = self._db()
            try:
                sess.add(AuditLog(
                    event_type=event_type,
                    symbol=result.symbol,
                    actor=self._actor,
                    detail=json.dumps({
                        "status": result.status.value,
                        "issues": [i.value for i in result.issues],
                        "ts": result.ts.isoformat() if result.ts else None,
                        "detail": result.detail,
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
        except Exception as exc:
            logger.warning("OHLCV validation audit log 실패: %s", exc)
