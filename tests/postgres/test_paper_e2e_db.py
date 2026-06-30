"""
페이퍼 트레이딩 E2E — DB 의존 시나리오. TASK P3-01A Phase C (Postgres tier).

이 파일의 모든 테스트는 tests/postgres/conftest.py 의 pytest_collection_modifyitems
훅에 의해 TEST_DATABASE_URL(postgresql://…) 이 없으면 자동 스킵된다. CI 의
ci-postgres.yml 잡에서만 실제로 실행된다.

검증 매트릭스 중 DB 가 있어야만 의미 있는 셀:
- Reconciler: 외부 매수(broker has, DB missing) → insert
- Reconciler: 외부 매도/청산(DB has, broker missing, aged) → delete
- Reconciler: dry_run 은 갭만 탐지하고 DB 를 수정하지 않음
- Reconciler × CorporateAction: 수량 점프 분류·게이트(fail-closed)
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from backend.brokers.paper_broker import ScriptedPaperBroker
from backend.execution.reconciler import PositionReconciler

BROKER_NAME = "kis"


@pytest.fixture()
def db_factory(pg_trading_engine):
    """reconciler 가 기대하는 '세션을 반환하는 콜러블'. 각 테스트 전 관련 테이블 정리."""
    from backend.database.models import (
        Position as DBPosition, Order as DBOrder, Fill, AuditLog,
        ReconciliationLog, CorporateAction, CorporateActionHistory,
    )
    Factory = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    s = Factory()
    try:
        for model in (CorporateActionHistory, CorporateAction, Fill, DBOrder,
                      DBPosition, ReconciliationLog, AuditLog):
            s.query(model).delete()
        s.commit()
    finally:
        s.close()
    return Factory


@pytest.fixture()
def broker():
    b = ScriptedPaperBroker(default_price=100.0)
    b.set_price("SPY", 100.0)
    return b


def _db_positions(db_factory):
    from backend.database.models import Position as DBPosition
    s = db_factory()
    try:
        return {p.symbol: p for p in s.query(DBPosition).all()}
    finally:
        s.close()


# ── 외부 매수: 브로커에 있는데 DB 에 없음 → insert ──────────────────────────
def test_reconcile_inserts_external_broker_position(broker, db_factory):
    broker.set_position("SPY", 7, 101.5, market="US")
    rec = PositionReconciler(broker, db_factory, broker_name=BROKER_NAME)

    result = rec.reconcile("startup")

    assert any(r["kind"] == "insert_position" and r["symbol"] == "SPY"
               for r in result.repairs)
    positions = _db_positions(db_factory)
    assert "SPY" in positions
    assert positions["SPY"].qty == 7
    assert positions["SPY"].avg_price == pytest.approx(101.5)


# ── 외부 매도/청산: DB 에 있는데 브로커에 없음(1h+ 경과) → delete ────────────
def test_reconcile_deletes_stale_db_position(broker, db_factory):
    from backend.database.models import Position as DBPosition
    s = db_factory()
    try:
        s.add(DBPosition(symbol="OLD", qty=3, avg_price=50.0, market="US",
                         broker=BROKER_NAME,
                         updated_at=datetime.utcnow() - timedelta(hours=2)))
        s.commit()
    finally:
        s.close()

    rec = PositionReconciler(broker, db_factory, broker_name=BROKER_NAME)
    result = rec.reconcile("periodic")

    assert any(r["kind"] == "delete_position" and r["symbol"] == "OLD"
               for r in result.repairs)
    assert "OLD" not in _db_positions(db_factory)


# ── dry_run: 갭만 탐지, DB 변경 없음 ─────────────────────────────────────────
def test_reconcile_dry_run_detects_without_mutation(broker, db_factory):
    broker.set_position("SPY", 5, 100.0, market="US")
    rec = PositionReconciler(broker, db_factory, broker_name=BROKER_NAME)

    result = rec.reconcile("manual", dry_run=True)

    assert any(g["kind"] == "missing_in_db" and g["symbol"] == "SPY"
               for g in result.gaps)
    # dry_run 이므로 DB 는 그대로 비어 있어야 한다
    assert "SPY" not in _db_positions(db_factory)


# ── 수량 점프 × 기업행위: 분류 + 동기화 ──────────────────────────────────────
def test_reconcile_classifies_quantity_jump_as_corporate_action(broker, db_factory):
    from backend.database.models import Position as DBPosition
    from backend.data.corporate_action_runtime import CorporateActionRuntime

    # DB: 10주 @100 / 브로커: 20주 @50 (2:1 분할 형태의 수량 점프)
    s = db_factory()
    try:
        s.add(DBPosition(symbol="SPY", qty=10, avg_price=100.0, market="US",
                         broker=BROKER_NAME,
                         updated_at=datetime.utcnow() - timedelta(hours=2)))
        s.commit()
    finally:
        s.close()
    broker.set_position("SPY", 20, 50.0, market="US")

    ca = CorporateActionRuntime(db_factory=db_factory, broker=BROKER_NAME)
    rec = PositionReconciler(broker, db_factory, broker_name=BROKER_NAME, ca_runtime=ca)

    result = rec.reconcile("periodic")

    # 수량 점프는 단순 불일치가 아니라 기업행위 후보로 분류되어야 한다
    assert any(g["kind"].startswith("qty_corporate_action") and g["symbol"] == "SPY"
               for g in result.gaps)
    # reconciler 가 유일한 포지션 writer — 브로커 값으로 동기화
    positions = _db_positions(db_factory)
    assert positions["SPY"].qty == 20
    assert positions["SPY"].avg_price == pytest.approx(50.0)
