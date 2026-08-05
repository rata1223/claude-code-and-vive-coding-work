"""
P0-07 S1 — Halt-vs-Exit policy (Policy B: ENTRY HALT + EXIT ALLOWED).

Core invariant these tests pin down:

    A halt stops the creation of NEW risk. It must not remove the system's
    ability to reduce risk it already holds — EXCEPT when the halt exists
    because position state itself is untrusted.

Before S1 the worker gate (``_live_trade_allowed``) checked ``SAFE_MODE.can_trade``
for buy and sell identically, so an MDD kill switch froze the book at maximum
drawdown: the automatic control that fires to stop losses also disabled the
stop-losses. These tests fix the cause-aware behavior instead.
"""
import pytest

from backend.brokers.capabilities import SIMULATOR_CAPABILITIES
from backend.brokers.models import Order, OrderStatus, Position
from backend.risk.halt_policy import (
    HaltCause,
    OperationClass,
    is_allowed,
    is_valid_execution_price,
    prove_exit,
)
from backend.strategy.base import StrategyBase
from backend.worker.recovery import SAFE_MODE


class _FakeBroker:
    """Minimal live-broker stub with a configurable position book and quote."""

    def __init__(self, is_live=True, positions=None, price=100.0, positions_error=None):
        self.is_live = is_live
        self.capabilities = SIMULATOR_CAPABILITIES
        self.placed: list[tuple] = []
        self._positions = positions if positions is not None else []
        self._price = price
        self._positions_error = positions_error

    def get_positions(self):
        if self._positions_error:
            raise self._positions_error
        return self._positions

    def get_price(self, symbol):
        if callable(self._price):
            return self._price(symbol)
        return self._price

    def place_order(self, symbol, side, qty, price, order_type="limit"):
        self.placed.append((symbol, side, qty, price, order_type))
        return Order(id="X1", symbol=symbol, side=side, qty=qty, price=price,
                     status=OrderStatus.SUBMITTED)


def _pos(symbol="SPY", qty=10, avg=100.0):
    return Position(symbol=symbol, qty=qty, avg_price=avg, market="US")


@pytest.fixture(autouse=True)
def restore_safe_mode(monkeypatch):
    """Restore the SAFE_MODE singleton, and pass the shadow gate by default so
    these tests exercise the halt policy rather than ENABLE_LIVE_TRADING."""
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    original_can_trade = SAFE_MODE.can_trade
    original_cause = SAFE_MODE.halt_cause
    yield
    if original_can_trade:
        SAFE_MODE.enable()
    else:
        SAFE_MODE.disable("test teardown", cause=original_cause or HaltCause.UNTRUSTED_STATE)


@pytest.fixture()
def strat():
    return StrategyBase(broker=None, name="halt-policy-test")


# ── 1. Decision matrix (pure) ────────────────────────────────────────────────

@pytest.mark.parametrize("cause,op,expected", [
    # RUNNING
    (None, OperationClass.ENTRY, True),
    (None, OperationClass.EXIT, True),
    (None, OperationClass.EMERGENCY, True),
    (None, OperationClass.NON_EXPOSURE, True),
    # RISK_BREACH
    (HaltCause.RISK_BREACH, OperationClass.ENTRY, False),
    (HaltCause.RISK_BREACH, OperationClass.EXIT, True),
    (HaltCause.RISK_BREACH, OperationClass.EMERGENCY, True),
    (HaltCause.RISK_BREACH, OperationClass.NON_EXPOSURE, True),
    # DEGRADED_FEED
    (HaltCause.DEGRADED_FEED, OperationClass.ENTRY, False),
    (HaltCause.DEGRADED_FEED, OperationClass.EXIT, True),
    (HaltCause.DEGRADED_FEED, OperationClass.EMERGENCY, True),
    (HaltCause.DEGRADED_FEED, OperationClass.NON_EXPOSURE, True),
    # UNTRUSTED_STATE — the exception: exits blocked too
    (HaltCause.UNTRUSTED_STATE, OperationClass.ENTRY, False),
    (HaltCause.UNTRUSTED_STATE, OperationClass.EXIT, False),
    (HaltCause.UNTRUSTED_STATE, OperationClass.EMERGENCY, True),
    (HaltCause.UNTRUSTED_STATE, OperationClass.NON_EXPOSURE, True),
])
def test_decision_matrix(cause, op, expected):
    assert is_allowed(cause, op) is expected


# ── 2. EXIT proof (R2) ───────────────────────────────────────────────────────

def test_exit_proof_passes_for_full_and_partial_close():
    broker = _FakeBroker(positions=[_pos(qty=10)])
    assert prove_exit(broker.get_positions, "SPY", 10)[0] is True
    assert prove_exit(broker.get_positions, "SPY", 4)[0] is True


