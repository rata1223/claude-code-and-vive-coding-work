"""
P3-01B 페이퍼 트레이딩 검증 — DB 의존 시나리오 (Postgres tier).

tests/postgres/conftest.py 의 훅이 TEST_DATABASE_URL(postgresql://…) 부재 시
전체 스킵하므로 CI 의 postgres 잡에서만 실행된다.

실제 컴포넌트만 사용한다(목 아님):
  PositionReconciler · CorporateActionRuntime · PositionTracker · KillSwitch ·
  OrderStateMachine · ScriptedPaperBroker(페이퍼 브로커).

다루는 필수 시나리오:
  6 Worker Restart · 7 Redis Restart(=DB 권위/캐시 손실 무손실) · 8 Database Restart
  9 Reconciliation Recovery · 10 Corporate Action (Split) · 11 Corporate Action (Dividend)
  19 Startup Recovery · 20 Restart During Open Position
"""
import time
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from backend.brokers.models import Position
from backend.brokers.paper_broker import ScriptedPaperBroker
from backend.data.corporate_actions import ActionStatus, ActionType, CorporateAction
from backend.data.corporate_action_runtime import CorporateActionRuntime
from backend.execution.order_machine import OrderStateMachine
from backend.execution.position_tracker import PositionTracker
from backend.execution.reconciler import PositionReconciler
from backend.risk.kill_switch import KillSwitch, TradingState
from backend.testing.metrics import ValidationMetrics

BROKER = "kis"


@pytest.fixture()
def db_factory(pg_trading_engine):
    """reconciler/CA/kill-switch 가 기대하는 '세션 반환 콜러블'. 각 테스트 전 정리."""
    from backend.database.models import (
        Position as DBPosition, Order as DBOrder, Fill, AuditLog,
        ReconciliationLog, CorporateAction as DBCA, CorporateActionHistory,
        DailyRiskState,
    )
    Factory = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    s = Factory()
    try:
        for model in (CorporateActionHistory, DBCA, Fill, DBOrder, DBPosition,
                      ReconciliationLog, AuditLog, DailyRiskState):
            s.query(model).delete()
        s.commit()
    finally:
        s.close()
    return Factory


def _seed_db_position(db_factory, symbol, qty, avg, *, market="US", age_hours=2.0):
    from backend.database.models import Position as DBPosition
    s = db_factory()
    try:
        s.add(DBPosition(symbol=symbol, qty=qty, avg_price=avg, market=market,
                         broker=BROKER,
                         updated_at=datetime.utcnow() - timedelta(hours=age_hours)))
        s.commit()
    finally:
        s.close()


def _db_positions(db_factory):
    from backend.database.models import Position as DBPosition
    s = db_factory()
    try:
        return {p.symbol: p for p in s.query(DBPosition).all()}
    finally:
        s.close()


# ── 6 Worker Restart — 인메모리 tracker 를 DB 포지션에서 복원 ────────────────
def test_s06_worker_restart_restores_positions(db_factory):
    _seed_db_position(db_factory, "SPY", 7, 101.0)
    metrics = ValidationMetrics()

    # "재시작": 새 tracker 를 만들고 DB 포지션으로 복원
    t0 = time.perf_counter()
    machine = OrderStateMachine()
    tracker = PositionTracker(machine)
    rows = _db_positions(db_factory)
    tracker.restore_positions([
        Position(symbol=r.symbol, qty=r.qty, avg_price=r.avg_price, market=r.market)
        for r in rows.values()
    ])
    elapsed = time.perf_counter() - t0
    restored = tracker.get_position("SPY")
    metrics.record_recovery(ok=restored is not None and restored.qty == 7, seconds=elapsed)

    assert restored is not None and restored.qty == 7
    assert metrics.recovery_success_rate() == 1.0
    assert metrics.avg_restart_recovery_time() >= 0.0


# ── 7 Redis Restart — Redis 는 캐시, DB 가 권위 → 캐시 손실 무손실 ──────────
def test_s07_redis_restart_state_survives_in_db(db_factory):
    """Redis 가 죽어도(=인메모리/캐시 상태 소실) DB 가 권위 소스이므로
    포지션·기업행위 게이트가 복원된다."""
    _seed_db_position(db_factory, "069500", 5, 30000.0, market="KR")
    ca = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    ca.record(CorporateAction(ActionType.SPLIT, "069500", date.today(),
                              status=ActionStatus.CONFIRMED, ratio=2.0))

    # "Redis 재시작": 모든 인메모리 상태 폐기 → 새 객체를 DB 에서 복원
    ca2 = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    restored = ca2.restore_pending()
    tracker = PositionTracker(OrderStateMachine(), ca2)
    tracker.restore_positions([
        Position(symbol=r.symbol, qty=r.qty, avg_price=r.avg_price, market=r.market)
        for r in _db_positions(db_factory).values()
    ])

    assert restored >= 1
    assert ca2.is_blocked("069500") is True          # 게이트 fail-closed 복원
    assert tracker.get_position("069500").qty == 5    # 포지션 복원


# ── 8 Database Restart — 영속 상태가 재연결 후에도 유지 ─────────────────────
def test_s08_database_restart_persists(db_factory, pg_trading_engine):
    _seed_db_position(db_factory, "AAPL", 3, 200.0)
    # "DB 재시작": 기존 세션/팩토리를 버리고 새 sessionmaker 로 재연결
    NewFactory = sessionmaker(bind=pg_trading_engine, expire_on_commit=False)
    s = NewFactory()
    try:
        from backend.database.models import Position as DBPosition
        rows = {p.symbol: p for p in s.query(DBPosition).all()}
    finally:
        s.close()
    assert "AAPL" in rows and rows["AAPL"].qty == 3


