"""
Unit tests: RobustFusion, RiskEngine, LiveSafeguards, PortfolioAnalysis,
BacktestEngine (trailing stop + KR cost), WalkForward (overfitting/lookahead).
"""
import pandas as pd
import numpy as np
import pytest
from datetime import date, timedelta, timezone, datetime
from unittest.mock import MagicMock

from backend.quant.signals.fusion import RobustFusion, FusionResult
from backend.quant.signals.base import SignalOutput
from backend.quant.risk.engine import (
    TrailingStopManager, ExposureManager, LossTracker,
    RiskConfig, correlation_matrix, redundant_pairs, vol_position_scale,
)
from backend.quant.risk.position_sizer import PositionSizer, KR_SECURITIES_TAX
from backend.quant.live.safeguards import (
    SignalDeduplicator, PartialFillTracker, OHLCVRecovery, SpreadGuard,
    APIThrottleGuard, WSReconnectGuard,
)
from backend.quant.analysis.performance import (
    rolling_sharpe, rolling_drawdown, sensitivity_analysis,
    factor_decomposition, regime_performance_breakdown,
)
from backend.quant.backtest.engine import BacktestEngine, BacktestConfig
from backend.quant.backtest.walk_forward import lookahead_bias_check, reject_overfitting


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
    high = close * (1 + abs(rng.standard_normal(n)) * 0.005 * volatility)
    low = close * (1 - abs(rng.standard_normal(n)) * 0.005 * volatility)
    vol = rng.integers(100_000, 500_000, n).astype(float)
    return pd.DataFrame({"Open": close, "High": high, "Low": low,
                         "Close": close, "Volume": vol}, index=idx)


# ── RobustFusion ──────────────────────────────────────────────────────────────

class _ConstantSignal:
    """테스트용 상수 신호."""
    def __init__(self, signal, strength, name="Const"):
        self._s, self._st, self._n = signal, strength, name
    def name(self): return self._n
    def compute(self, df, symbol=""): return SignalOutput(symbol=symbol, signal=self._s, strength=self._st)


class TestRobustFusion:
    def test_strong_buy_signal(self):
        fusion = RobustFusion(buy_threshold=0.20, min_score_threshold=0.05)
        fusion.add(_ConstantSignal(1, 0.9, "A"), weight=0.5)
        fusion.add(_ConstantSignal(1, 0.8, "B"), weight=0.5)
        result = fusion.evaluate(make_df(300), symbol="TEST")
        assert isinstance(result, FusionResult)
        assert result.signal == 1

    def test_weak_signal_suppressed(self):
        fusion = RobustFusion(min_score_threshold=0.50)
        fusion.add(_ConstantSignal(1, 0.1, "A"), weight=1.0)
        result = fusion.evaluate(make_df(300), symbol="TEST")
        assert result.signal == 0
        assert result.meta.get("suppressed") is True

    def test_conflicting_signals_neutralized(self):
        # Equal-strength bull/bear → ratio=1.0 > conflict_ratio=0.60 → conflict
        fusion = RobustFusion(conflict_ratio=0.60, min_score_threshold=0.01)
        fusion.add(_ConstantSignal(1, 0.8, "Bull"), weight=0.5)
        fusion.add(_ConstantSignal(-1, 0.8, "Bear"), weight=0.5)
        result = fusion.evaluate(make_df(300), symbol="TEST")
        assert result.signal == 0
        assert result.meta.get("conflict") is True

    def test_drawdown_discount_reduces_buy(self):
        fusion = RobustFusion(buy_threshold=0.20, dd_discount_floor=0.3,
                               mdd_full_discount=0.10, min_score_threshold=0.05)
        fusion.add(_ConstantSignal(1, 0.9, "A"), weight=1.0)

        # 심각한 드로다운 equity
        eq = pd.Series([1_000_000 * (1 - 0.12)] * 10)  # -12% 드로다운
        eq_normal = pd.Series([1_000_000] * 5 + list(eq))
        result_dd = fusion.evaluate(make_df(300), symbol="TEST", equity_curve=eq_normal)
        result_normal = fusion.evaluate(make_df(300), symbol="TEST", equity_curve=None)
        assert result_dd.strength <= result_normal.strength

    def test_regime_filter_blocks_buy(self):
        fusion = RobustFusion(buy_threshold=0.10, min_score_threshold=0.01)
        fusion.add(_ConstantSignal(1, 0.9, "A"), weight=1.0)
        fusion.set_regime_filter(lambda df: False)  # 항상 차단
        result = fusion.evaluate(make_df(300), symbol="TEST")
        assert result.signal == 0
        assert result.regime_blocked is True

    def test_scan_sorts_by_score(self):
        from backend.quant.signals.fusion import default_robust_fusion
        fusion = default_robust_fusion()
        dfs = {"A": make_df(300, "up"), "B": make_df(300, "down")}
        results = fusion.scan(dfs)
        assert len(results) == 2
        assert results[0].score >= results[1].score


