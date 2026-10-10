"""
A corporate-action gate blocks new entries, not exits.

While a pending/UNKNOWN corporate action gates a symbol, the tracker's qty and
avg may be wrong (an unrecorded split doubles the broker qty and halves the
price). So an exit on a gated symbol is sized from a live broker lookup — the
broker's sellable qty — and the stop-loss is measured against the broker's
split-adjusted avg. Any doubt about the broker figure skips the exit
(fail-closed). Buys stay blocked.
"""
import pytest

from backend.brokers.models import Order, OrderStatus, Position
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import CA_EXIT_PENDING, Fill, PositionTracker
from backend.strategy.indicator.strategy import IndicatorStrategy


class _CA:
    def __init__(self, blocked=(), boom=False):
        self.blocked = set(blocked)
        self.boom = boom

    def is_blocked(self, symbol):
        if self.boom:
            raise RuntimeError("gate check failed")
        return symbol in self.blocked


class _Broker:
    is_live = False

    def __init__(self, positions=None, price=100.0, fail=False):
        self.positions = positions or []
        self.price = price
        self.fail = fail
        self.position_calls = 0

    def get_positions(self):
        self.position_calls += 1
        if self.fail:
            raise RuntimeError("broker down")
        return self.positions

    def get_price(self, symbol):
        return self.price

    def get_balance(self):
        raise AssertionError("not used")


def _bpos(qty=10, avg=50.0, sellable="same", symbol="SPY"):
    return Position(symbol=symbol, qty=qty, avg_price=avg, market="US",
                    sellable_qty=qty if sellable == "same" else sellable)


def _make(broker, ca, tracked_qty=5, tracked_avg=100.0):
    tracker = PositionTracker(OrderStateMachine(), corporate_action_runtime=ca)
    tracker.on_fill(Fill(order_id="F", symbol="SPY", side="buy", qty=tracked_qty,
                         price=tracked_avg, market="US"))
    strat = IndicatorStrategy(broker=broker, tracker=tracker, machine=OrderStateMachine(),
                              name="t", config={"stop_loss_pct": 0.07})
    sells, buys = [], []

    def _sell(symbol, qty, price=None, order_type="limit"):
        sells.append((symbol, qty))
        return Order(id=f"S{len(sells)}", symbol=symbol, side="sell", qty=qty,
                     price=price or 0, status=OrderStatus.SUBMITTED)

    def _buy(symbol, qty, price=None, order_type="limit"):
        buys.append((symbol, qty))
        return Order(id="B", symbol=symbol, side="buy", qty=qty, price=price or 0,
                     status=OrderStatus.SUBMITTED)

    strat.sell, strat.buy = _sell, _buy
    return strat, tracker, sells, buys


# ── tracker ─────────────────────────────────────────────────────────────────
def _gated_tracker(ca=None, tracked_qty=5, tracked_avg=100.0):
    tracker = PositionTracker(OrderStateMachine(), corporate_action_runtime=ca or _CA({"SPY"}))
    tracker.on_fill(Fill(order_id="F", symbol="SPY", side="buy", qty=tracked_qty,
                         price=tracked_avg, market="US"))
    return tracker


