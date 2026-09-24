# KIS Trading Platform — 인수인계 문서 (Claude 새 세션용)

> 이 문서를 읽으면 이전 대화 없이도 프로젝트 전체를 파악하고 바로 이어서 작업할 수 있다.

---

## 프로젝트 한 줄 요약

한국투자증권(KIS) + 키움증권 전용 자동매매 플랫폼.
**모바일 앱**(Vue 3 + Capacitor) + **백엔드 봇** (Python + Docker) 구조.
운용 자금 200만원, 모의투자 4주 검증 후 실전 전환.

---

## 프로젝트 진행 현황 (2026-09-24 기준, main `fe128e8` = PR #169)

> **이 섹션이 최신 상태의 단일 진실 공급원(SoT).** 아래 "다음 작업 목록(Stage 1~9)"은 초기 설계 로드맵으로,
> 대부분 이미 구현 완료됐다. 실제 진행은 `AUDIT.md` → `ROADMAP.md` 기반 하드닝 트랙으로 이어지고 있다.

**PR #79 (TASK 4-1C 실패 시나리오 통합테스트, 2026-06-16 머지) 이후 머지된 작업:**

| 영역 | PR | 내용 |
|---|---|---|
| DB/인프라 | #85, #86 | Postgres를 정규 CI DB로 확정 + Alembic 마이그레이션·회귀 스위트 (P1-05B) |
| 배포 안전 | #88 | 배포를 그린 테스트에 게이팅 + 배포 후 실제 헬스 프로브 (R-D) |
| 코퍼레이트 액션 | #89~#97 | 배당·분할·병합 처리: 감사→설계→구현→검증. `CorporateActionService` 라이브 런타임 통합 완료 (P2-01x, P2-02x) |
| 페이퍼 트레이딩 | #99, #102 | 페이퍼 트레이딩 하네스 + E2E 검증 스위트 (P3-01A/B) |
| 주문 실행 런타임 | #109 | 브로커 터미널 이벤트(취소/거부/만료)가 런타임에 반영 (P3-02B) |
| 보안 하드닝 | #110, #114 | CodeQL 알림 다수 수정, 커밋된 안드로이드 서명키 제거, RestrictedPython 업그레이드 |
| CI 품질 | #100, #101, #115 | Codacy SARIF/이진자산 예외 처리, `_reset_last_run()` 리팩터 |

**#119~#156에서 머지된 작업 (위 표 이후):**

| 영역 | PR | 내용 |
|---|---|---|
| 런타임 재조정 | #119 | P3-02C-B poller self-heal + `reconciler→resync()` 라우팅. **(#116은 미머지 종료, #119가 대체)** |
| 리스크 게이트 | #145, #146, #148 | EmergencyFlatten 가격 없으면 fail-closed / halt는 신규 위험만 차단(청산은 허용) / 보유≠매도가능 — 브로커 주문가능수량 강제 |
| 거래 UI 안전 | #149, #150 | KR을 UI에서 도달 가능하게 + 브로커 장애를 0으로 보고하지 않기 / 금액 아닌 주수·지정가 전용·취소·청산 도달성 |
| 종목 선택 | #151 | 심볼 추상화 계층(`raw`→`provider`→`backend`) + 정규 거래소 어휘 |
| 크립토 잔재 정리 | #152, #155 | 백엔드 없는 화면 9개 제거(32→23), 죽은 API 호출 13→0 / 자격증명 화면 KIS 문구 10개 로케일 |
| 주문 정확성 | #153 | **NYSE 종목 주문 불가 수정** — 거래소 코드를 심볼에서 유도. 주문(`NASD`)과 시세(`NAS`) 코드 체계 분리 |
| KIS 클라이언트 | #154, #156 | 레이트 리밋을 앱키 단위로(요청마다 새 리미터라 무효였음) / **주문은 1회 전송·재전송 금지**(중복 주문 차단, AUDIT R-01) |
| 종료 안전 | #157, #159 | 킬스위치 해제가 워커 재시작 없이 유지됨 / **P0-10 워커 graceful shutdown** — SIGTERM을 받아도 죽기만 하던 문제. `install_signal_handlers` + 종료 예산 8초 |
| 킬스위치 소유권 | #163 | **#158 근본 수정** — 플래그를 "마지막에 쓴 쪽"이 아니라 "의도가 있는 쪽"이 소유한다(`_ks_epoch` 카운터). 운영자 해제도 워치독 halt도 더 이상 조용히 덮이지 않는다 |
| 거래일 날짜 키 | #165 | **#160·#167 수정** — `DailyRiskState.trade_date`를 `trading_day()`(KST) 하나로 통일. 컨테이너가 UTC라 **KST 00~09시** 아홉 시간 동안 읽는 쪽과 쓰는 쪽이 하루 어긋났다. 덤으로 미국 세션이 서울 자정을 가로지르는 문제까지(`trading_days_in_play()`) |
| 기동 중 종료 | #169 | **#161 수정** — 복구 4·5단계 브로커 조회를 `call_with_deadline`(데몬 스레드 + `should_abort` 폴링)으로 묶음. `ThreadPoolExecutor`의 `with` 블록은 타임아웃 뒤에도 호출이 끝날 때까지 기다렸다. 기동 중 SIGTERM은 브로커 실패가 아니라 `기동 중 종료 요청 — 복구 중단`으로 기록 |