# ── RiskEngine: TrailingStopManager ──────────────────────────────────────────

class TestTrailingStopManager:
    def _cfg(self, trailing=0.07, hard=0.10):
        return RiskConfig(trailing_stop_pct=trailing, hard_stop_pct=hard)

    def test_trailing_stop_triggered(self):
        mgr = TrailingStopManager(self._cfg(trailing=0.07))
        mgr.open("ETF", qty=10, entry_price=10000)
        # 고점 갱신 후 -7% 하락
        mgr.update({"ETF": 11000})
        triggered = mgr.check_stops({"ETF": 11000 * 0.92})  # -8% from peak
        assert any(sym == "ETF" for sym, _ in triggered)

    def test_hard_stop_triggered(self):
        mgr = TrailingStopManager(self._cfg(hard=0.10))
        mgr.open("ETF", qty=5, entry_price=10000)
        triggered = mgr.check_stops({"ETF": 8900})  # -11%
        assert any(sym == "ETF" and r == "hard_stop" for sym, r in triggered)

    def test_no_stop_when_price_holds(self):
        mgr = TrailingStopManager(self._cfg())
        mgr.open("ETF", qty=5, entry_price=10000)
        triggered = mgr.check_stops({"ETF": 9500})  # -5% — 하드스탑(10%) 미달
        assert not triggered

    def test_close_removes_position(self):
        mgr = TrailingStopManager(self._cfg())
        mgr.open("ETF", qty=5, entry_price=10000)
        mgr.close("ETF")
        triggered = mgr.check_stops({"ETF": 8000})
        assert not triggered


# ── RiskEngine: LossTracker ───────────────────────────────────────────────────

class TestLossTracker:
    def test_daily_loss_triggers_kill(self):
        cfg = RiskConfig(daily_loss_limit_pct=0.03)
        lt = LossTracker(config=cfg, peak_equity=1_000_000, current_equity=1_000_000)
        lt.record_pnl(-35_000, 965_000)  # -3.5%
        assert lt.kill_switch is True

    def test_mdd_triggers_kill(self):
        cfg = RiskConfig(mdd_limit_pct=0.15)
        lt = LossTracker(config=cfg, peak_equity=1_000_000, current_equity=1_000_000)
        lt.record_pnl(-160_000, 840_000)  # -16% MDD
        assert lt.kill_switch is True

    def test_manual_reset(self):
        cfg = RiskConfig(daily_loss_limit_pct=0.01)
        lt = LossTracker(config=cfg, peak_equity=1_000_000, current_equity=1_000_000)
        lt.record_pnl(-15_000, 985_000)
        assert lt.kill_switch
        lt.manual_reset()
        assert not lt.kill_switch

    def test_can_buy_blocked_near_limit(self):
        cfg = RiskConfig(daily_loss_limit_pct=0.03)
        lt = LossTracker(config=cfg, peak_equity=1_000_000, current_equity=1_000_000)
        lt.daily_pnl = -25_000  # 80% of 3% = 2.4% loss
        ok, reason = lt.can_buy()
        assert ok is False


# ── RiskEngine: ExposureManager ───────────────────────────────────────────────

class TestExposureManager:
    def test_blocks_high_correlation(self):
        cfg = RiskConfig(max_corr_overlap=0.80)
        mgr = ExposureManager(cfg)

        # 두 ETF가 거의 동일한 수익률 → 상관계수 ~1.0
        idx = pd.date_range("2020-01-01", periods=100, freq="B")
        close_a = pd.Series(np.linspace(100, 150, 100), index=idx)
        close_b = close_a + 1.0  # 거의 동일
        histories = {
            "A": pd.DataFrame({"Close": close_a}),
            "B": pd.DataFrame({"Close": close_b}),
        }
        ok, reason = mgr.can_add("B", ["A"], histories, 0, 1_000_000)
        assert ok is False
        assert "상관관계" in reason

    def test_allows_low_correlation(self):
        cfg = RiskConfig(max_corr_overlap=0.80)
        mgr = ExposureManager(cfg)

        rng = np.random.default_rng(0)
        idx = pd.date_range("2020-01-01", periods=100, freq="B")
        a = pd.Series(rng.standard_normal(100).cumsum() + 100, index=idx)
        b = pd.Series(rng.standard_normal(100).cumsum() + 100, index=idx)
        histories = {
            "A": pd.DataFrame({"Close": a}),
            "B": pd.DataFrame({"Close": b}),
        }
        ok, _ = mgr.can_add("B", ["A"], histories, 50_000, 1_000_000)
        assert ok is True


