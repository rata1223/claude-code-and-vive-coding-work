"""
Integration tests: 백테스트 엔진 + 성과 지표 + Walk-Forward.
"""
import pandas as pd
import numpy as np
import pytest

from backend.quant.backtest.engine import BacktestConfig, BacktestEngine
from backend.quant.backtest.metrics import cagr, max_drawdown, sharpe, summarize


def make_df(n=500, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    close = pd.Series(100 + rng.standard_normal(n).cumsum(), index=idx)
    close = close.clip(lower=1.0)
    high = close + abs(rng.standard_normal(n)) * 0.3
    low = close - abs(rng.standard_normal(n)) * 0.3
    return pd.DataFrame({"Open": close, "High": high, "Low": low,
                         "Close": close, "Volume": np.ones(n) * 1e6}, index=idx)


def buy_and_hold_signal(df):
    sig = pd.Series(0, index=df.index)
    sig.iloc[0] = 1  # 첫날 매수, 이후 유지
    return sig


def alternating_signal(df):
    sig = pd.Series(0, index=df.index)
    for i in range(0, len(sig), 10):
        sig.iloc[i] = 1
    for i in range(5, len(sig), 10):
        sig.iloc[i] = -1
    return sig


class TestBacktestMetrics:
    def test_cagr_positive_equity(self):
        eq = pd.Series([100, 110, 121, 133])
        result = cagr(eq)
        assert result > 0

    def test_max_drawdown_negative(self):
        eq = pd.Series([100, 90, 80, 95, 100])
        mdd = max_drawdown(eq)
        assert mdd < 0
        assert abs(mdd) <= 1.0

    def test_sharpe_zero_std(self):
        eq = pd.Series([100] * 10)
        s = sharpe(eq)
        assert s == 0.0

    def test_summarize_keys(self):
        eq = pd.Series([100.0, 105.0, 102.0, 108.0] * 50)
        result = summarize(eq, [], 100.0)
        for k in ("total_return_pct", "cagr_pct", "sharpe", "max_drawdown_pct",
                  "win_rate_pct", "total_trades", "final_equity"):
            assert k in result


class TestBacktestEngine:
    def setup_method(self):
        self.df = make_df(500)
        self.config = BacktestConfig(initial_capital=1_000_000, stop_loss_pct=0.1)
        self.engine = BacktestEngine(self.config)

    def test_buy_hold_equity_curve_length(self):
        sig = buy_and_hold_signal(self.df)
        result = self.engine.run(self.df, sig, "TEST")
        assert len(result.equity_curve) == len(self.df) - 1

    def test_equity_starts_near_initial(self):
        sig = buy_and_hold_signal(self.df)
        result = self.engine.run(self.df, sig, "TEST")
        assert abs(result.equity_curve.iloc[0] - self.config.initial_capital) / self.config.initial_capital < 0.1

    def test_no_trades_when_all_zero_signal(self):
        sig = pd.Series(0, index=self.df.index)
        result = self.engine.run(self.df, sig, "TEST")
        assert len(result.trades) == 0

    def test_alternating_produces_trades(self):
        sig = alternating_signal(self.df)
        result = self.engine.run(self.df, sig, "TEST")
        assert len(result.trades) > 0

    def test_equity_never_negative(self):
        sig = alternating_signal(self.df)
        result = self.engine.run(self.df, sig, "TEST")
        assert (result.equity_curve > 0).all()

    def test_metrics_present(self):
        sig = alternating_signal(self.df)
        result = self.engine.run(self.df, sig, "TEST")
        assert "sharpe" in result.metrics
        assert "max_drawdown_pct" in result.metrics

    def test_commission_reduces_capital(self):
        """수수료가 있으면 무수수료보다 최종 자산이 낮아야 함."""
        sig = alternating_signal(self.df)
        r_with = self.engine.run(self.df, sig)
        engine_free = BacktestEngine(BacktestConfig(commission=0.0, slippage=0.0))
        r_free = engine_free.run(self.df, sig)
        # 거래가 있는 경우에만 비교
        if r_with.trades:
            assert r_with.metrics["final_equity"] <= r_free.metrics["final_equity"]


class TestWalkForward:
    def test_wfo_runs_without_error(self):
        from backend.quant.backtest.walk_forward import WalkForwardOptimizer
        from backend.quant.indicators.trend import sma_cross

        df = make_df(700)

        def signal_fn(df, params):
            return sma_cross(df, fast=params["fast"], slow=params["slow"]).signal

        param_grid = [
            {"fast": 10, "slow": 50},
            {"fast": 20, "slow": 100},
        ]
        wfo = WalkForwardOptimizer(signal_fn, param_grid,
                                   config=BacktestConfig(initial_capital=1_000_000))
        result = wfo.run(df, is_bars=252, oos_bars=63)
        assert result.combined_metrics is not None
        assert len(result.best_params_per_window) > 0

    def test_monte_carlo_returns_stats(self):
        from backend.quant.backtest.walk_forward import monte_carlo_robustness
        from backend.quant.backtest.engine import BacktestResult, Trade
        trades = [Trade("X", "2020-01-01", "2020-02-01", "long", 10, 100.0, 105.0, 500.0, "signal"),
                  Trade("X", "2020-03-01", "2020-04-01", "long", 10, 100.0, 95.0, -500.0, "stop_loss")]
        eq = pd.Series([1_000_000.0, 1_000_500.0, 1_000_000.0])
        result = BacktestResult(equity_curve=eq, trades=trades, metrics={},
                                config=BacktestConfig())
        mc = monte_carlo_robustness(result, n_simulations=100)
        assert "sharpe_mean" in mc
        assert "mdd_worst_pct" in mc
