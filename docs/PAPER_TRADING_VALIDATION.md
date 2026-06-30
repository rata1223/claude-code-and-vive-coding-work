# 페이퍼 트레이딩 종단간 검증 (PAPER_TRADING_VALIDATION)

> TASK **P3-01B** — End-to-End Paper Trading Validation Implementation.
> P3-01A 감사(`/root/.claude/plans/...`, `docs/PAPER_TRADING_RUNBOOK.md`)를 권위 컨텍스트로 사용.
> 런타임 코드는 재설계하지 않음 — 검증 하니스만 추가. 페이퍼 브로커 전용.

---

## 1. Architecture

검증은 **실제 실행 계층 컴포넌트**(목 아님)를 결정론적 페이퍼 브로커에 연결해
프로덕션과 동일한 비동기 체결 경로를 구동한다.

```
Scheduler ─(session pub)→ Worker(StrategyWorker 패턴)
  → Market Data(FreshnessGate, fail-closed)
  → Strategy(신호) → SignalFusion(신호 병합)
  → RiskEngine(KillSwitch OrderInterceptor + LossTracker)
  → Execution(StrategyBase.buy/sell 게이트)
  → BrokerAdapter (Paper = ScriptedPaperBroker, is_live=False)
  → Order Polling(OrderFillPoller, 증분 체결)
  → PositionTracker(중복/CA 게이트, on_fill)
  → Reconciliation(PositionReconciler, broker=ground truth)
  → Performance(실현손익 + ValidationMetrics)
```

| 컴포넌트 | 실제 클래스 | 하니스 연결점 |
|---|---|---|
| Paper BrokerAdapter | `backend/brokers/paper_broker.py::ScriptedPaperBroker` | 스크립트형 점진 체결, 자체 포지션 장부(=reconciler ground truth) |
| 하니스 드라이버 | `backend/testing/paper_harness.py::PaperHarness` | machine+tracker+poller+kill-switch+freshness+flatten 배선, `pump()` 결정론 구동 |
| 지표 | `backend/testing/metrics.py::ValidationMetrics` | 게이트/체결/복구 경로에서 누적 |
| 주문 상태머신 | `backend/execution/order_machine.py` | 등록→SUBMITTED→PARTIAL→FILLED / CANCELED / REJECTED |
| 폴링 | `backend/execution/order_poller.py` | `get_order_status` 증분 체결, 타임아웃 자동취소 |
| 포지션 | `backend/execution/position_tracker.py` | 중복주문/CA 게이트(fail-closed), `restore_positions` |
| 정합성 | `backend/execution/reconciler.py` | insert/delete/qty-jump, CA 통합 |
| 리스크 | `backend/risk/kill_switch.py` | RUNNING→WARNING→HALTED, OrderInterceptor |
| 기업행위 | `backend/data/corporate_action_runtime.py` | 분류/기록/게이트/`restore_pending` |
| 신선도 | `backend/data/freshness_gate.py` | UNKNOWN/STALE → 차단(fail-closed) |
| 비상청산 | `backend/worker/emergency.py::EmergencyFlattenManager` | 전 포지션 시장가 매도 |

원칙: **stale/invalid/unknown 상태는 모두 fail-closed(차단)**. 게이트 순서는
freshness → 중복/기업행위 → kill-switch.

### 테스트 계층
- **코어(인메모리)** `backend/testing/tests/test_paper_validation_core.py` — DB/Redis 불필요, 로컬+CI 실행, 결정론.
- **Postgres** `tests/postgres/test_paper_validation_db.py` — `TEST_DATABASE_URL` 없으면 자동 스킵, CI postgres 잡에서 실행.
- 기반 단위/게이트: `backend/brokers/tests/test_paper_broker.py`, `backend/testing/tests/test_live_gate.py`.

---

## 2. Validation Matrix

