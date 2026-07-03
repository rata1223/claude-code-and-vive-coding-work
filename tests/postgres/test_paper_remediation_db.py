"""
P3-02B 런타임 교정 검증 — DB 의존(Postgres tier).

TEST_DATABASE_URL 부재 시 자동 스킵(CI postgres 잡에서만 실행). 실제 컴포넌트만
사용: PositionReconciler · ScriptedPaperBroker · PositionTracker · OrderStateMachine.

시나리오: reconciliation after cancel · worker restart after cancel.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from backend.brokers.models import OrderStatus, Position
from backend.brokers.paper_broker import ScriptedPaperBroker
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import PositionTracker
from backend.execution.reconciler import PositionReconciler

BROKER = "kis"
_OPEN = ("pending", "submitted", "partial_filled")


@pytest.fixture()
def db_factory(pg_trading_engine):
    from backend.database.models import Order as DBOrder, Fill, AuditLog, ReconciliationLog
    Factory = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    s = Factory()
    try:
        for model in (Fill, DBOrder, ReconciliationLog, AuditLog):
            s.query(model).delete()
        s.commit()
    finally:
        s.close()
    return Factory


def _seed_order(db_factory, broker_order_id, symbol="SPY", status="submitted", market="US"):
    from backend.database.models import Order as DBOrder
    s = db_factory()
    try:
        row = DBOrder(broker_order_id=broker_order_id, symbol=symbol, side="buy",
                      qty=10, price=100.0, status=status, market=market, broker=BROKER,
                      created_at=datetime.utcnow() - timedelta(minutes=5))
        s.add(row)
        s.commit()
        return row.id
    finally:
        s.close()


def _order_status(db_factory, db_id):
    from backend.database.models import Order as DBOrder
    s = db_factory()
    try:
        return s.get(DBOrder, db_id).status
    finally:
        s.close()


# ── reconciliation after cancel — 브로커 취소를 DB 에 동기화 ─────────────────
def test_reconciliation_after_cancel_syncs_db(db_factory):
    # 브로커에 주문 접수 후 취소 → get_order_status 가 CANCELED 반환
    broker = ScriptedPaperBroker(default_price=100.0)
    broker.set_price("SPY", 100.0)
    placed = broker.place_order("SPY", "buy", 10, 100.0)
    assert broker.cancel_order(placed.id) is True

    # DB 는 아직 'submitted' (터미널 이벤트를 놓친 상태를 모사)
    db_id = _seed_order(db_factory, broker_order_id=placed.id, status="submitted")

    rec = PositionReconciler(broker, db_factory, broker_name=BROKER)
    result = rec.reconcile("periodic")

    assert any(g["kind"] == "order_status_mismatch" and g["symbol"] == "SPY"
               for g in result.gaps)
    assert _order_status(db_factory, db_id) == OrderStatus.CANCELED.value


# ── worker restart after cancel — 취소된 주문은 pending 으로 부활하지 않음 ───
def test_worker_restart_after_cancel_no_stuck_pending(db_factory):
    from backend.database.models import Order as DBOrder
    # 취소 동기화가 끝난 상태(위 시나리오 이후)를 모사: DB 에 canceled 주문 + 포지션 없음
    _seed_order(db_factory, broker_order_id="SIM-CANCELLED", status="canceled")

    # DB 필터 계약(contract) 검증: runner._restore_pending_to_tracker 의 복원 필터
    # (status.in_(_OPEN) + broker_order_id NOT NULL)를 그대로 미러링해, 취소 주문이
    # 복원 대상에서 제외됨을 Postgres 상에서 고정한다. 실제 메서드 배선(콜백/머신
    # 등록)은 backend/worker/tests/test_recovery_safety.py 의 인메모리 테스트가 커버.
    s = db_factory()
    try:
        restorable = s.query(DBOrder).filter(
            DBOrder.broker == BROKER,
            DBOrder.status.in_(_OPEN),
            DBOrder.broker_order_id.isnot(None),
        ).all()
    finally:
        s.close()
    assert restorable == []                      # 취소 주문은 복원 대상에서 제외

    # 포지션도 없으므로 재시작 후 심볼은 재매매 가능(스테일 pending 없음)
    tracker = PositionTracker(OrderStateMachine())
    tracker.restore_positions([])
    assert tracker.can_place_order("SPY") is True
