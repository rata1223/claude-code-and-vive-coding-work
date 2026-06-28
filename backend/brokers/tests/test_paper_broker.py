"""
ScriptedPaperBroker 단위 테스트 — TASK P3-01A Phase A.

검증 범위:
- BrokerAdapter 계약 충족(capabilities, balance, positions, price)
- 기본 동작: place_order → SUBMITTED, 첫 get_order_status 에 전량 FILLED
- 스크립트형 부분체결 → 완전체결(증분 노출)
- 즉시 거부(script_reject)
- 미체결(script_no_fill) → 폴러 타임아웃용
- 취소
- 브로커 장부(ground truth) 가 체결을 반영 (reconciler 소스)
- 수량 점프 / 액면분할 주입(기업행위 시나리오용)
- 강제 장애 플래그(브로커 실패 시뮬레이션)
- is_live 토글
"""
import pytest

from backend.brokers.capabilities import SIMULATOR_CAPABILITIES
from backend.brokers.models import OrderStatus
from backend.brokers.paper_broker import ScriptedPaperBroker, FillStep


@pytest.fixture()
def sim():
    b = ScriptedPaperBroker(initial_cash_krw=2_000_000.0, default_price=100.0)
    b.set_price("SPY", 100.0)
    b.set_price("069500", 30_000.0)
    return b


# ── 계약 ────────────────────────────────────────────────────────────────────
def test_capabilities_reuses_simulator_preset(sim):
    assert sim.capabilities is SIMULATOR_CAPABILITIES
    assert sim.capabilities.broker_id == "simulator"


def test_is_live_default_false_and_overridable():
    assert ScriptedPaperBroker().is_live is False
    assert ScriptedPaperBroker(is_live=True).is_live is True


def test_get_price_default_and_set(sim):
    assert sim.get_price("SPY") == 100.0
    assert sim.get_price("UNKNOWN") == 100.0  # default_price
    sim.set_price("AAPL", 222.5)
    assert sim.get_price("AAPL") == 222.5


# ── 기본 체결 ────────────────────────────────────────────────────────────────
def test_default_full_fill_on_first_poll(sim):
    order = sim.place_order("SPY", "buy", 10, 100.0)
    assert order.status == OrderStatus.SUBMITTED
    assert order.filled_qty == 0

    st = sim.get_order_status(order.id, "SPY")
    assert st.status == OrderStatus.FILLED
    assert st.filled_qty == 10
    assert st.avg_fill_price == 100.0


def test_place_order_returns_copy_not_internal(sim):
    order = sim.place_order("SPY", "buy", 5, 100.0)
    order.status = OrderStatus.FILLED  # mutate the returned copy
    st = sim.get_order_status(order.id, "SPY")
    # internal state should not be FILLED-by-mutation; it reveals its own plan
    assert st.filled_qty == 5


# ── 스크립트형 부분체결 ──────────────────────────────────────────────────────
def test_scripted_partial_then_full_reveals_incrementally(sim):
    sim.script_fills("SPY", "buy", [
        FillStep(40, OrderStatus.PARTIAL_FILLED),
        FillStep(100, OrderStatus.FILLED, avg_price=101.0),
    ])
    order = sim.place_order("SPY", "buy", 100, 100.0)

    s1 = sim.get_order_status(order.id)
    assert s1.status == OrderStatus.PARTIAL_FILLED
    assert s1.filled_qty == 40

    s2 = sim.get_order_status(order.id)
    assert s2.status == OrderStatus.FILLED
    assert s2.filled_qty == 100
    assert s2.avg_fill_price == 101.0

    # 마지막 step 에 고정 — 이후 호출도 FILLED 유지
    s3 = sim.get_order_status(order.id)
    assert s3.status == OrderStatus.FILLED
    assert s3.filled_qty == 100


# ── 거부 / 미체결 / 취소 ─────────────────────────────────────────────────────
def test_scripted_reject_returns_rejected_immediately(sim):
    sim.script_reject("SPY", "buy")
    order = sim.place_order("SPY", "buy", 10, 100.0)
    assert order.status == OrderStatus.REJECTED
    st = sim.get_order_status(order.id)
    assert st.status == OrderStatus.REJECTED


