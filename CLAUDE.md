# KIS Trading Platform — 인수인계 문서 (Claude 새 세션용)

> 이 문서를 읽으면 이전 대화 없이도 프로젝트 전체를 파악하고 바로 이어서 작업할 수 있다.

---

## 프로젝트 한 줄 요약

한국투자증권(KIS) + 키움증권 전용 자동매매 플랫폼.
**모바일 앱**(Vue 3 + Capacitor) + **백엔드 봇** (Python + Docker) 구조.
운용 자금 200만원, 모의투자 4주 검증 후 실전 전환.

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
| `kis_adapter/client.py` | Rate limit(모의 5/s, 실전 15/s), 재시도 3회 |
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
2. **키움증권**: 아직 스텁 없음. 코드에 없으니 구현 필요
3. **모의→실전**: `.env`에서 `KIS_ENV=paper` → `KIS_ENV=real`만 변경. **4주 모의 전 절대 금지**

---

## 다음 작업 목록 (우선순위 순) 🔜

아래 Stage 순서대로 구현하면 된다. **Stage 1부터 시작.**

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

POSTGRES_PASSWORD=quantdinger123
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

- **메인**: `rata1223/claude-code-and-vive-coding-work`
- **PR #1**: 초기 시스템 구축 (`claude/vibrant-davinci-skmpx`)
- **PR #2**: 버그 수정 + 전략 고도화 (`claude/vibrant-davinci-skmpx-fixes`)
- 새 작업: PR #2 브랜치에 계속 push

```bash
# 새 세션에서 작업 시작
git checkout claude/vibrant-davinci-skmpx-fixes
git pull origin claude/vibrant-davinci-skmpx-fixes
```