**열린 PR 0건.** 다음 작업은 `origin/main`에서 새로 분기하면 된다.

**아키텍처 실제 현황 (초기 로드맵 대비 완료분):**
- `backend/brokers/`: `base`·`models`·`kis`·`kiwoom`·`capabilities`·`router`·`paper_broker`·`semantic_mapper`·`validator` 모두 존재 (Stage 1 완료)
- `backend/execution/`: `order_poller`(폴링·서킷브레이커)·`reconciler`(브로커-우선 재조정)·주문 상태머신
- `backend/quant/`: `indicators`·`signals`·`risk/engine`·`live`·`analysis`·`data` 퀀트 엔진 (`QUANT_ENGINE.md` 참고)
- 프로세스 분리: `docker-compose`에서 레거시 `kis-bot` 비활성화 → `kis-api`·`kis-worker`·`kis-ws`로 분리 (P1-08/P5-04)
- `kiwoom_adapter/`: `client`·`market_data`·`orders`·`portfolio` 모듈 존재 (더 이상 빈 스텁 아님)
- 핵심 문서: `AUDIT.md`·`ROADMAP.md`(P0~P6, 1105줄)·`PHILOSOPHY.md`·`BROKER_SEMANTICS.md`·`QUANT_ENGINE.md` + `docs/` 30여 개 설계·감사 문서

---

## 운영 환경

- **서버**: AWS (Ubuntu 22.04), Docker Compose로 전체 스택 실행
- **배포**: GitHub Actions → SSH → AWS 자동 배포 (`.github/workflows/deploy.yml`)
- **모바일**: Vue 3 + Capacitor 6 (Android/iOS 앱)
- **알림**: 텔레그램 봇

---

## 현재 구현 완료 목록 ✅

### 백엔드 봇 (`kis_adapter/`, `strategy/`, `bot/`)
| 파일 | 내용 |
|---|---|
| `kis_adapter/auth.py` | KIS 토큰 발급·갱신(24h), Redis 캐시, Hashkey 발급 |
| `kis_adapter/client.py` | Rate limit(앱키 단위, 모의 5/s·실전 15/s). **GET만 재시도 3회**(전송 실패·429·5xx·`EGW00201`) — **주문 POST는 1회 전송, 절대 재전송 안 함**(중복 주문 방지, 불확정은 `QT_RESERVED`로 복구) |
| `kis_adapter/orders.py` | 미국/한국 매수·매도·취소 (TR_ID 모의/실전 자동 전환) |
| `kis_adapter/market_data.py` | 미국/한국 현재가 조회 |
| `kis_adapter/portfolio.py` | 미국/한국 잔고·포지션 조회 |
| `strategy/signals.py` | `MultiTimeframeSignals`: 일봉+주봉+레짐 탐지+12-1모멘텀+섹터분산+ATR사이징 |
| `strategy/optimizer.py` | PyPortfolioOpt 샤프 최대화 + ATR 기반 포지션 사이징 |
| `strategy/risk.py` | 일손실 3%, MDD 15%, 손절 7%, peak equity 파일 영속 저장 |
| `bot/main.py` | TradingEngine: 실시간 환율, 실제 PnL, 세션별 stop-loss 체크 |
| `bot/scheduler.py` | APScheduler: 09:05 KST(한국) / 22:35 KST(미국) / 월간 리밸런싱 |
| `bot/notifier.py` | 텔레그램 매수·매도·오류·긴급 알림 |
| `docker-compose.yml` | postgres + redis + quantdinger-frontend + quantdinger-backend + kis-bot |
| `scripts/setup_oracle_cloud.sh` | AWS/Oracle 최초 설치 스크립트 (Docker + QuantDinger 클론) |
| `scripts/test_connection.py` | KIS API 연결 검증 스크립트 |
| `scripts/test_paper_trade.py` | 드라이런 스크립트 (DRY_RUN=true) |