class TestTrackerExitClaim:
    def test_the_gate_still_refuses_every_plain_claim(self):
        tracker = _gated_tracker()
        assert tracker.try_mark_pending("SPY") is False
        assert tracker.can_place_order("SPY") is False

    def test_claim_returns_the_broker_sellable_and_adopts_the_broker_position(self):
        tracker = _gated_tracker(tracked_qty=5, tracked_avg=100.0)
        qty, why = tracker.claim_ca_exit("SPY", lambda: [_bpos(qty=10, avg=50.0, sellable=8)])
        assert (qty, why) == (8, "")
        pos = tracker.get_position("SPY")
        assert (pos.qty, pos.avg_price, pos.market) == (10, 50.0, "US")
        assert tracker.claim_ca_exit("SPY", lambda: [_bpos()]) == (None, CA_EXIT_PENDING)

    def test_a_partial_fill_reduces_the_adopted_quantity(self):
        tracker = _gated_tracker(tracked_qty=5)
        tracker.claim_ca_exit("SPY", lambda: [_bpos(qty=10, avg=50.0)])
        tracker.on_fill(Fill(order_id="S", symbol="SPY", side="sell", qty=5, price=50.0,
                             market="US"))
        assert tracker.get_position("SPY").qty == 5

    @pytest.mark.parametrize("positions", [
        [], [_bpos(symbol="QQQ")], [_bpos(sellable=None)], [_bpos(sellable=0)],
        [_bpos(avg=0.0)], [_bpos(avg=float("nan"))],
    ], ids=["none", "other-symbol", "sellable-unknown", "sellable-zero", "avg-zero", "avg-nan"])
    def test_no_trustworthy_broker_figure_refuses_and_leaves_the_tracker_alone(self, positions):
        tracker = _gated_tracker(tracked_qty=5, tracked_avg=100.0)
        qty, why = tracker.claim_ca_exit("SPY", lambda: positions)
        assert qty is None and why
        assert tracker.get_position("SPY").qty == 5
        assert tracker.get_position("SPY").avg_price == 100.0
        assert tracker.claim_ca_exit("SPY", lambda: [_bpos()])[0] is not None   # no lock left

    def test_a_failed_lookup_refuses(self):
        tracker = _gated_tracker()

        def boom():
            raise RuntimeError("down")
        qty, why = tracker.claim_ca_exit("SPY", boom)
        assert qty is None and "down" in why

    def test_a_failing_gate_check_counts_as_gated(self):
        tracker = _gated_tracker(ca=_CA(boom=True))
        assert tracker.is_ca_blocked("SPY") is True
        assert tracker.try_mark_pending("SPY") is False

    def test_ungated_symbol_is_unchanged(self):
        tracker = _gated_tracker(ca=_CA({"QQQ"}))
        assert tracker.is_ca_blocked("SPY") is False
        assert tracker.try_mark_pending("SPY") is True


# ── strategy exits ──────────────────────────────────────────────────────────
class TestGatedSell:
    def test_sells_the_brokers_sellable_qty(self):
        broker = _Broker([_bpos(qty=10, avg=50.0)])
        strat, tracker, sells, _ = _make(broker, _CA({"SPY"}), tracked_qty=5)
        strat._execute_sell("SPY", "signal")
        assert sells == [("SPY", 10)]
        assert tracker.can_place_order("SPY") is False

    def test_sells_only_what_the_broker_says_is_orderable(self):
        broker = _Broker([_bpos(qty=10, sellable=7)])
        strat, _, sells, _ = _make(broker, _CA({"SPY"}))
        strat._execute_sell("SPY", "signal")
        assert sells == [("SPY", 7)]

    @pytest.mark.parametrize("broker", [
        _Broker(fail=True),
        _Broker([]),
        _Broker([_bpos(symbol="QQQ")]),
        _Broker([_bpos(qty=10, sellable=None)]),
        _Broker([_bpos(qty=10, sellable=0)]),
        _Broker([_bpos(qty=10, avg=0.0)]),
    ], ids=["lookup-fails", "no-positions", "other-symbol", "sellable-unknown",
            "sellable-zero", "avg-invalid"])
    def test_no_trustworthy_broker_figure_means_no_sell(self, broker):
        strat, tracker, sells, _ = _make(broker, _CA({"SPY"}))
        strat._execute_sell("SPY", "signal")
        assert sells == []
        assert tracker.claim_ca_exit("SPY", lambda: [_bpos()])[0] is not None   # lock released

    def test_buys_stay_blocked(self):
        broker = _Broker([_bpos()])
        strat, _, _, buys = _make(broker, _CA({"SPY"}))
        strat._execute_buy("SPY", capital=10_000_000)
        assert buys == []

    def test_ungated_sell_uses_the_tracker_and_never_asks_the_broker(self):
        broker = _Broker([_bpos(qty=10)])
        strat, _, sells, _ = _make(broker, _CA({"QQQ"}), tracked_qty=5)
        strat._execute_sell("SPY", "signal")
        assert sells == [("SPY", 5)]
        assert broker.position_calls == 0


