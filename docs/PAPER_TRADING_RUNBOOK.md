# 페이퍼 트레이딩 운영 런북 (PAPER_TRADING_RUNBOOK)

> TASK P3-01A — End-to-End Paper Trading Audit 의 운영자 산출물.
> 4주 모의투자 검증 → 실전 전환(`KIS_ENV=paper → real`) 의 go/no-go 게이트.

---

## 0. 목적과 범위

이 문서는 페이퍼 트레이딩 파이프라인을 **종단간으로 검증**하고, 운영자가 매일
점검하며, 실전 전환 가부를 판단하기 위한 절차다. 자동화 하니스(아래 §3)가 대부분의
실패 표면을 결정론적으로 재현하며, 본 런북은 그 위에 사람이 확인할 항목을 더한다.

**핵심 컴포넌트(이번 작업 산출물)**

| 항목 | 경로 | 설명 |
|---|---|---|
| 스크립트형 페이퍼 브로커 | `backend/brokers/paper_broker.py` (`ScriptedPaperBroker`) | 네트워크 없이 BrokerAdapter 구현. 체결을 `get_order_status()` 로 점진 노출 → 실제 OrderFillPoller 경로 구동 |
| E2E 하니스 드라이버 | `backend/testing/paper_harness.py` (`PaperHarness`) | machine+tracker+poller+kill-switch 를 실제 클래스로 연결, `pump()` 로 결정론 구동 |
| 코어 시나리오(인메모리) | `backend/testing/tests/test_paper_e2e_core.py` | happy/duplicate/partial/reject/timeout/kill-switch/CA게이트/PnL |
| is_live 게이트 | `backend/testing/tests/test_live_gate.py` | SAFE_MODE / ENABLE_LIVE_TRADING / 시뮬레이터 우회 검증 |
| DB 시나리오 | `tests/postgres/test_paper_e2e_db.py` | reconciler insert/delete/dry-run/기업행위 (Postgres 필요) |
| 라이브 스모크(opt-in) | `backend/brokers/tests/test_live_paper_smoke.py` | 실제 KIS VTS 인증·TR 파싱 |

> 백테스트용 `backend/strategy/runtime/simulator.py` 의 `SimulatedBroker` 와는
> 별개다(그쪽은 place_order 내부 즉시 체결, 폴러 경로 미사용).

---

## 1. 사전 점검 체크리스트 (배포 전)

- [ ] `.env`: `KIS_ENV=paper` (실전 아님)
- [ ] `.env`: `ENABLE_LIVE_TRADING=true` (페이퍼 주문을 실제로 모의서버에 제출하려는 경우) — 단, 검증 초기에는 `false`(shadow) 로 신호만 확인 권장
- [ ] `.env`: `DAILY_LOSS_LIMIT_PCT=0.03`, `MDD_LIMIT_PCT=0.15`, `STOP_LOSS_PCT=0.07`
- [ ] KIS 모의 자격증명(`KIS_APP_KEY/SECRET/ACCOUNT_NO/HTS_ID`) 입력
- [ ] 텔레그램 알림(`TELEGRAM_TOKEN/CHAT_ID`) 동작 확인
- [ ] kill-switch 가 **꺼져 있는지** 확인: `daily_risk_states.kill_switch=false` (이전 세션의 HALTED 가 남아 있으면 매매가 막힌다 — fail-closed)
- [ ] 자동화 하니스 스위트 전체 통과(아래 §3)

---

## 2. 기동 직후 확인 (Worker recovery)

Worker 기동 시 8단계 복구(`backend/worker/recovery.py`)가 끝나기 전까지
`SAFE_MODE.can_trade=False` 로 모든 주문이 차단된다. 다음을 확인:

- [ ] `/api/metrics` → `worker_alive=true, db_ok=true, redis_ok=true`
- [ ] 기동 로그에 "정상 모드 진입(can_trade=True)" 출력
- [ ] 시작 시 reconcile("startup") 이 broker=ground-truth 로 포지션 동기화 완료
- [ ] 미체결 주문이 poller 에 재등록되었는지(있다면)

---

## 3. 자동화 검증 스위트 실행

```bash
# 1) 코어(인메모리) — 어디서나 실행, 네트워크/DB 불필요
pytest backend/brokers/tests/test_paper_broker.py \
       backend/testing/tests/ -q

# 2) DB 시나리오 — Postgres 필요(없으면 자동 스킵)
TEST_DATABASE_URL=postgresql+psycopg2://quantdinger:quantdinger@localhost:5432/quantdinger_test \
  pytest tests/postgres/test_paper_e2e_db.py -q

# 3) 라이브 스모크 — KIS VTS 모의서버 대상(opt-in, 명시적 실행)
RUN_LIVE_PAPER=1 KIS_ENV=paper \
KIS_APP_KEY=... KIS_APP_SECRET=... KIS_ACCOUNT_NO=... \
  pytest backend/brokers/tests/test_live_paper_smoke.py -m live_paper -v
# 주문 제출/취소까지: RUN_LIVE_PAPER_ORDERS=1 추가
```

기존 CI(`pytest backend tests -q`)에 1)·2) 가 그대로 포함된다. 3) 은 기본 게이트
밖(opt-in).