### GitHub Actions
- `push to main` → SSH → `docker compose up -d --build`
- `workflow_dispatch` (수동 배포 트리거) 지원

---

## 현재 알려진 버그 / 주의사항 ⚠️

1. **QuantDinger 백엔드 빌드**: `docker-compose.yml`에서 `./quantdinger/backend_api_python`을 빌드하므로 서버에 먼저 `git clone https://github.com/brokermr810/QuantDinger.git ./quantdinger` 필요
2. **키움증권**: `kiwoom_adapter/`(client·market_data·orders·portfolio) + `backend/brokers/kiwoom.py` 존재하나 완성도·실거래 검증 미완. 세부 이슈는 `docs/KIWOOM_AUDIT_REPORT.md`·`ROADMAP.md`(P1-01 등) 참고
3. **모의→실전**: `.env`에서 `KIS_ENV=paper` → `KIS_ENV=real`만 변경. **4주 모의 전 절대 금지**
   — 강제 장치는 `backend/worker/promotion_guard.py`의 `LivePromotionGuard`다. 6개 관문 중
   **"4주 모의투자 완료"**는 `strategy_runs`에 `started_at <= now-28d`인 행이 있는지로 판정한다.
   **코드로 줄일 수 없는 유일한 항목이고, 실전 전환일 = 모의투자를 켜는 날 + 28일로 고정된다.**
4. ⚠️ **SPY·XL\* 섹터 ETF의 거래소 코드 미검증**: 이들은 NYSE Arca 상장인데 KIS 주문 코드에
   ARCA가 없다. KIS가 Arca를 NYSE로 두는지 AMEX로 두는지 **확인하지 못했다**(종목 마스터
   다운로드가 개발 환경 egress 정책에 막힘). 현재 매핑은 NYSE. **모의투자에서 이 종목들이
   거부되면 여기부터 의심할 것.** `backend/quant/data/universe.py` 주석 참고
5. **`EXCD_MAP`에 없는 미국 티커는 `NASD`로 폴백**한다. 검색 폴백이 임의 티커를 피커에 넘기므로
   #153이 고친 오라우팅이 그 경로로 재현된다. 제대로 닫으려면 종목 마스터가 필요
6. **`tr_cont` 페이지네이션 미구현 (7곳)**: `CTX_AREA_NK100/NK200`을 전부 `""`로 보내고 응답의
   연속 키를 읽지 않아, 2페이지 이상이면 **조용히 1페이지만** 돌아온다
7. **미구현 P0 (ROADMAP 참고)**: `P0-03` `EmergencyFlattenManager`
   `dry_run` 기본값이 아직 `True`(`backend/worker/emergency.py:57`) · `P0-11`은 기동 시점이 아니라
   `crypto.py:_get_fernet()` 최초 호출 시점에만 키를 검증
8. **킬스위치 해제는 유지되지만, 그것만으로 매매가 재개되지는 않는다.**
   이슈 #158(`_write_db`가 메모리 값으로 덮어쓰던 문제)은 해결됐다 — 트래커가 행을 다시
   읽고 **자기 판단이 있을 때만** 플래그를 주장한다. "워커 정지 → 해제 → 기동" 절차는
   더 이상 필요 없다. 다만 **두 가지가 남는다**: (a) 워커가 기동 시 킬스위치를 캐시해
   `SAFE_MODE`를 잠그므로 **재시작이 필요**하다(P0-12 남은 절반, P0-04 의존),
   (b) **위반 조건 자체가 유효하면** 다음 PnL 기록에서 `_evaluate()`가 다시 정지시킨다 —
   일손실·MDD 정지는 보통 그날 내내 조건이 유효하므로 **장중 재개는 이 API만으로는 안 된다**
