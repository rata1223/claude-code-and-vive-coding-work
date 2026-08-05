"""
P0-07 S1 — Halt-vs-Exit policy on the QuickTrade path (Policy B).

QuickTrade has two order routes and they must not share one gate:

  * ``place-order``    — no position proof, so it is ENTRY and stays blocked
                         by any halt.
  * ``close-position`` — proves the exit *before* reserving (live held qty,
                         ``0 < qty <= held``, live price), so under a
                         risk-limit halt it is permitted to reduce exposure.

The halt signal here is the Redis flag ``risk:trading_halted``, whose only
writer is ``RiskManager.record_daily_loss`` — i.e. a RISK_BREACH. Untrusted
state is a worker-process concept (``SAFE_MODE``) and is deliberately not
consulted here; unifying the two halt stores is out of S1 scope.
"""
import pytest

from api.routers import quick_trade
from api.services.quick_trade_service import RiskDenied
from backend.risk.halt_policy import HaltCause, OperationClass, is_allowed


class _FakeRiskManager:
    """Stands in for the production RiskManager Redis client."""

    def __init__(self, halted=False, exc=None):
        self._halted = halted
        self._exc = exc
        self.calls = 0

    def is_trading_halted(self):
        self.calls += 1
        if self._exc:
            raise self._exc
        return self._halted


@pytest.fixture()
def patch_rm(monkeypatch):
    def _apply(halted=False, exc=None):
        rm = _FakeRiskManager(halted=halted, exc=exc)
        monkeypatch.setattr(quick_trade, "RiskManager", lambda: rm)
        return rm
    return _apply


# ── ENTRY gate (place-order) — unchanged strict behavior ─────────────────────

def test_entry_gate_allows_when_not_halted(patch_rm):
    rm = patch_rm(halted=False)
    quick_trade.get_risk_gate()()          # must not raise
    assert rm.calls == 1


def test_entry_gate_blocks_when_halted(patch_rm):
    patch_rm(halted=True)
    with pytest.raises(RiskDenied):
        quick_trade.get_risk_gate()()


def test_entry_gate_fails_closed_when_evaluation_raises(patch_rm):
    """R3: a gate that cannot evaluate must not let the order through. The
    exception propagates and reserve_and_submit records QT_BLOCKED."""
    patch_rm(exc=RuntimeError("redis unreachable"))
    with pytest.raises(Exception):
        quick_trade.get_risk_gate()()


# ── EXIT gate (close-position) — Policy B ────────────────────────────────────

def test_exit_gate_allows_when_not_halted(patch_rm):
    rm = patch_rm(halted=False)
    quick_trade.get_exit_risk_gate()()
    assert rm.calls == 1


def test_exit_gate_allows_a_proven_exit_during_a_risk_halt(patch_rm):
    """The whole point of S1: a daily-loss/MDD halt must not trap the user in
    a position they are explicitly trying to close."""
    rm = patch_rm(halted=True)
    quick_trade.get_exit_risk_gate()()     # must not raise
    assert rm.calls == 1                   # the halt was still evaluated


def test_exit_gate_fails_closed_when_evaluation_raises(patch_rm):
    """R3 applies to exits too — an unevaluatable gate blocks."""
    patch_rm(exc=RuntimeError("redis unreachable"))
    with pytest.raises(Exception):
        quick_trade.get_exit_risk_gate()()


def test_the_two_gates_are_distinct_objects(patch_rm):
    patch_rm(halted=True)
    entry, exit_ = quick_trade.get_risk_gate(), quick_trade.get_exit_risk_gate()
    with pytest.raises(RiskDenied):
        entry()
    exit_()                                # same halt, opposite outcome


# ── Route wiring: the right gate is attached to the right route ──────────────

def _gate_dependency_of(func):
    import inspect
    param = inspect.signature(func).parameters["risk_gate"]
    return param.default.dependency


def test_place_order_uses_the_entry_gate():
    assert _gate_dependency_of(quick_trade.place_order) is quick_trade.get_risk_gate


def test_close_position_uses_the_exit_gate():
    assert _gate_dependency_of(quick_trade.close_position) is quick_trade.get_exit_risk_gate


# ── The policy module backs both decisions ───────────────────────────────────

def test_policy_matrix_matches_the_gates():
    assert is_allowed(HaltCause.RISK_BREACH, OperationClass.ENTRY) is False
    assert is_allowed(HaltCause.RISK_BREACH, OperationClass.EXIT) is True
    assert is_allowed(None, OperationClass.ENTRY) is True