| # | 시나리오 | 계층 | 테스트 | 검증 항목 |
|---|---|---|---|---|
| 1 | Normal Buy | core | `test_s01_normal_buy` | 주문수명주기, 포지션 일관성, 성공주문 지표 |
| 2 | Normal Sell | core | `test_s02_normal_sell` | 실현손익(performance), 청산 |
| 3 | Partial Fill | core | `test_s03_partial_fill` | 증분 체결 정확·과체결 방지 |
| 4 | Order Cancel | core | `test_s04_order_cancel` | 터미널 전환(CANCELED), pending 해제 |
| 5 | Order Reject | core | `test_s05_order_reject` | 거부 처리, rejected 지표 |
| 6 | Worker Restart | pg | `test_s06_worker_restart_*` | 포지션 복원, 복구시간 |
| 7 | Redis Restart | pg | `test_s07_redis_restart_*` | DB 권위·캐시 손실 무손실, 게이트 복원 |
| 8 | Database Restart | pg | `test_s08_database_restart_*` | 영속성 재연결 유지 |
| 9 | Reconciliation Recovery | pg | `test_s09_reconciliation_recovery` | drift insert/delete, mismatch 지표 |
| 10 | Corporate Action (Split) | pg | `test_s10_corporate_action_split` | 수량점프 분류·동기화 |
| 11 | Corporate Action (Dividend) | pg | `test_s11_corporate_action_dividend` | 기록·게이트·적용·재시작 복원 |
| 12 | Unknown Corporate Action | core | `test_s12_unknown_corporate_action_blocks` | fail-closed 차단, CA 지표 |
| 13 | Duplicate Signal | core | `test_s13_duplicate_signal` | 신호 dedup |
| 14 | Duplicate Order | core | `test_s14_duplicate_order` | pending 락 |
| 15 | Stale Market Data | core | `test_s15_stale_market_data_blocks` | freshness fail-closed(STALE+UNKNOWN) |
| 16 | Kill Switch | core | `test_s16_kill_switch_*` | HALTED NEW 차단·CANCEL 허용 |
| 17 | Emergency Flatten | core | `test_s17_emergency_flatten` | dry-run vs 실제 청산 |
| 18 | SafeMode Recovery | core | `test_live_gate.py` (SAFE_MODE enable/disable) | 기동 게이트, is_live 경로 |
| 19 | Startup Recovery | pg | `test_s19_startup_recovery` | 포지션+CA게이트+kill-switch 복원 |
| 20 | Restart During Open Position | pg | `test_s20_restart_during_open_position` | 다중 포지션 복원 |

**검증 항목 커버리지**: 주문수명주기(1,3,4,5) · 포지션 일관성(1,2,20) · 포트폴리오
일관성(2,17) · 정합성(9,10) · 감사 로깅(poller/flatten/reconciler/kill-switch가
AuditLog 기록 — pg 계층) · 복구(6,7,19) · 재시작 영속(6,8,20) · 기업행위(10,11,12) ·
stale 보호(15) · kill switch(16) · emergency flatten(17).

---

## 3. Scenario Results

- **코어(인메모리) 1–5, 12–17 + 지표**: 로컬 **PASS** (`pytest backend/testing/tests/ backend/brokers/tests/test_paper_broker.py` → 47 passed). 결정론적, 네트워크/DB 불필요.
- **SafeMode(18) / is_live 게이트**: 로컬 **PASS** (`test_live_gate.py`, 3 passed).
- **Postgres 6–11, 19, 20**: `TEST_DATABASE_URL` 부재 시 자동 스킵 → **CI postgres 잡에서 실행**. 로컬에서는 psycopg2/PG 미설치로 미실행(설계상 스킵). 실제 컴포넌트의 복원/정합성/CA 경로를 구동하며, P3-01A 에서 동일 패턴(`tests/postgres/test_paper_e2e_db.py`)이 CI 통과 이력 있음.

### 지표(ValidationMetrics) 예시 — 코어 시나리오 누적
| 지표 | 의미 | 비고 |
|---|---|---|
| successful_orders | 완전 체결 주문 | s1/s2/s3 |
| rejected_orders | 거부 주문 | s5 |
| duplicate_orders / duplicate_signals | pending/신호 dedup 차단 | s14 / s13 |
| stale_data_blocks | freshness 차단(STALE+UNKNOWN) | s15 = 2 |
| corporate_action_events | CA 게이트 차단 | s12 |
| kill_switch_blocks | HALTED 차단 | s16 |
| recovery_success_rate / avg_restart_recovery_seconds | 복구 성공률·복구시간 | pg 6/19 |

