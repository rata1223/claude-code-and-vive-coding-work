"""
The stop-loss actually runs (known issue 17).

The 7 % stop lived only in ``IndicatorStrategy.on_bar``, and nothing in the
worker feeds bars, so it never ran. ``check_stops`` is the sweep the worker now
calls on a timer: each held symbol whose market is open is priced from the
broker and run through the same stop ``on_bar`` uses. The worker side
(``WorkerSession.trigger_stop_check``, the ``stop_check`` job) is tested in
``backend/worker/tests/test_stop_check_job.py``.
"""

import pytest

from backend.data.calendar import Market
from backend.strategy.tests.test_ca_gate_exit import _CA, _bpos, _make


class _PricedBroker:
    """Per-symbol quotes; a quote may be an exception to raise."""

    def __init__(self, prices, positions=None, live=False):
        self.prices = prices
        self.positions = positions or []
        self.is_live = live
        self.price_calls = []

    def get_price(self, symbol):
        self.price_calls.append(symbol)
        quote = self.prices[symbol]
        if isinstance(quote, Exception):
            raise quote
        return quote

    def get_positions(self):
        return self.positions

    def get_balance(self):
        raise AssertionError("not used")


def _two_positions(broker, ca=None):
    """SPY 5 @100 and QQQ 3 @200 in the tracker."""
    from backend.execution.position_tracker import Fill
    strat, tracker, sells, buys = _make(broker, ca or _CA(), tracked_qty=5, tracked_avg=100.0)
    tracker.on_fill(Fill(order_id="Q", symbol="QQQ", side="buy", qty=3, price=200.0, market="US"))
    strat._is_feed_tradeable = lambda symbol: True
    return strat, tracker, sells


class TestSweep:
    def test_only_a_position_past_the_stop_is_sold(self):
        broker = _PricedBroker({"SPY": 90.0, "QQQ": 194.0})     # −10 %, −3 %
        strat, _, sells = _two_positions(broker)
        strat.check_stops()
        assert sells == [("SPY", 5)]

    def test_exactly_at_the_stop_sells(self):
        broker = _PricedBroker({"SPY": 93.0, "QQQ": 200.0})     # −7 %
        strat, _, sells = _two_positions(broker)
        strat.check_stops()
        assert sells == [("SPY", 5)]

    @pytest.mark.parametrize("bad", [RuntimeError("down"), 0, -1.0, None, float("nan")],
                             ids=["raises", "zero", "negative", "none", "nan"])
    def test_a_bad_quote_skips_that_symbol_only(self, bad):
        broker = _PricedBroker({"SPY": bad, "QQQ": 180.0})      # QQQ −10 %
        strat, _, sells = _two_positions(broker)
        strat.check_stops()
        assert sells == [("QQQ", 3)]

    def test_an_open_breaker_makes_no_broker_call(self):
        broker = _PricedBroker({"SPY": 50.0, "QQQ": 50.0})
        strat, _, sells = _two_positions(broker)
        for _ in range(3):
            strat._breaker.record_failure()
        assert strat._breaker.is_open()
        strat.check_stops()
        assert sells == [] and broker.price_calls == []

    def test_a_sell_already_in_flight_is_not_sent_twice(self):
        broker = _PricedBroker({"SPY": 80.0, "QQQ": 200.0})
        strat, tracker, sells = _two_positions(broker)
        strat.check_stops()
        strat.check_stops()                                      # the next sweep
        assert sells == [("SPY", 5)]
        assert tracker.can_place_order("SPY") is False

    def test_a_gated_symbol_is_measured_against_the_broker_avg(self):
        # Tracker avg 100 predates a 2:1 split; the broker holds 10 @50.
        broker = _PricedBroker({"SPY": 48.0, "QQQ": 200.0}, positions=[_bpos(qty=10, avg=50.0)])
        strat, _, sells = _two_positions(broker, ca=_CA({"SPY"}))
        strat.check_stops()                                      # −4 % on the broker avg
        assert sells == []
        broker.prices["SPY"] = 45.0                              # −10 %
        strat.check_stops()
        assert sells == [("SPY", 10)]

    def test_no_positions_no_calls(self):
        broker = _PricedBroker({})
        strat, tracker, sells, _ = _make(broker, _CA(), tracked_qty=5)
        tracker.restore_positions([])
        strat.check_stops()
        assert broker.price_calls == [] and sells == []


class _Permission:
    def __init__(self, allowed):
        self.allowed = allowed


class _Calendar:
    def __init__(self, open_markets, boom=False):
        self.open_markets = open_markets
        self.boom = boom
        self.asked = []

    def check_order_permission(self, market, dt_utc):
        self.asked.append(market)
        if self.boom:
            raise RuntimeError("calendar down")
        return _Permission(market in self.open_markets)


