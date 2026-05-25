"""
간단한 벡터라이즈 백테스트 엔진.
IndicatorStrategy 또는 ScriptStrategy와 함께 사용.
"""
import logging
from dataclasses import dataclass, field
from typing import Any
import yfinance as yf
import pandas as pd

from .indicator_strategy import IndicatorStrategy
from .script_strategy import ScriptStrategy, Bar, Signal

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    symbol: str
    total_return_pct: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    equity_curve: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "total_return_pct": round(self.total_return_pct, 4),
            "win_rate": round(self.win_rate, 4),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "profit_factor": round(self.profit_factor, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "equity_curve": self.equity_curve,
            "trades": self.trades,
        }


class Backtester:
    def __init__(
        self,
        strategy: IndicatorStrategy | ScriptStrategy,
        symbol: str,
        initial_capital: float = 1_000_000,
        commission_pct: float = 0.001,
        period: str = "1y",
    ):
        self.strategy = strategy
        self.symbol = symbol
        self.initial_capital = initial_capital
        self.commission_pct = commission_pct
        self.period = period

    def _fetch(self) -> pd.DataFrame:
        is_kr = self.symbol.isdigit() and len(self.symbol) == 6
        ticker = f"{self.symbol}.KS" if is_kr else self.symbol
        df = yf.Ticker(ticker).history(period=self.period)
        if df.empty and is_kr:
            df = yf.Ticker(f"{self.symbol}.KQ").history(period=self.period)
        return df

    def run(self) -> BacktestResult:
        df = self._fetch()
        if df.empty:
            return BacktestResult(symbol=self.symbol)

        result = BacktestResult(symbol=self.symbol)
        capital = self.initial_capital
        position: float = 0.0
        entry_price: float = 0.0
        peak_equity = capital
        trades = []
        equity_curve = []
        wins = 0
        losses = 0
        gross_profit = 0.0
        gross_loss = 0.0

        for i in range(1, len(df)):
            window = df.iloc[: i + 1]
            close = float(window["Close"].iloc[-1])
            ts = window.index[-1]

            buy_sig = False
            sell_sig = False

            if isinstance(self.strategy, IndicatorStrategy):
                if position == 0:
                    buy_sig = self.strategy.buy_signal(window)
                else:
                    sell_sig = self.strategy.sell_signal(window, entry_price)
            elif isinstance(self.strategy, ScriptStrategy):
                bar = Bar(
                    symbol=self.symbol,
                    timestamp=ts.timestamp() if hasattr(ts, "timestamp") else float(i),
                    open=float(window["Open"].iloc[-1]),
                    high=float(window["High"].iloc[-1]),
                    low=float(window["Low"].iloc[-1]),
                    close=close,
                    volume=float(window["Volume"].iloc[-1]),
                )
                sig = self.strategy.on_bar(bar)
                if sig is not None:
                    if sig.action == "buy" and position == 0:
                        buy_sig = True
                    elif sig.action == "sell" and position > 0:
                        sell_sig = True

            if buy_sig and position == 0:
                cost = capital * 0.95
                commission = cost * self.commission_pct
                qty = (cost - commission) / close
                position = qty
                entry_price = close
                capital -= cost

                trades.append({
                    "side": "buy",
                    "symbol": self.symbol,
                    "price": close,
                    "qty": round(qty, 4),
                    "timestamp": str(ts),
                })

            elif sell_sig and position > 0:
                proceeds = position * close
                commission = proceeds * self.commission_pct
                net = proceeds - commission
                pnl = net - (position * entry_price)
                capital += net

                if pnl > 0:
                    wins += 1
                    gross_profit += pnl
                else:
                    losses += 1
                    gross_loss += abs(pnl)

                trades.append({
                    "side": "sell",
                    "symbol": self.symbol,
                    "price": close,
                    "qty": round(position, 4),
                    "pnl": round(pnl, 2),
                    "timestamp": str(ts),
                })
                position = 0.0
                entry_price = 0.0

            equity = capital + position * close
            if equity > peak_equity:
                peak_equity = equity
            drawdown = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0
            if drawdown > result.max_drawdown_pct:
                result.max_drawdown_pct = drawdown

            equity_curve.append({"date": str(ts.date()), "equity": round(equity, 2)})

        final_equity = capital + position * float(df["Close"].iloc[-1])
        total_trades = wins + losses

        result.total_return_pct = (final_equity - self.initial_capital) / self.initial_capital
        result.win_rate = wins / total_trades if total_trades > 0 else 0.0
        result.total_trades = total_trades
        result.winning_trades = wins
        result.losing_trades = losses
        result.profit_factor = gross_profit / gross_loss if gross_loss > 0 else (1.0 if gross_profit > 0 else 0.0)
        result.equity_curve = equity_curve[-200:]
        result.trades = trades[-100:]

        if len(equity_curve) > 1:
            import statistics
            daily_returns = []
            for j in range(1, len(equity_curve)):
                prev = equity_curve[j - 1]["equity"]
                curr = equity_curve[j]["equity"]
                if prev > 0:
                    daily_returns.append((curr - prev) / prev)
            if len(daily_returns) > 1:
                mean_r = statistics.mean(daily_returns)
                std_r = statistics.stdev(daily_returns)
                result.sharpe_ratio = (mean_r / std_r) * (252 ** 0.5) if std_r > 0 else 0.0

        return result
