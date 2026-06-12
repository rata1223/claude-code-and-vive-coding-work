"""
Stale market-data detector — freshness and data-source health tracking.

Classifies each (symbol/feed) key as FRESH / WARNING / STALE / UNKNOWN before
new orders are allowed. Fail-closed: an unexpected error during evaluation is
treated as STALE.

Usage:
    from backend.data.stale_detector import StaleDataDetectionService
    svc = StaleDataDetectionService()
    svc.record_bar(bar)               # call on each StrategyBase.on_bar bar
    result = svc.check(bar["symbol"]) # inspect without raising
    svc.assert_fresh(bar["symbol"])   # raises StaleFeedError if blocking
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# Mirrors backend/strategy/base.py's _BAR_STALE_SECONDS env var.
_DEFAULT_STALE_AFTER_SECONDS = float(os.environ.get("BAR_STALE_SECONDS", "600"))


# ─────────────────────────────────────────────────────────────────
# 1. Enums & dataclasses
# ─────────────────────────────────────────────────────────────────

class StaleState(str, Enum):
    FRESH = "fresh"
    WARNING = "warning"
    STALE = "stale"
    UNKNOWN = "unknown"


class FeedStatus(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


@dataclass
class SourceHealth:
    status: FeedStatus = FeedStatus.UNKNOWN
    last_update: Optional[datetime] = None
    consecutive_failures: int = 0


@dataclass
class StalenessResult:
    state: StaleState
    key: str
    age_seconds: Optional[float]
    status: FeedStatus
    consecutive_failures: int
    detail: str


class StaleFeedError(Exception):
    """Raised by assert_fresh()/TradingGate when a feed is STALE (or UNKNOWN
    if configured to block).

    NOT a subclass of RuntimeError — intentional, mirrors MarketClosedError
    (backend/data/calendar.py) and InvalidCandleError (backend/data/validator.py)
    so ConsecutiveFailureBreaker does not count data-staleness rejections as
    broker failures.
    """

    def __init__(self, result: StalenessResult) -> None:
        self.result = result
        super().__init__(result.detail)


# ─────────────────────────────────────────────────────────────────
# 2. FreshnessChecker — pure, time-based
# ─────────────────────────────────────────────────────────────────

class FreshnessChecker:
    """Pure time-based freshness classification. No connection-state awareness."""

    def __init__(self,
                 warn_after_seconds: float = 300.0,
                 stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS) -> None:
        self._warn = warn_after_seconds
        self._stale = stale_after_seconds

    @staticmethod
    def age_seconds(last_update: Optional[datetime], now: Optional[datetime] = None) -> Optional[float]:
        if last_update is None:
            return None
        now = now or datetime.now(timezone.utc)
        if last_update.tzinfo is None:
            last_update = last_update.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return max(0.0, (now - last_update).total_seconds())

    def classify(self, last_update: Optional[datetime], now: Optional[datetime] = None) -> StaleState:
        age = self.age_seconds(last_update, now)
        if age is None:
            return StaleState.UNKNOWN
        if age > self._stale:
            return StaleState.STALE
        if age > self._warn:
            return StaleState.WARNING
        return StaleState.FRESH


# ─────────────────────────────────────────────────────────────────
# 3. DataSourceHealthTracker
# ─────────────────────────────────────────────────────────────────

class DataSourceHealthTracker:
    """Tracks per-key connection health, independent of last-update freshness.

    Reconnection-after-data-resumes is signaled by the next record_update().
    """

    def __init__(self, failure_threshold: int = 5) -> None:
        self._failure_threshold = failure_threshold
        self._lock = threading.Lock()
        self._sources: dict[str, SourceHealth] = {}

    def record_update(self, key: str, ts: Optional[datetime] = None) -> SourceHealth:
        with self._lock:
            h = self._sources.setdefault(key, SourceHealth())
            h.last_update = ts or datetime.now(timezone.utc)
            h.consecutive_failures = 0
            h.status = FeedStatus.CONNECTED
            return replace(h)

    def record_failure(self, key: str) -> SourceHealth:
        with self._lock:
            h = self._sources.setdefault(key, SourceHealth())
            h.consecutive_failures += 1
            if h.consecutive_failures >= self._failure_threshold:
                h.status = FeedStatus.DISCONNECTED
            return replace(h)

    def record_disconnect(self, key: str) -> SourceHealth:
        with self._lock:
            h = self._sources.setdefault(key, SourceHealth())
            h.status = FeedStatus.DISCONNECTED
            return replace(h)

    def get(self, key: str) -> SourceHealth:
        with self._lock:
            return replace(self._sources.get(key, SourceHealth()))

    def reset(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._sources.clear()
            else:
                self._sources.pop(key, None)


# ─────────────────────────────────────────────────────────────────
# 4. StalenessClassifier — "Stale State"
# ─────────────────────────────────────────────────────────────────

class StalenessClassifier:
    """Combines connection health with time-based freshness into a single StaleState."""

    def __init__(self, freshness_checker: Optional[FreshnessChecker] = None) -> None:
        self._freshness = freshness_checker or FreshnessChecker()

    def classify(self, health: SourceHealth, now: Optional[datetime] = None) -> StaleState:
        if health.status == FeedStatus.DISCONNECTED:
            return StaleState.STALE
        return self._freshness.classify(health.last_update, now)


# ─────────────────────────────────────────────────────────────────
# 5. TradingGate
# ─────────────────────────────────────────────────────────────────

class TradingGate:
    """Decides whether a StalenessResult should block new orders.

    UNKNOWN handling is configurable via `block_on_unknown` (default True =
    fail-closed: a never-seen feed blocks trading until its first update).
    """

    def __init__(self, block_on_unknown: bool = True) -> None:
        self._block_on_unknown = block_on_unknown

    def is_blocking(self, result: StalenessResult) -> bool:
        if result.state == StaleState.STALE:
            return True
        if result.state == StaleState.UNKNOWN:
            return self._block_on_unknown
        return False

    def assert_fresh(self, result: StalenessResult) -> StalenessResult:
        if self.is_blocking(result):
            raise StaleFeedError(result)
        return result


# ─────────────────────────────────────────────────────────────────
# 6. RecoveryHook
# ─────────────────────────────────────────────────────────────────

RecoveryCallback = Callable[[StalenessResult], None]


class RecoveryHook:
    """Fires registered callbacks when a key transitions back to FRESH from
    WARNING/STALE/UNKNOWN. Each callback is isolated — an exception in one
    never prevents the others or propagates out of fire()."""

    def __init__(self, callbacks: Optional[Iterable[RecoveryCallback]] = None) -> None:
        self._callbacks: list[RecoveryCallback] = list(callbacks or [])

    def register(self, callback: RecoveryCallback) -> None:
        self._callbacks.append(callback)

    def fire(self, result: StalenessResult) -> None:
        for cb in self._callbacks:
            try:
                cb(result)
            except Exception as exc:
                logger.warning("Stale-data recovery hook error for %s: %s", result.key, exc)


# ─────────────────────────────────────────────────────────────────
# 7. StaleDataDetectionService — orchestrator
# ─────────────────────────────────────────────────────────────────

class StaleDataDetectionService:
    """Single entry point: record feed updates/failures/disconnects, classify
    staleness, gate trading, fire recovery hooks, persist transitions."""

    _EVENT_BY_STATE: dict[StaleState, str] = {
        StaleState.WARNING: "stale_data_warning",
        StaleState.STALE: "stale_data_stale",
        StaleState.UNKNOWN: "stale_data_unknown",
    }
    _EVENT_RECOVERED = "stale_data_recovered"

    def __init__(self, *,
                 freshness_checker: Optional[FreshnessChecker] = None,
                 health_tracker: Optional[DataSourceHealthTracker] = None,
                 classifier: Optional[StalenessClassifier] = None,
                 gate: Optional[TradingGate] = None,
                 recovery_hook: Optional[RecoveryHook] = None,
                 db_factory=None,
                 actor: str = "stale_detector") -> None:
        self._freshness = freshness_checker or FreshnessChecker()
        self._health = health_tracker or DataSourceHealthTracker()
        self._classifier = classifier or StalenessClassifier(self._freshness)
        self._gate = gate or TradingGate()
        self._recovery = recovery_hook or RecoveryHook()
        self._db = db_factory
        self._actor = actor
        self._lock = threading.Lock()
        self._last_state: dict[str, StaleState] = {}

    def record_update(self, key: str, ts: Optional[datetime] = None,
                       *, now: Optional[datetime] = None) -> StalenessResult:
        """Call when new data arrives for `key` (e.g., each on_bar)."""
        self._health.record_update(key, ts)
        return self._evaluate(key, now)

    def record_failure(self, key: str, *, now: Optional[datetime] = None) -> StalenessResult:
        """Call on a poll/connection error for `key`."""
        self._health.record_failure(key)
        return self._evaluate(key, now)

    def record_disconnect(self, key: str, *, now: Optional[datetime] = None) -> StalenessResult:
        """Call on an explicit disconnect (websocket closed, polling stopped)."""
        self._health.record_disconnect(key)
        return self._evaluate(key, now)

    def record_bar(self, bar: dict, *, now: Optional[datetime] = None) -> StalenessResult:
        """Convenience: record_update from a StrategyBase.on_bar bar dict
        (`{"symbol":..., "ts":..., ...}`); `symbol` is the tracking key."""
        return self.record_update(bar.get("symbol"), ts=bar.get("ts"), now=now)

    def check(self, key: str, now: Optional[datetime] = None) -> StalenessResult:
        """Evaluate current staleness for `key` without raising."""
        return self._evaluate(key, now)

    def assert_fresh(self, key: str, now: Optional[datetime] = None) -> StalenessResult:
        """check() and raise StaleFeedError if the result is blocking."""
        result = self.check(key, now)
        return self._gate.assert_fresh(result)

    def reset(self, key: Optional[str] = None) -> None:
        """Clear tracked state (all keys if `key` is None)."""
        self._health.reset(key)
        with self._lock:
            if key is None:
                self._last_state.clear()
            else:
                self._last_state.pop(key, None)

    def _evaluate(self, key: str, now: Optional[datetime] = None) -> StalenessResult:
        try:
            health = self._health.get(key)
            state = self._classifier.classify(health, now)
            age = self._freshness.age_seconds(health.last_update, now)
        except Exception as exc:
            logger.warning("Stale-data check error for %s: %s", key, exc)
            health = SourceHealth()
            state = StaleState.STALE
            age = None

        result = StalenessResult(
            state=state,
            key=key,
            age_seconds=age,
            status=health.status,
            consecutive_failures=health.consecutive_failures,
            detail=f"state={state.value}, status={health.status.value}, "
                   f"age={'n/a' if age is None else f'{age:.0f}s'}, "
                   f"failures={health.consecutive_failures}",
        )
        self._handle_transition(result)
        return result

    def _handle_transition(self, result: StalenessResult) -> None:
        with self._lock:
            prev = self._last_state.get(result.key)
            self._last_state[result.key] = result.state
        if result.state == prev:
            return
        if result.state == StaleState.FRESH:
            if prev in (StaleState.WARNING, StaleState.STALE, StaleState.UNKNOWN):
                self._recovery.fire(result)
                self._persist(result, self._EVENT_RECOVERED)
        else:
            self._persist(result, self._EVENT_BY_STATE[result.state])

    def _persist(self, result: StalenessResult, event_type: str) -> None:
        if self._db is None:
            return
        try:
            from backend.database.models import AuditLog
            sess = self._db()
            try:
                sess.add(AuditLog(
                    event_type=event_type,
                    symbol=result.key,
                    actor=self._actor,
                    detail=json.dumps({
                        "state": result.state.value,
                        "status": result.status.value,
                        "age_seconds": result.age_seconds,
                        "consecutive_failures": result.consecutive_failures,
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
            logger.warning("Stale-data audit log 실패: %s", exc)