9. **워커 종료 예산은 8초**(`_SHUTDOWN_BUDGET_SEC`). `docker-compose.yml`은 `kis-worker`에
   `stop_grace_period`를 지정하지 않아 도커 기본값 10초가 적용된다. 종료 단계를 늘리려면
   예산과 grace period를 함께 봐야 한다

---

## 다음 작업 목록 (초기 설계 로드맵 — 참고용) 🔜

> ⚠️ **이 Stage 1~9는 초기 설계안이며 대부분 이미 구현 완료됐다.** 최신 진행 상태는 위 "프로젝트 진행 현황"
> 섹션과 `ROADMAP.md`(P0~P6 하드닝 계획)를 단일 진실 공급원으로 삼을 것. 아래는 원래 아키텍처 의도를 남겨둔 기록이다.

### Stage 1 — 기반 안정화 (지금 당장 해야 함)

**1-A. `mobile/` 디렉토리에 QuantDinger-Mobile 복사**
```bash
git clone https://github.com/brokermr810/QuantDinger-Mobile.git mobile
rm -rf mobile/.git  # 서브모듈 아닌 직접 포함
```
- `mobile/capacitor.config.json`: `appId → com.kistrade.mobile`, `appName → KIS Trading`
- `mobile/src/config/index.js`: `DEFAULT_SERVER_URL = ''`

**1-B. `backend/brokers/base.py` — BrokerAdapter 추상 클래스**
```python
from abc import ABC, abstractmethod
class BrokerAdapter(ABC):
    @abstractmethod
    def get_balance(self) -> Balance: ...
    @abstractmethod
    def get_positions(self) -> list[Position]: ...
    @abstractmethod
    def place_order(self, symbol, side, qty, price, order_type) -> Order: ...
    @abstractmethod
    def cancel_order(self, order_id) -> bool: ...
    @abstractmethod
    def get_price(self, symbol) -> float: ...
```

**1-C. `backend/brokers/models.py` — 공통 데이터 모델**
```python
class OrderStatus(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL_FILLED = "partial_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"

@dataclass
class Order:
    id: str; symbol: str; side: str
    qty: int; price: float; status: OrderStatus
    filled_qty: int = 0; avg_fill_price: float = 0.0

@dataclass
class Position:
    symbol: str; qty: int; avg_price: float; market: str  # KR/US

@dataclass
class Balance:
    cash_krw: float; cash_usd: float; total_eval_krw: float
```

**1-D. `backend/brokers/kis.py`** — 기존 `kis_adapter/`를 BrokerAdapter로 래핑
**1-E. `backend/brokers/kiwoom.py`** — 스텁 (NotImplementedError)
**1-F. `backend/database/models.py`** — SQLAlchemy ORM:
```
trades, orders, fills, strategy_runs, equity_snapshots, positions
```

---

### Stage 2 — 주문 상태머신

**`backend/execution/order_machine.py`**
- 상태 전환: PENDING→SUBMITTED→PARTIAL_FILLED→FILLED / CANCELED / REJECTED
- `process_fill_event()`: 체결 이벤트 처리

**`backend/execution/position_tracker.py`**
- 체결 → 포지션 업데이트
- 재시작 시 DB에서 복원 (`restore_positions()`)
- 중복 주문 방지

---

### Stage 3 — 이벤트 기반 전략 엔진

**`backend/strategy/base.py`** — StrategyBase
```python
class StrategyBase:
    def on_start(self): ...
    def on_bar(self, bar: dict): ...
    def on_fill(self, fill: Fill): ...
    def on_market_open(self): ...
    def on_market_close(self): ...
    def on_stop(self): ...
    def buy(self, symbol, qty, price=None): ...
    def sell(self, symbol, qty, price=None): ...
```

**`backend/strategy/runtime/simulator.py`** — SimulatedBroker
- 백테스트·라이브가 **동일한 BrokerAdapter 인터페이스** 사용 (괴리 없음)
- 수수료: KIS 0.015%

---

### Stage 4 — API / Worker 프로세스 분리

```
python -m backend.api.server    # Flask API (포트 5000)
python -m backend.worker.runner # 전략 실행기 (백그라운드)
```
- 통신: Redis Pub/Sub (`strategy:start`, `strategy:stop`)
- Worker 재시작 시 `strategy_runs` 테이블에서 활성 전략 자동 복원

