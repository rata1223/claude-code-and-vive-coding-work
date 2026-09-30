"""#188 — a user-script backtest runs in a child process with a hard budget.

A script that never finishes used to hold the API worker that ran it: the loop
ran inside the request, and neither a thread nor a trace hook can stop a long C
call like ``sum(range(10**13))``. ``strategy.script_backtest`` runs the whole
backtest in a spawned child and kills it when the time budget runs out; the
child's address space is capped too.

Prices are synthetic — no network. Each case spawns a real process (~1.5 s).
"""
import math
import multiprocessing as mp
import time

import pandas as pd
import pytest

from strategy import SCRIPT_TEMPLATES, Backtester, ScriptStrategy
from strategy.script_backtest import (
    ScriptBacktestError,
    ScriptBacktestTimeout,
    run_script_backtest,
)


@pytest.fixture(scope="module")
def prices():
    idx = pd.date_range("2025-01-01", periods=260, freq="B")
    px = [100 + 15 * math.sin(i / 9) + i * 0.05 for i in range(260)]
    return pd.DataFrame({"Open": px, "High": px, "Low": px, "Close": px,
                         "Volume": [1000] * 260}, index=idx)


def _run(code, df, **kw):
    return run_script_backtest(code, {}, "AAPL", initial_capital=1_000_000,
                               period="1y", df=df, **kw)


def _no_child_left():
    """Checked within 2 s — well before the child's own RLIMIT_CPU backstop
    (budget + 5 s of CPU) could end it — so this sees the parent's kill."""
    deadline = time.time() + 2
    while mp.active_children() and time.time() < deadline:
        time.sleep(0.1)
    return not mp.active_children()


def test_the_result_matches_the_in_process_backtester(prices):
    code = SCRIPT_TEMPLATES["ma_crossover"]["code"]
    strat = ScriptStrategy(code=code)
    strat.on_start()                           # as the API always did
    expected = Backtester(strat, "AAPL",
                          initial_capital=1_000_000, period="1y").run(df=prices)
    assert _run(code, prices, timeout_sec=30) == expected.to_dict()
    assert expected.total_trades > 0          # a result worth comparing


@pytest.mark.parametrize("body", [
    "    while True:\n        pass\n",             # a Python-level loop
    "    return sum(range(10 ** 13))\n",          # one long C call
])
def test_a_script_that_never_finishes_is_stopped(prices, body):
    started = time.time()
    with pytest.raises(ScriptBacktestTimeout, match="시간 초과"):
        _run("def on_bar(bar, ctx):\n" + body, prices, timeout_sec=4)
    assert time.time() - started < 12
    assert _no_child_left()


def test_an_oversized_allocation_is_refused_in_the_child(prices):
    """The allocation fails under the address-space cap and the backtest is an
    error — not a "successful" result built from the bars before the failure."""
    with pytest.raises(ScriptBacktestError, match="메모리 한도"):
        _run("def on_bar(bar, ctx):\n    x = 'x' * (4 * 1024 ** 3)\n",
             prices, timeout_sec=30, memory_mb=256)
    assert _no_child_left()


def test_a_failure_inside_the_child_is_reported(prices):
    with pytest.raises(ScriptBacktestError, match="실패"):
        _run("def on_bar(bar, ctx):\n    return None\n",
             prices.drop(columns=["Close"]), timeout_sec=30)
    assert _no_child_left()
