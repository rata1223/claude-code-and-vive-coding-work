"""
FreshnessGate — the single authoritative stale-data gate for the execution path
(R-11 fix).

One gate instance per process (see `get_freshness_gate()`), backed by the
existing `StaleDataDetectionService` (the single source of truth) and the single
threshold config in `freshness_config.py`. Every code path that can generate a
signal or size/place an order routes through this gate, so there is exactly one
place that decides "is this market data fresh enough to act on?".

Behaviour:
  * Fail-closed. A missing timestamp, a never-seen feed, or any internal error
    blocks trading (StaleState.UNKNOWN / STALE).
  * STALE == CRITICAL → fires the optional halt callback (kill switch).
  * Structured logging on every non-FRESH decision: symbol, source,
    last_timestamp, age_seconds, reason.

Typical wiring:
    gate = get_freshness_gate()
    gate.validate_dataframe(symbol, df, source="loader", tier=FreshnessTier.DAILY_BAR)
    ...
    gate.assert_tradeable(symbol, source="get_price", tier=FreshnessTier.DAILY_BAR)
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from backend.data.freshness_config import (
    FreshnessConfig,
    FreshnessTier,
    load_freshness_config,
)
from backend.data.stale_detector import (
    FreshnessChecker,
    StaleDataDetectionService,
    StaleFeedError,
    StalenessResult,
    StaleState,
    TradingGate,
)

logger = logging.getLogger(__name__)

# halt_callback(result, source) — invoked when staleness reaches CRITICAL (STALE).
HaltCallback = Callable[[StalenessResult, str], None]


def _coerce_utc(ts) -> Optional[datetime]:
    """Best-effort conversion of a pandas/py timestamp-or-date to tz-aware UTC.
    Returns None if it cannot be interpreted (→ treated as missing → blocked)."""
    if ts is None:
        return None
    try:
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if not isinstance(ts, datetime):
            # plain date or similar
            ts = datetime(ts.year, ts.month, ts.day)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:
        return None


class FreshnessGate:
    """Authoritative freshness gate. Thin orchestration over
    StaleDataDetectionService + FreshnessConfig + kill-switch halt."""

    def __init__(self,
                 config: Optional[FreshnessConfig] = None,
                 service: Optional[StaleDataDetectionService] = None,
                 halt_callback: Optional[HaltCallback] = None,
                 db_factory=None):
        self._config = config or load_freshness_config()
        self._policy = TradingGate(block_on_unknown=self._config.block_on_unknown)
        if service is None:
            tier_checkers = {
                tier.value: FreshnessChecker(
                    warn_after_seconds=self._config.threshold(tier).warn_after_seconds,
                    stale_after_seconds=self._config.threshold(tier).stale_after_seconds,
                )
                for tier in FreshnessTier
            }
            service = StaleDataDetectionService(
                gate=self._policy,
                tier_checkers=tier_checkers,
                db_factory=db_factory,
                actor="freshness_gate",
            )
        self._service = service
        self._halt_callback = halt_callback
        self._lock = threading.Lock()

    # ── configuration ──────────────────────────────────────────────────────

    def set_halt_callback(self, cb: Optional[HaltCallback]) -> None:
        with self._lock:
            self._halt_callback = cb

    @property
    def service(self) -> StaleDataDetectionService:
        return self._service

    # ── validation entry points ──────────────────────────────────────────

    def validate_timestamp(self, symbol: str, last_ts,
                           *, tier: FreshnessTier, source: str,
                           raise_on_block: bool = True,
                           now: Optional[datetime] = None) -> StalenessResult:
        """Record `last_ts` for (symbol, tier) and classify freshness.

        A missing/uninterpretable timestamp is treated as UNKNOWN (fail-closed):
        it never refreshes the feed and blocks per `block_on_unknown`."""
        ts = _coerce_utc(last_ts)
        if ts is None:
            # Do NOT record_update(ts=None) — that would mark the feed fresh.
            # A timestamp-less payload is always UNKNOWN-by-omission, regardless
            # of any prior state (FRESH or WARNING), to stay fail-closed.
            prior = self._service.check(symbol, now=now, tier=tier)
            result = StalenessResult(
                state=StaleState.UNKNOWN, key=prior.key, age_seconds=None,
                status=prior.status, consecutive_failures=prior.consecutive_failures,
                detail="missing timestamp",
            )
            return self._finalize(result, symbol=symbol, source=source,
                                  last_ts=None, raise_on_block=raise_on_block)
        result = self._service.record_update(symbol, ts=ts, now=now, tier=tier)
        return self._finalize(result, symbol=symbol, source=source,
                              last_ts=ts, raise_on_block=raise_on_block)

    def validate_dataframe(self, symbol: str, df,
                           *, source: str,
                           tier: FreshnessTier = FreshnessTier.DAILY_BAR,
                           raise_on_block: bool = True,
                           now: Optional[datetime] = None) -> StalenessResult:
        """Validate the last candle timestamp of an OHLCV DataFrame."""
        last_ts = None
        try:
            if df is not None and len(df) > 0:
                last_ts = df.index[-1]
        except Exception:
            last_ts = None
        return self.validate_timestamp(symbol, last_ts, tier=tier, source=source,
                                       raise_on_block=raise_on_block, now=now)

    def assert_tradeable(self, symbol: str,
                         *, source: str,
                         tier: FreshnessTier = FreshnessTier.DAILY_BAR,
                         raise_on_block: bool = True,
                         now: Optional[datetime] = None) -> StalenessResult:
        """Re-check the most recently recorded freshness for (symbol, tier)
        without new data — used before get_price()-based order sizing, where the
        quote itself carries no timestamp. Fail-closed: a symbol whose feed was
        never recorded is UNKNOWN and blocks."""
        result = self._service.check(symbol, now=now, tier=tier)
        return self._finalize(result, symbol=symbol, source=source,
                              last_ts=None, raise_on_block=raise_on_block)

    def is_blocking(self, result: StalenessResult) -> bool:
        return self._policy.is_blocking(result)

    # ── internals ────────────────────────────────────────────────────────

    def _finalize(self, result: StalenessResult, *, symbol: str, source: str,
                  last_ts: Optional[datetime], raise_on_block: bool) -> StalenessResult:
        blocking = self._policy.is_blocking(result)
        if result.state != StaleState.FRESH:
            log = logger.error if blocking else logger.warning
            log(
                "stale-data gate: symbol=%s source=%s last_timestamp=%s "
                "age_seconds=%s state=%s reason=%s blocking=%s",
                symbol, source,
                last_ts.isoformat() if last_ts else "none",
                "n/a" if result.age_seconds is None else f"{result.age_seconds:.0f}",
                result.state.value, result.detail, blocking,
                extra={"symbol": symbol, "source": source,
                       "last_timestamp": last_ts.isoformat() if last_ts else None,
                       "age_seconds": result.age_seconds,
                       "state": result.state.value, "reason": result.detail},
            )
        # CRITICAL staleness (STALE) → trigger emergency halt.
        if result.state == StaleState.STALE:
            self._fire_halt(result, source)
        if blocking and raise_on_block:
            raise StaleFeedError(result)
        return result

    def _fire_halt(self, result: StalenessResult, source: str) -> None:
        with self._lock:
            cb = self._halt_callback
        if cb is None:
            return
        try:
            cb(result, source)
        except Exception as exc:
            logger.error("stale-data halt callback failed for %s: %s", result.key, exc)


# ── process-wide singleton ───────────────────────────────────────────────

_GATE: Optional[FreshnessGate] = None
_GATE_LOCK = threading.Lock()


def get_freshness_gate() -> FreshnessGate:
    """Return the process-wide FreshnessGate, building it on first use."""
    global _GATE
    if _GATE is None:
        with _GATE_LOCK:
            if _GATE is None:
                _GATE = FreshnessGate()
    return _GATE


def set_freshness_gate(gate: Optional[FreshnessGate]) -> None:
    """Override the process-wide gate (tests / worker wiring)."""
    global _GATE
    with _GATE_LOCK:
        _GATE = gate


def make_kill_switch_halt_callback(kill_switch) -> HaltCallback:
    """Build a halt callback that escalates CRITICAL staleness into the
    KillSwitch as a WATCHDOG_FAILURE (Severity.CRITICAL → HALTED). Loose
    coupling: the data layer never imports the risk layer at module load."""
    def _halt(result: StalenessResult, source: str) -> None:
        from backend.risk.kill_switch import Severity
        kill_switch.report_watchdog_failure(
            Severity.CRITICAL,
            f"stale market data: {result.key} ({source})",
            detail={"key": result.key, "source": source,
                    "age_seconds": result.age_seconds, "state": result.state.value},
        )
    return _halt
