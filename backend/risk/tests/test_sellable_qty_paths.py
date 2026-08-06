"""
P0-07 S2 — per-path enforcement (T4, T5, T6, T12).

The core rule lives in ``test_sellable_qty.py``. This module proves each sell
path actually consumes it, and that S1's halt-vs-exit behaviour is unchanged.
"""
import pytest

from backend.brokers.capabilities import SIMULATOR_CAPABILITIES
from backend.brokers.models import Order, OrderStatus, Position
from backend.risk.halt_policy import HaltCause, prove_exit
from backend.strategy.base import StrategyBase
from backend.worker.recovery import SAFE_MODE


class _FakeBroker:
    def __init__(self, positions=None, price=100.0):
        self.is_live = True
        self.capabilities = SIMULATOR_CAPABILITIES
        self.placed: list[tuple] = []
        self._positions = positions or []
        self._price = price

    def get_positions(self):
        return self._positions

    def get_price(self, symbol):
        return self._price

    def place_order(self, symbol, side, qty, price, order_type="limit"):
        self.placed.append((symbol, side, qty, price))
        return Order(id="X1", symbol=symbol, side=side, qty=qty, price=price,
                     status=OrderStatus.SUBMITTED)


def _pos(symbol="SPY", qty=10, sellable=None):
    return Position(symbol=symbol, qty=qty, avg_price=100.0, market="US",
                    sellable_qty=sellable)


