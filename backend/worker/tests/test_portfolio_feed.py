"""Positions and equity for the operator live feed (backend/worker/portfolio_feed.py).

kis-ws relayed position:update and equity:update but nothing published them.
The publisher is a view: each read is independent, nothing escapes, and
overlapping triggers coalesce instead of piling up broker reads.
"""
import threading

import pytest

import backend.websocket.server as ws_server
from backend.brokers.models import Balance, Position
from backend.worker import portfolio_feed
from backend.worker import runner


class _Broker:
    def __init__(self, balance=None, positions=None, fail=()):
        self.balance = balance or Balance(cash_krw=1_000_000, cash_usd=120.5,
                                          total_eval_krw=2_000_000, equity_verified=False)
        self.positions = positions if positions is not None else [
            Position(symbol="SPY", qty=2, avg_price=500.0, market="US", current_price=510.0)]
        self.fail = set(fail)
        self.calls = []

    def get_balance(self):
        self.calls.append("balance")
        if "balance" in self.fail:
            raise RuntimeError("balance down")
        return self.balance

    def get_positions(self):
        self.calls.append("positions")
        if "positions" in self.fail:
            raise RuntimeError("positions down")
        return self.positions


@pytest.fixture()
def published(monkeypatch):
    out = {}
    monkeypatch.setattr(ws_server, "publish_equity_update", lambda p: out.__setitem__("equity", p))
    monkeypatch.setattr(ws_server, "publish_position_update", lambda p: out.__setitem__("positions", p))
    return out


def test_both_are_published(published):
    assert portfolio_feed.publish_portfolio(_Broker()) is True
    eq = published["equity"]
    assert (eq["total_eval_krw"], eq["cash_krw"], eq["cash_usd"], eq["equity_verified"]) == \
        (2_000_000, 1_000_000, 120.5, False)
    assert published["positions"]["positions"] == [
        {"symbol": "SPY", "qty": 2, "avg_price": 500.0, "current_price": 510.0, "market": "US"}]
    assert eq["at"] and published["positions"]["at"]


def test_an_unknown_current_price_is_none_not_zero(published):
    portfolio_feed.publish_portfolio(_Broker(positions=[
        Position(symbol="005930", qty=1, avg_price=70000.0, market="KR")]))
    assert published["positions"]["positions"][0]["current_price"] is None


@pytest.mark.parametrize("fail, present", [({"balance"}, "positions"), ({"positions"}, "equity")])
def test_one_failed_read_does_not_suppress_the_other(published, fail, present):
    assert portfolio_feed.publish_portfolio(_Broker(fail=fail)) is True
    assert set(published) == {present}


def test_both_failing_publishes_nothing_and_never_raises(published):
    assert portfolio_feed.publish_portfolio(_Broker(fail={"balance", "positions"})) is False
    assert published == {}


def test_a_publish_error_never_escapes(monkeypatch):
    def boom(_):
        raise ConnectionError("redis down")
    monkeypatch.setattr(ws_server, "publish_equity_update", boom)
    monkeypatch.setattr(ws_server, "publish_position_update", boom)
    assert portfolio_feed.publish_portfolio(_Broker()) is False


def test_overlapping_publishes_coalesce(published):
    broker = _Broker()
    assert portfolio_feed._busy.acquire(blocking=False)
    try:
        assert portfolio_feed.publish_portfolio(broker) is False
    finally:
        portfolio_feed._busy.release()
    assert broker.calls == [], "no broker reads while another publish runs"
    assert portfolio_feed.publish_portfolio(broker) is True


# ── triggers ───────────────────────────────────────────────────────────────

def _bare_worker():
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w._lock = threading.Lock()
    w._aux_threads = []
    return w


def test_a_fill_publishes_the_portfolio_off_the_fill_path():
    import inspect
    src = inspect.getsource(runner.StrategyWorker._make_fill_callback)
    assert src.index("self._publish_order_update(order)") < src.index("self._publish_portfolio_soon()")


def test_the_publish_runs_on_a_tracked_aux_thread(monkeypatch):
    done = threading.Event()
    monkeypatch.setattr(runner, "publish_portfolio", lambda: done.set())
    w = _bare_worker()
    w._portfolio_feed = True
    w._publish_portfolio_soon()
    assert done.wait(5)
    assert [t.name for t in w._aux_threads] == ["portfolio-feed"], "shutdown joins it"


def test_test_built_workers_never_reach_a_broker(monkeypatch):
    monkeypatch.setattr(runner, "publish_portfolio",
                        lambda: pytest.fail("a __new__ worker must not publish"))
    w = _bare_worker()                     # no _portfolio_feed: built without __init__
    w._publish_portfolio_soon()
    assert w._aux_threads == []


def test_the_scheduler_refreshes_it_in_market_hours():
    from backend.worker.scheduler import build_scheduler
    job = build_scheduler().get_job("portfolio_feed")
    assert job is not None and job.func is portfolio_feed.publish_portfolio
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["day_of_week"], fields["hour"], fields["minute"]) == ("mon-sat", "0-6,9-15,22-23", "*/10")
    assert job.max_instances == 1 and job.coalesce is True