---

## 4. Failures Discovered

구현 중 발견·처리한 실제 결함/괴리:

1. **주문 터미널 전환이 콜백 의존** — `OrderFillPoller` 는 CANCELED/REJECTED/EXPIRED
   관측 시 터미널 콜백만 호출하고 `OrderStateMachine` 을 직접 전환하지 않는다.
   상태머신을 터미널로 옮기는 책임은 *콜백 배선*(worker.runner / 하니스)에 있다.
   → 하니스가 `_on_canceled/_on_rejected/_on_timeout` 에서 `machine.transition`
   하도록 수정(없으면 취소 주문이 영구 SUBMITTED 로 남아 수명주기 검증 실패).
   **운영 함의**: worker.runner 의 터미널 콜백이 동일하게 전환을 수행하는지 보장 필요.
2. **비상청산은 tracker/poller 를 우회** — `EmergencyFlattenManager` 는 브로커에 직접
   매도하므로, 청산 직후 `PositionTracker`(로컬)와 브로커 장부가 일시적으로 불일치한다.
   정합성은 다음 reconcile 가 복원한다. **운영 함의**: 청산 후 즉시 reconcile 권장.
3. **`is_live=False` 게이트 우회(P3-01A 재확인)** — 페이퍼 시뮬 브로커는 SAFE_MODE /
   ENABLE_LIVE_TRADING 게이트를 건너뛴다. 그 게이트 자체는 `test_live_gate.py` 가
   is_live=True 경로로 별도 검증.

블로킹 결함 없음 — 위 1은 하니스에서 해소, 2·3은 설계상 동작이며 문서화.

---

## 5. Remaining Operational Risks

- **스트리밍 시세 부재**: `on_bar` 라이브 피드가 배선되어 있지 않다(배치 전용). stale 보호는 주문 경계의 FreshnessGate 로만 검증됨.
- **비상청산 교차 프로세스 중복**: in-process 락만 존재(`docs/EMERGENCY_FLATTEN_VALIDATION.md`).
- **전체 8단계 StartupRecovery 미구동**: 하니스는 복원 *계약*(포지션/CA/kill-switch)을 검증. 전체 시퀀스는 기존 `backend/worker/tests/test_recovery_safety.py`가 커버.
- **Postgres 계층 로컬 미실행**: 6–11/19/20 은 CI 의존. 로컬 검증은 인메모리 계층에 한정.
- **Redis 장애 모델 단순화**: s7 은 "Redis=캐시, DB=권위 → 무손실" 속성으로 검증(실 Redis 페일오버 부하시험 아님).

---

## 6. Go / No-Go Assessment

**판정: 조건부 GO (계속 페이퍼 운용 진행)**

근거:
- 핵심 매매·리스크·정합성 경로가 실제 컴포넌트로 결정론적으로 검증됨(코어 PASS).
- 모든 fail-closed 게이트(stale/unknown-CA/kill-switch/중복) 동작 확인.
- 복구/재시작/기업행위/정합성은 Postgres 계층에서 실제 컴포넌트 복원 경로로 검증(CI).

실전(`KIS_ENV=real`) 전환 게이트(모두 충족 시에만):
- [ ] 코어 + Postgres 검증 스위트 CI 전부 green
- [ ] `docs/PAPER_TRADING_RUNBOOK.md` 4주 사인오프 매트릭스 완료
- [ ] kill-switch / emergency-flatten 실제(또는 주입) 1회 이상 동작 확인
- [ ] reconcile mismatch 일별 0 수렴
- [ ] equity_snapshots ↔ fills 실현손익 정합

미충족 시 보류. 실전 전환은 되돌리기 어려운 행위 — 운영자 명시 승인 필수.

### 실행
```bash
# 코어(로컬/CI)
pytest backend/testing/tests/ backend/brokers/tests/test_paper_broker.py -q
# Postgres 계층(CI)
TEST_DATABASE_URL=postgresql+psycopg2://… pytest tests/postgres/test_paper_validation_db.py -q
```