---

## 4. 검증 매트릭스 (수동 사인오프)

각 행은 자동 시나리오로 커버되며, 운영자는 페이퍼 기간 중 실제 발생/주입으로
한 번씩 확인하고 서명한다.

| # | 시나리오 | 자동 테스트 | 운영자 확인 | 서명/일자 |
|---|---|---|---|---|
| 1 | 정상 매수→폴링→체결→포지션 | `test_happy_path_buy_fills_*` | 모의 주문 1건 체결 확인 | |
| 2 | 중복주문 차단(pending) | `test_duplicate_order_blocked_*` | 동일 심볼 연속주문 차단 로그 | |
| 3 | 부분체결 증분 누적·과체결 방지 | `test_partial_then_full_fill_*` | 부분체결 종목 평단 정확 | |
| 4 | 주문 거부 처리·pending 해제 | `test_reject_releases_pending_*` | 거부 알림 + 재주문 가능 | |
| 5 | 타임아웃 자동취소·pending 해제 | `test_timeout_cancels_*` | 30분 미체결 자동취소 확인 | |
| 6 | kill-switch HALTED → NEW 차단 | `test_kill_switch_halts_*` | 손실 한도 시 매매 중단 알림 | |
| 7 | kill-switch CANCEL 허용·수동 재개 | `test_kill_switch_allows_cancel_*`, `*manual_resume*` | 쿨다운 후 재개 절차 | |
| 8 | 기업행위 게이트(fail-closed) | `test_corporate_action_blocks_*` | 분할/배당 종목 매매 차단 | |
| 9 | is_live 게이트(SAFE_MODE/shadow) | `test_live_gate.py` | shadow 모드 신호-only 확인 | |
| 10 | reconciler 외부매수 insert | `test_reconcile_inserts_external_*` | 수동 매수 후 DB 동기화 | |
| 11 | reconciler 스테일 청산 delete | `test_reconcile_deletes_stale_*` | 외부 매도 후 DB 정리 | |
| 12 | reconciler 수량점프=기업행위 | `test_reconcile_classifies_quantity_jump_*` | 분할 반영 후 동기화·게이트 | |
| 13 | 실현손익 재구성 | `test_realized_pnl_on_round_trip` | 일일 PnL ↔ 체결 일치 | |
| 14 | KIS VTS 인증·TR 파싱 | `test_live_paper_smoke.py` | 모의서버 잔고/현재가 조회 | |

---

## 5. 4주 페이퍼 기간 — 일일 점검

- [ ] 23:50 KST 일일 결산 텔레그램 수신(`equity_snapshots` 적재)
- [ ] 00:01/06:01 일일 리스크 카운터 리셋 확인(`risk:daily_*`); kill_switch 는 자동 리셋 안 됨
- [ ] 미체결/타임아웃 주문 누적 여부(`PollingHealth`)
- [ ] reconcile(periodic, 30분) 갭/수정 로그 검토(`reconciliation_logs`)
- [ ] 기업행위 게이트로 막힌 종목이 있으면 수동 검토 후 해제
- [ ] kill-switch HALTED 발생 시: 원인(`kill_reason`) 확인 → 수동 `resume`

### 성과 재구성(go/no-go 근거)

```sql
-- 일별 평가금액 곡선
SELECT snapped_at::date AS d, total_krw FROM equity_snapshots ORDER BY d;
-- 체결 기반 실현손익 교차검증
SELECT f.symbol, SUM(CASE WHEN o.side='sell' THEN 1 ELSE -1 END * f.qty * f.price)
FROM fills f JOIN orders o ON o.broker_order_id=f.order_id GROUP BY f.symbol;
```

`equity_snapshots` 곡선과 `fills` 기반 PnL 이 일치해야 한다(불일치 시 파이프라인
누수 의심 → 전환 보류).

---

## 6. 실전 전환 go/no-go 게이트

다음을 **모두** 충족할 때만 `KIS_ENV=real` 전환을 승인한다:

- [ ] 4주(영업일 20일+) 무중단 운영, 치명적 미해결 인시던트 0건
- [ ] §3 자동 스위트 전부 통과(CI green) + §4 매트릭스 14행 운영자 서명 완료
- [ ] kill-switch / emergency-flatten 이 최소 1회 실제(또는 주입)로 동작 확인
- [ ] reconcile 갭이 매일 0 또는 즉시 자동수정으로 수렴
- [ ] `equity_snapshots` ↔ `fills` PnL 정합(±오차 허용 내)
- [ ] 라이브 스모크(`RUN_LIVE_PAPER=1`) 통과 — 인증/TR 파싱 정상
- [ ] 전환 후 첫날 `ENABLE_LIVE_TRADING` 및 소액 한도로 점진 가동 계획 수립

> 하나라도 미충족이면 전환 보류. 전환은 되돌리기 어려운 outward-facing 행위다.

---

## 7. 범위 밖(이번 작업 미포함, 문서화만)

- 스트리밍 시세 / 라이브 `on_bar` 피드(현재 배치 전용)
- 키움증권 어댑터(스텁)
- 모바일 UI