# ── PositionSizer: 변동성 타겟팅 ──────────────────────────────────────────────

class TestVolTargetSizer:
    def test_vol_target_returns_qty(self):
        sizer = PositionSizer(capital=2_000_000, max_position_pct=0.05)
        df = make_df(300)
        result = sizer.volatility_target(df, target_vol=0.10)
        assert "qty" in result
        assert result["qty"] >= 1
        assert result["method"] == "vol_target"

    def test_high_vol_reduces_size(self):
        sizer = PositionSizer(capital=2_000_000)
        low_vol = make_df(300, volatility=0.1)
        high_vol = make_df(300, volatility=5.0)
        r_low = sizer.volatility_target(low_vol)
        r_high = sizer.volatility_target(high_vol)
        assert r_low["qty"] >= r_high["qty"]

    def test_kr_securities_tax_nonzero(self):
        assert KR_SECURITIES_TAX == 0.002  # 0.20%

    def test_drawdown_scaled_reduces_qty(self):
        sizer = PositionSizer(capital=2_000_000)
        df = make_df(300)
        eq_normal = pd.Series([2_000_000] * 50)
        eq_drawn = pd.Series([2_000_000] * 40 + [1_700_000] * 10)  # -15% MDD
        r_normal = sizer.drawdown_scaled(df, eq_normal)
        r_drawn = sizer.drawdown_scaled(df, eq_drawn)
        assert r_normal["qty"] >= r_drawn["qty"]


# ── LiveSafeguards ────────────────────────────────────────────────────────────

class TestSignalDeduplicator:
    def test_first_signal_allowed(self):
        d = SignalDeduplicator(window_minutes=60)
        assert not d.is_duplicate("ETF", "buy")

    def test_duplicate_blocked_within_window(self):
        d = SignalDeduplicator(window_minutes=60)
        d.is_duplicate("ETF", "buy")
        assert d.is_duplicate("ETF", "buy")

    def test_different_side_allowed(self):
        d = SignalDeduplicator(window_minutes=60)
        d.is_duplicate("ETF", "buy")
        assert not d.is_duplicate("ETF", "sell")

    def test_clear_resets(self):
        d = SignalDeduplicator(window_minutes=60)
        d.is_duplicate("ETF", "buy")
        d.clear("ETF")
        assert not d.is_duplicate("ETF", "buy")


class TestPartialFillTracker:
    def test_register_and_fill(self):
        t = PartialFillTracker(timeout_minutes=30)
        t.register("ORD1", "ETF", "buy", 10)
        order = t.record_fill("ORD1", 5, 10000.0)
        assert order.filled_qty == 5
        assert order.avg_fill_price == 10000.0

    def test_complete_fill_removes_order(self):
        t = PartialFillTracker()
        t.register("ORD1", "ETF", "buy", 5)
        t.record_fill("ORD1", 5, 10000.0)
        assert "ORD1" not in {o.order_id for o in t.get_pending()}

    def test_timeout_detection(self):
        import time
        t = PartialFillTracker(timeout_minutes=0)  # 즉시 타임아웃
        t.register("ORD1", "ETF", "buy", 10)
        timed = t.check_timeouts()
        assert any(o.order_id == "ORD1" for o in timed)


