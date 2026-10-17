"""
Backtest the worker's house strategy under the worker's own rules and print a
markdown report (see ``backend/quant/backtest/house.py`` for the rules).

    # Where the internet is reachable (the server):
    python scripts/backtest_house_strategy.py --online --start 2016-01-01 > report.md

    # Offline, from CSVs named <SYMBOL>.csv with Date,Open,High,Low,Close,Volume:
    python scripts/backtest_house_strategy.py --csv-dir data/ > report.md

Scenarios: the live settings (2,000,000 won, 5 % a symbol) and, for
comparison only, 10 % / 20 % a symbol and 10,000,000 won. The worker's
parameters are not changed by this script.
"""
import argparse
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.quant.backtest.house import (  # noqa: E402
    LOOKBACK_DAYS_BEFORE_FIX, HouseConfig, buy_and_hold, fusion_signals, run_house_backtest,
)
from backend.quant.backtest.metrics import summarize  # noqa: E402
from backend.quant.data.universe import UNIVERSE  # noqa: E402

SCENARIOS = [
    ("live: 2,000,000 won, 5 %", dict(capital_krw=2_000_000, position_size_pct=0.05)),
    ("2,000,000 won, 10 %", dict(capital_krw=2_000_000, position_size_pct=0.10)),
    ("2,000,000 won, 20 %", dict(capital_krw=2_000_000, position_size_pct=0.20)),
    ("10,000,000 won, 5 %", dict(capital_krw=10_000_000, position_size_pct=0.05)),
]


def load_csv_dir(path, symbols):
    out = {}
    for sym in symbols:
        f = os.path.join(path, f"{sym}.csv")
        if not os.path.exists(f):
            continue
        df = pd.read_csv(f, parse_dates=[0], index_col=0).sort_index()
        df.columns = [c.strip().capitalize() for c in df.columns]
        out[sym] = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    return out


def load_online(symbols, start, end):
    from backend.quant.data.loader import DataLoader
    return DataLoader().fetch_multi(symbols, start=start, end=end)


def _row(name, m, exposure, skipped):
    return (f"| {name} | {m.get('cagr_pct', 0):.2f} | {m.get('sharpe', 0):.2f} | "
            f"{m.get('max_drawdown_pct', 0):.2f} | {m.get('win_rate_pct', 0):.1f} | "
            f"{m.get('profit_factor', 0):.2f} | {m.get('total_trades', 0)} | {exposure:.1f} | "
            f"{skipped.get('zero_shares', 0)} / {skipped.get('below_minimum', 0)} |")


def report(dfs, universe, fx, halves=True):
    signals = fusion_signals(dfs)
    before_fix = fusion_signals(dfs, lookback_days=LOOKBACK_DAYS_BEFORE_FIX)
    first = min(df.index[0] for df in dfs.values()).date()
    last = max(df.index[-1] for df in dfs.values()).date()
    lines = [f"Data: {len(dfs)} symbols, {first} – {last}. "
             f"Universe order: {', '.join(universe)}. FX {fx:,.0f} won/USD.", ""]

    def table(title, frames, sigs):
        out = [f"### {title}", "",
               "| scenario | CAGR % | Sharpe | MDD % | win % | profit factor | trades | "
               "exposure % | skipped: 0 shares / below min |",
               "|---|---|---|---|---|---|---|---|---|"]
        for name, kw in SCENARIOS:
            res = run_house_backtest(frames, universe, HouseConfig(fx=fx, **kw), signals=sigs)
            out.append(_row(name, res.metrics, res.exposure_pct, res.skipped))
        if sigs is signals:
            res = run_house_backtest(frames, universe, HouseConfig(fx=fx, **SCENARIOS[0][1]),
                                     signals=before_fix)
            out.append(_row("live, 1-year window (before #230)", res.metrics,
                            res.exposure_pct, res.skipped))
        if "SPY" in frames:
            bh = buy_and_hold(frames["SPY"], 2_000_000, fx)
            m = summarize(bh, [], 2_000_000)
            out.append(f"| SPY buy & hold | {m['cagr_pct']:.2f} | {m['sharpe']:.2f} | "
                       f"{m['max_drawdown_pct']:.2f} | – | – | – | 100.0 | – |")
        return out + [""]

    lines += table("Whole period", dfs, signals)
    if halves:
        cut = pd.Timestamp(first) + (pd.Timestamp(last) - pd.Timestamp(first)) / 2
        for label, sl in (("First half", slice(None, cut)), ("Second half", slice(cut, None))):
            frames = {s: df.loc[sl] for s, df in dfs.items() if len(df.loc[sl]) > 1}
            sigs = {s: signals[s].loc[sl] for s in frames}
            lines += table(f"{label} (signals still use the bars before the cut)", frames, sigs)
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv-dir")
    src.add_argument("--online", action="store_true")
    ap.add_argument("--universe", default=",".join(UNIVERSE),
                    help="comma-separated, in the operator's order (the worker buys only "
                         "the first 3 below 3,000,000 won)")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--fx", type=float, default=1_350.0)
    args = ap.parse_args(argv)

    universe = [s.strip() for s in args.universe.split(",") if s.strip()]
    dfs = (load_csv_dir(args.csv_dir, universe) if args.csv_dir
           else load_online(universe, args.start, args.end))
    missing = [s for s in universe if s not in dfs]
    if missing:
        print(f"> Missing data, left out: {', '.join(missing)}\n")
    universe = [s for s in universe if s in dfs]
    if not universe:
        sys.exit("no data")
    print(report(dfs, universe, args.fx))


if __name__ == "__main__":
    main()
