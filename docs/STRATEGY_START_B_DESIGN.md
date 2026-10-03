# 선택지 B 설계 — 앱에서 운영 계좌의 워커 전략을 시작한다

작성: 2026-10-03 · 기준 main `294d6af` (PR #203) · 선행 문서 `docs/STRATEGY_START_AUDIT.md`(선택지 A는 PR #199에서 구현)

**이 문서는 설계만 담는다.** 구현은 아래 "결정"에 따라 단계별 PR로 진행한다.

## 요약

감사 문서는 B를 이렇게 그렸다. 앱 사용자의 지표 전략(조건 JSON)을 워커 형식으로 번역하고, 계좌번호가 `.env`와 같은 사용자만 시작하게 한다.

코드를 다시 읽어 보니 전제 두 개가 틀렸다.

1. **워커의 지표 전략은 설정의 조건을 읽지 않는다.**
   - `backend/strategy/indicator/strategy.py`는 docstring에 `buy_conditions`·`sell_conditions`를 예시로 들지만, 실제로는 매수·매도 판단에 `default_fusion()`(추세 40% + 모멘텀 40% + 변동성돌파 20%, SMA200 레짐 필터)만 쓴다.
   - 설정에서 읽는 값은 `universe`, `position_size_pct`, `stop_loss_pct` 셋뿐이다.
   - 운영자가 `kis-api`로 시작하는 전략도 마찬가지다. 워커는 **하나의 하우스 전략**을 돌린다.
   - 따라서 "앱 조건을 워커 형식으로 번역"할 대상이 없다. 사용자 조건으로 라이브 매매를 하려면 조건 평가기를 가진 **새 라이브 전략 클래스**가 필요하다. 이는 신호 로직·백테스트 동등성·주문 경로를 모두 새로 검증해야 하는 일로, 크기가 C에 가깝다.
2. **운영자 판별을 계좌번호로 할 필요가 없다.**
   - 앱 `api` 서비스는 `KIS_ACCOUNT_NO`를 받지 않는다. 계좌 비교를 하려면 운영 계좌번호를 앱 프로세스에 새로 넘기고 사용자 자격증명을 복호화해야 한다.
   - 이미 같은 문제를 푼 장치가 있다. 비상청산 프록시(`api/routers/quick_trade.py:emergency_flatten`)는 허용 목록 + `kis-api` 프록시로 운영 계좌 제어를 앱에 노출한다.

그래서 **B를 "하우스 전략 제어판"(B1)으로 좁히는 것을 권장한다.**

- 사용자가 만든 전략은 계속 시작할 수 없다(A 유지).
- 운영자에게만 보이는 "운영 전략" 화면에서 워커의 하우스 전략을 시작·중지·조회한다.
- 앱은 워커 테이블에 직접 쓰지 않는다. 기존 `kis-api` 엔드포인트를 서버 측 프록시로 부른다(비상청산과 같은 방식).
- 워커, `backend/execution/*`, 주문 경로는 바꾸지 않는다. 4주 시계(`strategy_runs`)는 `kis-api`가 지금처럼 쓴다.

## 설계 중 발견한 것

### F1. 운영자 허용 목록이 이메일이다 (우선 수정)

- 앱 쪽의 운영 계좌 제어 두 개가 이메일 허용 목록으로 사용자를 판별한다.
  - 비상청산: `EMERGENCY_FLATTEN_ADMINS`(`api/routers/quick_trade.py:_flatten_authorized`)
  - 킬스위치 해제: `KILL_SWITCH_ADMINS`(`api/routers/risk.py`)
- 가입(`api/routers/auth.py:register`)은 메일 소유를 확인하지 않는다.
- PR #202에서 `kis-ws`에 대해 같은 지적(CodeRabbit)을 받고 허용 목록을 **사용자 id**(토큰 `sub`)로 바꿨다. 이 두 곳은 아직 이메일이다.
- **운영자가 이미 가입했어도 안전하지 않다.** 허용 목록 비교는 대소문자를 무시한다(`.lower()`). 그런데 가입의 중복 검사(`User.email ==`)는 대소문자를 구분하고, `EmailStr`도 로컬 부분을 정규화하지 않는다. 그래서 "이미 가입된 주소는 다른 사람이 다시 가입할 수 없다"는 가정이 성립하지 않는다(CodeRabbit 지적).
- 영향 범위: `EMERGENCY_FLATTEN_ADMINS`는 compose `api` 블록에 선언돼 있어 설정하면 동작한다. `KILL_SWITCH_ADMINS`는 선언돼 있지 않아 지금은 늘 비활성이다(아래 참고).
- **0단계가 들어가기 전까지는 `EMERGENCY_FLATTEN_ADMINS`를 비워 둔다**(기본값). 비어 있으면 아무도 허용하지 않는다. 0단계는 B1과 상관없이 먼저 한다.
- B1은 세 번째 운영자 제어가 된다. 그 전에 **운영자 판별을 사용자 id 하나로 통일**하는 것이 맞다.
- 참고: `KILL_SWITCH_ADMINS`는 compose `api` 블록에 선언돼 있지 않다. 그래서 `.env`에 넣어도 앱 프로세스에 닿지 않고, 앱에서의 해제는 항상 비활성(fail-closed)이다.

### F2. 4주 관문이 "28일 전에 시작한 행이 하나라도 있는가"만 본다 — ✅ PR #206

> 구현 결과: `paper_run_qualifies`. 28일 동안 **중지되지 않은** 실행만 센다. 두 경우가 있다.
> - 활성이고 `stopped_at`이 없으면, 시작부터 지금까지 28일 이상.
> - `stopped_at`이 있으면, `stopped_at - started_at`이 28일 이상.
>
> 중지 요청은 됐는데 종료 기록이 없는 행은 언제 멈췄는지 알 수 없으므로 불통과다.
>
> 시작조차 못 한 실행도 센 적이 있었다. 워커가 전략을 만들지 못하면 행이 활성으로 남았기 때문이다. 이제 새 시작이 실패하면 `stopped_at = started_at`(0일)으로 기록한다. kis-api는 워커가 만들 수 없는 유형을 400으로 거부한다.
>
> **남은 한계**(스키마 변경이 필요해 미뤘다): `strategy_runs`에 열을 더하면 `create_all`로 만든 기존 DB에서 쿼리가 깨진다(#194와 같은 문제).
> - 실행 환경(모의/실전)을 기록하지 않는다.
> - 실제로 매매했는지 보지 않는다. 워커가 `orders.strategy_run_id`를 채우지 않기 때문이다.
> - 워커가 내려가 있던 시간을 구분하지 못한다. 재시작해도 실행은 활성으로 남는다.

- `LivePromotionGuard._check_paper_run`(`backend/worker/promotion_guard.py`)은 `started_at <= now-28d`인 `strategy_runs` 행의 **개수**만 센다.
- 28일 전에 시작해 1분 뒤 중지한 행도 통과한다. 실제로 계속 돌았는지, 주문을 하나라도 냈는지는 보지 않는다.
- B1로 앱에서 쉽게 시작·중지할 수 있게 되면 이 허점이 더 쉽게 밟힌다.
- 강화 방향: "활성이면서 28일 이상" 또는 "`stopped_at - started_at >= 28일`".
- 이것은 실전 전환 정책의 변경이므로 B1과 분리해 **따로 결정**한다(아래 "결정" 3).

## B1 설계

### 0단계 — 운영자 판별 통일 (F1) — ✅ PR #205

> 구현 결과: `backend/security/operators.py`(kis-ws 이미지에 `api/`가 없어 `backend/`에 둔다). 결정 4에 따라 `WS_OPERATOR_USER_IDS`도 합쳤다 — api와 kis-ws가 같은 `OPERATOR_USER_IDS`를 읽는다. 옛 변수 셋은 읽지 않고 기동 시 경고한다.

- `backend/security/operators.py`를 신설한다: `operator_user_ids()`와 `is_operator(user)`.
  - `OPERATOR_USER_IDS`(쉼표로 구분한 정수 id)를 읽는다. 비어 있으면 아무도 허용하지 않는다(fail-closed).
  - 형식이 틀린 항목은 무시하고 경고한다.
  - `kis-ws`의 `WS_OPERATOR_USER_IDS` 파싱(`backend/websocket/server.py:_operator_user_ids`)과 같은 규칙을 쓴다.
- `_flatten_authorized`와 킬스위치 해제 판별이 `is_operator`를 쓰게 한다.
- `EMERGENCY_FLATTEN_ADMINS`·`KILL_SWITCH_ADMINS`를 지운다. 지우기 전에 읽는 곳을 전부 확인하고, `.env.example`·compose·문서·테스트의 흔적도 함께 정리한다.
  - 운영 중인 `.env`에 이메일 목록이 남아 있으면 배포 후 해당 제어가 꺼진다(fail-closed). 기능이 조용히 바뀌지 않도록, 옛 변수가 설정돼 있으면 **기동 시 경고**를 한 줄 남긴다.
- **compose 변경(승인 필요)**: `api` 서비스는 명시적인 `environment:` 블록만 받는다. `OPERATOR_USER_IDS: ${OPERATOR_USER_IDS:-}`를 추가하고 `EMERGENCY_FLATTEN_ADMINS`를 지운다.
- `kis-ws`의 `WS_OPERATOR_USER_IDS`도 `OPERATOR_USER_IDS`로 합친다(결정 4). compose `kis-ws`를 함께 바꾼다.

### 1단계 — 프록시 엔드포인트 (`api/`)

`api/routers/operator.py`를 신설한다. 모든 경로는 `is_operator`를 확인한 뒤에만 진행하고, 아니면 비상청산과 같은 방식으로 아무것도 알려주지 않고 거부한다.

| 앱 경로 | 프록시 대상 (`kis-api`) | 비고 |
|---|---|---|
| `GET /api/operator/strategies` | `GET /api/strategies` | 최근 50개 실행. 활성 여부와 시작 시각, 4주 시계 경과일을 계산해 덧붙인다 |
| `POST /api/operator/strategies/start` | `POST /api/strategies/start` | 아래 입력 제한 |
| `POST /api/operator/strategies/stop` | `POST /api/strategies/<run_id>/stop` | `run_id` 정수만 |

- 호출 방식은 비상청산 프록시를 그대로 따른다.
  - 기본 주소는 `KIS_ADMIN_API_BASE`, 없으면 `http://kis-api:5001`.
  - 평문 HTTP 기본값은 공유되지 않는 단일 호스트 compose 네트워크에서만 허용된다(비상청산 프록시의 배포 주석과 같은 조건). 서비스를 호스트 간에 나누면 `KIS_ADMIN_API_BASE`를 `https://` 주소로 설정해야 한다.
  - 헤더는 `X-API-Key: KIS_API_KEY`.
  - 예외 문구에서 키를 가린다.
  - 상위 응답(429 포함)을 그대로 전달한다.
  - 테스트 이음새로 `_admin_post`를 둔다. 공용 헬퍼로 뺄지는 구현 때 정한다.
- **입력 제한**: `kis-api`는 아무 설정이나 받는다. `strategy_type="script"`이면 `config.script`를 워커 스레드에서 실행한다. 앱 프록시는 받을 수 있는 입력을 좁힌다.
  - `strategy_type`은 `"indicator"`로 고정한다. 요청 본문의 값은 무시한다.
  - `universe`는 `backend/quant/data/universe.py:UNIVERSE`의 부분집합만 허용하고, 1개 이상이어야 한다.
  - `position_size_pct`는 0보다 크고 0.05 이하로 제한한다(리스크 규칙 "종목당 최대 5%").
  - `stop_loss_pct`는 0.01 이상 0.15 이하로 제한한다(기본 0.07).
  - `name`은 1~100자로 제한한다(`StrategyRun.name`은 `String(100)`).
  - 이 밖의 키는 거부한다(모르는 키를 조용히 넘기지 않는다).
  - 검증은 pydantic 모델(`api/schemas.py`)로 한다.
- **동시 실행**: 워커는 실행마다 자기 트래커를 만든다. 실행 두 개가 같은 계좌에서 같은 종목을 각자 사고팔 수 있다. 그래서 **활성 실행은 1개만 허용**한다.
  - **강제하는 곳은 `kis-api` `start_strategy`다.** 프록시의 사전 확인으로는 막을 수 없다. `GET /api/strategies`는 최근 50개만 돌려줘 오래된 활성 실행이 빠질 수 있고, 확인과 시작 사이에 동시 요청이 둘 다 통과할 수 있기 때문이다(CodeRabbit 지적).
  - `start_strategy`는 같은 트랜잭션 안에서 직렬화한 뒤(Postgres `pg_advisory_xact_lock` 또는 `is_active`에 대한 부분 유니크 인덱스) 활성 행이 있으면 409를 돌려준다. 인덱스로 할 경우 `strategy_runs`는 Alembic 관리 테이블이므로 마이그레이션이 필요하다. 어느 방식으로 할지는 구현 때 정한다.
  - 운영자가 `kis-api`를 직접 부르는 경로에도 같은 제한이 걸린다. 의도한 동작이다.
  - **"점유 중"은 `is_active`만으로 판정하지 않는다.** `kis-api` 중지(`stop_strategy`)는 `is_active=False`만 쓴다. 워커는 세션 스레드가 실제로 끝날 때 `stopped_at`을 쓴다(`WorkerSession._mark_stopped`). 그래서 중지 직후 시작하면, 옛 실행이 아직 주문을 정리하는 동안 새 실행이 뜰 수 있다. 판정 조건은 `is_active OR stopped_at IS NULL`로 한다(CodeRabbit 보안 요약).
    - 부작용: 워커가 죽어 `stopped_at`을 쓰지 못한 행은 계속 점유 중으로 남는다. 이 상태는 fail-closed다(새 시작이 막힐 뿐 주문은 나가지 않는다).
    - 해제 절차: 워커가 그 실행을 돌리고 있지 않음을 로그로 확인한 뒤, 운영자가 그 행의 `stopped_at`을 채운다. 이 절차를 문서에 적고, 거부 응답에 막고 있는 `run_id`를 담는다.
  - **시작 요청은 재시도하지 않는다.** 응답을 잃어버렸을 때 프록시나 화면이 다시 보내지 않는다. 다시 보내더라도 위의 원자적 강제가 409로 막는다. 화면은 목록을 다시 읽어 상태를 확인한다.
  - 프록시의 확인(`is_active`)은 빠른 거부와 화면 표시용으로만 남긴다.
- **사용자 전략 시작(`/api/strategies/start`)은 그대로 거부한다**(A 유지). 응답 문구에 "운영자는 운영 전략 화면을 쓰라"는 안내를 덧붙일지는 구현 때 정한다. 운영자가 아닌 사용자에게 운영자 기능이 있다는 사실을 알릴 필요는 없다.

### 2단계 — 앱 화면 (웹·모바일)

- "운영 전략" 화면을 하나 만든다.
  - 활성 실행과 4주 시계(시작일 + 28일 = 실전 전환 가능일)를 보여준다.
  - 시작 폼은 유니버스 체크박스, 종목당 비중, 손절을 받는다.
  - 중지 버튼에는 확인 대화상자를 둔다.
  - 시작 확인 대화상자에 다음을 명시한다: "운영 계좌(`.env`)로 실제 주문이 나갑니다(모의/실전은 `KIS_ENV`)."
- 메뉴 노출: 서버가 `is_operator(current_user)`의 결과를 **사용자 정보를 돌려주는 모든 응답**에 넣는다. 지금 그런 응답은 `GET /api/auth/info`(`api/routers/auth.py:get_info`)와 `/api/users/profile`의 조회·수정(`api/routers/users.py`) 두 곳이다. 클라이언트는 이 응답을 그대로 스토어에 넣는다. 한 곳이라도 값을 빠뜨리면 메뉴가 사라지거나 남는다. 클라이언트에서 운영자 여부를 추론하지 않는다. 서버가 요청마다 다시 확인하므로 이 값은 보안 경계가 아니라 표시용이다.
- 스토어: `frontend/src/stores/`와 `mobile/src/stores/`는 동일해야 한다(`tests/integration/test_frontend_store_parity.py`). 새 상태는 기존 스토어에 넣거나 새 모듈을 양쪽에 똑같이 추가한다.
- 문구는 5개 로케일 × 2개 앱에 넣는다.

### 바꾸지 않는 것

- 워커(`backend/worker/runner.py`), `backend/execution/*`, 하우스 전략 로직.
- `kis-api`는 바꾸지 않는다. 예외는 `start_strategy`의 활성 실행 1개 강제 하나다.
- `LivePromotionGuard`의 나머지 관문(F2는 #206에서 따로 고쳤다).
- 킬스위치, 일손실·MDD, `SAFE_MODE`. 앱에서 시작한 실행도 이 장치들의 적용을 그대로 받는다.
- 사용자 전략의 백테스트(레거시 `strategy/indicator_strategy.py`).

## 백테스트와 라이브의 관계

- 하우스 전략의 백테스트 쌍은 이미 있다: `backend/quant/backtest/engine.py:BacktestEngine.run_from_fusion`이 같은 `default_fusion()`으로 신호를 낸다.
- 앱의 백테스트 화면은 사용자 조건 전략(레거시)만 돌린다. 운영 전략 화면에 하우스 전략 백테스트를 붙일지는 B1 이후 선택 사항이다.
- B1은 "사용자가 백테스트한 전략과 다른 전략이 실행된다"는 문제를 만들지 않는다. 앱이 시작하는 것은 이름부터 "운영 전략"이고, 사용자 전략과 섞지 않기 때문이다.

## 위험

- **앱 버튼 하나가 운영 계좌에 실제 주문을 낸다.** 모의 계좌라도 마찬가지다. 완화책은 네 가지다.
  - 운영자 id 허용 목록(fail-closed)
  - 활성 실행 1개 제한
  - 입력 범위 제한
  - 확인 대화상자
- **키 노출 면적은 늘지 않는다.** `KIS_API_KEY`는 이미 비상청산 때문에 `api` 프로세스에 있다. 브라우저에는 가지 않는다.
- **`kis-api` 시작 레이트 리밋(분당 5회)이 앱 요청에도 적용된다.** 429는 그대로 전달한다.
- **F1을 건너뛰면** 운영자 제어 세 개가 이메일 판별에 기대게 된다. 0단계를 먼저 하는 이유다.

## 결정 (2026-10-03)

1. **B1으로 좁힌다.** B2(사용자 조건 라이브 매매)는 하지 않는다.
2. **0단계를 먼저 하고, compose `api` 변경을 승인했다.** → PR #205.
3. **F2(4주 관문 강화)는 바로 한다.** B1보다 먼저, 별도 PR로.
4. **`WS_OPERATOR_USER_IDS`를 `OPERATOR_USER_IDS`로 합친다.** compose `kis-ws`도 바꾼다. → PR #205에 포함.

## 단계별 PR과 검증

| PR | 범위 | 검증 |
|---|---|---|
| 1 (#205) | 0단계: `backend/security/operators.py`, 비상청산·킬스위치 판별 교체, 옛 변수 제거 + 기동 경고, compose `api` env | 운영자 id만 통과, 목록이 비면 전원 거부, 같은 이메일(대소문자만 다른 주소 포함)의 다른 id 거부(#202와 같은 회귀 테스트), 옛 변수만 있으면 경고 + 거부, compose 선언 정적 검사 |
| 2 | 1단계: `api/routers/operator.py` + `kis-api` `start_strategy` 활성 실행 1개 강제 + 사용자 정보 응답의 `is_operator` | 비운영자 거부(상위 호출 0회), 입력 제한(script·범위 밖·모르는 키·유니버스 밖 종목), 키가 응답·로그에 없음, 429 전달(`_admin_post` 바꿔치기로 네트워크 없이). `kis-api`: 점유 중(`is_active` 또는 `stopped_at` 없음)이면 409와 `run_id`(50개보다 오래된 행, 중지 요청 후 워커 종료 전 행 포함), 동시 시작 두 건 중 하나만 성공(Postgres). `is_operator`가 info·profile(조회·수정) 응답 모두에 있음 |
| 3 | 2단계: 화면·스토어·로케일 | 빌드, 스토어 동일성 가드, playwright로 운영자/비운영자 메뉴 노출 확인 |
| (별도, #206) | F2 4주 관문 | 짧게 돈 행은 불통과, 활성 28일 행은 통과, 닫힌 28일 행은 통과 |

각 PR은 기존 흐름을 따른다: 전체 스위트(kst_noon·kst_night·nosock) + Postgres 세트 + API 단독 venv, `/code-review`, CodeRabbit.

## 근거

| 내용 | 위치 |
|---|---|
| 워커 지표 전략이 읽는 설정 | `backend/strategy/indicator/strategy.py`: `__init__`(`universe`, `position_size_pct`), `_scan_and_trade`(`default_fusion`), `_check_exit`(`stop_loss_pct`) |
| 하우스 신호 | `backend/quant/signals/fusion.py:default_fusion` |
| 하우스 백테스트 | `backend/quant/backtest/engine.py:run_from_fusion` |
| 운영자 시작·중지·조회 | `backend/api/server.py`: `list_strategies`, `start_strategy`, `stop_strategy` |
| 워커 전략 생성 | `backend/worker/runner.py:_build_strategy` |
| 프록시 선례 | `api/routers/quick_trade.py`: `emergency_flatten`, `_flatten_authorized`, `_admin_post` |
| 킬스위치 해제 허용 목록 | `api/routers/risk.py`(`KILL_SWITCH_ADMINS`) |
| 사용자 id 허용 목록 선례 | `backend/websocket/server.py:_operator_user_ids` |
| 4주 관문 | `backend/worker/promotion_guard.py:_check_paper_run` |
| 유니버스 | `backend/quant/data/universe.py:UNIVERSE` |
