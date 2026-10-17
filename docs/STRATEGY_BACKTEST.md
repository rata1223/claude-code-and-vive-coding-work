# House strategy backtest

What the worker actually trades, simulated under the worker's own rules — so the
4-week paper run tests something whose expected behaviour is known.

## Method

`backend/quant/backtest/house.py` (`run_house_backtest`) takes its rules from the
worker's code, not from a restatement:

| Rule | Source |
|---|---|
| Signal: `default_fusion().evaluate` on the trailing window up to the previous close | `backend/quant/signals/fusion.py`, scan window `SCAN_PERIOD` |
| Sells on a −1 signal first, then buys on +1, buys only in `_capital_aware_universe` | `IndicatorStrategy._scan_and_trade` |
| Allocation = equity (won) × `position_size_pct`; US price × FX; skip below `MIN_POSITION_VALUE_KRW` | `IndicatorStrategy._execute_buy`, PR #229 |
| Fill at the next open ± slippage; commission; KR transaction tax on sells | `backend/quant/risk/position_sizer.py` |
| −7 % stop on the intraday low (gap → open); no trailing stop | `IndicatorStrategy.check_stops`, PR #228 |
| Daily loss 3 % or rolling 7-day loss 6 % → no new entries for the rest of the run (exits continue); MDD 15 % → flatten (retried on a closed market's next bar), stop | risk engine — its halt holds until an operator releases it; no release is modelled, so after the first halt the run only exits |

Simplifications: one fixed FX rate (default 1,350); KR and US bars share one daily
clock; dividends only if the input bars are adjusted for them; survivorship — the
universe is today's large caps; Sharpe uses a 5 % risk-free rate (a mostly-cash
portfolio reads strongly negative).

## How to run

```bash
# Where Yahoo / Naver are reachable (the server):
python scripts/backtest_house_strategy.py --online --start 2016-01-01 > report.md
# Offline: CSVs named <SYMBOL>.csv with Date,Open,High,Low,Close,Volume
python scripts/backtest_house_strategy.py --csv-dir data/ > report.md
# The operator's universe order matters (only the first 3 are bought under 3,000,000 won):
python scripts/backtest_house_strategy.py --online --universe SPY,QQQ,069500
```

Scenarios: the live settings (2,000,000 won, 5 % a symbol), 10 % and 20 % a
symbol, 10,000,000 won, the live settings on the one-year window the worker used
before PR #230, and SPY buy & hold — for the whole period and each half.

## Results

**Not yet run.** The development environment blocks Yahoo and Naver (network
policy). Fill this section from a run with network access.

## What to look for

- **Does the live setting trade at all?** 2,000,000 × 5 % = 100,000 won is below
  one share of SPY, QQQ and the US large caps, and equal to `MIN_POSITION_VALUE_KRW` —
  any loss stops every buy. The "skipped" column counts both.
- **1-year vs 2-year window**: how much of the signal the window bug suppressed.
- **Halves**: a strategy that only works in one half is not a strategy.
