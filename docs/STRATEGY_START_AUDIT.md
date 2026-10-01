# 앱의 전략 시작/중지 감사 — 앱에서 "실행 중"인 전략은 아무 데서도 실행되지 않는다

작성: 2026-10-01 · 기준 main `1333331` (PR #198) · 코드 변경 없음(조사만)

## 요약

- 앱(`api/`, 프론트·모바일이 부르는 FastAPI)의 `POST /api/strategies/start`는 다음 세 가지만 한다.
  - `strategies.status`를 `"running"`으로 바꾼다.
  - Redis 집합 `running_strategies`에 id를 넣는다.
  - 로그를 남긴다.
- **워커에는 아무것도 보내지 않는다.** 화면은 초록색 "실행 중"을 보여주지만 신호도 주문도 생기지 않는다.
- 워커를 실제로 움직이는 길은 운영자용 `kis-api`(`backend/api/server.py`, Flask, `X-API-Key`)뿐이다.
  - 이 API는 `strategy_runs` 행과 `commands` 행을 쓴 뒤 `strategy:start`를 발행한다.
- 결과적으로 **4주 모의투자 시계가 앱에서는 시작되지 않는다.**
  - `LivePromotionGuard`의 "4주 모의투자 완료" 관문은 `strategy_runs.started_at <= now-28d`로 판정한다.
  - 앱에서 시작한 전략은 `strategy_runs`에 쓰지 않는다. 앱으로 4주를 돌려도 이 관문은 계속 닫혀 있다.

## 현재 경로

| | 앱 `api/` (FastAPI) | 운영자 `kis-api` (`backend/api/server.py`, Flask) |
|---|---|---|
| 인증 | 사용자 JWT, 다중 사용자 | `KIS_API_KEY` 단일 키 |
| 전략 저장 | `strategies`(사용자별) | `strategy_runs`(사용자 없음) |
| 시작 | `status="running"` + Redis `SADD running_strategies` | `StrategyRun` + `Command(strategy:start)` 커밋 → Redis `PUBLISH strategy:start` |
| 워커 수신 | **없음** | `StrategyWorker._handle_start`. Redis 장애 시 `commands` 폴링, 재시작 시 `is_active` 행 복원 |
| 브로커 | 사용자 자격증명 (`credentials`, 암호화) | `.env`의 KIS 계좌 하나 (`get_kis_broker()`) |

`running_strategies` 집합은 아무 코드도 읽지 않는다. 쓰기만 하는 데이터다.

## 앱을 워커에 그대로 잇지 못하는 이유

1. **계좌가 다르다.**
   - 워커는 `.env`의 단일 KIS 계좌로 주문한다.
   - 앱 사용자는 각자 자격증명을 등록한다.
   - 그대로 이으면 사용자 A가 시작한 전략이 **운영자 계좌로** 주문한다.
   - 워커의 `orders`·`fills`·`positions`·`strategy_runs`에는 사용자 열도, 자격증명 열도 없다(PR #198과 같은 이유).
2. **설정 스키마가 다르다.**
   - 앱 지표 전략의 `config`는 `{indicators, entry_conditions, exit_conditions, stop_loss_pct, take_profit_pct}`다. 레거시 `strategy/indicator_strategy.py` 형식이고 백테스트가 이것을 쓴다.
   - 워커의 `backend/strategy/indicator/strategy.py`는 `{universe, position_size_pct, stop_loss_pct}`를 읽는다.
   - 앱 설정을 그대로 넘기면 워커는 조건을 무시하고 기본 유니버스(`SPY`, `QQQ`)로 자기 로직을 돌린다. **사용자가 백테스트한 전략과 다른 전략이 실행된다.**
3. **스크립트 전략은 실행 위치가 위험하다.**
   - 워커의 `ScriptStrategy`는 사용자 코드를 워커 프로세스 안의 스레드에서 실행하고 `join(timeout)`으로만 제한한다.
   - 이 방식으로는 멈추지 않는 코드를 끝낼 수 없다.
   - 백테스트 쪽은 같은 문제를 PR #188·#191에서 자식 프로세스 + kill로 고쳤다. 주문을 내는 워커에 같은 문제를 들이면 안 된다.
   - 앱은 코드를 `script_code`로, 워커는 `config.script`로 받는다. 이 차이도 있다.
4. **안전 장치의 범위.**
   - 킬스위치, 일손실·MDD, `SAFE_MODE`는 워커의 단일 계좌 기준이다.
   - 사용자별 계좌로 확장하면 P0-04(브로커·계좌별 SAFE_MODE)가 선행돼야 한다.

## 선택지

### A. 정직한 최소 조치 (권장, 작음)

앱이 하지 않는 일을 했다고 말하지 않게 한다.

- `/api/strategies/start`는 상태를 바꾸지 않고 다음 오류를 돌려준다: "자동 실행은 아직 앱에서 연결되지 않았습니다. 백테스트와 퀵트레이드는 사용할 수 있습니다." `/stop`은 그대로 둔다(이미 `running`인 행을 내릴 수 있게).
- 기존 `running` 행을 처리한다. 마이그레이션(API 테이블은 `create_all` 관리) 대신 응답에서 `running`을 내보내지 않는 방식도 가능하다. 구현할 때 정한다.
- 아무도 읽지 않는 `running_strategies` 집합과 그 Redis 연결 코드를 제거한다.
- 프론트의 시작 버튼은 비활성화하고 이유를 표시한다(5개 로케일 × 2개 앱).
- 대시보드의 `running_strategies` 수는 항상 0이 된다. 키를 유지할지 뺄지는 구현할 때 정한다.
- **주문 경로는 바꾸지 않는다.** 워커, `backend/execution/*`, compose 모두 그대로다.

### B. 운영자 계좌 소유자만 연결 (중간, 주문 경로 변경 — 승인 필요)

자격증명이 `.env` 계좌와 같은 사용자만 앱에서 워커 전략을 시작하게 한다.

- 계좌번호를 비교한다. 다른 사용자는 A와 같은 오류를 받는다.
- 시작하면 `strategy_runs` + `commands`를 쓰고 `strategy:start`를 발행한다(kis-api와 같은 방식). 그러면 4주 시계도 앱에서 시작된다.
- 앱 지표 설정을 워커 형식으로 바꾸는 번역층이 필요하다. 아니면 워커 지표 전략이 조건 기반 설정을 받도록 바꾼다. 어느 쪽이든 **백테스트와 라이브가 같은 로직**이라는 증명 테스트가 필요하다.
- 스크립트 전략은 거부한다. 프로세스 격리 실행기를 만들기 전까지는 받지 않는다.
- `strategies` ↔ `strategy_runs` 연결 열이 필요하다(상태와 중지 동기화용).
- 위험: 앱 버튼 하나가 실제 주문을 내게 된다(모의 계좌라도). 킬스위치·리스크 게이트는 그대로 적용된다. 실전 전환 관문(`LivePromotionGuard`)도 그대로다.

### C. 다중 사용자 워커 (큼)

- 워커가 자격증명마다 브로커·트래커·리스크 상태를 갖는다.
- 워커 테이블에 사용자·자격증명 열을 추가한다(Alembic).
- P0-04가 선행돼야 한다.
- 운용 목표(200만원, 단일 운영자)에 비해 과하다.

## 권장

1. **지금 A.** 사용자에게 보이는 거짓 "실행 중"과 4주 시계에 대한 오해를 없앤다. 작고 주문 경로 밖이다.
2. 모의투자를 시작할 때가 오면 **B를 별도로 설계**한다. 그때까지 4주 시계는 운영자가 `kis-api`로 시작한다. 이 사실은 CLAUDE.md에 기록한다.

## 근거

| 내용 | 위치 |
|---|---|
| 앱 시작/중지 | `api/routers/strategies.py`: `start_strategy`, `stop_strategy`, `_mark_running`, `RUNNING_KEY` |
| 운영자 시작/중지 | `backend/api/server.py`: `start_strategy`, `stop_strategy` |
| 워커 수신·복원 | `backend/worker/runner.py`: `_SUBSCRIBE_CHANNELS`, `_handle_start`, `_restore_active`, `_build_strategy` |
| 4주 관문 | `backend/worker/promotion_guard.py`: `StrategyRun.started_at <= cutoff` |
| 설정 스키마 | `strategy/indicator_strategy.py`: `from_config`(앱·백테스트) / `backend/strategy/indicator/strategy.py`: `universe`, `position_size_pct`(워커) |
| 워커 스크립트 실행 | `backend/strategy/script/sandbox.py`(스레드 + `join(timeout)`) |
