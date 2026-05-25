"""
Deliverable 6 (일부): 백테스트 성과 지표.
Sharpe, Sortino, Calmar, MDD, CAGR, Win Rate, Profit Factor.
"""
import numpy as np
import pandas as pd


def equity_to_returns(equity: pd.Series) -> pd.Series:
    return equity.pct_change().dropna()


def cagr(equity: pd.Series) -> float:
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0
    years = len(equity) / 252
    return (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1 if years > 0 else 0.0


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return float(dd.min())


def sharpe(equity: pd.Series, risk_free: float = 0.05) -> float:
    rets = equity_to_returns(equity)
    if rets.std() == 0:
        return 0.0
    excess = rets.mean() * 252 - risk_free
    return excess / (rets.std() * np.sqrt(252))


def sortino(equity: pd.Series, risk_free: float = 0.05) -> float:
    rets = equity_to_returns(equity)
    downside = rets[rets < 0]
    if downside.std() == 0:
        return 0.0
    excess = rets.mean() * 252 - risk_free
    return excess / (downside.std() * np.sqrt(252))


def calmar(equity: pd.Series) -> float:
    mdd = max_drawdown(equity)
    if mdd == 0:
        return 0.0
    return cagr(equity) / abs(mdd)


def win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    return wins / len(trades)


def profit_factor(trades: list[dict]) -> float:
    gross_win = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) < 0))
    return gross_win / gross_loss if gross_loss > 0 else float("inf")


def summarize(equity: pd.Series, trades: list[dict], initial_capital: float) -> dict:
    return {
        "total_return_pct": round((equity.iloc[-1] / initial_capital - 1) * 100, 2),
        "cagr_pct": round(cagr(equity) * 100, 2),
        "sharpe": round(sharpe(equity), 3),
        "sortino": round(sortino(equity), 3),
        "calmar": round(calmar(equity), 3),
        "max_drawdown_pct": round(max_drawdown(equity) * 100, 2),
        "win_rate_pct": round(win_rate(trades) * 100, 1),
        "profit_factor": round(profit_factor(trades), 3),
        "total_trades": len(trades),
        "final_equity": round(float(equity.iloc[-1]), 0),
    }
