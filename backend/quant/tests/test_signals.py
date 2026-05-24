"""
Unit tests: 신호 모듈 + SignalFusion 스코어링.
"""
import pandas as pd
import numpy as np
import pytest

from backend.quant.signals.base import SignalOutput

pandas_ta_available = False
try:
    import pandas_ta  # noqa
    pandas_ta_available = True
except ImportError:
    pass


def make_df(n=300, trend="up", seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    if trend == "up":
        close = pd.Series(100 + np.linspace(0, 50, n) + rng.standard_normal(n) * 2, index=idx)
    elif trend == "down":
        close = pd.Series(150 - np.linspace(0, 50, n) + rng.standard_normal(n) * 2, index=idx)
    else:
        close = pd.Series(100 + rng.standard_normal(n).cumsum(), index=idx)

    high = close + abs(rng.standard_normal(n)) * 0.5
    low = close - abs(rng.standard_normal(n)) * 0.5
    vol = rng.integers(100_000, 500_000, n).astype(float)
    return pd.DataFrame({"Open": close, "High": high, "Low": low,
                         "Close": close, "Volume": vol}, index=idx)


class TestSignalOutputStructure:
    def test_signal_output_valid_range(self):
        out = SignalOutput(symbol="SPY", signal=1, strength=0.8)
        assert out.signal in (-1, 0, 1)
        assert 0.0 <= out.strength <= 1.0

    def test_signal_output_defaults(self):
        out = SignalOutput(symbol="AAPL", signal=0, strength=0.0)
        assert out.indicators == {}
        assert out.meta == {}


@pytest.mark.skipif(not pandas_ta_available, reason="pandas_ta not installed")
class TestTrendFollowingSignal:
    def test_insufficient_data_returns_neutral(self):
        from backend.quant.signals.trend_following import TrendFollowingSignal
        df = make_df(100)
        result = TrendFollowingSignal().compute(df, "TEST")
        assert result.signal == 0

    def test_uptrend_produces_buy_or_neutral(self):
        from backend.quant.signals.trend_following import TrendFollowingSignal
        df = make_df(300, trend="up")
        result = TrendFollowingSignal().compute(df, "TEST")
        assert result.signal >= 0  # 상승 추세에서 매도 신호 없음

    def test_downtrend_never_strong_buy(self):
        from backend.quant.signals.trend_following import TrendFollowingSignal
        df = make_df(300, trend="down")
        result = TrendFollowingSignal().compute(df, "TEST")
        # 하락 추세에서 strength=1.0 매수 없음
        assert not (result.signal == 1 and result.strength == 1.0)


@pytest.mark.skipif(not pandas_ta_available, reason="pandas_ta not installed")
class TestMomentumSignal:
    def test_needs_252_bars(self):
        from backend.quant.signals.momentum import MomentumSignal
        df = make_df(200)
        result = MomentumSignal().compute(df)
        assert result.signal == 0

    def test_returns_signal_output(self):
        from backend.quant.signals.momentum import MomentumSignal
        df = make_df(300)
        result = MomentumSignal().compute(df, "SPY")
        assert isinstance(result, SignalOutput)
        assert result.signal in (-1, 0, 1)


@pytest.mark.skipif(not pandas_ta_available, reason="pandas_ta not installed")
class TestSignalFusion:
    def setup_method(self):
        from backend.quant.signals.fusion import SignalFusion
        from backend.quant.signals.trend_following import TrendFollowingSignal
        from backend.quant.signals.momentum import MomentumSignal

        self.fusion = SignalFusion(buy_threshold=0.2, sell_threshold=-0.2)
        self.fusion.add(TrendFollowingSignal(), weight=0.6)
        self.fusion.add(MomentumSignal(), weight=0.4)

    def test_evaluate_returns_fusion_result(self):
        from backend.quant.signals.fusion import FusionResult
        df = make_df(300)
        result = self.fusion.evaluate(df, "SPY")
        assert isinstance(result, FusionResult)
        assert result.signal in (-1, 0, 1)
        assert -1.0 <= result.score <= 1.0

    def test_regime_filter_blocks_buy(self):
        from backend.quant.signals.fusion import SignalFusion
        from backend.quant.signals.trend_following import TrendFollowingSignal
        fusion = SignalFusion()
        fusion.add(TrendFollowingSignal(), weight=1.0)
        fusion.set_regime_filter(lambda df: False)  # 항상 차단
        df = make_df(300, trend="up")
        result = fusion.evaluate(df, "SPY")
        assert result.signal != 1 or result.regime_blocked

    def test_scan_returns_sorted_list(self):
        df_up = make_df(300, trend="up", seed=1)
        df_dn = make_df(300, trend="down", seed=2)
        results = self.fusion.scan({"A": df_up, "B": df_dn})
        assert len(results) == 2
        # 강한 신호가 앞에 정렬
        assert results[0].score >= results[1].score

    def test_default_fusion_returns_result(self):
        from backend.quant.signals.fusion import default_fusion
        df = make_df(300)
        result = default_fusion().evaluate(df, "SPY")
        assert result.signal in (-1, 0, 1)
