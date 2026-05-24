"""
Unit tests: PositionSizer + PortfolioAllocator.
"""
import numpy as np
import pandas as pd
import pytest

from backend.quant.risk.position_sizer import PositionSizer, transaction_cost, effective_price


def make_df(n=100, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=n, freq="B")
    close = pd.Series(50_000 + rng.standard_normal(n).cumsum() * 500, index=idx)
    high = close + 300
    low = close - 300
    return pd.DataFrame({"Open": close, "High": high, "Low": low,
                         "Close": close, "Volume": np.ones(n) * 1e6}, index=idx)


class TestTransactionCosts:
    def test_commission_calculation(self):
        cost = transaction_cost(price=100.0, qty=100, commission=0.00015, slippage=0.0)
        assert abs(cost - 1.5) < 0.01

    def test_slippage_buy_increases_price(self):
        p = effective_price(100.0, "buy", slippage=0.001)
        assert p > 100.0

    def test_slippage_sell_decreases_price(self):
        p = effective_price(100.0, "sell", slippage=0.001)
        assert p < 100.0


class TestPositionSizer:
    def setup_method(self):
        self.df = make_df(100)
        self.sizer = PositionSizer(capital=10_000_000, max_position_pct=0.05)

    def test_atr_based_returns_dict(self):
        result = self.sizer.atr_based(self.df)
        assert "qty" in result
        assert "stop_price" in result
        assert result["qty"] >= 1

    def test_atr_stop_below_entry(self):
        result = self.sizer.atr_based(self.df)
        assert result["stop_price"] < result["entry_price"]

    def test_max_position_pct_respected(self):
        result = self.sizer.atr_based(self.df)
        max_allowed = self.sizer.capital * self.sizer.max_position_pct
        assert result["position_value"] <= max_allowed * 1.01  # 1% 허용 오차

    def test_fixed_fraction_qty_positive(self):
        result = self.sizer.fixed_fraction(self.df, pct=0.02)
        assert result["qty"] >= 1

    def test_kelly_with_valid_stats(self):
        result = self.sizer.kelly_based(win_rate=0.55, avg_win=1000, avg_loss=500, df=self.df)
        assert result["qty"] >= 1
        assert result["method"] == "kelly"

    def test_kelly_zero_avg_loss_fallback(self):
        result = self.sizer.kelly_based(win_rate=0.6, avg_win=1000, avg_loss=0, df=self.df)
        assert result["method"] == "fallback"


class TestPortfolioAllocator:
    def test_equal_weight_single_symbol(self):
        from backend.quant.risk.portfolio import PortfolioAllocator
        alloc = PortfolioAllocator(total_capital=1_000_000, method="equal")
        result = alloc.allocate(["SPY"])
        assert "SPY" in result
        assert result["SPY"] == 1_000_000

    def test_equal_weight_multiple(self):
        from backend.quant.risk.portfolio import PortfolioAllocator
        alloc = PortfolioAllocator(total_capital=1_000_000, method="equal")
        result = alloc.allocate(["A", "B", "C", "D"])
        total = sum(result.values())
        assert abs(total - 1_000_000) < 100  # 반올림 오차 허용

    def test_empty_returns_empty(self):
        from backend.quant.risk.portfolio import PortfolioAllocator
        alloc = PortfolioAllocator(total_capital=1_000_000)
        result = alloc.allocate([])
        assert result == {}