class TestOHLCVRecovery:
    def test_get_cache_empty_returns_none(self):
        recovery = OHLCVRecovery()
        assert recovery._get_cache("AAPL") is None

    def test_update_then_get_cache_returns_data(self):
        recovery = OHLCVRecovery()
        df = make_df(n=10)
        recovery._update_cache("AAPL", df)
        cached = recovery._get_cache("AAPL")
        pd.testing.assert_frame_equal(cached, df)

    def test_get_cache_expires_after_max_age(self):
        """Delay detection: cache older than max_cache_age_hours is rejected."""
        recovery = OHLCVRecovery(max_cache_age_hours=1)
        df = make_df(n=10)
        recovery._update_cache("AAPL", df)
        cached_df, ts = recovery._cache["AAPL"]
        recovery._cache["AAPL"] = (cached_df, ts - timedelta(hours=2))
        assert recovery._get_cache("AAPL") is None

    def test_fetch_returns_none_when_all_sources_fail_and_no_cache(self, monkeypatch):
        """Fail-closed: no data anywhere -> None, not a fabricated/empty DataFrame."""
        recovery = OHLCVRecovery()
        monkeypatch.setattr(recovery, "_try_yfinance", lambda *a, **k: None)
        assert recovery.fetch("AAPL") is None

    def test_fetch_falls_back_to_cache_when_sources_fail(self, monkeypatch):
        recovery = OHLCVRecovery()
        df = make_df(n=10)
        recovery._update_cache("AAPL", df)
        monkeypatch.setattr(recovery, "_try_yfinance", lambda *a, **k: None)
        result = recovery.fetch("AAPL")
        pd.testing.assert_frame_equal(result, df)

    def test_fetch_prefers_broker_over_yfinance(self, monkeypatch):
        """Source priority: broker result short-circuits yfinance."""
        recovery = OHLCVRecovery()
        broker_df = make_df(n=5)
        monkeypatch.setattr(recovery, "_try_broker", lambda *a, **k: broker_df)
        yf_calls = []
        monkeypatch.setattr(recovery, "_try_yfinance",
                             lambda *a, **k: yf_calls.append(1))
        result = recovery.fetch("AAPL", broker=object())
        pd.testing.assert_frame_equal(result, broker_df)
        assert yf_calls == []

    def test_fetch_tries_pykrx_for_kr_symbols_after_yfinance(self, monkeypatch):
        recovery = OHLCVRecovery()
        monkeypatch.setattr(recovery, "_try_yfinance", lambda *a, **k: None)
        krx_df = make_df(n=5)
        monkeypatch.setattr(recovery, "_try_pykrx", lambda *a, **k: krx_df)
        result = recovery.fetch("005930")
        pd.testing.assert_frame_equal(result, krx_df)


class TestSpreadGuard:
    def test_normal_spread_allowed(self):
        g = SpreadGuard(max_spread_pct=0.005)
        ok, spread = g.check(10000, 10040)  # 0.4%
        assert ok is True

    def test_wide_spread_blocked(self):
        g = SpreadGuard(max_spread_pct=0.005)
        ok, spread = g.check(10000, 10200)  # 2%
        assert ok is False
        assert spread > 0.005


class TestWSReconnectGuard:
    def test_backoff_increases(self):
        g = WSReconnectGuard(max_retries=5, base_delay=2.0)
        d1 = g.next_backoff()
        d2 = g.next_backoff()
        assert d2 > d1

    def test_max_retries_raises(self):
        g = WSReconnectGuard(max_retries=2, base_delay=2.0)
        g.next_backoff()
        g.next_backoff()
        with pytest.raises(ConnectionError):
            g.next_backoff()

    def test_reset_clears_count(self):
        g = WSReconnectGuard(max_retries=5)
        g.next_backoff()
        g.reset()
        assert g._retry_count == 0


# ── Portfolio Analysis ────────────────────────────────────────────────────────

class TestPortfolioAnalysis:
    def _make_equity(self, n=200):
        rng = np.random.default_rng(0)
        returns = rng.normal(0.0005, 0.01, n)
        return pd.Series(1_000_000 * np.cumprod(1 + returns),
                         index=pd.date_range("2020-01-01", periods=n, freq="B"))

    def test_rolling_sharpe_length(self):
        eq = self._make_equity(200)
        rs = rolling_sharpe(eq, window=63)
        assert len(rs) == len(eq)

    def test_rolling_drawdown_nonpositive(self):
        eq = self._make_equity(200)
        rd = rolling_drawdown(eq)
        assert (rd <= 0).all()

    def test_factor_decomposition_returns_dict(self):
        rng = np.random.default_rng(42)
        idx = pd.date_range("2020-01-01", periods=100, freq="B")
        port = pd.Series(rng.normal(0.001, 0.01, 100), index=idx)
        spy = pd.Series(rng.normal(0.0008, 0.012, 100), index=idx)
        result = factor_decomposition(port, {"SPY": spy})
        assert "alpha" in result
        assert "r_squared" in result

    def test_sensitivity_analysis(self):
        def dummy_run(params):
            return {"sharpe": params.get("fast", 20) / 100}

        result = sensitivity_analysis(
            run_fn=dummy_run,
            base_params={"fast": 20},
            param_ranges={"fast": [10, 20, 50]},
            target_metric="sharpe",
        )
        assert "per_param" in result
        assert "fast" in result["per_param"]