---

### Stage 5 — IndicatorStrategy UI

**`backend/strategy/indicator/backtest.py`** — backtesting.py 래퍼
- 입력: 조건 JSON + 기간 + 종목
- 출력: `{ sharpe, mdd, win_rate, cagr, equity_curve[], trades[] }`

**`mobile/src/views/trading/BotFromIndicator.vue`** 확장
- 스텝1: 인디케이터 선택·파라미터
- 스텝2: AND/OR 신호 조건 빌더
- 스텝3: 백테스트 결과 (lightweight-charts)
- 스텝4: 배포 (종목·브로커·스케줄)

---

### Stage 6 — ScriptStrategy (샌드박싱 필수)

**`backend/strategy/script/sandbox.py`**
- RestrictedPython + AST 검사 + 허용 노드 whitelist + timeout
- `import os; os.remove("/")` 같은 위험 코드 차단 필수

**`mobile/src/views/trading/CreateBot.vue`** 확장
- CodeMirror 6 편집기 (Python 하이라이팅)
- 기본 템플릿: `on_bar(self, bar)` → `self.buy()` / `self.sell()`

---

### Stage 7 — Mobile 브로커 UI 교체

**`mobile/src/constants/exchanges.js`**
- 11개 암호화폐 거래소 전부 삭제
- KIS + 키움 2개로 교체

**`mobile/src/views/profile/CredentialForm.vue`**
- KIS: 앱키·시크릿·계좌번호·HTS ID·모의/실전 토글
- 키움: 앱키·시크릿·계좌번호

**`mobile/src/stores/`** — Pinia 스토어 분리
```
auth.js / broker.js / strategy.js / market.js / websocket.js
```

**제거 라우트**: `profile/referral`, `profile/credits`, `market/*`

---

### Stage 8 — 운영 안정화

- 전략 재시작 복구: `strategy_runs` 테이블에서 활성 전략 자동 복원
- 스케줄러: 09:05 KST(한국) / 22:35 KST(미국) / 00:01 리셋 / 23:50 결산
- 모든 체결·주문 이벤트 Postgres에 영속 저장

---

### Stage 9 — AI 어드바이저 (선택)

**`backend/strategy/ai/advisor.py`** — TradingAgents 경량 래퍼
- Ollama 로컬 LLM (무료) 우선
- LLM은 설명·리스크 요약만. **매매 결정은 deterministic 전략이 담당**

---

## 전체 아키텍처

```
mobile/  (Vue 3 + Capacitor 6 + Vant 4)
    ↓ REST + WebSocket
backend/
├── api/            Flask API (포트 5000)
├── worker/         전략 실행 프로세스 (API와 분리)
├── scheduler/      APScheduler
├── brokers/
│   ├── base.py     BrokerAdapter ABC
│   ├── kis.py      KIS 구현 (기존 kis_adapter/ 래핑)
│   ├── kiwoom.py   키움 스텁
│   └── models.py   Order·Position·Fill·Balance
├── strategy/
│   ├── base.py     StrategyBase 이벤트 메서드
│   ├── indicator/  IndicatorStrategy + backtesting.py
│   ├── script/     ScriptStrategy + Sandbox
│   ├── runtime/    SimulatedBroker (백테스트·라이브 동일 인터페이스)
│   └── ai/         TradingAgents 래퍼
├── execution/      주문 상태머신 + PositionTracker
├── database/       SQLAlchemy 모델
└── websocket/      실시간 push
```

---

## 채택한 라이브러리

| 역할 | 라이브러리 | 라이선스 |
|---|---|---|
| 빠른 백테스트 | backtesting.py | AGPL (내부 사용 자유) |
| 포트폴리오 최적화 | PyPortfolioOpt | MIT |
| 기술지표 | pandas-ta | MIT |
| LLM 신호 보조 | TradingAgents | Apache-2.0 |
| 유니버스 메타 | FinanceDatabase | MIT |
| 모바일 UI | Vant 4 + Vue 3 + Capacitor 6 | MIT |

