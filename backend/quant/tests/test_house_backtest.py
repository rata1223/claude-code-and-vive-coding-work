"""
``run_house_backtest`` simulates the worker's account under the worker's rules.
Synthetic bars and injected signals pin each rule; the real ``default_fusion``
is exercised once for wiring.
"""
import numpy as np
import pandas as pd
import pytest

from backend.quant.backtest.house import (
    HouseConfig, active_universe, buy_and_hold, fusion_signals, run_house_backtest,
)


def _bars(opens, lows=None, closes=None, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=len(opens))
    opens = np.array(opens, dtype=float)
    lows = np.array(lows if lows is not None else opens * 0.995, dtype=float)
    closes = np.array(closes if closes is not None else opens, dtype=float)
    return pd.DataFrame({"Open": opens, "High": np.maximum(opens, closes) * 1.005,
                         "Low": lows, "Close": closes, "Volume": 1_000_000}, index=idx)


def _sig(df, days):
    """+1/−1 on the given bar positions, 0 elsewhere."""
    s = pd.Series(0, index=df.index, dtype=int)
    for i, v in days.items():
        s.iloc[i] = v
    return s


NO_COST = dict(commission=0.0, slippage=0.0)


def test_a_signal_is_acted_on_at_the_next_open():
    df = _bars([100, 110, 120, 130])
    res = run_house_backtest({"069500": df}, ["069500"],
                             HouseConfig(capital_krw=10_000_000, **NO_COST),
                             signals={"069500": _sig(df, {0: 1})})
    t = res.trades[0]
    assert t.entry_date == str(df.index[1].date()) and t.entry_price == 110.0


def test_a_us_buy_is_sized_in_won_at_the_fx_rate():
    df = _bars([100.0] * 4)                       # $100 × 1,000 = 100,000 won a share
    res = run_house_backtest({"AAPL": df}, ["AAPL"],
                             HouseConfig(capital_krw=10_000_000, fx=1_000.0, **NO_COST),
                             signals={"AAPL": _sig(df, {0: 1})})
    assert res.trades[0].qty == 5                 # 500,000 won / 100,000 won


def test_a_buy_below_the_minimum_is_skipped():
    df = _bars([100.0] * 4)
    res = run_house_backtest({"069500": df}, ["069500"],
                             HouseConfig(capital_krw=1_000_000, min_position_krw=100_000, **NO_COST),
                             signals={"069500": _sig(df, {0: 1, 1: 1, 2: 1})})
    assert res.trades == [] and res.skipped["below_minimum"] == 3


def test_a_share_dearer_than_the_allocation_is_skipped():
    df = _bars([800.0] * 4)                       # SPY-like: $800 × 1,350 > 100,000 won
    res = run_house_backtest({"SPY": df}, ["SPY"], HouseConfig(**NO_COST),
                             signals={"SPY": _sig(df, {0: 1})})
    assert res.trades == [] and res.skipped["zero_shares"] == 1


def test_the_stop_exits_at_the_stop_price():
    df = _bars([100, 100, 100, 100], lows=[99, 99, 92, 99])
    res = run_house_backtest({"069500": df}, ["069500"],
                             HouseConfig(capital_krw=10_000_000, **NO_COST),
                             signals={"069500": _sig(df, {0: 1})})
    t = res.trades[0]
    assert t.exit_reason == "stop" and t.exit_price == pytest.approx(93.0)


def test_a_gap_through_the_stop_exits_at_the_open():
    df = _bars([100, 100, 85, 85], lows=[99, 99, 84, 84])
    res = run_house_backtest({"069500": df}, ["069500"],
                             HouseConfig(capital_krw=10_000_000, **NO_COST),
                             signals={"069500": _sig(df, {0: 1})})
    assert res.trades[0].exit_price == 85.0


def test_a_sell_signal_exits_at_the_next_open():
    df = _bars([100, 101, 102, 103, 104])
    res = run_house_backtest({"069500": df}, ["069500"],
                             HouseConfig(capital_krw=10_000_000, **NO_COST),
                             signals={"069500": _sig(df, {0: 1, 2: -1})})
    t = res.trades[0]
    assert t.exit_reason == "signal" and t.exit_price == 103.0


def test_only_the_capital_aware_universe_is_bought():
    dfs = {s: _bars([10_000.0] * 4) for s in ["069500", "360750", "091160", "005930"]}
    sigs = {s: _sig(df, {0: 1}) for s, df in dfs.items()}
    res = run_house_backtest(dfs, list(dfs), HouseConfig(**NO_COST), signals=sigs)
    assert sorted(t.symbol for t in res.trades) == sorted(active_universe(list(dfs), 2_000_000))
    assert "005930" not in {t.symbol for t in res.trades}