class TestMarketHours:
    def _live(self, monkeypatch, calendar, prices):
        monkeypatch.setattr("backend.data.calendar.get_calendar_service", lambda: calendar)
        from backend.execution.position_tracker import Fill
        broker = _PricedBroker(prices, live=True)
        strat, tracker, sells, _ = _make(broker, _CA(), tracked_qty=5, tracked_avg=100.0)
        tracker.on_fill(Fill(order_id="K", symbol="069500", side="buy", qty=2,
                             price=30_000.0, market="KR"))
        strat._is_feed_tradeable = lambda symbol: True
        return strat, broker, sells

    def test_a_closed_market_is_skipped_before_any_broker_call(self, monkeypatch):
        cal = _Calendar({Market.KRX})
        strat, broker, sells = self._live(monkeypatch, cal, {"SPY": 50.0, "069500": 25_000.0})
        strat.check_stops()
        assert "SPY" not in broker.price_calls                   # SPY's market is closed
        assert sells == [("069500", 2)]
        assert set(cal.asked) == {Market.KRX, Market.NYSE}

    def test_a_calendar_error_skips_everything(self, monkeypatch):
        strat, broker, sells = self._live(monkeypatch, _Calendar(set(), boom=True),
                                          {"SPY": 50.0, "069500": 25_000.0})
        strat.check_stops()
        assert broker.price_calls == [] and sells == []


def test_on_bar_still_runs_the_same_stop():
    broker = _PricedBroker({"SPY": 90.0})
    strat, _, sells, _ = _make(broker, _CA(), tracked_qty=5, tracked_avg=100.0)
    strat.on_bar({"symbol": "SPY", "close": 90.0})
    assert sells == [("SPY", 5)]


class TestSweepOrders:
    def test_a_restart_does_not_hold_the_stop_shut(self):
        """The daily-bar freshness record is written only by the market-open
        scan, so right after a restart every symbol reads UNKNOWN. A stop
        judged on a validated live quote still sells; the bar path does not."""
        broker = _PricedBroker({"SPY": 90.0, "QQQ": 200.0})
        strat, _, sells = _two_positions(broker)
        strat._is_feed_tradeable = lambda symbol: False
        strat.on_bar({"symbol": "SPY", "close": 90.0})
        assert sells == []
        strat.check_stops()
        assert sells == [("SPY", 5)]

    def test_the_sell_goes_out_at_the_quote_the_stop_was_judged_on(self):
        broker = _PricedBroker({"SPY": 90.0, "QQQ": 200.0})
        strat, _, _ = _two_positions(broker)
        priced = []
        inner = strat.sell
        strat.sell = lambda symbol, qty, price=None, order_type="limit": (
            priced.append(price), inner(symbol, qty, price, order_type))[1]
        strat.check_stops()
        assert priced == [90.0]
        assert broker.price_calls.count("SPY") == 1

    def test_a_symbol_with_an_open_order_is_skipped(self):
        from backend.brokers.models import Order, OrderStatus
        broker = _PricedBroker({"SPY": 50.0, "QQQ": 100.0})
        strat, _, sells = _two_positions(broker)
        strat._machine.register(Order(id="O1", symbol="SPY", side="sell", qty=5, price=95.0,
                                      status=OrderStatus.SUBMITTED))
        strat.check_stops()
        assert "SPY" not in broker.price_calls
        assert sells == [("QQQ", 3)]

    def test_a_refused_stop_does_not_trip_the_scan_breaker(self):
        from backend.brokers.models import Order, OrderStatus
        broker = _PricedBroker({"SPY": 50.0, "QQQ": 200.0})
        strat, tracker, _ = _two_positions(broker)
        strat.sell = lambda symbol, qty, price=None, order_type="limit": Order(
            id="", symbol=symbol, side="sell", qty=0, price=0, status=OrderStatus.REJECTED)
        for _ in range(5):
            strat.check_stops()
        assert strat._breaker.is_open() is False
        assert tracker.can_place_order("SPY") is True           # lock released for the next sweep

    def test_a_scan_sell_still_counts_against_the_breaker(self):
        from backend.brokers.models import Order, OrderStatus
        broker = _PricedBroker({"SPY": 50.0, "QQQ": 200.0})
        strat, _, _ = _two_positions(broker)
        strat.sell = lambda symbol, qty, price=None, order_type="limit": Order(
            id="", symbol=symbol, side="sell", qty=0, price=0, status=OrderStatus.REJECTED)
        for _ in range(3):
            strat._execute_sell("SPY", "signal")
        assert strat._breaker.is_open() is True

    def test_should_stop_ends_the_sweep_before_the_next_symbol(self):
        broker = _PricedBroker({"SPY": 50.0, "QQQ": 50.0})
        strat, _, sells = _two_positions(broker)
        asked = []

        def should_stop():
            asked.append(1)
            return len(asked) > 1
        strat.check_stops(should_stop=should_stop)
        assert len(sells) == 1 and len(broker.price_calls) == 1


def test_a_gated_stop_after_a_restart_still_sells_at_the_quote():
    broker = _PricedBroker({"SPY": 45.0, "QQQ": 200.0}, positions=[_bpos(qty=10, avg=50.0)])
    strat, _, sells = _two_positions(broker, ca=_CA({"SPY"}))
    strat._is_feed_tradeable = lambda symbol: False
    strat.check_stops()
    assert sells == [("SPY", 10)]
    assert broker.price_calls.count("SPY") == 1
