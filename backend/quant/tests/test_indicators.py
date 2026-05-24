"""
Unit tests: 지표 라이브러리 — no-lookahead, 리페인트 방지 검증.
"""
import pandas as pd
import numpy as np
import pytest

from backend.quant.indicators.base import no_lookahead, safe_sma, safe_ema


def make_df(n=300, seed=42):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="B")
    close = pd.Series(100 + rng.standard_normal(n).cumsum(), index=idx)
    high = close + abs(rng.standard_normal(n)) * 0.5
    low = close - abs(rng.standard_normal(n)) * 0.5
    vol = (rng.integers(100_000, 1_000_000, n)).astype(float)
    return pd.DataFrame({"Open": close, "High": high, "Low": low,
                         "Close": close, "Volume": vol}, index=idx)


class TestNoLookahead:
    def test_shift_by_one(self):
        s = pd.Series([1, 2, 3, 4, 5])
        shifted = no_lookahead(s)
        assert pd.isna(shifted.iloc[0])
        assert shifted.iloc[1] == 1
        assert shifted.iloc[4] == 4

    def test_last_value_is_prev_bar(self):
        s = pd.Series([10.0, 20.0, 30.0])
        shifted = no_lookahead(s)
        assert shifted.iloc[-1] == 20.0


pandas_ta_available = False
try:
    import pandas_ta  # noqa
    pandas_ta_available = True
except ImportError:
    pass


class TestTrendIndicators:
    def setup_method(self):
        self.df = make_df(300)

    def test_sma_cross_returns_indicator_result(self):
        from backend.quant.indicators.trend import sma_cross
        result = sma_cross(self.df, fast=20, slow=50)
        assert result.name.startswith("SMA")
        assert not result.signal.empty
        assert result.signal.dropna().isin([-1, 0, 1]).all()

    def test_ema_trend_signal_bounded(self):
        from backend.quant.indicators.trend import ema_trend
        result = ema_trend(self.df, length=50)
        assert result.signal.dropna().isin([-1, 0, 1]).all()

    @pytest.mark.skipif(not pandas_ta_available, reason="pandas_ta not installed")
    def test_macd_signal_no_future_data(self):
        from backend.quant.indicators.trend import macd_signal
        result = macd_signal(self.df)
        first_valid = result.signal.dropna().index[0]
        assert first_valid > self.df.index[0]

    def test_ichimoku_needs_enough_data(self):
        from backend.quant.indicators.trend import ichimoku_regime
        small_df = make_df(30)
        result = ichimoku_regime(small_df)
        assert (result.signal.dropna() == 0).all() or result.signal.dropna().empty


class TestVolatilityIndicators:
    def setup_method(self):
        self.df = make_df(100)

    @pytest.mark.skipif(not pandas_ta_available, reason="pandas_ta not installed")
    def test_atr_no_lookahead(self):
        from backend.quant.indicators.volatility import atr
        atr_series = atr(self.df)
        assert pd.isna(atr_series.iloc[0])

    @pytest.mark.skipif(not pandas_ta_available, reason="pandas_ta not installed")
    def test_bollinger_signal_bounded(self):
        from backend.quant.indicators.volatility import bollinger_bands
        result = bollinger_bands(self.df)
        assert result.signal.dropna().isin([-1, 0, 1]).all()

    @pytest.mark.skipif(not pandas_ta_available, reason="pandas_ta not installed")
    def test_atr_stop_long(self):
        from backend.quant.indicators.volatility import atr_stop
        stop = atr_stop(self.df, entry_price=100.0, side="long", multiplier=2.0)
        assert stop < 100.0


class TestMomentumIndicators:
    def setup_method(self):
        self.df = make_df(300)

    @pytest.mark.skipif(not pandas_ta_available, reason="pandas_ta not installed")
    def test_rsi_range(self):
        from backend.quant.indicators.momentum import rsi_signal
        result = rsi_signal(self.df)
        vals = result.values.dropna()
        assert (vals >= 0).all() and (vals <= 100).all()

    def test_momentum_12_1_needs_252(self):
        from backend.quant.indicators.momentum import momentum_12_1
        small = make_df(100)
        result = momentum_12_1(small)
        assert result.values.empty

    def test_momentum_12_1_no_lookahead(self):
        from backend.quant.indicators.momentum import momentum_12_1
        result = momentum_12_1(self.df)
        assert pd.isna(result.signal.iloc[0])