@pytest.fixture(autouse=True)
def restore_safe_mode(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    was_trading, cause = SAFE_MODE.can_trade, SAFE_MODE.halt_cause
    yield
    if was_trading:
        SAFE_MODE.enable()
    else:
        SAFE_MODE.disable("teardown", cause=cause or HaltCause.UNTRUSTED_STATE)


@pytest.fixture()
def strat():
    return StrategyBase(broker=None, name="s2-paths")


# ── T6: strategy exit sells respect sellable, not held ───────────────────────

def test_t6_exit_proof_uses_sellable_not_held():
    broker = _FakeBroker([_pos(qty=10, sellable=4)])
    assert prove_exit(broker.get_positions, "SPY", 4)[0] is True
    assert prove_exit(broker.get_positions, "SPY", 10)[0] is False   # held, not sellable


def test_t6_strategy_exit_blocked_above_sellable(strat):
    SAFE_MODE.disable("MDD", cause=HaltCause.RISK_BREACH)
    broker = _FakeBroker([_pos(qty=10, sellable=4)])
    strat._broker = broker

    result = strat.sell("SPY", 10, price=100.0)

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []


def test_t6_strategy_exit_allowed_within_sellable(strat):
    SAFE_MODE.disable("MDD", cause=HaltCause.RISK_BREACH)
    broker = _FakeBroker([_pos(qty=10, sellable=4)])
    strat._broker = broker

    strat.sell("SPY", 4, price=100.0)

    assert broker.placed == [("SPY", "sell", 4, 100.0)]


def test_t6_unreported_sellable_blocks_the_exit(strat):
    """A broker that states no orderable figure fails closed on this path —
    only EmergencyFlatten falls back to held."""
    SAFE_MODE.disable("MDD", cause=HaltCause.RISK_BREACH)
    broker = _FakeBroker([_pos(qty=10, sellable=None)])
    strat._broker = broker

    result = strat.sell("SPY", 10, price=100.0)

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []


def test_t6_zero_sellable_blocks_even_with_shares_held(strat):
    SAFE_MODE.disable("MDD", cause=HaltCause.RISK_BREACH)
    broker = _FakeBroker([_pos(qty=10, sellable=0)])
    strat._broker = broker

    assert strat.sell("SPY", 1, price=100.0).status == OrderStatus.REJECTED
    assert broker.placed == []


# ── T12: S1 halt-vs-exit behaviour is unchanged ──────────────────────────────

def test_t12_entry_still_blocked_under_every_halt_cause(strat):
    broker = _FakeBroker([_pos(qty=10, sellable=10)])
    strat._broker = broker
    for cause in HaltCause:
        SAFE_MODE.disable("halted", cause=cause)
        assert strat.buy("SPY", 1, price=100.0).status == OrderStatus.REJECTED
    assert broker.placed == []


def test_t12_untrusted_state_still_blocks_a_fully_sellable_exit(strat):
    SAFE_MODE.disable("복구 미완료", cause=HaltCause.UNTRUSTED_STATE)
    broker = _FakeBroker([_pos(qty=10, sellable=10)])
    strat._broker = broker

    assert strat.sell("SPY", 10, price=100.0).status == OrderStatus.REJECTED
    assert broker.placed == []


def test_t12_risk_breach_still_allows_a_fully_sellable_exit(strat):
    SAFE_MODE.disable("MDD", cause=HaltCause.RISK_BREACH)
    broker = _FakeBroker([_pos(qty=10, sellable=10)])
    strat._broker = broker

    strat.sell("SPY", 10, price=100.0)

    assert broker.placed == [("SPY", "sell", 10, 100.0)]


def test_t12_running_state_still_needs_no_position_lookup(strat):
    """Unhalted trading must not pay for a position lookup — S1 behaviour."""
    SAFE_MODE.enable()

    class _Boom(_FakeBroker):
        def get_positions(self):
            raise AssertionError("must not be consulted while running")

    broker = _Boom([])
    strat._broker = broker
    strat.sell("SPY", 10, price=100.0)

    assert broker.placed == [("SPY", "sell", 10, 100.0)]


# ── Adapters state a sellable figure where held is sellable by construction ──

def test_paper_broker_reports_sellable_equal_to_held():
    from backend.brokers.paper_broker import ScriptedPaperBroker

    b = ScriptedPaperBroker(default_price=100.0)
    b.set_position("SPY", 10, 100.0, market="US")

    pos = b.get_positions()[0]
    assert pos.sellable_qty == pos.qty == 10


def test_position_tracker_reports_sellable_equal_to_held():
    from backend.execution.position_tracker import PositionTracker

    tracker = PositionTracker(machine=None)
    tracker.restore_positions([_pos(qty=7, sellable=None)])

    pos = tracker.get_position("SPY")
    assert pos.sellable_qty == pos.qty == 7


def test_kis_adapter_parses_orderable_quantity():
    from backend.brokers.kis import _orderable_qty

    assert _orderable_qty({"ord_psbl_qty": "4"}, held=10) == 4
    assert _orderable_qty({"ord_psbl_qty": "40"}, held=10) == 10   # capped by held
    assert _orderable_qty({}, held=10) is None                     # not reported
    assert _orderable_qty({"ord_psbl_qty": ""}, held=10) is None
    assert _orderable_qty({"ord_psbl_qty": "oops"}, held=10) is None
    assert _orderable_qty({"ord_psbl_qty": "-1"}, held=10) is None


def test_orderable_qty_rejects_values_it_cannot_read_exactly():
    """``int(float(raw))`` accepted a bool, truncated "1.9" to 1, and let "inf"
    raise OverflowError out of get_positions(). A count we cannot read exactly
    must fail closed, not authorise a sell."""
    from backend.brokers.kis import _orderable_qty

    assert _orderable_qty({"ord_psbl_qty": True}, held=10) is None
    assert _orderable_qty({"ord_psbl_qty": "1.9"}, held=10) is None
    assert _orderable_qty({"ord_psbl_qty": 1.9}, held=10) is None
    assert _orderable_qty({"ord_psbl_qty": "inf"}, held=10) is None
    assert _orderable_qty({"ord_psbl_qty": "nan"}, held=10) is None
    assert _orderable_qty({"ord_psbl_qty": float("inf")}, held=10) is None
    assert _orderable_qty({"ord_psbl_qty": None}, held=10) is None


def test_orderable_qty_still_reads_the_shapes_kis_actually_sends():
    from backend.brokers.kis import _orderable_qty

    assert _orderable_qty({"ord_psbl_qty": "7"}, held=10) == 7
    assert _orderable_qty({"ord_psbl_qty": 7}, held=10) == 7
    assert _orderable_qty({"ord_psbl_qty": "7.0"}, held=10) == 7
    assert _orderable_qty({"ord_psbl_qty": "0"}, held=10) == 0


def test_an_unrecognised_order_status_still_reserves_quantity():
    """Listing the *open* statuses would make a renamed constant — or a
    partially-filled state added later — silently report 0 pending, which
    permits an over-ask. Only the terminal states release quantity."""
    from backend.risk.sellable_qty import pending_sell_qty_from_rows

    rows = [("AAPL", "sell", 4, "partially_filled")]
    assert pending_sell_qty_from_rows(rows, "AAPL") == 4

    for released in ("rejected", "failed", "blocked"):
        assert pending_sell_qty_from_rows([("AAPL", "sell", 4, released)], "AAPL") == 0


def test_unknown_sellable_causes_are_distinguishable():
    from backend.risk.sellable_qty import (
        CAUSE_UNREPORTED, CAUSE_UNTRUSTED, UNKNOWN, resolve_sellable,
    )

    assert resolve_sellable(10, UNKNOWN).cause == CAUSE_UNREPORTED
    assert resolve_sellable(10, "garbage").cause == CAUSE_UNTRUSTED
    assert resolve_sellable(10, float("nan")).cause == CAUSE_UNTRUSTED
    assert resolve_sellable(10, 4).cause is None          # known → no cause
