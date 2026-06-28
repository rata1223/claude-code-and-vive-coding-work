"""
페이퍼 트레이딩 E2E 시나리오 — 코어(인메모리, DB 불필요). TASK P3-01A Phase C.

실행 계층 전체(machine + tracker + poller + kill-switch)를 ScriptedPaperBroker 에
연결한 PaperHarness 로 §2 검증 매트릭스의 다음 셀을 *연결된 파이프라인*에서 확인한다:
- Happy path (제출→폴링→체결→포지션)
- Duplicate-order (pending 게이트)
- Partial fill (증분 누적, 과체결 방지)
- Reject / Timeout (터미널 처리 + pending 해제)
- Kill-switch (HALTED 시 NEW 차단, CANCEL 허용, 수동 재개)
- Corporate-action 게이트 (fail-closed, tracker 경계)
- Performance (실현손익 재구성)

DB 의존 시나리오(reconciliation drift, recovery 복원, 영속 fills)는
tests/postgres/test_paper_e2e_db.py 에서 TEST_DATABASE_URL 있을 때만 실행된다.
"""
from datetime import datetime, timezone, timedelta

import pytest

from backend.brokers.models import OrderStatus
from backend.brokers.paper_broker import ScriptedPaperBroker, FillStep
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


# ── Happy path ───────────────────────────────────────────────────────────────
def test_happy_path_buy_fills_and_updates_position(harness):
    res = harness.submit_order("SPY", "buy", 10, 100.0)
    assert res.order is not None and res.blocked_by is None
    assert res.order.status == OrderStatus.SUBMITTED

    harness.pump()

    assert harness.position_qty("SPY") == 10
    assert harness.position_avg("SPY") == 100.0
    assert harness.order(res.order.id).status == OrderStatus.FILLED
    assert len(harness.fills) == 1
    assert harness.fills[0].qty == 10


# ── Duplicate-order ──────────────────────────────────────────────────────────
def test_duplicate_order_blocked_while_pending(harness):
    first = harness.submit_order("SPY", "buy", 10, 100.0, no_fill=True)
    assert first.order is not None

    dup = harness.submit_order("SPY", "buy", 10, 100.0)
    assert dup.order is None
    assert dup.blocked_by == "pending_or_ca"


def test_pending_released_after_fill_allows_new_order(harness):
    first = harness.submit_order("SPY", "buy", 10, 100.0)
    harness.pump()
    # pending 해제 → 동일 심볼 재주문 가능
    second = harness.submit_order("SPY", "buy", 5, 100.0)
    assert second.order is not None and second.blocked_by is None


# ── Partial fill ─────────────────────────────────────────────────────────────
def test_partial_then_full_fill_credits_exactly_once(harness):
    res = harness.submit_order(
        "SPY", "buy", 100, 100.0,
        fill_steps=[FillStep(40, OrderStatus.PARTIAL_FILLED),
                    FillStep(100, OrderStatus.FILLED)],
    )
    harness.pump()

    assert harness.position_qty("SPY") == 100
    assert harness.total_filled(res.order.id) == 100
    assert harness.order(res.order.id).status == OrderStatus.FILLED
    # 두 번의 증분 체결: 40, 60
    qtys = [f.qty for f in harness.fills]
    assert qtys == [40, 60]


def test_no_overfill_beyond_order_qty(harness):
    # 누적이 주문 수량을 넘는 스크립트라도 machine 이 과체결을 막아야 한다
    res = harness.submit_order(
        "SPY", "buy", 50, 100.0,
        fill_steps=[FillStep(50, OrderStatus.FILLED)],
    )
    harness.pump()
    assert harness.total_filled(res.order.id) == 50


# ── Reject / Timeout ─────────────────────────────────────────────────────────
def test_reject_releases_pending_and_no_position(harness):
    res = harness.submit_order("SPY", "buy", 10, 100.0, reject=True)
    assert res.order.status == OrderStatus.REJECTED
    assert harness.position_qty("SPY") == 0
    # pending 해제됨 → 재주문 가능
    again = harness.submit_order("SPY", "buy", 10, 100.0)
    assert again.order is not None