def test_script_no_fill_stays_submitted(sim):
    sim.script_no_fill("SPY", "buy")
    order = sim.place_order("SPY", "buy", 10, 100.0)
    for _ in range(5):
        st = sim.get_order_status(order.id)
        assert st.status == OrderStatus.SUBMITTED
        assert st.filled_qty == 0


def test_cancel_open_order(sim):
    sim.script_no_fill("SPY", "buy")
    order = sim.place_order("SPY", "buy", 10, 100.0)
    assert sim.cancel_order(order.id) is True
    st = sim.get_order_status(order.id)
    assert st.status == OrderStatus.CANCELED
    # 취소된 주문 재취소는 False
    assert sim.cancel_order(order.id) is False


def test_cancel_unknown_order_returns_false(sim):
    assert sim.cancel_order("does-not-exist") is False


def test_get_status_unknown_order_returns_none(sim):
    assert sim.get_order_status("nope") is None


# ── 브로커 장부(ground truth) ────────────────────────────────────────────────
def test_book_reflects_buy_fill(sim):
    # KR 심볼(6자리) → cash_krw 에서 차감
    order = sim.place_order("069500", "buy", 10, 30_000.0)
    sim.get_order_status(order.id)  # reveal full fill
    positions = {p.symbol: p for p in sim.get_positions()}
    assert "069500" in positions
    assert positions["069500"].qty == 10
    assert positions["069500"].avg_price == 30_000.0
    # 현금 차감
    assert sim.get_balance().cash_krw == pytest.approx(2_000_000.0 - 10 * 30_000.0)


def test_us_buy_debits_usd_cash(sim):
    # US 심볼 → cash_usd 에서 차감(KIS 외화 계좌 모델)
    order = sim.place_order("SPY", "buy", 10, 100.0)
    sim.get_order_status(order.id)
    assert sim.get_balance().cash_usd == pytest.approx(-1_000.0)


def test_partial_fills_average_into_book(sim):
    sim.script_fills("SPY", "buy", [
        FillStep(40, OrderStatus.PARTIAL_FILLED, avg_price=100.0),
        FillStep(100, OrderStatus.FILLED, avg_price=110.0),
    ])
    order = sim.place_order("SPY", "buy", 100, 100.0)
    sim.get_order_status(order.id)  # +40 @100
    sim.get_order_status(order.id)  # +60 @110
    pos = {p.symbol: p for p in sim.get_positions()}["SPY"]
    assert pos.qty == 100
    # weighted avg = (40*100 + 60*110)/100 = 106.0
    assert pos.avg_price == pytest.approx(106.0)


def test_sell_reduces_and_clears_book(sim):
    # seed a position then sell it all
    sim.set_position("SPY", 10, 100.0, market="US")
    sim.set_price("SPY", 120.0)
    order = sim.place_order("SPY", "sell", 10, 120.0)
    sim.get_order_status(order.id)
    assert {p.symbol for p in sim.get_positions()} == set()


# ── 기업행위 주입 ────────────────────────────────────────────────────────────
def test_set_position_for_external_state(sim):
    sim.set_position("069500", 5, 30_000.0)
    pos = {p.symbol: p for p in sim.get_positions()}["069500"]
    assert pos.qty == 5
    assert pos.market == "KR"  # 6자리 숫자 → KR


def test_apply_split_doubles_qty_halves_avg(sim):
    sim.set_position("SPY", 10, 100.0, market="US")
    sim.apply_split("SPY", 2.0)
    pos = {p.symbol: p for p in sim.get_positions()}["SPY"]
    assert pos.qty == 20
    assert pos.avg_price == pytest.approx(50.0)


# ── 장애 시뮬레이션 ──────────────────────────────────────────────────────────
def test_fail_next_status_raises_once(sim):
    order = sim.place_order("SPY", "buy", 10, 100.0)
    sim.fail_next_status = True
    with pytest.raises(RuntimeError):
        sim.get_order_status(order.id)
    # 플래그는 1회성 — 다음 호출은 정상
    st = sim.get_order_status(order.id)
    assert st.status == OrderStatus.FILLED


def test_fail_next_order_raises_once(sim):
    sim.fail_next_order = True
    with pytest.raises(RuntimeError):
        sim.place_order("SPY", "buy", 10, 100.0)
    order = sim.place_order("SPY", "buy", 10, 100.0)
    assert order.status == OrderStatus.SUBMITTED
