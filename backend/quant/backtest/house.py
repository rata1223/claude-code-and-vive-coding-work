"""
Portfolio backtest of the worker's house strategy, under the worker's own rules.

``BacktestEngine`` runs one symbol with its own defaults (a trailing stop the
worker does not have, a per-symbol capital pool). The live worker trades a
universe from one account in won. This simulates that account, reusing the
worker's pieces rather than restating them:

* signals — ``default_fusion().evaluate`` on the daily bars of the trailing
  ``lookback_days`` calendar days up to the previous close — what the worker's
  09:05 / 22:35 scan loads (``SCAN_PERIOD``: two years since #230, one year
  before it) — skipping a symbol with fewer than 50 bars;
* order of play — sells on a -1 signal first, then buys on +1, buys limited to
  ``IndicatorStrategy._capital_aware_universe`` (``_scan_and_trade``);
* sizing — equity (won) × ``position_size_pct``, skipped below
  ``MIN_POSITION_VALUE_KRW``; a US price is converted at ``fx`` (#229);
* fills — the next open with ``position_sizer``'s slippage and commission
  (plus the KR transaction tax on a KR sell);
* the stop — intraday low at or below entry × (1 − ``stop_loss_pct``) exits at
  that price, or at the open when the day gaps through it (the worker's 5-minute
  ``check_stops`` sweep, #228); no trailing stop;
* risk — a risk day that loses ``daily_loss_limit`` of its opening equity takes
  no new entries the next day; a drawdown of ``mdd_limit`` from the peak exits
  everything at the next open and stays halted (no operator release here).

Simplifications, stated so the numbers are read correctly: one fixed FX rate;
US and KR bars share one daily clock (a KR day and a US day with the same date
are the same step); no dividends unless the input bars are adjusted for them.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable, Optional

import pandas as pd

from backend.market.symbols import is_kr
from backend.quant.backtest.metrics import summarize
from backend.quant.risk.position_sizer import (
    DEFAULT_COMMISSION, DEFAULT_SLIPPAGE, KR_SECURITIES_TAX,
)

logger = logging.getLogger(__name__)

#: Calendar days of history the worker's scan loads: ``SCAN_PERIOD`` "2y"
#: (the loader asks for 730 days), and "1y" (365) before #230.
LOOKBACK_DAYS = 730
LOOKBACK_DAYS_BEFORE_FIX = 365
#: The worker skips a symbol with fewer bars than this.
MIN_BARS = 50


@dataclass
class HouseConfig:
    capital_krw: float = 2_000_000.0
    position_size_pct: float = 0.05
    stop_loss_pct: float = 0.07
    fx: float = 1_350.0
    daily_loss_limit: float = 0.03
    mdd_limit: float = 0.15
    commission: float = DEFAULT_COMMISSION
    slippage: float = DEFAULT_SLIPPAGE
    min_position_krw: Optional[float] = None   # None → the worker's MIN_POSITION_VALUE_KRW

    def minimum(self) -> float:
        if self.min_position_krw is not None:
            return self.min_position_krw
        from backend.strategy.indicator.strategy import MIN_POSITION_VALUE_KRW
        return float(MIN_POSITION_VALUE_KRW)


@dataclass
class HouseTrade:
    symbol: str
    entry_date: str
    exit_date: str
    qty: int
    entry_price: float
    exit_price: float
    pnl: float              # won, after costs
    exit_reason: str        # signal | stop | mdd_flatten | end


@dataclass
class HouseResult:
    equity_curve: pd.Series
    trades: list[HouseTrade]
    metrics: dict
    exposure_pct: float                       # mean invested share of equity
    skipped: dict = field(default_factory=dict)   # why buys did not happen, counted


def fusion_signals(dfs: dict[str, pd.DataFrame],
                   lookback_days: int = LOOKBACK_DAYS) -> dict[str, pd.Series]:
    """The house signal for each symbol and day, judged on the bars of the
    ``lookback_days`` calendar days up to and including that day's close. The
    backtest acts on day *t*'s value at day *t+1*'s open, so nothing from the
    day being traded is seen."""
    from backend.quant.signals.fusion import default_fusion
    fusion = default_fusion()
    span = pd.Timedelta(days=lookback_days)
    out = {}
    for sym, df in dfs.items():
        values = []
        index = df.index
        for i in range(len(df)):
            start = index.searchsorted(index[i] - span, side="right")
            window = df.iloc[start:i + 1]
            if len(window) < MIN_BARS:
                values.append(0)
                continue
            try:
                values.append(int(fusion.evaluate(window, symbol=sym).signal))
            except Exception as e:  # noqa: BLE001 - the worker skips a failed symbol too
                logger.debug("signal failed %s %s: %s", sym, df.index[i], e)
                values.append(0)
        out[sym] = pd.Series(values, index=df.index, dtype=int)
    return out


def active_universe(universe: list[str], capital: float) -> list[str]:
    """``IndicatorStrategy._capital_aware_universe`` itself, on this universe."""
    from backend.strategy.indicator.strategy import IndicatorStrategy
    return IndicatorStrategy._capital_aware_universe(SimpleNamespace(_universe=universe), capital)


def run_house_backtest(dfs: dict[str, pd.DataFrame], universe: list[str],
                       cfg: Optional[HouseConfig] = None,
                       signals: Optional[dict[str, pd.Series]] = None) -> HouseResult:
    cfg = cfg or HouseConfig()
    signals = signals if signals is not None else fusion_signals(dfs)
    minimum = cfg.minimum()
    calendar = sorted(set().union(*(df.index for df in dfs.values())))

    cash = cfg.capital_krw
    positions: dict[str, dict] = {}       # sym → {qty, entry (local ccy), date}
    trades: list[HouseTrade] = []
    equity_points, exposure_points = [], []
    skipped = {"below_minimum": 0, "zero_shares": 0, "no_cash": 0, "halted": 0}
    last_close: dict[str, float] = {}
    peak = cfg.capital_krw
    equity_prev = cfg.capital_krw
    no_entries_today = False
    mdd_halted = False
    flatten_next_open = False

    def fx_of(sym):
        return 1.0 if is_kr(sym) else cfg.fx

    def sell(sym, price_local, date, reason):
        nonlocal cash
        pos = positions.pop(sym)
        kr = is_kr(sym)
        fill = price_local * (1 - cfg.slippage)
        gross = fill * pos["qty"] * fx_of(sym)
        cost = gross * (cfg.commission + (KR_SECURITIES_TAX if kr else 0.0))
        cash += gross - cost
        trades.append(HouseTrade(
            symbol=sym, entry_date=pos["date"], exit_date=str(date.date()), qty=pos["qty"],
            entry_price=round(pos["entry"], 4), exit_price=round(fill, 4),
            pnl=round((fill - pos["entry"]) * pos["qty"] * fx_of(sym) - cost - pos["buy_cost"], 0),
            exit_reason=reason))

    for date in calendar:
        today = {s: df.loc[date] for s, df in dfs.items() if date in df.index}

        # Open: an MDD flatten decided yesterday, then the scan (sells, buys).
        if flatten_next_open:
            for sym in [s for s in positions if s in today]:
                sell(sym, today[sym]["Open"], date, "mdd_flatten")
            flatten_next_open = False

        if not mdd_halted:
            for sym in [s for s in positions if s in today]:
                prev = _previous(signals[sym], date)
                if prev == -1:
                    sell(sym, today[sym]["Open"], date, "signal")

            buys = active_universe(universe, equity_prev)
            for sym in buys:
                if sym in positions or sym not in today or _previous(signals.get(sym), date) != 1:
                    continue
                if no_entries_today:
                    skipped["halted"] += 1
                    continue
                amount = equity_prev * cfg.position_size_pct
                if amount < minimum:
                    skipped["below_minimum"] += 1
                    continue
                fill = today[sym]["Open"] * (1 + cfg.slippage)
                qty = int(amount / (fill * fx_of(sym)))
                if qty <= 0:
                    skipped["zero_shares"] += 1
                    continue
                cost_krw = fill * qty * fx_of(sym)
                fee = cost_krw * cfg.commission
                if cost_krw + fee > cash:
                    skipped["no_cash"] += 1
                    continue
                cash -= cost_krw + fee
                positions[sym] = {"qty": qty, "entry": fill, "date": str(date.date()),
                                  "buy_cost": fee}

        # Intraday: the stop sweep.
        for sym in [s for s in positions if s in today]:
            stop_px = positions[sym]["entry"] * (1 - cfg.stop_loss_pct)
            bar = today[sym]
            if bar["Low"] <= stop_px:
                sell(sym, min(bar["Open"], stop_px), date, "stop")

        # Close: mark to market, risk checks for tomorrow.
        for sym, bar in today.items():
            last_close[sym] = float(bar["Close"])
        invested = sum(p["qty"] * last_close[s] * fx_of(s) for s, p in positions.items()
                       if s in last_close)
        equity = cash + invested
        equity_points.append(equity)
        exposure_points.append(invested / equity if equity > 0 else 0.0)

        no_entries_today = equity <= equity_prev * (1 - cfg.daily_loss_limit)
        peak = max(peak, equity)
        if not mdd_halted and equity <= peak * (1 - cfg.mdd_limit):
            mdd_halted = True
            flatten_next_open = bool(positions)
        equity_prev = equity

    end = calendar[-1] if calendar else None
    for sym in list(positions):
        sell(sym, last_close[sym], end, "end")
    if equity_points:
        equity_points[-1] = cash

    curve = pd.Series(equity_points, index=pd.DatetimeIndex(calendar), name="equity")
    metrics = summarize(curve, [t.__dict__ for t in trades], cfg.capital_krw) if len(curve) > 1 else {}
    exposure = float(pd.Series(exposure_points).mean() * 100) if exposure_points else 0.0
    return HouseResult(equity_curve=curve, trades=trades, metrics=metrics,
                       exposure_pct=round(exposure, 2), skipped=skipped)


def _previous(series: Optional[pd.Series], date) -> int:
    """The signal from the last bar before ``date`` (yesterday's close)."""
    if series is None:
        return 0
    before = series.loc[:date]
    if len(before) and before.index[-1] == date:
        before = before.iloc[:-1]
    return int(before.iloc[-1]) if len(before) else 0


def buy_and_hold(df: pd.DataFrame, capital: float, fx: float = 1.0) -> pd.Series:
    """Benchmark: all capital into one symbol at the first open, held."""
    qty = capital / (df["Open"].iloc[0] * fx)
    return (df["Close"] * qty * fx).rename("buy_and_hold")