@pytest.mark.parametrize("qty", [0, -1, 11, 100])
def test_exit_proof_rejects_non_reducing_quantities(qty):
    broker = _FakeBroker(positions=[_pos(qty=10)])
    proven, reason = prove_exit(broker.get_positions, "SPY", qty)
    assert proven is False and reason


def test_exit_proof_rejects_unknown_symbol():
    broker = _FakeBroker(positions=[_pos("QQQ", qty=10)])
    assert prove_exit(broker.get_positions, "SPY", 1)[0] is False


def test_exit_proof_fails_closed_when_lookup_raises():
    broker = _FakeBroker(positions_error=RuntimeError("broker down"))
    proven, reason = prove_exit(broker.get_positions, "SPY", 1)
    assert proven is False and "broker down" in reason


def test_exit_proof_never_clamps():
    """An over-close is rejected outright, not silently reduced to held_qty."""
    broker = _FakeBroker(positions=[_pos(qty=10)])
    proven, _ = prove_exit(broker.get_positions, "SPY", 25)
    assert proven is False


# ── 3. Worker gate: ENTRY blocked under every halt ───────────────────────────

@pytest.mark.parametrize("cause", list(HaltCause))
def test_all_halt_causes_block_buy(strat, cause):
    SAFE_MODE.disable("halted", cause=cause)
    broker = _FakeBroker(positions=[_pos(qty=10)])
    strat._broker = broker

    result = strat.buy("SPY", 10, price=100.0)

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []


# ── 4. Worker gate: EXIT allowed under RISK_BREACH ───────────────────────────

def test_risk_breach_allows_verified_exit_sell(strat):
    SAFE_MODE.disable("MDD 한도 초과", cause=HaltCause.RISK_BREACH)
    broker = _FakeBroker(positions=[_pos(qty=10)])
    strat._broker = broker

    result = strat.sell("SPY", 10, price=100.0)

    assert result.status != OrderStatus.REJECTED
    assert broker.placed == [("SPY", "sell", 10, 100.0, "limit")]


def test_risk_breach_allows_partial_exit(strat):
    SAFE_MODE.disable("일일 손실 한도", cause=HaltCause.RISK_BREACH)
    broker = _FakeBroker(positions=[_pos(qty=10)])
    strat._broker = broker

    strat.sell("SPY", 3, price=100.0)

    assert broker.placed == [("SPY", "sell", 3, 100.0, "limit")]


# ── 5. Worker gate: unproven SELL stays blocked (R1/R2) ──────────────────────

def test_risk_breach_blocks_sell_without_a_position(strat):
    SAFE_MODE.disable("MDD", cause=HaltCause.RISK_BREACH)
    broker = _FakeBroker(positions=[])
    strat._broker = broker

    result = strat.sell("SPY", 10, price=100.0)

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []


def test_risk_breach_blocks_sell_exceeding_held_qty(strat):
    SAFE_MODE.disable("MDD", cause=HaltCause.RISK_BREACH)
    broker = _FakeBroker(positions=[_pos(qty=5)])
    strat._broker = broker

    result = strat.sell("SPY", 10, price=100.0)

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []


def test_risk_breach_blocks_sell_when_position_lookup_fails(strat):
    """R3: gate evaluation failure fails closed."""
    SAFE_MODE.disable("MDD", cause=HaltCause.RISK_BREACH)
    broker = _FakeBroker(positions_error=RuntimeError("position lookup down"))
    strat._broker = broker

    result = strat.sell("SPY", 10, price=100.0)

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []


# ── 6. UNTRUSTED_STATE blocks exits too ──────────────────────────────────────

def test_untrusted_state_blocks_even_a_verified_exit(strat):
    SAFE_MODE.disable("복구 미완료", cause=HaltCause.UNTRUSTED_STATE)
    broker = _FakeBroker(positions=[_pos(qty=10)])
    strat._broker = broker

    result = strat.sell("SPY", 10, price=100.0)

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []


def test_disable_without_a_cause_defaults_to_untrusted_state(strat):
    """R3: a halt with no declared cause is the most restrictive one."""
    SAFE_MODE.disable("legacy caller with no cause")
    assert SAFE_MODE.halt_cause is HaltCause.UNTRUSTED_STATE

    broker = _FakeBroker(positions=[_pos(qty=10)])
    strat._broker = broker
    assert strat.sell("SPY", 10, price=100.0).status == OrderStatus.REJECTED
    assert broker.placed == []


def test_startup_state_is_untrusted_before_recovery():
    """A fresh process has not recovered yet — exits must not be permitted."""
    from backend.worker.recovery import SafeModeState
    fresh = SafeModeState()
    assert fresh.can_trade is False
    assert fresh.halt_cause is HaltCause.UNTRUSTED_STATE


