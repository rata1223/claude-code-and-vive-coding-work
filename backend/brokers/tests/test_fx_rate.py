"""
``KISBroker.get_fx_rate`` sizes US orders, so it returns only a rate actually
fetched recently; ``valuation_fx_rate`` values balances and P&L, so it never
raises. A failing source is retried at most every ``_FX_RETRY_SEC``.
"""
import sys
import types

import pytest

import backend.brokers.kis as kis


@pytest.fixture()
def fx(monkeypatch):
    """A fresh rate cache and a controllable yfinance."""
    state = {"rate": 1_400.0, "calls": 0, "clock": 10_000.0}
    monkeypatch.setattr(kis, "_FX_CACHE", {"rate": 1350.0, "ts": state["clock"],
                                           "ok_ts": None, "tried_ts": None})
    monkeypatch.setattr(kis.time, "monotonic", lambda: state["clock"])

    class _Ticker:
        def __init__(self, sym):
            state["calls"] += 1
            if isinstance(state["rate"], Exception):
                raise state["rate"]
            self.fast_info = {"last_price": state["rate"]}

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))
    broker = kis.KISBroker.__new__(kis.KISBroker)
    return broker, state


def test_the_seeded_default_never_sizes_an_order(fx):
    broker, state = fx
    state["rate"] = RuntimeError("blocked")
    with pytest.raises(RuntimeError):
        broker.get_fx_rate()
    assert broker.valuation_fx_rate() == 1350.0           # balances still valued


def test_a_fetched_rate_sizes_orders(fx):
    broker, state = fx
    assert broker.get_fx_rate() == 1_400.0
    assert broker.valuation_fx_rate() == 1_400.0


def test_an_implausible_rate_is_not_adopted(fx):
    broker, state = fx
    state["rate"] = 1.0
    with pytest.raises(RuntimeError):
        broker.get_fx_rate()


def test_a_rate_too_old_stops_sizing_but_still_values(fx):
    broker, state = fx
    broker.get_fx_rate()
    state["rate"] = RuntimeError("blocked")
    state["clock"] += kis._FX_SIZING_MAX_AGE + 1
    with pytest.raises(RuntimeError):
        broker.get_fx_rate()
    assert broker.valuation_fx_rate() == 1_400.0


def test_a_failing_source_is_not_hit_on_every_call(fx):
    broker, state = fx
    state["rate"] = RuntimeError("blocked")
    for _ in range(5):
        with pytest.raises(RuntimeError):
            broker.get_fx_rate()
    assert state["calls"] == 1
    state["clock"] += kis._FX_RETRY_SEC
    state["rate"] = 1_420.0
    assert broker.get_fx_rate() == 1_420.0
    assert state["calls"] == 2


def test_a_rate_older_than_an_hour_is_refreshed_for_sizing(fx):
    broker, state = fx
    assert broker.get_fx_rate() == 1_400.0
    state["rate"] = 1_450.0
    state["clock"] += kis._FX_TTL
    assert broker.get_fx_rate() == 1_450.0
