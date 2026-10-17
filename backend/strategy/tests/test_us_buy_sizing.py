"""
A US buy is sized in won, not in dollars.

``_execute_buy`` sizes a buy as capital (won) × ``position_size_pct``. A US
quote from KIS is in USD (``output.last``), and the old ``int(amount_krw /
price)`` divided won by dollars: 100,000 won against SPY at $763 asked for 131
shares (~135 million won). The price is now converted at the broker's rate; a
live broker without a usable rate skips the buy.
"""
import pytest

from backend.brokers.models import Order, OrderStatus
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import PositionTracker
from backend.strategy.indicator.strategy import IndicatorStrategy


class _Broker:
    def __init__(self, prices, rate=1350.0, live=True, has_rate=True):
        self.prices = prices
        self.is_live = live
        self.rate = rate if has_rate else NotImplementedError()

    def get_price(self, symbol):
        return self.prices[symbol]

    def get_fx_rate(self):
        if isinstance(self.rate, Exception):
            raise self.rate
        return self.rate


def _make(broker):
    tracker = PositionTracker(OrderStateMachine())
    strat = IndicatorStrategy(broker=broker, tracker=tracker, machine=OrderStateMachine(),
                              name="t", config={"position_size_pct": 0.05})
    strat._is_feed_tradeable = lambda symbol: True
    buys = []

    def _buy(symbol, qty, price=None, order_type="limit"):
        buys.append((symbol, qty))
        return Order(id=f"B{len(buys)}", symbol=symbol, side="buy", qty=qty,
                     price=price or 0, status=OrderStatus.SUBMITTED)

    strat.buy = _buy
    return strat, tracker, buys


def test_a_us_buy_is_sized_at_the_broker_rate():
    # 10,000,000 won × 5 % = 500,000 won; AAPL at $185 × 1,350 = 249,750 won → 2 shares.
    strat, _, buys = _make(_Broker({"AAPL": 185.0}))
    strat._execute_buy("AAPL", capital=10_000_000)
    assert buys == [("AAPL", 2)]


def test_the_old_bug_would_have_asked_for_131_spy_shares():
    """2,000,000 won × 5 % = 100,000 won is less than one SPY share at $763."""
    strat, tracker, buys = _make(_Broker({"SPY": 763.0}))
    strat._execute_buy("SPY", capital=2_000_000)
    assert buys == []
    assert tracker.can_place_order("SPY") is True          # lock released


def test_a_kr_buy_is_unchanged():
    strat, _, buys = _make(_Broker({"069500": 40_000.0}, rate=RuntimeError("never asked")))
    strat._execute_buy("069500", capital=2_000_000)
    assert buys == [("069500", 2)]


@pytest.mark.parametrize("broker", [
    _Broker({"AAPL": 185.0}, rate=RuntimeError("fx down")),
    _Broker({"AAPL": 185.0}, rate=1.0),
    _Broker({"AAPL": 185.0}, rate=900.0),
    _Broker({"AAPL": 185.0}, rate=2000.0),
    _Broker({"AAPL": 185.0}, rate=float("nan")),
    _Broker({"AAPL": 185.0}, has_rate=False),
], ids=["rate-raises", "rate-one", "rate-at-low-bound", "rate-at-high-bound", "rate-nan",
        "live-broker-without-rate"])
def test_no_usable_rate_skips_the_us_buy(broker):
    strat, tracker, buys = _make(broker)
    strat._execute_buy("AAPL", capital=10_000_000)
    assert buys == []
    assert tracker.can_place_order("AAPL") is True
    assert strat._breaker._failures == 0, "a sizing refusal is not a broker failure"


def test_a_simulated_broker_without_a_rate_prices_in_its_own_currency():
    strat, _, buys = _make(_Broker({"AAPL": 185.0}, live=False, has_rate=False))
    strat._execute_buy("AAPL", capital=10_000_000)
    assert buys == [("AAPL", 2702)]                         # 500,000 / 185, as before


def test_the_paper_broker_and_the_router_expose_the_rate():
    from backend.brokers.paper_broker import ScriptedPaperBroker
    from backend.brokers.router import MarketRouter
    paper = ScriptedPaperBroker(fx_usdkrw=1_310.0)
    assert paper.get_fx_rate() == 1_310.0
    assert MarketRouter(kr_broker=object(), us_broker=paper).get_fx_rate() == 1_310.0


def test_a_kr_etf_routed_by_the_shared_definition_is_not_converted():
    """``backend.market.symbols.is_kr`` is the one KR-vs-US definition."""
    from backend.quant.data.universe import KR_ETF
    sym = KR_ETF[0]
    strat, _, buys = _make(_Broker({sym: 40_000.0}, rate=RuntimeError("never asked")))
    strat._execute_buy(sym, capital=2_000_000)
    assert buys == [(sym, 2)]