class TestGatedStopLoss:
    def _bar(self, close):
        return {"symbol": "SPY", "close": close}

    def test_split_does_not_fire_a_false_stop(self):
        # Tracker avg 100 predates a 2:1 split; the broker's adjusted avg is 50.
        broker = _Broker([_bpos(qty=10, avg=50.0)])
        strat, _, sells, _ = _make(broker, _CA({"SPY"}), tracked_qty=5, tracked_avg=100.0)
        strat.on_bar(self._bar(48.0))   # −4 % against the broker avg
        assert sells == []

    def test_a_real_stop_against_the_broker_avg_sells_the_broker_qty(self):
        broker = _Broker([_bpos(qty=10, avg=50.0)])
        strat, _, sells, _ = _make(broker, _CA({"SPY"}), tracked_qty=5, tracked_avg=100.0)
        strat.on_bar(self._bar(45.0))   # −10 %
        assert sells == [("SPY", 10)]

    def test_the_stop_sizes_from_the_same_lookup(self):
        broker = _Broker([_bpos(qty=10, avg=50.0)])
        strat, _, sells, _ = _make(broker, _CA({"SPY"}))
        strat.on_bar(self._bar(45.0))
        assert sells == [("SPY", 10)]
        assert broker.position_calls == 1

    def test_the_fill_is_measured_against_the_broker_cost_basis(self):
        """The worker reads realized P&L's entry price from the tracker; after a
        gated exit claim the tracker holds the broker's split-adjusted avg, so a
        2:1 split does not book a fake 50 % loss into the kill-switch tracker."""
        broker = _Broker([_bpos(qty=10, avg=50.0)])
        strat, tracker, _, _ = _make(broker, _CA({"SPY"}), tracked_qty=5, tracked_avg=100.0)
        strat.on_bar(self._bar(45.0))
        assert tracker.get_position("SPY").avg_price == 50.0

    def test_a_broker_avg_that_is_not_a_price_means_no_stop(self):
        broker = _Broker([_bpos(qty=10, avg=None)])
        strat, _, sells, _ = _make(broker, _CA({"SPY"}))
        strat.on_bar(self._bar(1.0))
        assert sells == []

    def test_no_broker_lookup_no_stop(self):
        broker = _Broker(fail=True)
        strat, _, sells, _ = _make(broker, _CA({"SPY"}), tracked_avg=100.0)
        strat.on_bar(self._bar(10.0))
        assert sells == []

    def test_ungated_stop_uses_the_tracker_avg(self):
        broker = _Broker([_bpos(qty=10, avg=50.0)])
        strat, _, sells, _ = _make(broker, _CA(), tracked_qty=5, tracked_avg=100.0)
        strat.on_bar(self._bar(90.0))   # −10 % against the tracker
        assert sells == [("SPY", 5)]
        assert broker.position_calls == 0


def test_a_gate_raised_between_check_and_claim_blocks_the_tracker_sized_sell():
    """The ungated path sizes from the tracker; if a gate lands after the check,
    the claim must still see it rather than sell a possibly wrong quantity."""
    class _Flip(_CA):
        calls = 0

        def is_blocked(self, symbol):
            self.calls += 1
            return self.calls > 1

    broker = _Broker([_bpos(qty=10)])
    strat, _, sells, _ = _make(broker, _Flip(), tracked_qty=5)
    strat._execute_sell("SPY", "signal")
    assert sells == []
