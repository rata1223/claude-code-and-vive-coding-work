"""
Tests for the unified FreshnessGate (R-11 fix).

Covers the required scenarios:
  1. fresh data passes
  2. stale data blocks signal generation
  3. stale data blocks order sizing
  4. stale data blocks execution (StaleFeedError raised)
  5. missing timestamp blocks (fail-closed)
  6. unknown source blocks (never-recorded feed)
  7. threshold override (config-driven)
  8. fail-closed behavior (block_on_unknown, internal-error → STALE)
  9. kill-switch integration: CRITICAL staleness halts trading
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.data.freshness_config import (
    FreshnessConfig,
    FreshnessTier,
    TierThreshold,
    load_freshness_config,
)
from backend.data.freshness_gate import FreshnessGate, make_kill_switch_halt_callback
from backend.data.stale_detector import StaleFeedError, StaleState

_NOW = datetime(2026, 6, 16, 14, 0, tzinfo=timezone.utc)


def _config(daily_stale=259200.0, daily_warn=93600.0,
            intraday_stale=600.0, intraday_warn=300.0, block_on_unknown=True):
    t_daily = TierThreshold(daily_warn, daily_stale)
    t_intra = TierThreshold(intraday_warn, intraday_stale)
    return FreshnessConfig(
        intraday_quote=t_intra, intraday_bar=t_intra, daily_bar=t_daily,
        block_on_unknown=block_on_unknown,
    )


def _gate(config=None, halt_callback=None):
    return FreshnessGate(config=config or _config(), halt_callback=halt_callback)


def _daily_df(last_ts, n=5):
    idx = pd.date_range(end=last_ts, periods=n, freq="D")
    return pd.DataFrame({"Close": range(n)}, index=idx)


# ── 1. fresh data passes ──────────────────────────────────────────────────

class TestFreshPasses:
    def test_fresh_daily_df_does_not_block(self):
        gate = _gate()
        df = _daily_df(_NOW - timedelta(hours=10))
        res = gate.validate_dataframe("SPY", df, source="yfinance",
                                      tier=FreshnessTier.DAILY_BAR,
                                      raise_on_block=False, now=_NOW)
        assert res.state == StaleState.FRESH
        assert gate.is_blocking(res) is False

    def test_fresh_does_not_raise(self):
        gate = _gate()
        df = _daily_df(_NOW - timedelta(hours=1))
        # should not raise
        gate.validate_dataframe("SPY", df, source="yfinance", now=_NOW)


# ── 2 & 4. stale blocks signal generation / execution ────────────────────

class TestStaleBlocks:
    def test_stale_daily_df_blocks(self):
        gate = _gate()
        df = _daily_df(_NOW - timedelta(days=5))  # > 72h stale threshold
        res = gate.validate_dataframe("SPY", df, source="yfinance",
                                      tier=FreshnessTier.DAILY_BAR,
                                      raise_on_block=False, now=_NOW)
        assert res.state == StaleState.STALE
        assert gate.is_blocking(res) is True

    def test_stale_raises_stalefeederror(self):
        gate = _gate()
        df = _daily_df(_NOW - timedelta(days=5))
        with pytest.raises(StaleFeedError):
            gate.validate_dataframe("SPY", df, source="yfinance",
                                    tier=FreshnessTier.DAILY_BAR, now=_NOW)


# ── 3. stale blocks order sizing (assert_tradeable) ───────────────────────

class TestOrderSizingGate:
    def test_assert_tradeable_blocks_when_feed_stale(self):
        gate = _gate()
        # record a stale daily bar, then re-check at order-sizing time
        gate.validate_dataframe("SPY", _daily_df(_NOW - timedelta(days=5)),
                                source="yfinance", raise_on_block=False, now=_NOW)
        res = gate.assert_tradeable("SPY", source="get_price",
                                    tier=FreshnessTier.DAILY_BAR,
                                    raise_on_block=False, now=_NOW)
        assert gate.is_blocking(res) is True

    def test_assert_tradeable_passes_when_feed_fresh(self):
        gate = _gate()
        gate.validate_dataframe("SPY", _daily_df(_NOW - timedelta(hours=2)),
                                source="yfinance", raise_on_block=False, now=_NOW)
        res = gate.assert_tradeable("SPY", source="get_price",
                                    tier=FreshnessTier.DAILY_BAR,
                                    raise_on_block=False, now=_NOW)
        assert gate.is_blocking(res) is False


# ── 5. missing timestamp blocks (fail-closed) ─────────────────────────────

class TestMissingTimestamp:
    def test_none_timestamp_is_unknown_and_blocks(self):
        gate = _gate()
        res = gate.validate_timestamp("SPY", None, tier=FreshnessTier.DAILY_BAR,
                                      source="yfinance", raise_on_block=False, now=_NOW)
        assert res.state == StaleState.UNKNOWN
        assert gate.is_blocking(res) is True

    def test_empty_dataframe_blocks(self):
        gate = _gate()
        res = gate.validate_dataframe("SPY", pd.DataFrame(), source="yfinance",
                                      raise_on_block=False, now=_NOW)
        assert gate.is_blocking(res) is True

    def test_missing_timestamp_does_not_revive_via_prior_fresh(self):
        """A timestamp-less payload must NOT pass just because the feed was
        fresh a moment ago — it is forced to UNKNOWN."""
        gate = _gate()
        gate.validate_dataframe("SPY", _daily_df(_NOW - timedelta(hours=1)),
                                source="yfinance", raise_on_block=False, now=_NOW)
        res = gate.validate_timestamp("SPY", None, tier=FreshnessTier.DAILY_BAR,
                                      source="yfinance", raise_on_block=False, now=_NOW)
        assert res.state == StaleState.UNKNOWN
        assert gate.is_blocking(res) is True

    def test_missing_timestamp_blocks_even_after_warning(self):
        """Fail-closed regardless of prior state: a missing ts after a WARNING
        reading must still resolve to UNKNOWN (not stay non-blocking WARNING)."""
        # daily warn=10h, stale=72h → a 20h-old bar is WARNING (non-blocking)
        gate = _gate(_config(daily_warn=36000.0, daily_stale=259200.0))
        warn = gate.validate_dataframe("SPY", _daily_df(_NOW - timedelta(hours=20)),
                                       source="yfinance", raise_on_block=False, now=_NOW)
        assert warn.state == StaleState.WARNING
        assert gate.is_blocking(warn) is False
        res = gate.validate_timestamp("SPY", None, tier=FreshnessTier.DAILY_BAR,
                                      source="yfinance", raise_on_block=False, now=_NOW)
        assert res.state == StaleState.UNKNOWN
        assert gate.is_blocking(res) is True


# ── 6. unknown source blocks (never recorded) ─────────────────────────────

class TestUnknownSource:
    def test_never_seen_symbol_blocks(self):
        gate = _gate()
        res = gate.assert_tradeable("NEVER_SEEN", source="get_price",
                                    tier=FreshnessTier.DAILY_BAR,
                                    raise_on_block=False, now=_NOW)
        assert res.state == StaleState.UNKNOWN
        assert gate.is_blocking(res) is True


# ── 7. threshold override ─────────────────────────────────────────────────

class TestThresholdOverride:
    def test_tighter_threshold_flips_fresh_to_stale(self):
        df = _daily_df(_NOW - timedelta(hours=10))  # 10h old
        # default daily stale = 72h → fresh
        assert _gate().validate_dataframe(
            "SPY", df, source="yfinance", raise_on_block=False, now=_NOW
        ).state == StaleState.FRESH
        # override daily stale to 1h → same data now stale
        tight = _gate(_config(daily_stale=3600.0, daily_warn=1800.0))
        assert tight.validate_dataframe(
            "SPY", df, source="yfinance", raise_on_block=False, now=_NOW
        ).state == StaleState.STALE

    def test_env_override_loads(self, monkeypatch):
        monkeypatch.setenv("FRESHNESS_DAILY_WARN_SECONDS", "1800")
        monkeypatch.setenv("FRESHNESS_DAILY_STALE_SECONDS", "3600")
        cfg = load_freshness_config()
        assert cfg.daily_bar.stale_after_seconds == 3600.0
        assert cfg.daily_bar.warn_after_seconds == 1800.0


class TestConfigValidation:
    def test_invalid_bool_env_falls_back_to_default(self, monkeypatch):
        # A typo must not silently disable fail-closed blocking.
        monkeypatch.setenv("FRESHNESS_BLOCK_ON_UNKNOWN", "ture")
        assert load_freshness_config().block_on_unknown is True

    def test_valid_falsy_bool_env(self, monkeypatch):
        monkeypatch.setenv("FRESHNESS_BLOCK_ON_UNKNOWN", "false")
        assert load_freshness_config().block_on_unknown is False

    def test_tier_threshold_rejects_negative(self):
        with pytest.raises(ValueError):
            TierThreshold(warn_after_seconds=-1.0, stale_after_seconds=600.0)

    def test_tier_threshold_rejects_warn_gt_stale(self):
        with pytest.raises(ValueError):
            TierThreshold(warn_after_seconds=900.0, stale_after_seconds=600.0)


# ── 8. fail-closed behavior ───────────────────────────────────────────────

class TestFailClosed:
    def test_block_on_unknown_false_allows_unknown(self):
        gate = _gate(_config(block_on_unknown=False))
        res = gate.assert_tradeable("NEVER", source="get_price",
                                    raise_on_block=False, now=_NOW)
        assert res.state == StaleState.UNKNOWN
        assert gate.is_blocking(res) is False

    def test_internal_error_is_treated_as_stale(self, monkeypatch):
        """An unexpected error during evaluation must fail closed (STALE)."""
        gate = _gate()

        def _boom(*a, **k):
            raise RuntimeError("classifier exploded")

        monkeypatch.setattr(gate.service._health, "get", _boom)
        res = gate.assert_tradeable("SPY", source="get_price",
                                    raise_on_block=False, now=_NOW)
        assert res.state == StaleState.STALE
        assert gate.is_blocking(res) is True


# ── 9. kill-switch integration ────────────────────────────────────────────

class TestKillSwitchIntegration:
    def test_halt_callback_fires_on_critical_stale(self):
        fired = []
        gate = _gate(halt_callback=lambda res, src: fired.append((res.state, src)))
        df = _daily_df(_NOW - timedelta(days=5))
        gate.validate_dataframe("SPY", df, source="yfinance",
                                raise_on_block=False, now=_NOW)
        assert len(fired) == 1
        assert fired[0][0] == StaleState.STALE
        assert fired[0][1] == "yfinance"

    def test_halt_callback_not_fired_on_unknown(self):
        fired = []
        gate = _gate(halt_callback=lambda res, src: fired.append(res))
        gate.assert_tradeable("NEVER", source="get_price",
                              raise_on_block=False, now=_NOW)
        assert fired == []  # UNKNOWN blocks the symbol but does not halt engine

    def test_real_kill_switch_transitions_to_halted(self):
        from backend.risk.kill_switch import KillSwitch, TradingState, OrderIntent, InterceptDecision
        ks = KillSwitch()
        gate = _gate(halt_callback=make_kill_switch_halt_callback(ks))
        assert ks.state == TradingState.RUNNING
        gate.validate_dataframe("SPY", _daily_df(_NOW - timedelta(days=5)),
                                source="yfinance", raise_on_block=False, now=_NOW)
        assert ks.state == TradingState.HALTED
        assert ks.check_order(OrderIntent.NEW).decision is InterceptDecision.BLOCK


# ── tier isolation: same symbol, different tiers ──────────────────────────

class TestTierIsolation:
    def test_daily_fresh_intraday_stale_coexist(self):
        gate = _gate()
        # A bar that is 1h old: fresh for DAILY (72h), stale for INTRADAY (600s)
        ts = _NOW - timedelta(hours=1)
        daily = gate.validate_timestamp("SPY", ts, tier=FreshnessTier.DAILY_BAR,
                                        source="yfinance", raise_on_block=False, now=_NOW)
        intraday = gate.validate_timestamp("SPY", ts, tier=FreshnessTier.INTRADAY_BAR,
                                           source="live_bar", raise_on_block=False, now=_NOW)
        assert daily.state == StaleState.FRESH
        assert intraday.state == StaleState.STALE