def test_a_bad_day_blocks_the_next_days_entries():
    a = _bars([100, 100, 100, 100, 100], closes=[100, 100, 50, 100, 100])
    b = _bars([100.0] * 5)
    cfg = HouseConfig(capital_krw=1_000_000, position_size_pct=0.5,
                      stop_loss_pct=0.9, mdd_limit=0.99, **NO_COST)
    res = run_house_backtest({"069500": a, "360750": b}, ["069500", "360750"], cfg,
                             signals={"069500": _sig(a, {0: 1}), "360750": _sig(b, {2: 1})})
    assert res.skipped["halted"] == 1
    assert all(t.symbol != "360750" for t in res.trades)


def test_an_mdd_breach_flattens_and_halts():
    df = _bars([100, 100, 100, 100, 100], closes=[100, 100, 40, 40, 40],
               lows=[99, 99, 40, 40, 40])
    cfg = HouseConfig(capital_krw=1_000_000, position_size_pct=0.9, stop_loss_pct=0.99,
                      daily_loss_limit=0.99, **NO_COST)
    res = run_house_backtest({"069500": df}, ["069500"], cfg,
                             signals={"069500": _sig(df, {0: 1, 3: 1})})
    assert [t.exit_reason for t in res.trades] == ["mdd_flatten"]


def test_costs_reduce_the_result():
    df = _bars([100, 100, 100, 100])
    sig = {"AAPL": _sig(df, {0: 1})}
    free = run_house_backtest({"AAPL": df}, ["AAPL"],
                              HouseConfig(capital_krw=10_000_000, fx=1_000.0, **NO_COST),
                              signals=sig)
    paid = run_house_backtest({"AAPL": df}, ["AAPL"],
                              HouseConfig(capital_krw=10_000_000, fx=1_000.0), signals=sig)
    assert paid.equity_curve.iloc[-1] < free.equity_curve.iloc[-1] == 10_000_000


def test_a_kr_sell_pays_the_transaction_tax():
    df = _bars([100, 100, 100, 100])
    res = run_house_backtest({"069500": df}, ["069500"],
                             HouseConfig(capital_krw=10_000_000, **NO_COST),
                             signals={"069500": _sig(df, {0: 1})})
    # 5,000 shares × 100 won sold → 0.2 % tax = 1,000 won.
    assert res.equity_curve.iloc[-1] == pytest.approx(10_000_000 - 1_000)


def test_buy_and_hold_benchmark():
    df = _bars([100, 110, 120], closes=[100, 110, 120])
    assert buy_and_hold(df, 1_000.0).iloc[-1] == pytest.approx(1_200.0)


def test_the_real_fusion_runs_on_real_shaped_data():
    rng = np.random.default_rng(0)
    price = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, 320)))
    df = _bars(price, closes=price * 1.001)
    sig = fusion_signals({"069500": df})["069500"]
    assert len(sig) == len(df) and set(sig.unique()) <= {-1, 0, 1}
    assert (sig.iloc[:49] == 0).all()             # fewer than 50 bars → no signal


def test_a_signal_never_sees_a_later_bar(monkeypatch):
    """Each day's signal is judged on a window ending at that day's close and
    reaching back the worker's fetch span in calendar days."""
    seen = []

    class _Fusion:
        def evaluate(self, window, symbol=""):
            seen.append((window.index[0], window.index[-1], len(window)))
            return type("R", (), {"signal": 0})()

    monkeypatch.setattr("backend.quant.signals.fusion.default_fusion", lambda: _Fusion())
    df = _bars(np.linspace(100, 130, 600))
    fusion_signals({"069500": df}, lookback_days=365)
    from backend.quant.backtest.house import MIN_BARS
    assert len(seen) == len(df) - (MIN_BARS - 1)
    for k, (first, last, n) in enumerate(seen, start=MIN_BARS - 1):
        assert last == df.index[k]
        assert first > df.index[k] - pd.Timedelta(days=365)
        if k > 300:
            assert first <= df.index[k] - pd.Timedelta(days=360)


def test_the_worker_loads_enough_history_for_its_signals():
    """``TrendFollowingSignal`` needs 260 bars and ``MomentumSignal`` 252. A
    year holds ~252 US and ~245 KR trading days, so "1y" starved them (#230);
    the scan now loads two years."""
    from backend.strategy.indicator.strategy import SCAN_PERIOD
    days = {"1y": 365, "2y": 730, "3y": 1095}[SCAN_PERIOD]
    kr_trading_days_per_year = 245
    assert days * kr_trading_days_per_year / 365 > 260
    assert 365 * kr_trading_days_per_year / 365 < 252        # what "1y" gave KR
