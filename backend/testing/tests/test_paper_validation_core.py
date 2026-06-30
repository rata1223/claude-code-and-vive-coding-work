"""
P3-01B 페이퍼 트레이딩 검증 — 코어 시나리오(인메모리, DB 불필요).

실제 실행 계층(machine + tracker + poller + kill-switch + FreshnessGate +
EmergencyFlattenManager)을 ScriptedPaperBroker 에 연결한 PaperHarness 로 구동한다.
DB/Redis/복구 의존 시나리오(6–11, 18–20)는 tests/postgres/test_paper_validation_db.py.

다루는 필수 시나리오:
  1 Normal Buy · 2 Normal Sell · 3 Partial Fill · 4 Order Cancel · 5 Order Reject
  12 Unknown Corporate Action · 13 Duplicate Signal · 14 Duplicate Order
  15 Stale Market Data · 16 Kill Switch · 17 Emergency Flatten
"""
from datetime import datetime, timezone, timedelta

import pytest

from backend.brokers.models import OrderStatus
from backend.brokers.paper_broker import ScriptedPaperBroker, FillStep
from backend.data.freshness_gate import FreshnessGate
from backend.data.freshness_config import FreshnessTier
from backend.risk.kill_switch import OrderIntent, TradingState
from backend.testing.paper_harness import PaperHarness


@pytest.fixture()
def broker():
    b = ScriptedPaperBroker(initial_cash_krw=2_000_000.0, default_price=100.0)
    b.set_price("SPY", 100.0)
    b.set_price("069500", 30_000.0)
    return b


@pytest.fixture()
def harness(broker):
    return PaperHarness(broker)


class _StubCA:
    """tracker/harness 가 기대하는 is_blocked(symbol) 인터페이스만 흉내."""
    def __init__(self, blocked):
        self._blocked = set(blocked)

    def is_blocked(self, symbol):
        return symbol in self._blocked


# ── 1. Normal Buy ────────────────────────────────────────────────────────────
def test_s01_normal_buy(harness):
    res = harness.submit_order("SPY", "buy", 10, 100.0)
    harness.pump()
    assert harness.position_qty("SPY") == 10
    assert harness.order(res.order.id).status == OrderStatus.FILLED
    assert harness.metrics.successful_orders == 1


# ── 2. Normal Sell ───────────────────────────────────────────────────────────
def test_s02_normal_sell(harness):
    harness.submit_order("069500", "buy", 10, 30_000.0)
    harness.pump()
    harness.broker.set_price("069500", 33_000.0)
    harness.submit_order("069500", "sell", 10, 33_000.0)
    harness.pump()
    assert harness.position_qty("069500") == 0
    assert harness.realized_pnl == pytest.approx(30_000.0)
    assert harness.metrics.successful_orders == 2


# ── 3. Partial Fill ──────────────────────────────────────────────────────────
def test_s03_partial_fill(harness):
    res = harness.submit_order("SPY", "buy", 100, 100.0,
                               fill_steps=[FillStep(40, OrderStatus.PARTIAL_FILLED),
                                           FillStep(100, OrderStatus.FILLED)])
    harness.pump()
    assert harness.total_filled(res.order.id) == 100
    assert [f.qty for f in harness.fills] == [40, 60]
    assert harness.metrics.successful_orders == 1  # 완전 체결 1회만 집계


# ── 4. Order Cancel ──────────────────────────────────────────────────────────
def test_s04_order_cancel(harness):
    res = harness.submit_order("SPY", "buy", 10, 100.0, no_fill=True)
    assert harness.broker.cancel_order(res.order.id) is True
    harness.pump()
    assert res.order.id in harness.cancels
    assert harness.order(res.order.id).status == OrderStatus.CANCELED
    assert harness.position_qty("SPY") == 0
    # pending 해제 → 재주문 가능
    assert harness.submit_order("SPY", "buy", 10, 100.0).order is not None


# ── 5. Order Reject ──────────────────────────────────────────────────────────
def test_s05_order_reject(harness):
    res = harness.submit_order("SPY", "buy", 10, 100.0, reject=True)
    assert res.order.status == OrderStatus.REJECTED
    assert harness.position_qty("SPY") == 0
    assert harness.metrics.rejected_orders == 1


