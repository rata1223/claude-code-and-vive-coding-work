"""
Integration test: the unified FreshnessGate (R-11) blocks the live
IndicatorStrategy scan→trade path on stale data, and lets fresh data through.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.data.freshness_config import FreshnessConfig, TierThreshold
from backend.data.freshness_gate import FreshnessGate, set_freshness_gate
from backend.strategy.indicator.strategy import IndicatorStrategy


def _fresh_gate():
    t = TierThreshold(warn_after_seconds=300.0, stale_after_seconds=600.0)
    daily = TierThreshold(warn_after_seconds=26 * 3600.0, stale_after_seconds=72 * 3600.0)
    cfg = FreshnessConfig(intraday_quote=t, intraday_bar=t, daily_bar=daily,
                          block_on_unknown=True)
    return FreshnessGate(config=cfg)


@pytest.fixture(autouse=True)
def _reset_gate():
    set_freshness_gate(_fresh_gate())
    yield
    set_freshness_gate(None)


class _FakeTracker:
    def all_positions(self):
        return []


class _LiveBroker:
    is_live = True

    def get_balance(self):
        return SimpleNamespace(total_eval_krw=5_000_000.0)


class _BuyFusion:
    """Always emits a buy signal, so the only thing that can suppress a trade
    is the freshness gate."""
    def evaluate(self, df, symbol=""):
        return SimpleNamespace(signal=1, score=0.9, strength=0.9)


def _df(last_ts, n=60):
    idx = pd.date_range(end=last_ts, periods=n, freq="D")
    return pd.DataFrame({"Close": range(1, n + 1)}, index=idx)


def _make_strategy(monkeypatch, last_ts):
    # loader.fetch → a df whose freshness is controlled by last_ts
    monkeypatch.setattr("backend.quant.data.loader.DataLoader.fetch",
                        lambda self, *a, **k: _df(last_ts))
    # default_fusion → always-buy fusion (removes signal nondeterminism)
    monkeypatch.setattr("backend.quant.signals.fusion.default_fusion",
                        lambda: _BuyFusion())
    strat = IndicatorStrategy(
        broker=_LiveBroker(), tracker=_FakeTracker(), machine=None,
        name="t", config={"universe": ["SPY"], "position_size_pct": 0.05},
    )
    executed = []
    monkeypatch.setattr(strat, "_execute_buy", lambda sym, cap=None: executed.append(("buy", sym)))
    monkeypatch.setattr(strat, "_execute_sell", lambda sym, reason: executed.append(("sell", sym)))
    return strat, executed


def test_fresh_data_allows_buy(monkeypatch):
    now = datetime.now(timezone.utc)
    strat, executed = _make_strategy(monkeypatch, now - timedelta(hours=10))
    strat._scan_and_trade()
    assert ("buy", "SPY") in executed


def test_stale_data_blocks_buy(monkeypatch):
    now = datetime.now(timezone.utc)
    strat, executed = _make_strategy(monkeypatch, now - timedelta(days=5))
    strat._scan_and_trade()
    assert executed == []  # freshness gate suppressed the signal entirely


def test_stale_data_blocks_order_sizing(monkeypatch):
    """Even if a buy candidate slips through, _is_feed_tradeable blocks sizing."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr("backend.quant.data.loader.DataLoader.fetch",
                        lambda self, *a, **k: _df(now - timedelta(days=5)))
    strat = IndicatorStrategy(
        broker=_LiveBroker(), tracker=_FakeTracker(), machine=None,
        name="t", config={"universe": ["SPY"], "position_size_pct": 0.05},
    )
    # record the stale feed first (as the scan would), then check the sizing gate
    strat._is_data_stale("SPY", _df(now - timedelta(days=5)))
    assert strat._is_feed_tradeable("SPY") is False
