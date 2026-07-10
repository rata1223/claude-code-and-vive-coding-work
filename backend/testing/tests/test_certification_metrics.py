"""
P3-03B Phase 1 — certification metric wiring + scenario-gap closure (TDD).

Closes the P3-03A gaps: the 9 required certification metrics must all be
collected by the REAL runtime harness (PaperHarness driving the real machine /
tracker / poller / kill-switch / EmergencyFlattenManager against the
ScriptedPaperBroker) and exported by ValidationMetrics.as_dict(), plus the
weak/missing scenarios (≥3-step multi-partial, timeout recovery, kill-switch
activation, emergency-flatten execution, duplicate-event suppression).

Real runtime only — no mock trading shortcuts. Existing metric keys are
preserved for backward compatibility.
"""
import pytest

from backend.brokers.models import OrderStatus
from backend.brokers.paper_broker import ScriptedPaperBroker, FillStep
from backend.risk.kill_switch import TradingState
from backend.testing.paper_harness import PaperHarness

# The 9 metrics P3-03B must collect (task-named).
REQUIRED_METRICS = [
    "successful_orders",
    "rejected_orders",
    "timeout_recovery",
    "reconciliation_repairs",
    "stale_data_blocks",
    "duplicate_event_suppression",
    "corporate_action_events",
    "kill_switch_activations",
    "emergency_flatten_executions",
]


@pytest.fixture()
def broker():
    b = ScriptedPaperBroker(initial_cash_krw=2_000_000.0, default_price=100.0)
    b.set_price("SPY", 100.0)
    return b


@pytest.fixture()
def harness(broker):
    return PaperHarness(broker)


# ── metric export contract ───────────────────────────────────────────────────
def test_as_dict_exposes_all_nine_required_metrics(harness):
    d = harness.metrics.as_dict()
    for name in REQUIRED_METRICS:
        assert name in d, f"required metric '{name}' missing from as_dict()"


def test_as_dict_preserves_existing_keys(harness):
    # Backward compatibility: existing consumers must keep working.
    d = harness.metrics.as_dict()
    for legacy in ("reconciliation_mismatches", "duplicate_orders",
                   "kill_switch_blocks", "recovery_success_rate"):
        assert legacy in d


# ── timeout recovery ─────────────────────────────────────────────────────────
def test_timeout_recovery_metric(harness):
    res = harness.submit_order("SPY", "buy", 10, 100.0, no_fill=True)
    harness.expire_pending(res.order.id)
    harness.pump()
    assert harness.metrics.timeout_recovery == 1
    assert harness.metrics.as_dict()["timeout_recovery"] == 1
    # pending lock released after the timeout recovery
    assert harness.tracker.can_place_order("SPY") is True


# ── kill-switch activation (RUNNING -> HALTED), distinct from blocks ─────────
def test_kill_switch_activation_metric(harness):
    assert harness.kill_switch.state == TradingState.RUNNING
    harness.report_loss(daily_pnl_pct=-0.05, mdd_pct=-0.20)  # CRITICAL breach
    assert harness.kill_switch.state == TradingState.HALTED
    assert harness.metrics.kill_switch_activations == 1
    # a second breach while already HALTED is not a new activation
    harness.report_loss(daily_pnl_pct=-0.06, mdd_pct=-0.25)
    assert harness.metrics.kill_switch_activations == 1


# ── emergency flatten executions (real manager; dry-run excluded) ────────────
def test_emergency_flatten_execution_metric(harness):
    harness.submit_order("SPY", "buy", 10, 100.0)
    harness.pump()
    assert harness.position_qty("SPY") == 10
    harness.emergency_flatten(dry_run=False, settle=True)
    assert harness.metrics.emergency_flatten_executions == 1
    # closure verified on the broker's ground-truth book (flatten bypasses the
    # tracker by design — documented R5 residual risk, out of scope here)
    spy = [p for p in harness.broker.get_positions() if p.symbol == "SPY"]
    assert (spy[0].qty if spy else 0) == 0
    # dry-run must NOT count as an execution
    harness.submit_order("SPY", "buy", 5, 100.0)
    harness.pump()
    harness.emergency_flatten(dry_run=True)
    assert harness.metrics.emergency_flatten_executions == 1


# ── reconciliation repairs (repairs, not gaps) ───────────────────────────────
def test_reconciliation_repairs_metric(harness):
    from backend.execution.reconciler import ReconciliationResult
    r = ReconciliationResult("test")
    r.gap("order_status_mismatch", "SPY", "d")
    r.repaired("sync_order", "SPY", "d")
    r.repaired("fix_qty", "SPY", "d")
    harness.record_reconciliation(r)
    assert harness.metrics.reconciliation_repairs == 2
    assert harness.metrics.reconciliation_mismatches == 1  # gaps still tracked


# ── duplicate broker/event suppression ───────────────────────────────────────
def test_duplicate_event_suppression_metric(harness):
    from backend.brokers.models import Order
    res = harness.submit_order("SPY", "buy", 10, 100.0, no_fill=True)
    oid = res.order.id
    canceled = Order(id=oid, symbol="SPY", side="buy", qty=10, price=100.0,
                     status=OrderStatus.CANCELED)
    harness._on_canceled(canceled)                       # first: SUBMITTED -> CANCELED
    assert harness.order(oid).status == OrderStatus.CANCELED
    before = harness.metrics.duplicate_event_suppression
    # Duplicate terminal event for an already-terminal order is suppressed + counted.
    harness._on_canceled(canceled)
    assert harness.metrics.duplicate_event_suppression == before + 1


# ── scenario 4: ≥3-step multiple partial fills through the real chain ─────────
def test_multiple_partial_fills_three_steps(harness):
    res = harness.submit_order(
        "SPY", "buy", 100, 100.0,
        fill_steps=[FillStep(30, OrderStatus.PARTIAL_FILLED),
                    FillStep(70, OrderStatus.PARTIAL_FILLED),
                    FillStep(100, OrderStatus.FILLED)],
    )
    harness.pump()
    assert harness.position_qty("SPY") == 100
    assert harness.total_filled(res.order.id) == 100
    assert harness.metrics.successful_orders == 1
    # exactly three increments, cumulative 30 -> 70 -> 100 = 30, 40, 30
    incs = [f.qty for f in harness.fills if f.order_id == res.order.id]
    assert incs == [30, 40, 30]
