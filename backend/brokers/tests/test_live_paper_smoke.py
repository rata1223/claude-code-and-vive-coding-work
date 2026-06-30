"""
KIS 페이퍼(VTS) 라이브 스모크 — TASK P3-01A Phase D (opt-in).

목적: 결정론적 시뮬레이터가 우회하는 실제 경로를 검증한다.
- KIS 인증(토큰 발급) + Hashkey
- 잔고/현재가 TR 응답 파싱 (semantic_mapper)
- is_live=True 브로커의 실제 동작

기본적으로 SKIP 된다. 실행하려면:
    RUN_LIVE_PAPER=1 \
    KIS_ENV=paper KIS_APP_KEY=... KIS_APP_SECRET=... KIS_ACCOUNT_NO=... \
    pytest backend/brokers/tests/test_live_paper_smoke.py -m live_paper -v

주문 제출/취소까지 검증하려면 추가로 RUN_LIVE_PAPER_ORDERS=1 을 설정한다
(체결 가능성이 낮은 지정가를 넣고 즉시 취소 — 계좌 부작용 최소화).
"""
import os

import pytest

pytestmark = [
    pytest.mark.live_paper,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_PAPER") != "1",
        reason="RUN_LIVE_PAPER!=1 — 라이브 페이퍼 스모크는 명시적 opt-in",
    ),
]

_REQUIRED_ENV = ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO")


@pytest.fixture(scope="module")
def kis_paper_broker():
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        pytest.skip(f"KIS 페이퍼 자격증명 누락: {missing}")
    if os.environ.get("KIS_ENV", "paper") != "paper":
        pytest.skip("KIS_ENV != paper — 라이브 스모크는 모의계좌에서만 실행")

    from backend.brokers.kis import get_kis_broker
    broker = get_kis_broker()
    assert broker.is_live is True  # KISBroker 는 항상 live 경로(게이트 대상)
    return broker


def test_auth_and_balance_tr_parses(kis_paper_broker):
    """토큰 발급 + 잔고 TR 파싱이 정상 동작하는지."""
    balance = kis_paper_broker.get_balance()
    assert balance is not None
    # 모의계좌라도 평가금액은 숫자로 파싱되어야 한다
    assert isinstance(balance.total_eval_krw, (int, float))


def test_overseas_price_tr_parses(kis_paper_broker):
    """해외 현재가 TR 파싱(semantic_mapper) 검증."""
    price = kis_paper_broker.get_price("SPY")
    assert isinstance(price, (int, float))
    assert price > 0


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_PAPER_ORDERS") != "1",
    reason="RUN_LIVE_PAPER_ORDERS!=1 — 주문 제출/취소 스모크는 별도 opt-in",
)
def test_place_far_limit_then_cancel(kis_paper_broker):
    """체결 가능성 낮은 지정가 매수 후 즉시 취소 — 주문/취소 TR 왕복 검증."""
    price = kis_paper_broker.get_price("SPY")
    far_limit = round(price * 0.5, 2)  # 시세의 절반 → 체결 안 됨
    order = kis_paper_broker.place_order("SPY", "buy", 1, far_limit, "limit")
    assert order.id, "broker_order_id 가 비어 있으면 안 됨"
    try:
        status = kis_paper_broker.get_order_status(order.id, "SPY")
        assert status is not None
    finally:
        kis_paper_broker.cancel_order(order.id, symbol="SPY", qty=1, price=far_limit)
