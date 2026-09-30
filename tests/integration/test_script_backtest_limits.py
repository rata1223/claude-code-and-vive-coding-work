"""#188 — a user-script backtest runs in a child process with a hard budget.

A script that never finishes used to hold the API worker that ran it: the loop
ran inside the request, and neither a thread nor a trace hook can stop a long C
call like ``sum(range(10**13))``. ``strategy.script_backtest`` runs the whole
backtest in a spawned child and kills it when the time budget runs out; the
child's address space is capped too.

Prices are synthetic — no network. Each case spawns a real process (~1.5 s).
The last cases cover the concurrency cap: past it a request is refused at once.
"""
import math
import multiprocessing as mp
import threading
import time

import pandas as pd
import pytest

import strategy.script_backtest as sb
from strategy import SCRIPT_TEMPLATES, Backtester, ScriptStrategy
from strategy.script_backtest import (
    ScriptBacktestBusy,
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


# ── concurrency cap ────────────────────────────────────────────────────────

_LOOP = "def on_bar(bar, ctx):\n    while True:\n        pass\n"
_IDLE = "def on_bar(bar, ctx):\n    return None\n"


@pytest.fixture()
def one_slot(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(sb, "_SLOTS", slots)
    return slots


def _free(slots) -> bool:
    if slots.acquire(blocking=False):
        slots.release()
        return True
    return False


def test_a_request_over_the_cap_is_refused_without_starting(prices, one_slot, monkeypatch):
    class _NoSpawn:
        def Pipe(self, *a, **k):
            raise AssertionError("a backtest started past the cap")

        Process = Pipe

    monkeypatch.setattr(sb, "_CTX", _NoSpawn())
    one_slot.acquire()
    try:
        with pytest.raises(ScriptBacktestBusy, match="실행 중"):
            _run(_IDLE, prices, timeout_sec=30)
    finally:
        one_slot.release()
    assert _free(one_slot)


def test_busy_is_a_backtest_error_the_route_already_reports():
    assert issubclass(ScriptBacktestBusy, ScriptBacktestError)


def test_the_slot_comes_back_after_every_outcome(prices, one_slot, monkeypatch):
    _run(_IDLE, prices, timeout_sec=30)                            # success
    assert _free(one_slot)

    with pytest.raises(ScriptBacktestTimeout):                     # timeout
        _run(_LOOP, prices, timeout_sec=2)
    assert _free(one_slot)

    with pytest.raises(ScriptBacktestError):                       # child failure
        _run(_IDLE, prices.drop(columns=["Close"]), timeout_sec=30)
    assert _free(one_slot)

    def no_network(self):
        raise ConnectionError("price fetch failed")

    monkeypatch.setattr(Backtester, "_fetch", no_network)
    with pytest.raises(ConnectionError):                           # parent fetch
        run_script_backtest(_IDLE, {}, "AAPL", initial_capital=1_000_000,
                            period="1y", timeout_sec=30)
    assert _free(one_slot)
    assert _no_child_left()


def test_a_third_concurrent_backtest_is_refused_while_two_run(prices, monkeypatch):
    monkeypatch.setattr(sb, "_SLOTS", threading.BoundedSemaphore(2))
    outcomes = []

    def hold():
        try:
            _run(_LOOP, prices, timeout_sec=6)
        except ScriptBacktestTimeout:
            outcomes.append("timeout")

    workers = [threading.Thread(target=hold) for _ in range(2)]
    for w in workers:
        w.start()
    deadline = time.time() + 5
    while len(mp.active_children()) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert len(mp.active_children()) == 2           # both slots are in use

    started = time.time()
    with pytest.raises(ScriptBacktestBusy):
        _run(_IDLE, prices, timeout_sec=30)
    assert time.time() - started < 1                # refused, not queued

    for w in workers:
        w.join(20)
    assert outcomes == ["timeout", "timeout"]
    assert _no_child_left()
    _run(_IDLE, prices, timeout_sec=30)             # slots free again