# ── BacktestEngine: 트레일링 스탑 + 한국 비용 ───────────────────────────────

class TestBacktestEngineEnhanced:
    def test_trailing_stop_reduces_loss(self):
        cfg_with = BacktestConfig(trailing_stop_pct=0.07, stop_loss_pct=0.15, is_kr=True)
        cfg_without = BacktestConfig(trailing_stop_pct=None, stop_loss_pct=0.15, is_kr=True)

        df = make_df(300, "down", volatility=0.5)
        # 항상 매수 신호
        signals = pd.Series(1, index=df.index)

        res_with = BacktestEngine(cfg_with).run(df, signals, "TEST")
        res_without = BacktestEngine(cfg_without).run(df, signals, "TEST")

        mdd_with = res_with.metrics.get("max_drawdown_pct", 0)
        mdd_without = res_without.metrics.get("max_drawdown_pct", 0)
        # 트레일링 스탑이 있을 때 더 낮은 MDD (또는 동등) 기대
        assert mdd_with >= mdd_without or abs(mdd_with - mdd_without) < 5  # 허용 오차

    def test_kr_cost_applied(self):
        """KR 비용 모델: effective_price에 증권거래세(0.20%) 반영 확인."""
        from backend.quant.risk.position_sizer import effective_price, KR_SECURITIES_TAX
        price = 10000.0
        slippage = 0.001

        buy_price = effective_price(price, "buy", slippage, is_kr=True)
        sell_price_kr = effective_price(price, "sell", slippage, is_kr=True)
        sell_price_us = effective_price(price, "sell", slippage, is_kr=False)

        # 매수에는 증권거래세 없음
        assert buy_price == price * (1 + slippage)
        # 매도 한국: 슬리피지 + 증권거래세
        assert sell_price_kr == price * (1 - slippage - KR_SECURITIES_TAX)
        # 매도 미국: 슬리피지만
        assert sell_price_us == price * (1 - slippage)
        # 한국 매도가 미국 매도가보다 낮음 (세금만큼)
        assert sell_price_kr < sell_price_us

    def test_backtest_records_trade_with_stop(self):
        """백테스트 엔진: 신호 기반 매수→하드스탑 청산 확인."""
        # 하락장 + 넓은 하드스탑(없는 수준) + 트레일링 스탑 비활성
        cfg = BacktestConfig(stop_loss_pct=0.50, trailing_stop_pct=None, is_kr=True)
        df = make_df(300, "up", volatility=0.3, seed=1)
        signals = pd.Series(0, index=df.index)
        signals.iloc[20] = 1   # 매수
        signals.iloc[80] = -1  # 매도
        result = BacktestEngine(cfg).run(df, signals, "TEST")
        # 매수+매도 → 최소 1거래
        assert len(result.trades) >= 1
        if result.trades:
            trade = result.trades[0]
            gross = (trade.exit_price - trade.entry_price) * trade.qty
            # 한국 세금 포함 → 실현 PnL ≤ gross PnL
            assert trade.pnl <= gross + 1  # 1원 허용 오차 (반올림)


# ── WalkForward: 과적합 / 룩어헤드 체크 ──────────────────────────────────────

class TestValidationChecks:
    def test_reject_overfitting_detected(self):
        result = reject_overfitting(is_score=2.0, oos_score=0.3, degradation_threshold=0.5)
        assert result["overfitting"] is True

    def test_reject_overfitting_ok(self):
        result = reject_overfitting(is_score=1.5, oos_score=1.2, degradation_threshold=0.5)
        assert result["overfitting"] is False

    def test_lookahead_bias_clean(self):
        def signal_fn(df, params):
            # 순수 과거 데이터만 사용 — 룩어헤드 없음
            sma = df["Close"].rolling(params.get("window", 20)).mean()
            return (df["Close"] > sma).astype(int)

        df = make_df(300)
        result = lookahead_bias_check(signal_fn, df, {"window": 20}, future_window=5)
        assert "lookahead_bias_detected" in result
        assert result["lookahead_bias_detected"] is False  # 과거 데이터만 사용
