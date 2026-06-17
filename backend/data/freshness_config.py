"""
Unified freshness thresholds — THE single source of truth for "how old is too
old" across the whole platform (R-11 fix).

Before R-11, staleness thresholds were scattered and inconsistent:
  - backend/strategy/base.py            _BAR_STALE_SECONDS = 600   (intraday bar)
  - backend/quant/data/loader.py        stale_hours = 26           (daily bar, WARN-only)
  - backend/strategy/indicator/strategy 3 calendar days            (daily scan gate)
  - backend/worker/emergency.py         max_age_hours = 2          (dead code)

All of those are now removed and replaced by the tiered thresholds defined
here. Nothing else in the codebase should hardcode a staleness threshold; it
should read it from this module (via FreshnessTier / load_freshness_config).

Every threshold is overridable by an environment variable so operators can
tune freshness without code changes. Defaults preserve the previous *blocking*
behaviour (intraday 600s, daily 3 days) while folding the loader's 26h marker
into a WARNING tier.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class FreshnessTier(str, Enum):
    """Data-recency class. Different feeds tolerate different ages."""
    INTRADAY_QUOTE = "intraday_quote"   # live price used for order sizing
    INTRADAY_BAR = "intraday_bar"       # streaming on_bar candles
    DAILY_BAR = "daily_bar"             # daily OHLCV used for signal generation


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Default intraday "stale" age preserves the legacy BAR_STALE_SECONDS=600 (10min)
# contract that backend/strategy/base.py and stale_detector.py used.
_LEGACY_BAR_STALE_SECONDS = _env_float("BAR_STALE_SECONDS", 600.0)

_HOUR = 3600.0


@dataclass(frozen=True)
class TierThreshold:
    warn_after_seconds: float
    stale_after_seconds: float


@dataclass(frozen=True)
class FreshnessConfig:
    """Resolved thresholds for every tier + the fail-closed policy.

    block_on_unknown=True means a feed that has never reported (or whose
    timestamp is missing) blocks trading until its first valid update — the
    fail-closed default required by R-11.
    """
    intraday_quote: TierThreshold
    intraday_bar: TierThreshold
    daily_bar: TierThreshold
    block_on_unknown: bool

    def threshold(self, tier: FreshnessTier) -> TierThreshold:
        return {
            FreshnessTier.INTRADAY_QUOTE: self.intraday_quote,
            FreshnessTier.INTRADAY_BAR: self.intraday_bar,
            FreshnessTier.DAILY_BAR: self.daily_bar,
        }[tier]


def load_freshness_config() -> FreshnessConfig:
    """Build the process FreshnessConfig from environment variables.

    Env vars (all optional; defaults preserve prior behaviour):
      FRESHNESS_INTRADAY_QUOTE_WARN_SECONDS   (default 300)
      FRESHNESS_INTRADAY_QUOTE_STALE_SECONDS  (default BAR_STALE_SECONDS or 600)
      FRESHNESS_INTRADAY_BAR_WARN_SECONDS     (default 300)
      FRESHNESS_INTRADAY_BAR_STALE_SECONDS    (default BAR_STALE_SECONDS or 600)
      FRESHNESS_DAILY_WARN_SECONDS            (default 26h)
      FRESHNESS_DAILY_STALE_SECONDS           (default 72h / 3 days)
      FRESHNESS_BLOCK_ON_UNKNOWN              (default true)
    """
    return FreshnessConfig(
        intraday_quote=TierThreshold(
            warn_after_seconds=_env_float("FRESHNESS_INTRADAY_QUOTE_WARN_SECONDS", 300.0),
            stale_after_seconds=_env_float("FRESHNESS_INTRADAY_QUOTE_STALE_SECONDS", _LEGACY_BAR_STALE_SECONDS),
        ),
        intraday_bar=TierThreshold(
            warn_after_seconds=_env_float("FRESHNESS_INTRADAY_BAR_WARN_SECONDS", 300.0),
            stale_after_seconds=_env_float("FRESHNESS_INTRADAY_BAR_STALE_SECONDS", _LEGACY_BAR_STALE_SECONDS),
        ),
        daily_bar=TierThreshold(
            warn_after_seconds=_env_float("FRESHNESS_DAILY_WARN_SECONDS", 26 * _HOUR),
            stale_after_seconds=_env_float("FRESHNESS_DAILY_STALE_SECONDS", 72 * _HOUR),
        ),
        block_on_unknown=_env_bool("FRESHNESS_BLOCK_ON_UNKNOWN", True),
    )