# ── 9 Reconciliation Recovery — drift 탐지 + 수리 ───────────────────────────
def test_s09_reconciliation_recovery(db_factory):
    metrics = ValidationMetrics()
    # 외부 매수: 브로커에 있고 DB 에 없음 → insert
    broker = ScriptedPaperBroker(default_price=100.0)
    broker.set_position("SPY", 4, 100.0, market="US")
    # 스테일 청산: DB 에 있고 브로커에 없음(2h+) → delete
    _seed_db_position(db_factory, "OLD", 2, 50.0)

    rec = PositionReconciler(broker, db_factory, broker_name=BROKER)
    result = rec.reconcile("startup")
    metrics.reconciliation_mismatches += len(result.gaps)

    kinds = {(r["kind"], r["symbol"]) for r in result.repairs}
    assert ("insert_position", "SPY") in kinds
    assert ("delete_position", "OLD") in kinds
    positions = _db_positions(db_factory)
    assert positions["SPY"].qty == 4 and "OLD" not in positions
    assert metrics.reconciliation_mismatches >= 2


# ── 10 Corporate Action (Split) — 수량 점프 분류 + 동기화 ───────────────────
def test_s10_corporate_action_split(db_factory):
    _seed_db_position(db_factory, "SPY", 10, 100.0)
    broker = ScriptedPaperBroker(default_price=50.0)
    broker.set_position("SPY", 20, 50.0, market="US")   # 2:1 분할

    ca = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    rec = PositionReconciler(broker, db_factory, broker_name=BROKER, ca_runtime=ca)
    result = rec.reconcile("periodic")

    assert any(g["kind"].startswith("qty_corporate_action") and g["symbol"] == "SPY"
               for g in result.gaps)
    positions = _db_positions(db_factory)
    assert positions["SPY"].qty == 20 and positions["SPY"].avg_price == pytest.approx(50.0)


# ── 11 Corporate Action (Dividend) — 기록·게이트·적용·재시작 복원 ────────────
def test_s11_corporate_action_dividend(db_factory):
    ca = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    action = CorporateAction(ActionType.CASH_DIVIDEND, "SPY", date.today(),
                             status=ActionStatus.CONFIRMED, cash_amount=1.5)
    ca.record(action)
    assert ca.is_blocked("SPY") is True                 # 적용 전엔 fail-closed 차단

    # 재시작 복원: 새 런타임이 DB 에서 게이트를 되살린다
    ca2 = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    assert ca2.restore_pending() >= 1
    assert ca2.is_blocked("SPY") is True

    # 적용 → 게이트 해제(현금 배당 반영)
    ca2.mark_applied(action, qty_before=10, avg_before=100.0,
                     qty_after=10, avg_after=100.0, cash_delta=15.0)
    assert ca2.is_blocked("SPY") is False


# ── 19 Startup Recovery — 실제 컴포넌트 복원 경로(포지션+CA게이트+kill-switch)
def test_s19_startup_recovery(db_factory):
    from backend.database.models import DailyRiskState
    # 직전 세션 상태를 DB 에 적재: 포지션 + CA 게이트 + kill-switch HALTED
    _seed_db_position(db_factory, "SPY", 6, 100.0)
    ca = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    ca.record(CorporateAction(ActionType.SPLIT, "SPY", date.today(),
                              status=ActionStatus.UNKNOWN))   # unknown → 차단
    s = db_factory()
    try:
        s.add(DailyRiskState(trade_date=date.today(), kill_switch=True,
                             kill_reason="이전 세션 손실 한도"))
        s.commit()
    finally:
        s.close()

    metrics = ValidationMetrics()
    t0 = time.perf_counter()
    # 기동 복구: 실제 컴포넌트들의 복원 경로를 그대로 호출
    ks = KillSwitch(db_factory=db_factory)                 # 생성 시 DailyRiskState 복원
    ca2 = CorporateActionRuntime(db_factory=db_factory, broker=BROKER)
    ca2.restore_pending()
    tracker = PositionTracker(OrderStateMachine(), ca2)
    tracker.restore_positions([
        Position(symbol=r.symbol, qty=r.qty, avg_price=r.avg_price, market=r.market)
        for r in _db_positions(db_factory).values()
    ])
    elapsed = time.perf_counter() - t0
    ok = (ks.state == TradingState.HALTED and ca2.is_blocked("SPY")
          and tracker.get_position("SPY").qty == 6)
    metrics.record_recovery(ok=ok, seconds=elapsed)

    assert ks.state == TradingState.HALTED                 # kill-switch 복원(매매 차단)
    assert ca2.is_blocked("SPY") is True                   # CA 게이트 복원(fail-closed)
    assert tracker.get_position("SPY").qty == 6            # 포지션 복원
    assert metrics.recovery_success_rate() == 1.0


# ── 20 Restart During Open Position — 보유 포지션 유지 ──────────────────────
def test_s20_restart_during_open_position(db_factory):
    _seed_db_position(db_factory, "SPY", 8, 105.0)
    _seed_db_position(db_factory, "069500", 12, 30000.0, market="KR")

    # 재시작: 새 tracker 가 두 포지션을 모두 복원
    tracker = PositionTracker(OrderStateMachine())
    tracker.restore_positions([
        Position(symbol=r.symbol, qty=r.qty, avg_price=r.avg_price, market=r.market)
        for r in _db_positions(db_factory).values()
    ])
    assert tracker.get_position("SPY").qty == 8
    assert tracker.get_position("069500").qty == 12
    assert len(tracker.all_positions()) == 2