def test_enable_clears_the_cause():
    SAFE_MODE.disable("halted", cause=HaltCause.RISK_BREACH)
    SAFE_MODE.enable()
    assert SAFE_MODE.halt_cause is None


# ── 7. DEGRADED_FEED requires a valid execution price (G2 rules) ─────────────

@pytest.mark.parametrize("bad", [0, 0.0, -1.0, float("nan"), float("inf"),
                                 float("-inf"), "100", True, None])
def test_invalid_execution_prices_are_rejected(bad):
    assert is_valid_execution_price(bad) is False


@pytest.mark.parametrize("good", [0.0001, 1, 100.0, 30000])
def test_valid_execution_prices_accepted(good):
    assert is_valid_execution_price(good) is True


def test_degraded_feed_allows_exit_with_a_valid_price(strat):
    SAFE_MODE.disable("stale feed", cause=HaltCause.DEGRADED_FEED)
    broker = _FakeBroker(positions=[_pos(qty=10)])
    strat._broker = broker

    strat.sell("SPY", 10, price=100.0)

    assert broker.placed == [("SPY", "sell", 10, 100.0, "limit")]


@pytest.mark.parametrize("bad_price", [0, -5.0, float("nan"), float("inf")])
def test_degraded_feed_blocks_exit_without_a_valid_price(strat, bad_price):
    SAFE_MODE.disable("stale feed", cause=HaltCause.DEGRADED_FEED)
    broker = _FakeBroker(positions=[_pos(qty=10)])
    strat._broker = broker

    result = strat.sell("SPY", 10, price=bad_price)

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []


def test_degraded_feed_blocks_exit_when_quote_lookup_raises(strat):
    SAFE_MODE.disable("stale feed", cause=HaltCause.DEGRADED_FEED)

    def boom(_symbol):
        raise RuntimeError("feed down")

    broker = _FakeBroker(positions=[_pos(qty=10)], price=boom)
    strat._broker = broker

    result = strat.sell("SPY", 10, price=None)   # price resolved from the feed

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []


# ── 8. Unhalted behavior unchanged (no regression) ───────────────────────────

def test_running_state_submits_buy_and_sell_without_position_lookup(strat):
    SAFE_MODE.enable()
    broker = _FakeBroker(positions_error=RuntimeError("must not be consulted"))
    strat._broker = broker

    strat.buy("SPY", 10, price=100.0)
    strat.sell("SPY", 10, price=100.0)

    assert len(broker.placed) == 2      # no EXIT proof needed while running


def test_simulated_broker_bypasses_the_policy_entirely(strat):
    SAFE_MODE.disable("halted", cause=HaltCause.UNTRUSTED_STATE)
    broker = _FakeBroker(is_live=False, positions=[])
    strat._broker = broker

    strat.buy("SPY", 10, price=100.0)
    strat.sell("SPY", 10, price=100.0)

    assert len(broker.placed) == 2      # backtests/dry-runs unaffected


def test_shadow_gate_still_blocks_exits_when_live_trading_disabled(strat, monkeypatch):
    """ENABLE_LIVE_TRADING is a separate gate and is NOT relaxed by Policy B."""
    monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
    SAFE_MODE.enable()
    broker = _FakeBroker(positions=[_pos(qty=10)])
    strat._broker = broker

    result = strat.sell("SPY", 10, price=100.0)

    assert result.status == OrderStatus.REJECTED
    assert broker.placed == []


# ── 9. Kill switch declares RISK_BREACH, so exits survive it ─────────────────

def test_kill_switch_sets_risk_breach_not_untrusted_state():
    """The MDD/loss kill switch must not freeze the book: it declares
    RISK_BREACH so proven exits keep working."""
    from backend.quant.risk.engine import LossTracker, RiskConfig

    tracker = LossTracker(RiskConfig())
    SAFE_MODE.enable()

    tracker._fire_kill_switch_alert("MDD 한도 초과 (-16%)")

    assert SAFE_MODE.can_trade is False
    assert SAFE_MODE.halt_cause is HaltCause.RISK_BREACH


# ── 10. EmergencyFlatten immunity (R6) ───────────────────────────────────────

def test_emergency_flatten_is_not_gated_by_any_halt(tmp_path):
    """EmergencyFlatten must remain the halt-immune last resort."""
    from backend.worker.emergency import EmergencyFlattenManager

    SAFE_MODE.disable("복구 미완료", cause=HaltCause.UNTRUSTED_STATE)
    broker = _FakeBroker(positions=[_pos(qty=10)], price=100.0)

    mgr = EmergencyFlattenManager(broker, db_factory=None, dry_run=False)
    res = mgr.flatten_all("halted-emergency")

    assert res["submitted"] == 1
    assert broker.placed == [("SPY", "sell", 10, 100.0, "limit")]
