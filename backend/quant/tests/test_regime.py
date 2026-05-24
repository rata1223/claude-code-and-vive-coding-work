"""
Unit tests: RegimeDetector, FundamentalFilter, TrendCTASignal, VolExpansionSignal,
MeanReversionSignal, LivePipeline kill switch / cooldown.
"""
import pandas as pd
import numpy as np
import pytest
from datetime import date, timedelta

from backend.quant.signals.regime import RegimeDetector, RegimeOutput
from backend.quant.signals.fundamental import FundamentalFilter, fundamental_weight
from backend.quant.signals.trend_cta import TrendCTASignal
from backend.quant.signals.vol_expansion import VolExpansionSignal
from backend.quant.signals.mean_reversion import MeanReversionSignal
from backend.quant.signals.base import SignalOutput
from backend.quant.live.pipeline import LiveConfig, RiskState


def make_df(n=300, trend="up", seed=42, volatility=1.0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    if trend == "up":
        close = pd.Series(100 + np.linspace(0, 50, n) + rng.standard_normal(n) * 2 * volatility,
                          index=idx)
    elif trend == "down":
        close = pd.Series(150 - np.linspace(0, 50, n) + rng.standard_normal(n) * 2 * volatility,
                          index=idx)
    else:
        close = pd.Series(100 + rng.standard_normal(n).cumsum() * volatility, index=idx)
    high = close + abs(rng.standard_normal(n)) * 0.5 * volatility
    low = close - abs(rng.standard_normal(n)) * 0.5 * volatility
    vol = rng.integers(100_000, 500_000, n).astype(float)
    return pd.DataFrame({"Open": close, "High": high, "Low": low,
                         "Close": close, "Volume": vol}, index=idx)


# ── RegimeDetector ────────────────────────────────────────────────────────────

class TestRegimeDetector:
    def test_returns_regime_output(self):
        df = make_df(300)
        result = RegimeDetector().detect(df)
        assert isinstance(result, RegimeOutput)
        assert result.regime in (RegimeDetector.TREND, RegimeDetector.RANGE, RegimeDetector.STRESS)

    def test_score_bounded(self):
        df = make_df(300)
        result = RegimeDetector().detect(df)
        assert 0.0 <= result.score <= 1.0

    def test_insufficient_data_returns_range(self):
        df = make_df(30)
        result = RegimeDetector().detect(df)
        assert result.regime == RegimeDetector.RANGE
        assert "insufficient_data" in result.meta.get("reason", "")

    def test_high_volatility_triggers_stress(self):
        # stress_threshold=0.0 → 항상 stress
        detector = RegimeDetector(stress_threshold=0.0)
        df = make_df(300)
        result = detector.detect(df)
        assert result.regime == RegimeDetector.STRESS

    def test_strong_trend_detected(self):
        # 명확한 상승추세 + trend_threshold 낮게 설정
        df = make_df(300, trend="up", volatility=0.3)
        detector = RegimeDetector(trend_threshold=0.3, stress_threshold=0.99)
        result = detector.detect(df)
        assert result.regime == RegimeDetector.TREND

    def test_adx_percentile_in_range(self):
        df = make_df(300)
        result = RegimeDetector().detect(df)
        assert 0.0 <= result.adx_pct <= 1.0

    def test_ema_slope_bounded(self):
        df = make_df(300)
        result = RegimeDetector().detect(df)
        assert -1.0 <= result.ema_slope <= 1.0


# ── FundamentalFilter ─────────────────────────────────────────────────────────

class TestFundamentalFilter:
    def test_returns_float(self):
        ff = FundamentalFilter()
        # yfinance 없거나 실패해도 기본값 1.0 반환
        w = ff.get_weight("INVALID_SYMBOL_XYZ999")
        assert isinstance(w, float)
        assert 0.3 <= w <= 1.5

    def test_cache_reuse(self):
        ff = FundamentalFilter(cache_ttl_hours=1)
        w1 = ff.get_weight("INVALID_SYMBOL_XYZ999")
        w2 = ff.get_weight("INVALID_SYMBOL_XYZ999")
        assert w1 == w2  # 캐시에서 동일 값

    def test_cache_clear(self):
        ff = FundamentalFilter()
        ff.get_weight("INVALID_SYMBOL_XYZ999")
        ff.clear_cache()
        assert len(ff._cache) == 0

    def test_is_fundamentally_sound_threshold(self):
        ff = FundamentalFilter()
        # 낮은 임계값 → 실패해도 True일 수 있음
        result = ff.is_fundamentally_sound("INVALID_SYMBOL_XYZ999", min_weight=0.0)
        assert isinstance(result, bool)


# ── TrendCTASignal ────────────────────────────────────────────────────────────

class TestTrendCTASignal:
    def test_returns_signal_output(self):
        df = make_df(300)
        result = TrendCTASignal().compute(df, "TEST")
        assert isinstance(result, SignalOutput)
        assert result.signal in (-1, 0, 1)

    def test_insufficient_data_returns_neutral(self):
        df = make_df(20)
        result = TrendCTASignal().compute(df, "TEST")
        assert result.signal == 0

    def test_strong_uptrend_bias_buy(self):
        # 명확한 상승 + 낮은 ADX 임계값
        df = make_df(300, trend="up", volatility=0.1)
        result = TrendCTASignal(adx_threshold=5.0).compute(df, "TEST")
        # 상승추세에서 매도 신호 없음
        assert result.signal >= 0

    def test_strength_bounded(self):
        df = make_df(300)
        result = TrendCTASignal().compute(df, "TEST")
        assert 0.0 <= result.strength <= 1.0


# ── VolExpansionSignal ────────────────────────────────────────────────────────

class TestVolExpansionSignal:
    def test_returns_signal_output(self):
        df = make_df(100)
        result = VolExpansionSignal().compute(df, "TEST")
        assert isinstance(result, SignalOutput)
        assert result.signal in (-1, 0, 1)

    def test_insufficient_data_returns_neutral(self):
        df = make_df(10)
        result = VolExpansionSignal().compute(df, "TEST")
        assert result.signal == 0

    def test_strength_bounded(self):
        df = make_df(200)
        result = VolExpansionSignal().compute(df, "TEST")
        assert 0.0 <= result.strength <= 1.0

    def test_low_expansion_factor_triggers(self):
        # expansion_factor=1.0 → 항상 확장으로 간주
        df = make_df(200, trend="up", volatility=0.3)
        result = VolExpansionSignal(expansion_factor=1.0).compute(df, "TEST")
        assert isinstance(result, SignalOutput)


# ── MeanReversionSignal ───────────────────────────────────────────────────────

class TestMeanReversionSignal:
    def test_returns_signal_output(self):
        df = make_df(100)
        result = MeanReversionSignal().compute(df, "TEST")
        assert isinstance(result, SignalOutput)
        assert result.signal in (-1, 0, 1)

    def test_insufficient_data_returns_neutral(self):
        df = make_df(15)
        result = MeanReversionSignal().compute(df, "TEST")
        assert result.signal == 0

    def test_strength_bounded(self):
        df = make_df(200)
        result = MeanReversionSignal().compute(df, "TEST")
        assert 0.0 <= result.strength <= 1.0


# ── LivePipeline 리스크 로직 ──────────────────────────────────────────────────

class TestLivePipelineRisk:
    def _make_state(self) -> RiskState:
        return RiskState()

    def test_kill_switch_triggers_on_consecutive_losses(self):
        from backend.quant.live.pipeline import LivePipeline, LiveConfig
        from unittest.mock import MagicMock

        cfg = LiveConfig(symbols=["SPY"], max_consecutive_losses=3, dry_run=True)
        broker = MagicMock()
        pipeline = LivePipeline(broker=broker, config=cfg)

        pipeline.record_trade_pnl(-1000)
        pipeline.record_trade_pnl(-1000)
        assert not pipeline.risk_state.kill_switch_active
        pipeline.record_trade_pnl(-1000)
        assert pipeline.risk_state.kill_switch_active

    def test_kill_switch_reset(self):
        from backend.quant.live.pipeline import LivePipeline, LiveConfig
        from unittest.mock import MagicMock

        cfg = LiveConfig(symbols=["SPY"], max_consecutive_losses=2, dry_run=True)
        broker = MagicMock()
        pipeline = LivePipeline(broker=broker, config=cfg)

        pipeline.record_trade_pnl(-1000)
        pipeline.record_trade_pnl(-1000)
        assert pipeline.risk_state.kill_switch_active

        pipeline.reset_kill_switch()
        assert not pipeline.risk_state.kill_switch_active

    def test_win_resets_consecutive_loss_counter(self):
        from backend.quant.live.pipeline import LivePipeline, LiveConfig
        from unittest.mock import MagicMock

        cfg = LiveConfig(symbols=["SPY"], max_consecutive_losses=5, dry_run=True)
        broker = MagicMock()
        pipeline = LivePipeline(broker=broker, config=cfg)

        pipeline.record_trade_pnl(-1000)
        pipeline.record_trade_pnl(-1000)
        assert pipeline.risk_state.consecutive_losses == 2

        pipeline.record_trade_pnl(500)
        assert pipeline.risk_state.consecutive_losses == 0

    def test_cooldown_tracking(self):
        state = RiskState()
        today = date.today()
        state.last_sell_date["SPY"] = today

        cooldown_days = 3
        cooldown_cutoff = today - timedelta(days=cooldown_days)
        blocked = {
            sym for sym, sell_date in state.last_sell_date.items()
            if sell_date > cooldown_cutoff
        }
        assert "SPY" in blocked

    def test_cooldown_expires(self):
        state = RiskState()
        old_date = date.today() - timedelta(days=5)
        state.last_sell_date["SPY"] = old_date

        cooldown_days = 3
        cooldown_cutoff = date.today() - timedelta(days=cooldown_days)
        blocked = {
            sym for sym, sell_date in state.last_sell_date.items()
            if sell_date > cooldown_cutoff
        }
        assert "SPY" not in blocked