# ── 12. Unknown Corporate Action ─────────────────────────────────────────────
def test_s12_unknown_corporate_action_blocks(broker):
    h = PaperHarness(broker, corporate_action_runtime=_StubCA({"SPY"}))
    res = h.submit_order("SPY", "buy", 10, 100.0)
    assert res.order is None
    assert res.blocked_by == "corporate_action"
    assert h.metrics.corporate_action_events == 1
    # 게이트되지 않은 심볼은 정상
    assert h.submit_order("069500", "buy", 1, 30_000.0).order is not None


# ── 13. Duplicate Signal ─────────────────────────────────────────────────────
def test_s13_duplicate_signal(harness):
    first = harness.submit_signal("SPY", "buy", 10, 100.0, signal_key="2026-06-30T09:05")
    dup = harness.submit_signal("SPY", "buy", 10, 100.0, signal_key="2026-06-30T09:05")
    assert first.order is not None
    assert dup.order is None and dup.blocked_by == "duplicate_signal"
    assert harness.metrics.duplicate_signals == 1


# ── 14. Duplicate Order ──────────────────────────────────────────────────────
def test_s14_duplicate_order(harness):
    first = harness.submit_order("SPY", "buy", 10, 100.0, no_fill=True)
    dup = harness.submit_order("SPY", "buy", 10, 100.0)
    assert first.order is not None
    assert dup.order is None and dup.blocked_by == "pending_or_ca"
    assert harness.metrics.duplicate_orders == 1


# ── 15. Stale Market Data (실제 FreshnessGate, fail-closed) ──────────────────
def test_s15_stale_market_data_blocks(broker):
    h = PaperHarness(broker, freshness_gate=FreshnessGate())
    old = datetime.now(timezone.utc) - timedelta(days=2)
    fresh = datetime.now(timezone.utc)

    stale = h.submit_order("SPY", "buy", 10, 100.0, bar_ts=old, tier=FreshnessTier.INTRADAY_BAR)
    assert stale.order is None and stale.blocked_by == "stale_data"

    unknown = h.submit_order("SPY", "buy", 10, 100.0, bar_ts=None, tier=FreshnessTier.INTRADAY_BAR)
    assert unknown.order is None and unknown.blocked_by == "stale_data"  # fail-closed

    ok = h.submit_order("SPY", "buy", 10, 100.0, bar_ts=fresh, tier=FreshnessTier.INTRADAY_BAR)
    assert ok.order is not None
    assert h.metrics.stale_data_blocks == 2


# ── 16. Kill Switch ──────────────────────────────────────────────────────────
def test_s16_kill_switch_halts_new_orders(harness):
    harness.report_loss(daily_pnl_pct=-0.04)  # 일손실 3% 한도 초과 → HALTED
    assert harness.kill_switch.state == TradingState.HALTED
    res = harness.submit_order("SPY", "buy", 10, 100.0)
    assert res.order is None and res.blocked_by == "kill_switch"
    assert harness.metrics.kill_switch_blocks == 1
    # HALTED 에서도 취소는 허용
    assert harness.kill_switch.check_order(OrderIntent.CANCEL).allowed is True


# ── 17. Emergency Flatten ────────────────────────────────────────────────────
def test_s17_emergency_flatten(harness):
    harness.submit_order("069500", "buy", 10, 30_000.0)
    harness.submit_order("SPY", "buy", 5, 100.0)
    harness.pump()
    assert len(harness.broker.get_positions()) == 2

    # dry-run: 주문 미제출, 포지션 유지
    dry = harness.emergency_flatten(dry_run=True)
    assert dry["attempted"] == 2 and dry["submitted"] == 0
    assert len(harness.broker.get_positions()) == 2

    # 실제 청산: 매도 제출 후 결제 → 브로커 장부 비워짐
    live = harness.emergency_flatten(dry_run=False)
    assert live["submitted"] == 2
    assert harness.broker.get_positions() == []


# ── 지표 집계 ────────────────────────────────────────────────────────────────
def test_metrics_dict_shape(harness):
    harness.submit_order("SPY", "buy", 1, 100.0)
    harness.pump()
    d = harness.metrics.as_dict()
    assert d["successful_orders"] == 1
    assert set(d) >= {
        "successful_orders", "rejected_orders", "reconciliation_mismatches",
        "duplicate_orders", "duplicate_signals", "stale_data_blocks",
        "corporate_action_events", "kill_switch_blocks", "recovery_success_rate",
        "avg_restart_recovery_seconds",
    }