def test_timeout_cancels_and_releases_pending(harness):
    res = harness.submit_order("SPY", "buy", 10, 100.0, no_fill=True)
    harness.expire_pending(res.order.id)
    harness.pump()

    assert res.order.id in harness.timeouts
    # 폴러가 브로커에 취소를 시도 → 브로커 주문이 CANCELED
    assert harness.broker.get_order_status(res.order.id).status == OrderStatus.CANCELED
    assert harness.position_qty("SPY") == 0
    # pending 해제 확인
    again = harness.submit_order("SPY", "buy", 10, 100.0)
    assert again.order is not None


# ── Kill-switch ──────────────────────────────────────────────────────────────
def test_kill_switch_halts_new_orders(harness):
    harness.report_loss(daily_pnl_pct=-0.04)  # 3% 한도 초과 → HALTED
    assert harness.kill_switch.state == TradingState.HALTED

    res = harness.submit_order("SPY", "buy", 10, 100.0)
    assert res.order is None
    assert res.blocked_by == "kill_switch"


def test_kill_switch_allows_cancel_when_halted(harness):
    harness.report_loss(daily_pnl_pct=-0.04)
    assert harness.kill_switch.check_order(OrderIntent.CANCEL).allowed is True
    assert harness.kill_switch.check_order(OrderIntent.NEW).allowed is False


def test_kill_switch_manual_resume_after_cooldown(broker):
    h = PaperHarness(broker)
    t0 = datetime.now(timezone.utc)
    h.kill_switch.report_loss_breach(-0.04, 0.0, _now=t0)
    assert h.kill_switch.state == TradingState.HALTED

    # 쿨다운(기본 300s) 경과 후 재개 승인
    outcome = h.kill_switch.resume("operator", _now=t0 + timedelta(seconds=301))
    assert outcome.approved is True
    assert h.kill_switch.state == TradingState.RUNNING

    res = h.submit_order("SPY", "buy", 10, 100.0)
    assert res.order is not None


# ── Corporate-action 게이트 (fail-closed, tracker 경계) ──────────────────────
class _StubCA:
    """tracker 가 기대하는 is_blocked(symbol) 인터페이스만 흉내."""
    def __init__(self, blocked: set[str]):
        self._blocked = blocked

    def is_blocked(self, symbol: str) -> bool:
        return symbol in self._blocked


def test_corporate_action_blocks_order_entry(broker):
    h = PaperHarness(broker, corporate_action_runtime=_StubCA({"SPY"}))
    res = h.submit_order("SPY", "buy", 10, 100.0)
    assert res.order is None
    assert res.blocked_by == "pending_or_ca"
    # 차단되지 않은 심볼은 정상 통과
    ok = h.submit_order("069500", "buy", 1, 30_000.0)
    assert ok.order is not None


def test_corporate_action_gate_fails_closed_on_error(broker):
    class _Boom:
        def is_blocked(self, symbol):
            raise RuntimeError("gate check failed")

    h = PaperHarness(broker, corporate_action_runtime=_Boom())
    res = h.submit_order("SPY", "buy", 10, 100.0)
    # 게이트 확인 자체가 실패하면 fail-closed → 차단
    assert res.order is None


# ── Performance (실현손익 재구성) ────────────────────────────────────────────
def test_realized_pnl_on_round_trip(harness):
    harness.submit_order("069500", "buy", 10, 30_000.0)
    harness.pump()
    harness.broker.set_price("069500", 33_000.0)
    harness.submit_order("069500", "sell", 10, 33_000.0)
    harness.pump()

    # 실현손익 = (33000 - 30000) * 10 = 30,000
    assert harness.realized_pnl == pytest.approx(30_000.0)
    assert harness.position_qty("069500") == 0
