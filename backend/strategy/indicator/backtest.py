"""
backtesting.py 래퍼.
입력: 조건 JSON + 기간 + 종목
출력: {sharpe, mdd, win_rate, cagr, equity_curve, trades}
"""
import logging
from datetime import datetime

import pandas as pd
import pandas_ta as ta
import yfinance as yf

logger = logging.getLogger(__name__)


def _fetch(symbol: str, start: str, end: str) -> pd.DataFrame:
    df = yf.download(symbol, start=start, end=end, auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"데이터 없음: {symbol}")
    return df


def _apply_buy_conditions(df: pd.DataFrame, conditions: dict) -> pd.Series:
    """조건 딕셔너리 → 매수 시그널 시리즈 (bool)."""
    signal = pd.Series(True, index=df.index)

    close = df["Close"]
    volume = df["Volume"] if "Volume" in df.columns else None

    if conditions.get("sma200"):
        sma200 = ta.sma(close, length=200)
        if sma200 is not None:
            signal &= close > sma200

    rsi_lt = conditions.get("rsi_lt")
    if rsi_lt:
        rsi = ta.rsi(close, length=14)
        if rsi is not None:
            signal &= rsi < float(rsi_lt)

    momentum_gt = conditions.get("momentum_gt")
    if momentum_gt is not None and len(close) >= 63:
        mom = close.pct_change(63)
        signal &= mom > float(momentum_gt)

    if conditions.get("volume_above_ma") and volume is not None:
        vol_ma = ta.sma(volume, length=20)
        if vol_ma is not None:
            signal &= volume > vol_ma

    return signal


def _apply_sell_conditions(df: pd.DataFrame, conditions: dict, entry_price: float | None = None) -> pd.Series:
    signal = pd.Series(False, index=df.index)
    close = df["Close"]

    if conditions.get("sma200_cross_below"):
        sma200 = ta.sma(close, length=200)
        if sma200 is not None:
            signal |= close < sma200

    rsi_gt = conditions.get("rsi_gt")
    if rsi_gt:
        rsi = ta.rsi(close, length=14)
        if rsi is not None:
            signal |= rsi > float(rsi_gt)

    return signal


def run_backtest(
    symbol: str,
    start: str,
    end: str,
    buy_conditions: dict,
    sell_conditions: dict,
    stop_loss_pct: float = 0.07,
    initial_cash: float = 2_000_000,
    commission: float = 0.00015,
) -> dict:
    """
    단순 벡터화 백테스트.
    반환: {sharpe, mdd, win_rate, cagr, equity_curve: [{date, equity}], trades: [{entry_date, exit_date, pnl_pct}]}
    """
    df = _fetch(symbol, start, end)
    close = df["Close"].squeeze()

    buy_sig = _apply_buy_conditions(df, buy_conditions)
    sell_sig = _apply_sell_conditions(df, sell_conditions)

    cash = initial_cash
    qty = 0
    entry_price = 0.0
    trades = []
    equity_curve = []
    entry_date = None

    for date, price in close.items():
        price = float(price)
        equity = cash + qty * price
        equity_curve.append({"date": str(date.date()), "equity": round(equity, 2)})

        if qty == 0 and buy_sig.get(date, False):
            invest = cash * 0.95
            qty = int(invest / price)
            cost = qty * price * (1 + commission)
            if qty > 0 and cost <= cash:
                cash -= cost
                entry_price = price
                entry_date = date

        elif qty > 0:
            stop_hit = price <= entry_price * (1 - stop_loss_pct)
            sell_hit = sell_sig.get(date, False)
            if stop_hit or sell_hit:
                proceeds = qty * price * (1 - commission)
                pnl_pct = (price - entry_price) / entry_price
                cash += proceeds
                trades.append({
                    "entry_date": str(entry_date.date()) if entry_date else "",
                    "exit_date": str(date.date()),
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(price, 4),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "reason": "stop_loss" if stop_hit else "sell_signal",
                })
                qty = 0
                entry_price = 0.0
                entry_date = None

    # 통계 계산
    equities = [e["equity"] for e in equity_curve]
    final_equity = equities[-1] if equities else initial_cash
    cagr = _cagr(initial_cash, final_equity, len(equities) / 252)

    returns = pd.Series(equities).pct_change().dropna()
    sharpe = float(returns.mean() / returns.std() * (252 ** 0.5)) if returns.std() > 0 else 0.0
    mdd = _mdd(equities)
    win_rate = (
        sum(1 for t in trades if t["pnl_pct"] > 0) / len(trades) * 100
        if trades else 0.0
    )

    return {
        "symbol": symbol,
        "start": start,
        "end": end,
        "sharpe": round(sharpe, 3),
        "mdd": round(mdd * 100, 2),
        "win_rate": round(win_rate, 1),
        "cagr": round(cagr * 100, 2),
        "total_trades": len(trades),
        "final_equity": round(final_equity, 0),
        "equity_curve": equity_curve,
        "trades": trades,
    }


def _cagr(initial: float, final: float, years: float) -> float:
    if years <= 0 or initial <= 0:
        return 0.0
    return (final / initial) ** (1 / years) - 1


def _mdd(equities: list[float]) -> float:
    peak = equities[0] if equities else 1.0
    max_dd = 0.0
    for v in equities:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return max_dd