**버린 것**: QuantConnect Lean (C# 복잡도 과다), nautilus_trader (Rust), blankly (유지보수 중단)

---

## 환경변수 (.env)

`.env.example` 참고.

```
KIS_APP_KEY=           # 한국투자증권 앱키
KIS_APP_SECRET=        # 시크릿
KIS_ACCOUNT_NO=        # 계좌번호 12자리
KIS_ENV=paper          # paper(모의) 또는 real(실전)
KIS_HTS_ID=            # HTS ID

TELEGRAM_TOKEN=        # 텔레그램 봇 토큰
TELEGRAM_CHAT_ID=      # 채팅 ID

QUANTDINGER_SECRET_KEY=   # python -c "import secrets; print(secrets.token_hex(32))"
QUANTDINGER_ADMIN_USER=admin
QUANTDINGER_ADMIN_PASSWORD=

POSTGRES_PASSWORD=         # 강력한 랜덤 비밀번호 입력
DAILY_LOSS_LIMIT_PCT=0.03
MDD_LIMIT_PCT=0.15
STOP_LOSS_PCT=0.07
```

---

## 실행 방법 (서버에서)

```bash
# 최초 1회
git clone https://github.com/rata1223/claude-code-and-vive-coding-work.git ~/kis-trading
git clone https://github.com/brokermr810/QuantDinger.git ~/kis-trading/quantdinger
cd ~/kis-trading
cp .env.example .env && nano .env

# 서비스 시작
docker compose up -d --build
docker compose logs -f kis-bot

# 연결 테스트 (KIS 자격증명 입력 후)
docker exec kis-bot python scripts/test_connection.py
```

---

## KIS API 핵심 정보

| 구분 | 엔드포인트 |
|---|---|
| 모의 | https://openapivts.koreainvestment.com:9443 |
| 실전 | https://openapi.koreainvestment.com:9443 |

| 기능 | 실전 TR_ID | 모의 TR_ID |
|---|---|---|
| 미국 매수 | TTTT1002U | JTTT1002U |
| 미국 매도 | TTTT1006U | JTTT1006U |
| 미국 잔고 | TTTS3012R | VTTS3012R |
| 한국 매수 | TTTC0802U | VTTC0802U |
| 한국 매도 | TTTC0801U | VTTC0801U |
| 한국 잔고 | TTTC8434R | VTTC8434R |

- 토큰 유효: 24시간, 만료 1시간 전 자동 갱신
- POST 주문에 Hashkey 필수
- Rate limit: 모의 5/s, 실전 15/s

---

## 매매 유니버스

```python
US_ETF   = ["SPY", "QQQ", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE"]
US_LARGE = ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "JPM", "V"]
KR_ETF   = ["069500", "360750", "091160"]  # KODEX200, TIGER S&P500, KODEX반도체
```

## 매매 전략 로직

**매수**: 4조건 모두 충족
1. 일봉 종가 > 200일 SMA
2. 3개월 수익률 > 0
3. RSI(14) < 70
4. 거래량 > 20일 평균거래량

추가 필터 (MultiTimeframeSignals):
- 주봉 20주 SMA 위 (중기 추세 확인)
- SPY 실현변동성 < 25% (시장 레짐 정상)
- 12-1 모멘텀 팩터 정렬 (강한 종목 우선)
- 동일 섹터 2개 초과 차단

**매도**: 하나라도 해당
- 200일 SMA 하향 돌파
- RSI > 80
- 진입가 대비 -7% 손절

**리스크 규칙**:
- 종목당 최대 5%
- 일손실 3% → 당일 매매 중단
- MDD 15% → 전량 청산 + 긴급 알림

---

## GitHub 저장소 / 브랜치

- **메인**: `rata1223/claude-code-and-vive-coding-work` (기본 브랜치 `main`)
- 초기 구축: PR #1 (`claude/vibrant-davinci-skmpx`), PR #2 (`claude/vibrant-davinci-skmpx-fixes`)
- **PR #79** (`claude/update-MW7LQ`): 실패 시나리오 통합테스트 (TASK 4-1C) — **머지됨** (2026-06-16)
- 하드닝 트랙 PR #85~#156: 위 "프로젝트 진행 현황" 표 참조 — **모두 머지됨**
- **PR #116**은 미머지 종료(2026-07-05). 같은 작업을 **#119**가 대체 구현해 머지했다
- **현재 열린 PR 0건.** main = `fe128e8` (PR #169)

> 작업 방식: 기능별 새 브랜치에서 작업 → `main`으로 드래프트 PR → CodeRabbit/CodeQL 리뷰 → 머지.
> 브랜치 보호 룰셋(PR 필수 + 코드 스캐닝)이 적용돼 `main` 직접 푸시 불가.
> CodeRabbit은 드래프트 PR을 자동 리뷰하지 않는다 — `@coderabbitai review` 코멘트로 수동 요청할 것.
> Codacy 스캔은 비필수(required 아님)이고 10분 넘게 걸릴 때가 있다. 나머지 8개가 그린이면 머지 가능.

```bash
# 새 세션에서 최신 상태 받기
git fetch origin main
git checkout -B <새-작업-브랜치> origin/main
```

### 다음 작업 (우선순위)

현재 방침: **배포·모의투자 시계는 나중, 하드닝을 계속한다.**

1. **`P0-03`은 로드맵 설명이 틀렸다 — 착수 전에 읽을 것.** 로드맵은
   "`dry_run` 기본값이 `True`이고 한 번도 오버라이드되지 않는다"고 하지만 **기본값을
   뒤집는 것은 무동작이다**: 프로덕션 유일 생성 지점 `backend/api/server.py:340-345`가 이미
   `ENABLE_LIVE_TRADING`에서 `dry_run`을 명시로 넘기고, 테스트 생성 지점도 전부 명시한다.
   **진짜 공백은 MDD 킬스위치가 flatten을 아예 호출하지 않는다는 것**이다 —
   `_fire_kill_switch_alert`(`backend/quant/risk/engine.py`)는 SAFE_MODE 차단·텔레그램·
   WebSocket 세 가지만 하고 `EmergencyFlattenManager`를 import조차 하지 않는다.
   그 배선은 **P0-04 의존**이다. `backend/worker/emergency.py`의 docstring이 MDD를
   트리거로 광고하는 것도 사실과 다르니 함께 고칠 것
2. `P0-12` — ⚠️ **부분 완료**. 위 알려진 이슈 8번 참고. 남은 건 (a) 인메모리 `SAFE_MODE`
   무재시작 해제(P0-04 의존), (b) 위반 조건이 유효할 때의 재개 의미 정의(리스크 정책 판단)
3. `P2-01` `order_events` append-only 테이블 (큰 변경)
4. `P3-04` Pinia 스토어 분리 — `frontend/src/stores/`가 아직 `index.js` 하나 (큰 변경)
5. 열린 이슈:
   - **#170** 기동 시 reconcile(복구 6단계 + `StrategyWorker.run()`의 시작 조정)이 무한정 —
     #161의 남은 절반. **작업 중**: `run_reconcile_bounded` + `StopGatedBroker`(읽기만 게이팅,
     `cancel_order`는 게이팅 안 함 — 취소와 커밋이 쪼개지지 않게). `backend/execution/` 무수정
   - **#164** `kill_switch` 컬럼의 프로세스 간 lost update (행 잠금 없음 — `version` 컬럼
     또는 `SELECT … FOR UPDATE`를 세 writer 전부에 일관 적용해야 한다. 스키마 변경 동반)
   - **#166** `daily_pnl`의 날 경계를 어디에 둘 것인가 — 미국 세션이 서울 자정을 가로지르므로
     한 야간 세션의 손익이 두 거래일로 쪼개진다. 리스크 정책 판단
   - **#168** `broker_order_id` 단독 조회 (`runner`·`persistence`·`recovery`) — KIS ODNO는
     거래일 안에서만 유일하다. PR #165에서 두 번 시도하고 **되돌렸다**: 날짜만으로는 판별이
     안 되고 "아직 열린 주문인가"에 기반한 동일성이 필요하다
   - ~~#161~~ 완료(PR #169) · ~~#160~~ 완료(PR #165) · ~~#158~~ 완료(PR #163) · ~~#167~~ 완료(PR #165)

> ~~`P0-10` SIGTERM 핸들러~~ — 완료(PR #159, `install_signal_handlers` +
> `StrategyWorker.shutdown`, 종료 예산 8초).

> `ROADMAP.md`의 각 항목에 `✅ DONE` / `❌ OPEN` 표기와 **근거 파일:행**을 달아두는 작업이
> 진행 중이다(P6-05). 표기가 없는 항목은 아직 검증되지 않은 것이지 미완이라는 뜻이 아니다.
