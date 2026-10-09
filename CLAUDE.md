# KIS Trading Platform — 인수인계 문서 (Claude 새 세션용)

> 이 문서를 읽으면 이전 대화 없이도 프로젝트 전체를 파악하고 바로 이어서 작업할 수 있다.

---

## 프로젝트 한 줄 요약

한국투자증권(KIS) + 키움증권 전용 자동매매 플랫폼.
**모바일 앱**(Vue 3 + Capacitor) + **백엔드 봇** (Python + Docker) 구조.
운용 자금 200만원, 모의투자 4주 검증 후 실전 전환.

---

## 프로젝트 진행 현황 (2026-10-09 기준, main `072d6e0` = PR #221)

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
| 기동 중 종료 | #171 | **#170 수정** — 기동 시 reconcile 두 곳(복구 6단계 + `StrategyWorker._startup_reconcile`)을 `run_reconcile_bounded`로 유계화(전용 예산 `RECONCILE_STARTUP_TIMEOUT` 180초). 포기된 reconcile은 `StopGatedBroker`가 다음 브로커 **읽기**에서 멈춘다 — `cancel_order`는 게이팅하지 않아 취소와 그 커밋이 쪼개지지 않는다 |
| 주문 행 동일성 | #174 | **#168 수정** — KIS 주문번호는 **매일 리셋**되는데 워커가 번호 하나로 행을 찾아 옛 주문을 덮어쓰고 체결을 엉뚱한 주문에 붙였다. 날짜가 아니라 "아직 열린 주문인가"(+종목·매매구분)로 판정. 체결은 번호가 아니라 **종목 락이 잡혀 있을 때 확보한 PK**로 기록(CodeRabbit 지적). 죽은 모듈 `backend/worker/persistence.py` 삭제 |
| 체결 수량 | #175 | **#172 수정** — 체결 파이프라인이 DB `filled_qty`를 두 번 셌다(1단계 누적값 + 4단계 증분 → 10주에 16). 머신이 처리한 체결은 4단계에서 누적값을 *설정*하고, 머신이 모르는 주문만 증분을 더한다 |
| 리스크 행 잠금 | #177 | **#164 수정** — `DailyRiskState`를 두 프로세스의 writer 넷(트래커·해제 API·워치독×2·종료 체크포인트)이 잠금 없이 읽기→쓰기 해 나중 커밋이 앞 결정을 지웠다(해제가 새 halt를 지우는 fail-open 포함). 모든 쓰기가 `lock_risk_row(s)`(`SELECT … FOR UPDATE`, 생성은 `ON CONFLICT DO NOTHING`, 여러 날은 날짜순)를 거친다. 정적 가드 + Postgres 동시성 테스트 |
| MDD 비상청산 | #179 | **P0-03** — MDD 위반이 실제로 `EmergencyFlattenManager`를 호출한다(워커 훅, 위반당 1회, 주문 0건 실패는 경보+재시도, 부팅 복원 halt는 청산 안 함). `_evaluate`는 MDD를 먼저 본다(폭락일에 일손실 분기가 MDD를 가리던 문제). **자동 경로는 `MDD_AUTO_FLATTEN=true` 전까지 dry-run**(#178). 종료 중 폴러 드레인이 띄운 청산 스레드도 join. P0-04는 보류(단일 브로커) |
| DB 엔진 누수 | #180 | **#176 수정** — 워치독(장애마다)과 주기 조정(30분마다)이 `init_db_factory`로 엔진+`create_all`을 매번 새로 만들고 dispose하지 않았다. 워치독은 인스턴스 캐시(성공 시만), 주기 조정은 모듈 팩토리 재사용 |
| 총자산 신뢰도 | #181 | **#178 부분** — `Balance.equity_verified`(필드 누락·USD 현금 보유·라우터 한쪽 실패 시 False) + 누락 필드 **이름** 진단 로그. 자동 MDD 청산은 위반 판독과 청산 직전 새 판독이 **모두** 검증돼야 매도, 아니면 보류·경보·재무장. 숫자 자체는 그대로(USD 현금 가산은 이중 계산 위험) |
| 자격증명 키 | #183 | **P0-11** — 키가 없거나 형식이 틀리면 API 기동 실패(lifespan 맨 앞 `crypto.validate_key()`). DB 응답 후 백그라운드에서 저장 자격증명 최대 20건의 암호화 필드 5개를 시험 복호화해 불일치를 `N/M` CRITICAL로(건수만, 기동은 막지 않음 — 재입력이 복구 경로) |
| 자격증명 fail-closed | #184 | **#182** — 저장돼 있는데 현재 키로 **풀리지 않는** 필드는 `CredentialUnreadable`(`decrypt_required`/`kis_credential_fields`). 빈 값으로 KIS를 부르지 않고 "자격증명을 다시 입력하세요"를 돌려준다. `place_order`는 **예약 전에** 판정한다(예전엔 `broker_submit` 안에서 실패해 **보내지도 않은 주문이 RESERVED로 남았다**). NULL(선택 필드)은 기존처럼 허용 |
| 테스트 안정성 | #186 | **#185**(①) + #184 후속(②) — 간헐 실패 두 건을 원인 확정 후 테스트에서만 수정(skip 없음). ① 킬스위치 해제 테스트: `kill-switch-alert` 스레드의 감사 쓰기가 SQLite `StaticPool` 단일 연결에서 해제 API의 읽기와 섞여 halt가 안 보였다(프로브 9/200 → 스텁 시 0/200, 운영은 세션마다 연결이라 무관). ② #184의 재생 테스트: 서버 유도 키에 10초 버킷이 있어 경계를 넘으면 재생이 아니었다 → 명시 키 |
| 스크립트 샌드박스 | #187 | RestrictedPython 8.0→8.4(Dependabot high 2건). 레거시 `strategy/script_strategy.py`는 RestrictedPython을 쓰지 않고 사용자 코드를 그대로 실행하고 있었다 — `/api/strategies/backtest` 경로. 이제 `compile_restricted` + 가드 훅, `math`/`statistics`는 허용 함수만 담은 네임스페이스, `pandas`·`type` 제거. 부수 효과로 깨져 있던 템플릿 2개가 다시 동작 |
| API 의존성 | #190 | `pip-audit` 지적 전부 해소. fastapi 0.111→0.142.1 + starlette 0.37.2→1.7.0(권고 다수) + pydantic 2.7→2.13. `python-jose`→`PyJWT`(HS256만 사용 — ecdsa·rsa·pyasn1 제거). 라우트 목록 테스트는 `app.openapi()` 기준(새 FastAPI는 `app.routes`에 포함 라우터를 평탄화하지 않는다) |
| 스크립트 시간 제한 | #191 | **#188** — 스크립트 백테스트를 `spawn` 자식 프로세스에서 실행(`strategy/script_backtest.py`). 예산(`SCRIPT_BACKTEST_TIMEOUT_SEC`, 기본 30초) 초과 시 kill, 주소 공간 상한(`SCRIPT_BACKTEST_MEMORY_MB`, 기본 1024) + `RLIMIT_CPU` 백스톱. 무한 루프·긴 C 호출 모두 중단. 가격은 부모가 받아 넘기고, 사용자 코드는 API 프로세스에서 실행되지 않는다 |
| 대시보드 0 보고 | #192 | **#149를 대시보드에** — `/api/dashboard/summary`가 어떤 실패든 0을 돌려 장애·풀리지 않는 자격증명·한쪽 시장 실패·빈 계좌가 모두 "총 자산 0"이었다. 알 수 없는 값은 `None` + `portfolio_status`(`ok`/`partial`/`unavailable`/`no_credential`)·`portfolio_errors`, KR·US를 따로 조회·파싱(응답 모양이 틀려도 그 시장만 실패). `/pendingOrders` 실패는 빈 목록이 아니라 오류. 프론트 스토어가 API가 안 보내는 `total_equity`를 읽어 **성공해도 0**이던 것 수정(KRW·USD 따로, null은 `—`). **별건**: #151 이후 `SymbolPicker.data()`가 computed를 읽어 홈·퀵트레이드·봇 폼이 **마운트 중 TypeError** — prop 직접 읽기로 수정 |
| 백테스트 동시성 | #193 | 스크립트 백테스트 동시 실행 상한 — 프로세스당 `BoundedSemaphore`(`SCRIPT_BACKTEST_MAX_CONCURRENT`, 기본 2, uvicorn 워커 1개라 전역). 슬롯이 없으면 대기 없이 즉시 `ScriptBacktestBusy`(아무것도 시작 안 함). 슬롯은 자식 회수 후 반환, 가격 조회는 슬롯 밖(느린 다운로드가 슬롯을 차지하지 않게) |
| trades 충돌·홈 KPI | #194 | **`trades` 테이블 이중 정의** — api와 backend ORM이 같은 이름을 다른 컬럼으로 매핑, 같은 DB + 비변경 `create_all`이라 워커가 먼저 뜨면 API의 `Trade` 쿼리(대시보드·전략 상세·퀵트레이드 내역)가 `UndefinedColumn`으로 500. API 테이블을 `strategy_trades`로 분리 + 두 ORM 테이블명 교집합 0 정적 가드 + Postgres 시작 순서 테스트. 홈 KPI는 실제 값 또는 `—`(오늘 손익 원천 없음, 미실현은 US 보유 시 `—`, 거래 통계는 `/summary.performance`) |
| KIS 페이지네이션 | #195 | **알려진 이슈 6** — 잔고(KR·US)·US 미체결·주문 조회(KR·US, `kis_adapter`·`KISBroker`) 7곳이 `CTX_AREA_*`를 빈 값으로만 보내 2페이지부터 조용히 버려졌다. `KISClient.get_page()`(요청·응답 헤더 `tr_cont`) + `kis_adapter.pagination.get_all_pages()`(F/M 동안 `ctx_area_*` 되돌려 보내며 행 병합, 요약은 첫 페이지). 연속인데 키·행 목록이 없거나 20페이지 초과면 일부 반환 대신 raise |
| 주문 조회 "알 수 없음" | #196 | **#195 리뷰 후속** — 주문 상태 조회가 모든 실패를 `None`으로 바꿨고 재조정기는 `None`을 "브로커에 없음"으로 읽어 1시간 지난 주문을 **취소 처리**했다(타임아웃이 금전 상태 전환으로). 브로커는 조회 실패 시 raise, `None`은 모든 페이지를 읽고 미매칭일 때만. 재조정기는 이미 예외를 오류로 처리 — 변경 없음. `RecoveryVerifier` 조회 실패는 `NOT_FOUND`(재전송 허용) 대신 신설 `UNKNOWN` |
| 미등록 미국 티커 | #197 | **알려진 이슈 5 부분** — `EXCD_MAP`에 없는 티커는 `NASD` 추정(틀리면 KIS 거부, 오체결 없음). 워커 `KISBroker`는 미등록 보유 종목을 잔고 행 `ovrs_excg_cd`로 매도·취소·조회·시세(비상청산이 미등록 NYSE 보유분에서 막히지 않게). 등록 종목은 맵 유지, 불일치는 경고 1회. 퀵트레이드는 라우팅 그대로, 추정이면 `exchange_assumed`·거부 문구로 알림. `symbols.is_mapped`/`broker_exchange` |
| 내 주문 내역 | #198 | 퀵트레이드 내역(`/quick-trade/history`)과 홈 "최근 주문"(`/summary.recent_orders`, 옛 `recent_trades`)이 아무도 쓰지 않는 `strategy_trades`를 읽어 늘 비어 있었다 → 사용자 자신의 `quick_trade_orders`(모든 상태, 최신순, 자격증명 필터). **주문이지 체결이 아니다** — 손익 없음, 홈 행은 "+0" 대신 주수·상태. 워커 `orders`/`fills`는 `.env` 단일 계좌라 사용자 열이 없어 API 사용자에게 보이면 안 된다 |
| 앱 전략 시작 | #199 | 앱 `/api/strategies/start`는 `status="running"`과 아무도 읽지 않는 Redis 집합만 바꾸고 워커엔 아무것도 보내지 않았다 — 화면은 "실행 중", 실제론 신호·주문·`strategy_runs` 없음(4주 시계도 안 감). 시작을 거부하고 이유 표시(선택지 A), 죽은 Redis 집합 제거, 시작 버튼은 API 호출 없이 안내. 감사·선택지 B/C는 `docs/STRATEGY_START_AUDIT.md` |
| Pinia 스토어 분리 | #200 | **P3-04** — `stores/index.js` 한 파일(웹 307줄·모바일 360줄)을 스토어별 모듈 8개 + `pinia.js` + 배럴 `index.js`로(가져오는 쪽 16곳씩 그대로). 웹·모바일 사본이 갈라져 버그가 났었다(#150 키움 자격증명, 모바일 프로필 크래시) → 두 앱이 **동일한** 스토어 파일. 모바일에만 있던 미사용 `useBrokerStore`·`useWebSocketStore`·`kiwoomItems` 제거. 정적 가드 `tests/integration/test_frontend_store_parity.py`(두 앱 동일·스토어 id 1회·로그아웃이 계정 스토어 전부 리셋) |
| API CI·이미지 | #201 | **#127** — `api/tests/`(627건)가 어느 CI에서도 돌지 않았다 → `tests.yml`에 `pytest-api` 잡(Postgres, **`requirements-api.txt`만 설치** — API 이미지와 같은 의존성). 그러다 발견: **API 이미지(`Dockerfile.api`)가 기동 불가**였다 — `backend/`를 COPY하지 않는데 `api/`가 import(`No module named 'backend'`), `requirements-api.txt`에 RestrictedPython 없음(`strategy/`). 둘 다 추가 + 이미지의 COPY 줄만으로 `import api.main`을 하는 테스트(`api/tests/test_api_image_layout.py`). 배포는 이 워크플로 성공에 게이팅되므로 API 테스트 실패도 배포를 막는다 |
| WS 토큰 검증 | #202 | **#189** — `kis-ws`(`Dockerfile.kis-bot`)는 `api.auth`를 import해 토큰을 검증했는데 이미지에 `api/`·JWT 라이브러리·`JWT_SECRET_KEY`가 없었고, `ImportError`를 삼켜 **모든 WS 클라이언트를 인증 실패로 거부**했다. 검증을 `backend/security/jwt_tokens.py`로 옮기고 `api.auth.decode_access_token`이 그것을 쓴다(검증기 하나). `requirements.txt`에 PyJWT(API와 같은 버전), compose `kis-ws`에 API와 같은 `JWT_SECRET_KEY`. 설정 누락은 이제 연결마다 거부가 아니라 **기동 실패**(`_require_token_verifier`). **유효한 토큰만으로는 안 된다** — 중계 데이터(주문·포지션·자산·경보)는 `.env` 단일 운영 계좌이고 가입은 열려 있어서 `WS_OPERATOR_USER_IDS`(compose·`.env`, 비우면 전원 거부 — **#205에서 `OPERATOR_USER_IDS`로 통합**)에 있는 사용자 id(토큰 `sub`)만 연결 — 이메일이 아닌 이유: 가입이 메일 소유를 확인하지 않아 등록 안 된 운영자 주소를 남이 가입할 수 있다(코드 리뷰·CodeRabbit 지적). 토큰에 `exp` 필수, 소켓은 토큰 만료 시 끊긴다(30초 주기 점검). **별건 수정**: 고정된 flask-socketio 5.3.6이 Flask 3.1과 비호환이라 모든 Socket.IO 이벤트가 `AttributeError`로 실패했다 → 5.6.1. ~~웹·모바일 앱에는 WS 클라이언트가 아직 없다~~ — #210에서 운영 전략 화면에 붙였다(그때 kis-ws가 컨테이너에서 **기동조차 못 했음**을 발견) |
| 운영 API 노출·경보 | #203 | 이미지 감사(#201·#202 후속). 이미지 구성·의존성은 문제없음(kis-api·kis-worker·kis-ws 진입점이 각자 COPY한 것만으로 import). 대신 compose에서: **kis-api(:5001)가 열려 있었다** — `KIS_API_KEY` 기본값이 빈 값이면 `_check_api_key`가 인증을 통째로 껐고 포트는 모든 인터페이스에 게시돼, 접근 가능한 누구나 `POST /api/admin/flatten {"confirm":true}`(운영 계좌 전량 매도)·전략 시작/중지·조정·잔고를 인증 없이 호출할 수 있었다. 이제 키가 없으면 열린 경로(`/api/health`·`status`·`metrics`) 외 503, gunicorn 기동 거부(`on_starting`→`require_api_key`), compose는 `KIS_API_KEY:?`로 시작 거부·포트는 `127.0.0.1:5001`만, 키 비교는 `hmac.compare_digest`. **워커 경보 유실**: kis-worker·kis-api에 `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID`가 전달되지 않아 킬스위치·MDD 청산·워치독·복구 경보가 전부 사라졌다 → 전달 |
| 전략 시작 B 설계 | #204 | **설계 문서만**(`docs/STRATEGY_START_B_DESIGN.md`). 워커 지표 전략은 설정의 조건을 읽지 않고 하우스 신호(`default_fusion`)만 쓴다(설정은 `universe`·`position_size_pct`·`stop_loss_pct`만) → "앱 조건 번역" 대신 **운영자 전용 하우스 전략 제어판(B1)**: `kis-api` 프록시(비상청산과 같은 방식), 입력 제한(지표 고정·유니버스 부분집합·비중 ≤5%), 활성 실행 1개. 발견: 비상청산·킬스위치 해제 허용 목록이 **이메일**이다. 가입이 메일을 확인하지 않고, 목록 비교는 대소문자를 무시하는데 가입 중복 검사는 구분한다 — **운영자가 이미 가입했어도 안전하지 않다**(CodeRabbit). 0단계(사용자 id `OPERATOR_USER_IDS` 통일)를 B1과 상관없이 먼저, 그때까지 `EMERGENCY_FLATTEN_ADMINS`는 비워 둘 것. 활성 실행 1개는 프록시가 아니라 `kis-api` `start_strategy`가 원자적으로 강제. 4주 관문은 "28일 전에 시작한 행이 있는가"만 본다(1분 만에 중지해도 통과) — 별도 결정 |
| 운영자 id 통일 | #205 | **B 설계 0단계(F1)** — 운영 계좌에 작용하는 제어 셋(비상청산·킬스위치 해제·kis-ws 피드)이 목록 셋을 읽었고 둘은 **이메일**이었다: 가입이 메일을 확인하지 않고, 목록 비교는 대소문자를 무시하는데 가입 중복 검사는 구분해 **운영자가 이미 가입했어도** 대소문자만 다른 주소로 일치할 수 있었다. 이제 하나의 `OPERATOR_USER_IDS`(사용자 id, `backend/security/operators.py`, 비우면 아무도 허용 안 함). `EMERGENCY_FLATTEN_ADMINS`·`KILL_SWITCH_ADMINS`·`WS_OPERATOR_USER_IDS`는 읽지 않고 기동 시 경고(api lifespan·kis-ws). compose `api`·`kis-ws` 모두 `OPERATOR_USER_IDS`. **동작 변화**: 킬스위치 해제는 `KILL_SWITCH_ADMINS`가 compose에 없어 늘 비활성이었는데, 이제 운영자 id가 있으면 앱에서 동작한다. **배포 시**: 옛 이메일 값은 옮겨지지 않는다 — 서버 `.env`에 운영자의 사용자 id로 `OPERATOR_USER_IDS`를 넣을 것(비어 있으면 세 제어 모두 꺼지고 기동 로그가 옛 변수명과 함께 알린다) |
| 4주 관문 | #206 | **B 설계 F2** — `LivePromotionGuard._check_paper_run`이 `started_at <= now-28d`인 행의 **개수**만 셌다. 28일 전에 시작해 1분 뒤 중지한 실행도 통과했다. 이제 `paper_run_qualifies`: 28일 동안 중지되지 않은 실행만 센다(활성이면 시작부터 지금까지, 종료가 기록됐으면 종료까지). 중지 요청 후 종료 기록이 없는 행은 불통과(fail-closed). **시작조차 못 한 실행도 막는다**(code-review): 워커가 전략을 못 만들면(알 수 없는 유형·브로커 없음) 행을 활성인 채 두어 28일 뒤 통과했다 → 새 시작이 실패하면 `stopped_at = started_at`(0일, 늦게 처리돼도 그 날수를 세지 않게). 기동 직후 `strategy.start()`가 실패해도 같다. 기록 쓰기는 최대 3회 시도 후 실패하면 긴급 경보(수동 조치 SQL 포함)(CodeRabbit). 다시 전달된 시작 명령(Redis 전달 뒤 `commands`에 `pending`으로 남아 DB 폴링이 재생)은 행이 활성이고 종료 기록이 없을 때만 시작한다 — 0일로 기록된 실행이나 운영자가 중지한 실행이 되살아나지 않게. 기동 복원 중 전략 생성 실패는 다음 기동에 재시도하도록 그대로 둔다. kis-api는 워커가 만들 수 없는 `strategy_type`을 400으로 거부. **스키마 변경 없음**: `strategy_runs`에 열을 더하면 `create_all` DB가 깨진다(#194). 그래서 환경(모의/실전)·실제 매매 여부(워커가 `orders.strategy_run_id`를 안 채움)·워커 다운타임은 후속으로 남겼다 |
| 운영 전략 프록시 | #207 | **B1 1단계** — 운영자(`OPERATOR_USER_IDS`)가 앱 API로 워커 하우스 전략을 시작·중지·조회한다: `api/routers/operator.py`가 kis-api를 서버 측에서 호출(비상청산 프록시와 같은 방식, 브라우저는 키를 모른다, 재시도 없음). 권한 확인이 입력 검증보다 먼저(비운영자는 형태를 알 수 없다). 입력은 `indicator` 고정·유니버스는 `UNIVERSE` 부분집합·비중 ≤5%·손절 1~15%·모르는 키 거부. **활성 실행 1개를 kis-api `start_strategy`가 강제**(`_occupying_run`: `is_active` 또는 `stopped_at` 없음이면 409+`run_id`, Postgres advisory lock으로 동시 시작 직렬화) — 프록시의 목록 확인(최근 50개)은 빠른 거부용. 중지 요청이 실행 중인 세션을 못 찾으면(생성 전에 중지, 재기동 뒤 재생된 중지) 워커가 `stopped_at = started_at`(0일)으로 기록해 슬롯을 푼다(code-review) — 그 실행이 실제로 돌았는지 알 수 없으니 4주 관문에 날수를 주지 않는다. 생성 중에 온 중지는 시작을 취소한다. 워커가 종료를 기록하지 못하고 죽은 행은 여전히 점유(fail-closed) — 워커가 그 실행을 돌리지 않음을 확인한 뒤 `stopped_at`을 채워 해제. 목록은 점유 여부·4주 관문 충족(`paper_run_qualifies`)·실행 일수를 붙인다. `/api/auth/info`·`/api/users/profile`(조회·수정)에 `is_operator`(표시용). 화면은 #208 |
| 운영 전략 화면 | #208 | **B1 2단계** — 웹·모바일 `views/profile/OperatorStrategy.vue`(`/profile/operator-strategy`): 실행 목록(점유 배지·실행 일수·4주 관문 충족/예정일, 중지된 미충족 실행은 "미충족"), 점유 실행 중지(확인 대화상자), 시작 폼(이름·종목당 비중 %·손절 %·유니버스 체크박스, 확인 대화상자에 "운영 계좌로 실제 주문"). 슬롯이 점유 중이거나 목록을 못 읽으면 시작 버튼 비활성, 시작·중지 뒤 목록 재조회(재전송 없음), 비율은 %로 편집하고 분수로 전송. 메뉴는 `userInfo.is_operator`일 때만(표시용). 시작·중지는 확인 대화상자 전에 잠근다(두 번 탭해도 요청 1회). 목록 조회가 겹치면 가장 최근 응답만 반영한다. **다시 보낸 중지가 슬롯을 일찍 풀지 않는다**(CodeRabbit 보안 리뷰). 워커는 먼저 `is_active=False`를 쓰고, 진행 중인 `on_market_open`과 `strategy.stop()`이 끝난 뒤에 `stopped_at`을 쓴다. 끝나는 중인 세션은 `_stopping`에 두어, 그 사이에 다시 온 중지가 0일로 기록되지 않는다. 0일 기록은 세션이 아예 없을 때만 한다. `operatorApi`, 유니버스 선택지 `constants/tradingUniverse.js`(서버 `UNIVERSE`와 동일 — 정적 가드 `tests/integration/test_frontend_operator_screen.py`, 두 앱 동일·로케일 키·메뉴 게이트·경로). 5개 로케일 × 2앱 |
| 4주 관문 환경·체결 | #209 | **#206의 남은 한계 두 가지** — 관문이 실행의 환경(모의/실전)과 실제 매매 여부를 몰랐다. 실전으로 시작한 실행(`SAFE_MODE`로 매매가 막혀도)이나 한 번도 체결되지 않은 실행이 28일만 지나면 통과했다. **스키마 변경 없이**: kis-api `start_strategy`가 `config.kis_env`에 서버의 `KIS_ENV`를 찍는다(클라이언트 값은 덮어씀, `config`가 객체가 아니면 400). 워커는 자기 환경과 다르게 찍힌 실행을 돌리지 않는다 — 새 시작은 0일 기록, 복원은 지금 시각으로 종료 기록. 기록 없는 옛 행은 그대로 돌리되 관문에 세지 않는다. 워커는 실행의 상태머신이 **새로 삽입하는** 주문에 `orders.strategy_run_id`를 채운다(열은 원래 있었다). 관문(`paper_gate_status`)은 모의 환경 + 28일 무중지 + 체결 수량이 있는 귀속 주문 1건 이상. kis-api 목록에 `kis_env`·`filled_orders`·`orders_enabled`, 운영 화면은 같은 함수로 사유(환경·기간·체결)를 보여준다. **4주 모의투자의 정의가 바뀐다**(code-review): 섀도 모드(`ENABLE_LIVE_TRADING=false`)는 주문을 내지 않아 체결이 없으므로 관문에 세지 않는다 — 모의투자는 `KIS_ENV=paper` + `ENABLE_LIVE_TRADING=true`(모의 계좌로 실제 주문). `.env.example`·compose 주석(값은 그대로)·기동 로그를 맞췄다. 워커는 시작·복원할 때 그 값을 `config.orders_enabled`에 찍고 화면이 섀도 실행을 표시한다 |
| 운영 실시간 피드 | #210 | **#202의 남은 것** — 앱에 WS 클라이언트가 없었다. 운영 전략 화면에 "실시간" 섹션: kis-ws가 Redis에서 중계하는 `order:update`·`alert`(포지션·자산은 #212부터), 연결 상태(연결 중·연결됨·재연결 중·거부됨·끊김)와 최근 30건, 치명 경보는 토스트, 주문 갱신이 오면 목록을 한 번 다시 읽는다(체결 수·관문). `src/services/operatorFeed.js`(두 앱 동일, `socket.io-client`). **토큰은 URL이 아니라 핸드셰이크 `auth`로**(프록시·접근 로그에 남지 않게) — kis-ws가 `auth.token`을 먼저 읽고 `?token=`도 계속 받는다. 거부는 연결 뒤 끊기가 아니라 `ConnectionRefusedError`(클라이언트가 "거부됨"과 "만료로 끊김"을 구분). 브라우저는 같은 서버의 `/socket.io`로 붙고 웹 개발 서버가 `http://kis-ws:5002`로 프록시한다(compose 서비스명, `VITE_WS_TARGET`로 바꿀 수 있음). **발견·수정: kis-ws는 컨테이너에서 기동 불가였다** — Flask-SocketIO가 stdin이 TTY가 아니면 Werkzeug 서버를 거부해 시작 즉시 종료(재시작 반복). #202의 테스트는 `test_client`라 못 잡았다. `allow_unsafe_werkzeug=True`(스레드 단일 프로세스, 운영자 몇 명) + compose 포트를 `127.0.0.1:5002`로(개발 서버가 외부에 노출되지 않게). TTY 없이 실제로 띄워 핸드셰이크를 확인하는 테스트 추가. 모바일 앱은 서버 주소를 웹 서버(프록시가 있는 곳)로 둬야 피드가 붙는다 |
| 업스트림 주소 제거 | #211 | **알려진 이슈 13 + 나머지** — 앱이 QuantDinger에서 와서 그쪽 서버를 기본값으로 갖고 있었다. ① 모바일 개발 서버 `/api` 프록시가 `api.quantdinger.com` — `npm run dev`의 로그인·자격증명 요청이 제3자 서버로 갔다 → `VITE_API_TARGET`(기본 `http://localhost:8000`). ② 모바일 `PUBLIC_WEB_BASE_URL` 기본값 `m.quantdinger.com`(아무도 안 씀) → 두 앱에서 제거. ③ About(두 앱 동일): 업데이트가 **업스트림 APK로 대체 다운로드**하던 경로(지금은 서버가 버전을 안 보내 휴면) 제거 — 서버가 준 `https://` 주소만 열고, 없거나 다른 스킴이면 대화상자 없이 안내. 업스트림 웹사이트·지원 메일 행 숨김(새 연락처는 만들지 않음), 소개 문구를 KIS 주식·ETF 플랫폼으로(디지털 자산 → 주식·ETF 위험 고지), 안 쓰는 로케일 키 4개 제거. 5개 로케일 × 2앱. 정적 가드: 두 앱 `src/**`·`vite.config.js`에 `quantdinger.com` 없음, 복사본 동일, https 전용 |
| 운영 계좌 카드 | #212 | kis-ws가 `position:update`·`equity:update`를 중계했지만 **발행처가 없었다**. 워커 `backend/worker/portfolio_feed.py` `publish_portfolio`: 브로커에서 잔고·포지션을 읽어 두 채널로 발행(총자산·현금 KRW/USD·`equity_verified`, 종목·수량·평단·현재가·시장). **보기일 뿐 판단 입력이 아니다** — 두 읽기는 서로 독립(한쪽이 실패해도 다른 쪽은 발행), 예외는 삼키고, 겹친 호출은 합친다(진행 중에 온 요청은 끝난 뒤 한 번 더 읽게 표시 — 마지막 체결이 반영되고, 몰려도 추가 조회는 1회). 트리거: 체결 직후(`_spawn_aux`, 종료 시 join, `__init__`으로 만든 워커만 — 테스트용 `__new__` 워커는 브로커에 닿지 않게), 기동 직후, 스케줄러 10분 주기(한국 장중 + 서울 기준 미국 장중). kis-ws는 두 상태 채널의 마지막 값을 `ws:last:<채널>`(TTL 1일)에 두고 **운영자 연결이 수락되면 그 소켓에만** 보낸다 — 발행 사이에 화면을 열어도 바로 보인다(주문·경보는 재생하지 않음). 운영 전략 화면 "실시간"에 계좌 카드: 모르는 숫자는 0이 아니라 `—`(#149·#192), `equity_verified`가 false면 미검증 표시(#178). 5개 로케일 × 2앱 |
| 4주 관문 가동 시간 | #213 | **#206·#209가 남긴 마지막 한계** — 관문이 달력 28일을 셌다. 워커가 내려가 있던 시간도 실행한 것으로 쳤다. 이제 **그 실행이 실제로 돌던 시간**이 28일이어야 한다(운영자 결정: 다운타임만큼 뒤로 민다 — 재기동은 그 몇 초만, 6시간 장애는 6시간). 기록: 새 테이블 `run_uptime`(열 추가가 아니라 새 테이블이라 `create_all`이 기존 DB에 만든다, #194 함정 아님 + Alembic `d1e2f3a4b5c6`). **워커가 아니라 실행 단위**(code-review): 복원이 실패한 실행은 다음 기동을 위해 활성으로 남는데 아무도 돌리지 않는다 — 워커 가동으로 세면 그 시간이 통과에 들어간다. 그래서 실행의 `WorkerSession`이 `strategy.start()`에 성공한 뒤부터 `backend/worker/uptime.py` `UptimeRecorder`로 기록한다(60초마다 `last_beat_at`, 세션이 끝나면 `ended_at`). 기록은 `main()`이 **복구가 성공한 뒤에만** 켠다(`enable_run_uptime` — 복구 중이거나 SafeMode로 남은 워커는 매매할 수 없다). 죽은 프로세스의 행은 마지막 박동 + `UPTIME_GRACE`(120초)까지. **박동을 못 쓴 시간은 가동이 아니다** — 마지막으로 쓴 박동에서 grace보다 오래 지나면 행을 늘리지 않고 새 행을 연다(DB 장애·컨테이너 일시정지가 가동으로 메워지지 않게). 종료 기록은 최대 2초만 기다린다 — DB가 멈춰도 세션 정리·워커 종료 예산을 잡아먹지 않는다(code-review, 못 쓰면 마지막 박동에서 끝난 것으로 센다). 계산은 `promotion_guard.uptime_by_run`(그 실행의 행만, 겹친 구간은 한 번, 실행 구간으로 자름, 기록 없으면 0 — 이전 실행은 세지 않음). `paper_gate_status`·`paper_run_qualifies`가 `uptime`을 받는다(사유는 그대로 `duration`). kis-api 목록에 `uptime_sec`·`downtime_sec`, 프록시는 없거나 이상한 값이면 0(fail-closed)·실행 구간으로 상한 후 판정, `uptime_days`·`downtime_hours`, 관문 예정일 = 지금 + 남은 가동 시간. 운영 화면에 "가동 X일 · 다운타임 Y시간". 5개 로케일 × 2앱 |
| 죽은 OAuth 제거·라이선스 표기 유지 | #214 | **OAuth 로그인 전부 제거**: 백엔드에 OAuth가 없는데(`/api/auth/oauth/*` 라우트 없음, `security-config`가 버튼을 켜지 않음) 로그인 화면은 URL의 `oauth_token`을 받아 그대로 `finalizeLogin`했다 — **`/login?oauth_token=X` 링크 하나로 방문자를 남의 계정에 로그인**시켰다(main 빌드에서 재현: `/home`으로 이동·토큰 저장). 그 계정에 KIS 자격증명을 저장하게 될 수 있었다. 로그인 화면 OAuth 섹션·핸들러·`$route.query` 감시, `main.js` 딥링크 처리(OAuth 전용), `utils/oauthRedirect.js`, 로케일 키 8개×10 삭제. **약관·소개 문구의 사실 오류 수정**: "디지털 자산"→주식·ETF, "거래소 API"→증권사 API(5개 로케일×2앱, 위험 강도·나머지 문장 그대로). **업스트림 표기는 유지한다**(운영자 결정) — 앱은 QuantDinger-Mobile 사본이고 라이선스 §3.1이 표기·브랜딩의 제거·변경을 서면 허락 없이 금지한다(code-review가 지적: 처음엔 이름을 걷어냈다). 그래서 약관·`<title>`·`appName`에 QuantDinger를 되돌렸고, 라우터가 탭 제목을 매번 덮어써서(`… \| Mobile`) 정적 `<title>`이 보이지 않았으므로 탭 제목도 `… \| QuantDinger`로(code-review), 소개 문구(#211이 KIS 설명으로 바꿨던 것)는 업스트림의 QuantDinger 첫 문장을 그대로 두고 그 뒤에 #211의 KIS 설명·위험 문구를 이었다, 라이선스 원문을 `frontend/`에도 둔다(웹 앱도 같은 코드의 사본인데 파일이 없었다), `mobile/README`는 업스트림 원문 + 이 사본의 차이 메모. 앱 ID는 식별자라 두 설정 모두 `com.kistrade.mobile`. 정적 가드(`test_frontend_no_upstream_hosts.py`): 두 앱의 라이선스 원문·제목·탭 제목·`appName`·약관·소개에 QuantDinger 유지, 약관·소개가 주식·ETF를 말함, **로그인 토큰은 로그인 API 응답에서만**(`this.finalizeLogin(res.data.token…)`만, 로그인·`main.js`·라우터가 `location.search/hash`·`URLSearchParams`·딥링크를 읽지 않고 쿼리는 `redirect`만), OAuth 코드·`oauthRedirect.js` 없음, 두 앱 로그인·`main.js`·약관·capacitor 설정 동일 |
| 리스크 데이 07:00 | #215 | **#166 수정** — 일손실 3%의 날이 **서울 자정**에 바뀌었는데 미국 세션(22:30~05:00, 겨울 23:30~06:00)이 자정을 가로질러 한 야간 세션이 **3% 예산을 두 번** 받았다(23시 −2% + 01시 −2% = 4%인데 정지 안 함). 이제 `trading_day()`는 **리스크 데이**: 07:00 KST~다음 날 07:00, 시작한 날짜로 표기(미국 마감 뒤·한국 개장 전이라 어떤 세션도 걸치지 않는다). 한국 세션 + 그날 밤 미국 세션이 하루. 리스크 쪽 호출처(엔진·킬스위치·하트비트·종료 체크포인트·23:50 요약·`/api/metrics`·`api/routers/risk.py`)는 모두 이 함수를 써서 그대로 따라온다. **주문 멱등성 키와 포지션 `entry_date`는 신설 `seoul_date()`(서울 달력 날짜)** — KIS 주문번호가 서울 자정에 리셋되므로 리스크 데이로 키를 만들면 23시와 01시의 같은 번호가 같은 키가 된다. **재기동이 주간 한도를 초기화하던 문제**: `week_start`가 트래커 생성 시각이라 워커를 재시작할 때마다 주간 6% 예산이 새로 생겼다 → 최근 7 리스크 데이 합(`_prior_days`, 기동 시 이전 6일 행의 `daily_pnl`로 재구성 — 오늘 행의 `weekly_pnl` 열이 아니라). 수동 `reset_daily`/`reset_weekly`가 끼어도 주간은 자기가 본 값을 넘긴다. **재기동이 MDD 기준을 잃던 문제**: 고점이 오늘 행에서만 복원돼 그날 첫 기록 전에 재시작하면 0 → 워커가 현재 잔고로 다시 심어 드로다운이 사라졌다 → 오늘 행에 고점이 있으면 그것, 없으면 **최근 7 리스크 데이 안에서 고점이 기록된 가장 최근 행**(그보다 오래됐으면 예전처럼 잔고로 심는다). code-review 후속: ① 07:00 이후 첫 체결 전의 쓰기(리셋·종료 체크포인트)가 **닫힌 날의 손익을 새 날 행에 써** 재시작이 일·주간에 두 번 셌다 → `roll_over(today)`(행 키와 같은 날로 먼저 넘긴다, `_write_db`·체크포인트), Redis 키는 숫자가 속한 날. ② 기동이 07:00을 가로지르면 닫힌 날이 주간에서 빠졌다 → 복원이 `trade_date`를 맞춘다. ③ 복원 조회 실패가 조용히 빈 값이었다 → ERROR 로그. ④ **정지가 조용한 이틀 뒤 사라졌다**(읽는 쪽은 오늘·어제만, 이월은 쓰기 때만 — 주말에 체결이 없으면 월요일 재무장) → 일일 작업을 **06:01→07:01**(리스크 데이가 바뀐 직후)로 옮기고 살아 있는 정지를 새 날 행에 이월(앞 행을 잠근 채 다시 읽어 그 사이 해제됐으면 안 함 — 해제 API는 진행 중인 정지 행을 모두 해제하므로 앞 행이 아직 정지면 오늘 행의 False는 해제가 아니다. CodeRabbit). 스키마 변경 없음. 배포 주의는 알려진 이슈 15 |
| 오래된 정지도 유효 | #216 | **알려진 이슈 15의 남은 구멍** — 정지는 해제할 때까지 유효한데, 읽는 쪽은 모두 오늘·어제 두 행(`trading_days_in_play`)만 봤다. 정지가 새 행으로 옮겨지는 건 누가 쓸 때뿐(트래커의 체결·종료, 07:01 작업)이라 **워커가 리스크 데이 이틀 내내 내려가 있으면** 정지가 창 밖으로 밀려났다 — 다음 기동이 **정지 없이** 뜨고, `/api/status`·`/api/metrics`·앱 킬스위치 화면은 "정지 아님", 해제 API는 "해제할 것 없음"이라 앱으로는 그 행을 지울 수도 없었다(fail-open, #215 이전부터). 이제 **정지된 행이 하나라도 있으면 정지**: `risk_days_in_play(sess)` = 오늘·어제 + `kill_switch`가 참인 모든 날(최신순 — 가장 최근 사유를 보고), 트래커 복원(조회 실패 시 두 날로 후퇴 + ERROR)·07:01 작업(가장 최근 정지 행에서 이월)·해제 API(모두 잠가 한 번에 해제, 날짜순)·Flask 상태·하네스 `KillSwitch`가 모두 이것을 쓴다. 배포일 1회성(옛 키가 D+1에 쓴 정지)도 같이 보인다. 해제된 행은 나이와 상관없이 세지 않는다. 스키마 변경 없음 |
| 일일 결산 06:50 | #217 | **알려진 이슈 15의 남은 항목** — 자산 스냅샷과 텔레그램 "일일 결산"이 **23:50 KST**에 돌아, 07:00~07:00 리스크 데이(#215) 중 한국 세션과 미국 개장 후 약 80분만 보고했다(미국 손익 대부분이 빠지고, 스냅샷은 미국 포지션을 장중 값으로 평가). 이제 **06:50 KST**(미국 마감 05:00·겨울 06:00 뒤, 07:00 경계 전 — `trading_day()`가 아직 방금 끝난 날) — 그날 행에 한국·미국 손익이 다 들어 있다. 메시지에 `리스크 데이: <날짜> (07:00~07:00 KST)`(레거시 `bot/main.py` 호출은 그대로). **덤으로 고친 것**: 결산의 정지 표시가 `peak_equity > 0` 조건 뒤에 숨어 있어 고점 없는 정지 행(07:01 작업이 이월한 행)은 "정지 없음"으로 보고됐다 → 정지는 고점과 상관없이, 정지된 모든 날(`risk_days_in_play`, #216)에서 그날 자기 정지를 먼저 읽는다. code-review 후속: 날짜는 **작업 시작 때** 정한다(느린 잔고 조회가 07:00을 넘겨도 새 빈 날을 보고하지 않게), 브로커·스냅샷이 실패해도 결산은 보낸다(정지 줄은 DB만 필요 — 모르는 값은 0이 아니라 `—`, DB 실패면 "킬스위치 상태 조회 실패"), 늦은 시작은 06:59:59까지만 실행(`misfire_grace_time` 599초 — CodeRabbit: 540초는 06:59:00까지였다). 작업 저장소가 메모리라 06:50~07:00에 워커가 재시작되면 그날 결산은 건너뛴다(정보용 알림 1건, 리스크 판단과 무관 — 받아들임). 레거시 `bot/scheduler.py`(compose에서 비활성인 `kis-bot`)는 아직 23:50 결산을 보낸다 — 손대지 않음 |
| 킬스위치 해제 후 재개 | #218 | **P0-12 완료** — 운영자가 해제해도(`POST /api/risk/kill-switch/reset`) ① 워커의 주문 게이트(`SAFE_MODE`)는 재시작 전까지 닫혀 있었고 ② 손실이 그대로면 다음 체결에서 바로 다시 정지했다. 운영자 결정: **리스크 한도 정지만 1분 안에 재시작 없이 재개**, **더 나빠질 때만 재정지**. 워커 `_resume_if_released`(60초 주기 `risk_resume`): 정지된 행이 하나도 없고(`risk_days_in_play`, 조회 실패면 아니오) 행과 맞춘 트래커(`refresh_from_db`)도 정지가 아니면 `SAFE_MODE`를 연다 — **원인이 `RISK_BREACH`이고 복구가 성공했거나 복구한 정지에서만 멈춘 워커만**(`allow_risk_resume`), 상태를 믿을 수 없는 정지는 여전히 재시작. 확인과 열기는 트래커 락 안(`if_clear`) — 그 사이 체결이 낸 정지 위에 열지 않는다. 재개 시 텔레그램 알림, 가동 기록(#213)이 꺼져 있었으면 켠다. **해제는 지금의 손실을 받아들인다**: 이미 정지된 트래커의 위반은 새 판단이 아니다(`_halt` — 매 체결 재정지·재경보가 해제를 덮어썼다) → 다음 쓰기가 해제를 채택하고 기준을 둔다(`_set_release_baseline`): 해제 때 **넘어 있던** 일·주간 한도만 해제 시점보다 **자본의 1%**(`release_step_pct`) 더 잃어야 다시 정지(넘지 않았던 한도는 그대로 — 워치독 정지의 해제는 아무 한도도 늦추지 않는다, CodeRabbit. 일 기준은 그 리스크 데이만, 주간은 7일 창 안), MDD는 위반 중이면 고점을 현재 자산으로(자산을 모르면 첫 판독 때). 주간 기준은 굴러가는 창을 따른다(받아들인 손실이 창에서 빠지면 그만큼 기준도 빠진다). 기준은 `AuditLog`(`risk_release_baseline`)에 남겨 재시작이 복원(스키마 변경 없음). **해제 = 앱의 `kill_switch_reset` 감사 행**(해제와 같은 트랜잭션) — 워커가 내려가 있을 때 한 해제도 기동 때 적용한다(MDD 기준은 기동 때 읽은 잔고로 — 못 읽었으면 그 리스크 데이의 첫 판독 때까지만 기다리고, 그 뒤엔 옛 고점으로 잰다, CodeRabbit). 기동 정지로 멈춘 채 복원된 실행도 재개되면 가동 기록을 시작한다. **함께 고친 것**: 기동 시 복원된 정지를 `StartupRecovery`가 **신뢰 불가 상태**로 기록해(두 번) 어떤 폴링도 열 수 없었다 → `RISK_BREACH`(`halted_by_risk`, 리스크 상태를 못 읽은 경우는 그대로 신뢰 불가). **07:01 작업이 원인 상관없이 `SAFE_MODE`를 열었다** — 복구 실패한 워커가 07:01부터 매매 → 07:01은 이제 열지 않는다(정지 이월만). 대신 복구 실패는 텔레그램으로 "재시작 필요"를 알린다. 오래된 행에서 복원한 정지는 기동 직후 오늘 행에 쓴다(`write_pending`) — 나중에 쓰면 그 이월이 해제를 덮는다. MDD 청산은 해제가 재무장하고 고점을 옮기므로 같은 위반으로 두 번 팔지 않는다 |
| 주문 이력 | #219 | **P2-01 1단계** — `orders`는 상태·체결 수량·평균가·브로커 주문번호를 제자리에서 고쳐 써서, 크래시나 재조정 뒤에 주문이 어떻게 지금 상태가 됐는지 아무 기록도 없었다. 이제 **append-only `order_events`**: 주문 행이 생기거나 그 네 값이 바뀔 때마다 한 줄. 쓰는 곳(러너·복구·재조정기·터미널 이벤트·하네스)을 하나씩 고치지 않고 **세션 훅 하나**(`backend/database/order_history.py`, `after_flush`)가 같은 연결·같은 트랜잭션에 쓴다 — 변경이 롤백되면 이력도 롤백, 앞으로 생길 쓰는 곳도 빠뜨릴 수 없다. ORM은 이력 행의 수정·삭제를 거부하고, Postgres는 트리거가 UPDATE·DELETE·TRUNCATE를 거부한다 — 운영 DB는 Alembic이 아니라 `create_all`로 만들어지므로 트리거는 워커·kis-api가 DB를 열 때(`init_db_factory` → `ensure_db_guard`, 멱등·advisory lock) 설치한다(설치 실패는 프로세스를 멈추지 않는다 — 같은 함수가 API 쪽 하트비트 워치독의 DB도 열어서, 멈추면 리스크 정지가 꺼진다. CodeRabbit 제안을 거절. 대신 기동 복구가 트리거 없음을 별도 감사 유형 `order_events_guard_missing`으로 기록 — `recovery_inconsistency`가 아니다: 주문·포지션 불일치가 아니라 DB 상태라서). Alembic `e2f3a4b5c6d7`도 같은 트리거를 두고, 이미 있는 표를 받아들인다. **기록하는 값은 방금 쓴 행을 다시 읽은 값**이다(세션의 오래된 객체가 아니라 — code-review), 추적하는 네 열은 `active_history`(만료된 값을 같은 값으로 다시 써도 이력이 생기지 않게). 세션을 거치지 않는 쓰기(Core·bulk·`__table__` DML·여러 줄 `query(...).update()`·raw SQL)는 AST 정적 가드가 막는다. 기동 복구가 최근 7일 안에 바뀐 주문 중 최신 이력과 상태가 다른 것을 `order_status_event_mismatch`로 감사 기록(이력 이전 주문은 건수만, 다른 검증과 따로 실패). 새 테이블이라 기존 테이블 스키마 변경 없음(#194 함정 아님). **2단계(이력에서 상태를 읽고 `orders.status`를 없애기)는 P6** — 읽는 쪽은 그대로 `orders` |
| ROADMAP 감사 | #220 | **P6-05** — 51개 항목 중 8개만 표기돼 있어 무엇이 남았는지 알 수 없었다. 표기 없는 43개를 HEAD 코드와 대조해 모두 표기 + `Audit` 노트(파일:행·PR·테스트). 결과 ✅34 · ⚠️11 · ❌4 · ⏸2, 맨 위 "Status at a glance"에 남은 항목 표. **발견**: ⓪ **체결 중복 제거 키가 약해 증분·가격이 앞 체결과 같은 두 번째 체결이 버려진다**(P2-03, code-review가 찾음) ① 웹 앱 자격증명 폼에 계좌번호·HTS ID가 없다(P3-02) ② 앱 퀵트레이드 정지 게이트가 아무도 쓰지 않는 레거시 Redis 키를 읽는다(P5-03) ③ 체결 기록 실패가 경고뿐(P1-10) ④ `./quantdinger` 클론은 더 이상 필요 없다(P6-04, 알려진 이슈 1 정리). 문서만 — 코드 변경 없음 |
| 체결 동일성 | #221 | **P2-03** — `_persist_fill`이 `(order_id, qty, price)`가 같은 체결을 중복으로 보고 버렸다. 증분·가격이 앞 체결과 같은 두 번째 실제 체결이 `fills`에서, 상태머신이 모르는 주문이면 `orders.filled_qty`에서도 사라졌다(복구 경로는 같은 이유로 이미 이 검사를 없앴다, P3-02C-D F2). 이제 체결의 정체는 **그 체결이 주문을 데려가는 누적 체결수량**이다: 폴러가 증분과 함께 누적을 넘기고(`Order.cumulative_filled_qty`, 기본 `None`), `_persist_fill`은 주문 행을 잠근 채(`FOR UPDATE`) 이미 기록된 체결 합이 그 누적에 닿았을 때만 건너뛴다 — 재전달은 같은 누적, 같은 크기의 두 번째 체결은 더 큰 누적. 누적을 모르면(폴러를 거치지 않은 호출) 둘을 구별할 수 없으니 **주문 수량을 넘기는 체결만** 거부하고 `fill_overfill_rejected`로 감사. 처방의 `UNIQUE(order_id, seq_no)`는 아니다 — 모델에 브로커 체결번호가 없고 `fills` 스키마는 바꾸지 않는다. 폴러 워터마크가 여전히 첫 방어선. `backend/execution/order_poller.py`는 복사본에 누적을 싣는 두 줄만(운영자 승인) |
| 체결 기록 실패 | #222 | **P1-10** — `_persist_fill`이 체결을 기록하지 못해도(쓰기 예외, 붙일 주문 행 없음) 경고 로그뿐이었다. DB가 브로커보다 적게 말하는데(`fills`·`orders.filled_qty`) 재시작은 DB에서 복원하고 아무도 몰랐다. 이제 `report_fill_write_failure`(`backend/worker/recovery.py`)가 `SAFE_MODE`를 **재시작까지 고정**한다(`SafeModeState.latch`): 신설 원인 `RECORD_FAILURE` — **신규 진입은 막고 청산·비상청산·취소는 허용**(메모리 트래커는 체결을 알고 DB만 뒤처졌다). 고정 중에는 `enable()`이 거부한다 — 킬스위치 해제 후 재개 폴링도, 기동 복구의 마지막 단계도 열 수 없다. 뒤에 온 정지가 원인을 완화하지도 못한다(`UNTRUSTED_STATE`만 덮는다 — code-review: 처음엔 킬스위치가 원인을 `RISK_BREACH`로 바꿔 해제하면 다시 열렸다). 프로세스당 긴급 경보 1회(DB 장애는 모든 체결을 실패시킨다), ERROR 로그, 자체 세션으로 `fill_write_failed` 감사. 복구 체결 스텁도 같은 경로이고, `_step_enable_trading`은 다른 분기보다 먼저 고정을 본다. 보고 수량은 브로커 누적으로 맞춘 빠진 수량(#221). 중복 건너뛰기·과체결 거부는 판단이지 실패가 아니다. 테스트 격리: 루트 `conftest.py`가 고정을 건 테스트 뒤에만 고정과 `SAFE_MODE`를 되돌린다 |

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
- **배포**: GitHub Actions → SSH → AWS (`.github/workflows/deploy.yml`). ⚠️ **이 워크플로는 현재 비활성(`disabled_manually`, 마지막 실행 2026-05-24)** — main 머지가 배포하지 않는다. 배포는 최종 단계로 미뤄 두었다(수동). 배포할 때 서버 `.env`에 `KIS_API_KEY` 필수(알려진 이슈 12)
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

1. ~~**QuantDinger 백엔드 빌드**~~ — 더 이상 필요 없다(P6-05 감사, PR #220): `docker-compose.yml`은 `./frontend`와 `.`만 빌드하고 `./quantdinger`를 쓰지 않는다. 업스트림 저장소를 클론할 필요가 없다. `scripts/setup_oracle_cloud.sh:50`은 아직 클론하고 "빌드에 필요"라고 적혀 있다 — 클론은 `.` 빌드 컨텍스트 안이라(`.dockerignore` 없음) 빌드마다 데몬으로 보내진다. 배포 스크립트 정리 때 뺄 것
2. **키움증권**: `kiwoom_adapter/`(client·market_data·orders·portfolio) + `backend/brokers/kiwoom.py` 존재하나 완성도·실거래 검증 미완. 세부 이슈는 `docs/KIWOOM_AUDIT_REPORT.md`·`ROADMAP.md`(P1-01 등) 참고
3. **모의→실전**: `.env`에서 `KIS_ENV=paper` → `KIS_ENV=real`만 변경. **4주 모의 전 절대 금지**
   — 강제 장치는 `backend/worker/promotion_guard.py`의 `LivePromotionGuard`다. 6개 관문 중
   **"4주 모의투자 완료"**는 28일 동안 **중지되지 않은** `strategy_runs` 행이 있는지로 판정한다(PR #206).
   기준은 두 가지다. 활성이고 `stopped_at`이 없으면 시작부터 지금까지, `stopped_at`이 있으면 시작부터 종료까지가 28일 이상이어야 한다.
   예전에는 28일 전에 **시작만** 했으면 1분 만에 중지해도 통과했다.
   그리고 **모의 환경**으로 찍힌 실행이어야 하고(kis-api가 시작 시 `config.kis_env`에 서버의 `KIS_ENV`를 찍는다) **체결된 주문이 1건 이상** 있어야 한다(워커가 `orders.strategy_run_id`를 채운다) — #209.
   환경 기록이 없는 옛 행은 세지 않는다. **모의투자는 `KIS_ENV=paper` + `ENABLE_LIVE_TRADING=true`로 돌린다**(모의 계좌로 실제 주문) —
   섀도 모드(`false`)는 주문이 나가지 않아 체결이 없으니 28일을 채워도 통과하지 못한다.
   **28일은 달력이 아니라 그 실행이 실제로 돌던 시간이다**(PR #213) — 워커가 6시간 내려가 있었거나 복원이 실패해 아무도 돌리지 않았으면 관문 날짜가 6시간 늦어진다.
   기록은 `run_uptime` 테이블(실행의 세션이 시작된 뒤부터, 워커 복구가 성공했을 때만, 1분마다 박동, 세션 종료 시 `ended_at`). 기록이 없는 실행은 세지 않는다.
   **코드로 줄일 수 없는 유일한 항목이고, 실전 전환일 = 모의투자를 켜는 날 + 28일로 고정된다.**
4. ⚠️ **SPY·XL\* 섹터 ETF의 거래소 코드 미검증**: 이들은 NYSE Arca 상장인데 KIS 주문 코드에
   ARCA가 없다. KIS가 Arca를 NYSE로 두는지 AMEX로 두는지 **확인하지 못했다**(종목 마스터
   다운로드가 개발 환경 egress 정책에 막힘). 현재 매핑은 NYSE. **모의투자에서 이 종목들이
   거부되면 여기부터 의심할 것.** `backend/quant/data/universe.py` 주석 참고
5. **`EXCD_MAP`에 없는 미국 티커는 `NASD`로 추정**한다(PR #197에서 부분 해결). 미국 티커는 거래소 간 유일해서
   거래소가 틀리면 KIS가 **거부**할 뿐 엉뚱한 종목이 체결되진 않는다. 이제 (a) 워커 `KISBroker`는 보유 종목을
   **미등록** 보유 종목을 잔고 행의 `ovrs_excg_cd`(브로커가 알려주는 실제 거래소)로 매도·취소·조회·시세한다 —
   비상청산이 미등록 NYSE 보유분에서 막히지 않게. 등록 종목은 `EXCD_MAP` 그대로(주문 후 거래소가 바뀌면 조회·취소가
   다른 거래소로 갈 수 있어서) 브로커 값과 다르면 경고만 1회 — 이 로그가 #4(SPY·XL\*) 확인 데이터,
   (b) 퀵트레이드는 추정이었음을 응답에 표시(`exchange_assumed`, 브로커 거부 시에만 "NASD로 추정" 문구). **남은 것**:
   미등록 NYSE/AMEX 종목 신규 매수, 퀵트레이드 매도(거래소가 멱등성 지문에 들어가 브로커 조회 전에 정해짐)는
   종목 마스터가 필요. **모의투자에서 확인할 것**: 잔고 행에 `ovrs_excg_cd`가 실제로 오는지
6. ~~`tr_cont` 페이지네이션 미구현 (7곳)~~ — PR #195에서 해결. **모의투자에서 확인할 것**: 연속 키 값을
   공백 포함 그대로 되돌려 보낸다(공식 예제와 같음). KR 주문 조회 경로 `/trading/inquire-order`가
   `TTTC8036R`의 실제 경로인지(보통 `inquire-psbl-rvsecncl`)는 이 PR 이전부터의 미검증 사항.
   ~~**계약 문제**~~ — PR #196에서 해결: `KISBroker._get_{kr,us}_order_status`가 조회 실패를 `None`으로 삼키지 않고
   예외를 던진다(`None` = 조회 성공 + 주문 없음). 재조정기는 예외를 오류로 기록하고 주문을 건드리지 않는다.
   `RecoveryVerifier`는 조회 실패에 `NOT_FOUND`(재전송 허용) 대신 `UNKNOWN`
7. **보류된 P0**: `P0-04`(브로커별 SAFE_MODE)는 워커가 KIS 단일 브로커라 **보류**
   (키움이 워커에 들어올 때 재검토). `P0-11`은 완료(PR #183). 대시보드 요약의 0 보고는
   PR #192에서 해결(알 수 없으면 `None` + `portfolio_status`). 홈 KPI는 PR #194에서 실제 값 또는 `—`로.
   **체결 기록이 비어 있다**: `strategy_trades`(API)도 backend `trades`도 라이브 경로에서 아무도 쓰지 않는다
   (체결은 워커의 `orders`/`fills`) — 거래 통계(승률·손익비)와 전략 상세의 거래·포지션·자산곡선은 비어 있다.
   퀵트레이드 내역과 홈 "최근 주문"은 PR #198부터 사용자 자신의 `quick_trade_orders`를 보여준다(주문·상태, 체결·손익 아님).
   워커 `orders`/`fills`를 API 화면에 쓰면 안 된다 — `.env` 단일 계좌 데이터라 사용자 구분이 없다.
   API 전략의 시작/중지는 Redis 집합만 바꾸고 워커에 전달되지 않는다(`strategy:start` 미발행) — 전략별 체결은 어디에도 없다.
   **앱은 전략을 실행하지 않는다** — PR #199부터 앱의 시작은 거부되고 이유를 보여준다(예전엔 "실행 중"으로만 표시).
   **4주 모의투자 시계(`strategy_runs`)는 운영자만 시작한다** — `kis-api`(`POST /api/strategies/start`, `X-API-Key`) 또는 PR #207부터 앱 API `POST /api/operator/strategies/start`(`OPERATOR_USER_IDS`). 실행은 한 번에 하나다(kis-api가 409로 강제).
   예전 방식으로 `running`이 된 앱 행은 정지 버튼 또는 `UPDATE strategies SET status = 'stopped' WHERE status = 'running';`.
   앱→워커 연결(계좌·설정 스키마·스크립트 격리)의 선택지 B/C는 `docs/STRATEGY_START_AUDIT.md`
8. **킬스위치 해제는 1분 안에 재시작 없이 매매를 재개한다 — 리스크 한도 정지만**(PR #218, P0-12).
   해제(앱의 킬스위치 해제)가 정지된 행을 모두 지우면 워커의 `risk_resume` 작업(60초)이 트래커를 행과 맞추고 `SAFE_MODE`를 연다.
   **재시작이 필요한 경우**: 복구 실패·리스크 상태를 못 읽음·**체결을 기록하지 못함**(PR #222, `[체결 기록 실패]` 경보 — 이 경우는 신규 진입만 막고 청산은 허용하며, 해제로도 복구 끝으로도 열리지 않는다)처럼 **상태를 믿을 수 없어** 멈춘 워커(07:01 작업도 더 이상 열지 않는다).
   **해제는 지금의 손실을 받아들인다**: 해제 때 넘어 있던 일·주간 한도는 해제 시점보다 자본의 1% 더 잃어야 다시 정지(넘지 않았던 한도는 그대로, 다음 리스크 데이는 새로 시작),
   MDD는 해제 시점 자산이 새 고점. 그러니 **해제 = "이 손실까지는 감수하고 계속"**이라는 운영 판단이다 — 원인을 확인하지 않았으면 해제하지 말 것.
   해제 기준은 `audit_logs`의 `risk_release_baseline` 행(재시작이 복원). #158(덮어쓰기)은 그대로 해결돼 있다 —
   다만 워커의 정지 **기록이 실패**해 아직 행에 없으면 그 정지가 해제 위에 다시 쓰인다(fail-closed, 다시 해제)
9. **워커 종료 예산은 8초**(`_SHUTDOWN_BUDGET_SEC`). `docker-compose.yml`은 `kis-worker`에
   `stop_grace_period`를 지정하지 않아 도커 기본값 10초가 적용된다. 종료 단계를 늘리려면
   예산과 grace period를 함께 봐야 한다
10. **MDD 자동 비상청산은 기본 dry-run이다**(`MDD_AUTO_FLATTEN=true`로 켠다). MDD가 쓰는
   `KISBroker.get_balance().total_eval_krw`가 USD 현금을 빼고, 미국 잔고를 `NASD`로만 조회하고,
   미국 요약 필드가 없으면 0으로 읽는다 — 낮게 읽히면 **가짜 MDD로 전 포지션 매도**가 된다(#178).
   모의투자에서 실제 드로다운 없이 `[DRY RUN] 비상청산`이 찍히지 않는지 확인한 뒤 켤 것.
   켜진 뒤에도 `Balance.equity_verified`가 False면(필드 누락·USD 현금 보유·라우터 한쪽 실패)
   **청산만 보류**하고 경보한다. 필드가 빠지면 `KIS <side> 잔고 응답에 … 필드 없음 … 응답 필드: [...]`
   경고가 **한 번** 찍힌다 — 그 필드 목록이 #178을 닫는 데이터다

11. **기존 DB의 `trades` 잔존 테이블**(PR #194): API가 먼저 떴던 DB에는 `trades`가 API 스키마로 남아 있다.
   비어 있고 워커는 이 모델을 쓰지 않아 런타임 영향은 없지만, 나중에 `alembic upgrade`로 초기 스키마를 적용하면
   충돌한다 — 비어 있음을 확인하고 `DROP TABLE trades` 후 적용할 것. 코드상 이 테이블에 쓰는 곳이 없어 비어 있어야
   정상이다. 만약 행이 있으면(코드 밖에서 넣은 경우) 버리지 말고 먼저 옮긴다:
   `INSERT INTO strategy_trades (strategy_id, symbol, side, qty, price, filled_at, pnl, fee) SELECT strategy_id, symbol, side, qty, price, filled_at, pnl, fee FROM trades;`
   API 테이블은 Alembic이 아니라 `create_all`로 관리되므로(`alembic/env.py`의 target은 backend `Base`) 자동 마이그레이션을 두지 않았다

12. **배포 전 `KIS_API_KEY` 필수**(PR #203): 서버 `.env`에 없으면 `docker compose up`이 시작을 거부한다(kis-api는 인증 없는 모드가 없다).
   `python -c "import secrets; print(secrets.token_hex(32))"`로 만들어 넣을 것. kis-api는 호스트 루프백(`127.0.0.1:5001`)에만 노출된다.
   **기록만 한 것**: `statsmodels`가 `requirements.txt`에 없다 — 쓰는 곳은 `PairsSignal`(`backend/quant/signals/mean_reversion.py`)의
   공적분 검정뿐이고 아무도 쓰지 않는다. 쓰게 되면 kis-bot 이미지에서 `ImportError`가 삼켜져 "공적분 없음"으로 **조용히 신호를 내지 않는다**

13. ~~**모바일 개발 서버의 `/api` 프록시가 외부 도메인을 가리킨다**~~ — PR #211에서 해결. `mobile/vite.config.js`가
   `process.env.VITE_API_TARGET || 'http://localhost:8000'`을 쓴다. 같은 PR에서 앱에 남은 QuantDinger 주소를 모두 걷어냈다(About의
   업스트림 APK 대체 다운로드·웹사이트·지원 메일, 모바일 `PUBLIC_WEB_BASE_URL`). 정적 가드 `tests/integration/test_frontend_no_upstream_hosts.py`.
   ~~**남은 잔재**: OAuth 딥링크 스킴~~ — PR #214에서 OAuth 경로를 통째로 제거(백엔드에 OAuth가 없었고, URL의 `oauth_token`으로 로그인되는 구멍이었다).

14. ⚠️ **앱(웹·모바일)은 QuantDinger-Mobile 사본이고 업스트림 라이선스가 적용된다**(`mobile/LICENSE` = `frontend/LICENSE`, "QuantDinger Frontend Source-Available License").
   - **§3.1 표기 유지**: 저작권 고지·라이선스 원문·QuantDinger 브랜딩/표기를 **서면 허락 없이 지우거나 바꾸면 안 된다**. 앱 안의 QuantDinger 이름(약관·소개·`<title>`·탭 제목·`appName`)은 정적 가드가 지킨다(PR #214). #211이 지운 About의 업스트림 웹사이트·지원 메일 행은 **복원하지 않았다** — 표기라기보다 연락처이고, 이 사본을 지원하지 않는 곳으로 사용자를 보내게 된다. 엄격히 따지면 다툼의 여지가 있다. 앱의 이름을 바꾸려면 저작권자(`brokermr810@gmail.com`)의 서면 허락이 먼저다.
   - **§1·§2.3 상업적 이용**: "사적 금전적 이득을 의도한 이용"까지 상업적 이용으로 정의하고 별도 상업 라이선스를 요구한다. **이 앱으로 자기 돈을 굴려 수익을 내는 것이 여기에 해당할 수 있다** — 코드로 정할 수 없는 법적 판단, 실전 전환 전에 운영자가 확인할 것(백엔드·워커는 이 저장소의 코드라 해당 없음, 해당 여부는 앱 프런트엔드).
   - 업스트림 이름은 배포 설정에도 남아 있다(환경변수 `QUANTDINGER_SECRET_KEY`(`backend/websocket/server.py`), compose의 `quantdinger-*` 서비스, 알려진 이슈 1의 클론 절차) — 백엔드는 업스트림 라이선스 대상이 아니므로 바꿔도 되지만 compose·`.env`와 묶여 별도 작업

15. **리스크 데이는 07:00 KST에 바뀐다**(PR #215, #166). `DailyRiskState.trade_date`는 07:00~다음 날 07:00을 시작한 날짜로 표기한다 — 01:00 KST의 손익은 **전날** 행에 쌓인다. 주문의 `trade_date`·멱등성 키는 여전히 서울 달력 날짜(`seoul_date()`)라 **두 날짜는 KST 00~07시에 하루 다르다**(의도된 것).
   - **배포 후 1회성**: 배포하는 날 KST 00~07시에 옛 코드가 그날 달력 날짜 행(D+1)에 쓴 손익은 그날(07:00 시작) 리스크 데이 예산에 합쳐진다(보수 쪽). 그 시간의 **정지는 보인다** — PR #216부터 정지된 행은 날짜와 상관없이 모두 읽는다.
   - ⚠️ **배포 전 확인**(PR #216): 정지된 행은 날짜와 상관없이 정지로 센다. 해제 API가 생기기 전에 SQL로 한 행만 풀어 둔 옛 정지 행이 남아 있으면 배포 직후 정지된다(정지 쪽). `SELECT trade_date, kill_reason FROM daily_risk_states WHERE kill_switch;`로 확인하고, 남아 있으면 앱의 킬스위치 해제로 지운다(정지된 행을 모두 한 번에 해제한다).
   - **`KIS_ENV` 전환(모의→실전)**: 리스크 행은 계좌 구분이 없다. 전환 후 7일 안이면 모의 계좌의 고점이 MDD 기준으로 복원돼 첫 체결에서 가짜 MDD 정지가 난다(정지 쪽, 실전 계좌엔 포지션이 없다). 전환할 때 `UPDATE daily_risk_states SET peak_equity = 0;`.
   - **일일 결산(자산 스냅샷 + 텔레그램)은 06:50 KST**(PR #217, 예전 23:50) — 미국 마감 뒤·07:00 경계 전이라 방금 끝난 리스크 데이 전체(한국 세션 + 그날 밤 미국 세션)를 보고하고, 메시지에 리스크 데이 날짜를 적는다. 토요일 06:50 결산은 금요일 리스크 데이다.
   - 주간 한도는 **최근 7 리스크 데이**(오늘 포함)의 `daily_pnl` 합이다. 재시작하면 이전 6일 행에서 다시 만든다. MDD 고점은 오늘 행 → 없으면 최근 7 리스크 데이 안에서 고점이 기록된 가장 최근 행에서 복원한다(없으면 워커가 잔고로 심는다). 일일 리스크 작업은 **07:01**(예전 06:01)에 돌고, 살아 있는 정지를 새 날 행에 이월한다(오늘 행이 이미 정지 없이 있어도 — 앞 행이 아직 정지라면 해제된 것이 아니므로).

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
- `mobile/capacitor.config.json`: `appId → com.kistrade.mobile`, ~~`appName → KIS Trading`~~ — `appName`은 `QuantDinger`로 되돌렸다(PR #214, 업스트림 라이선스 §3.1 — 알려진 이슈 14)
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
ENABLE_LIVE_TRADING=false  # false=섀도(주문 없음). 4주 모의투자는 paper + true(모의 계좌로 주문)
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
- **현재 열린 PR 0건.** main = `072d6e0` (PR #221)

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

1. ~~`P0-03`~~ — 완료(PR #179). 자동 청산은 기본 dry-run — 켜기 전 #178 확인. 알려진 이슈 10번 참고
2. ~~`P0-12`~~ — 완료(PR #218). 리스크 한도 정지는 해제 후 1분 안에 재개, 1% 더 나빠져야 재정지. 알려진 이슈 8번 참고
3. ~~`P2-01` 1단계~~ — 완료(PR #219). 2단계(이력에서 상태 도출·`orders.status` 제거)는 P6
4. **ROADMAP 감사(P6-05, PR #220)가 드러낸 남은 항목** — `ROADMAP.md` 맨 위 "Status at a glance"가 단일 목록이다(51개 중 ✅36 · ⚠️9 · ❌4 · ⏸2 — P2-03은 #221, P1-10은 #222에서 ✅). 그중 먼저 볼 것:
   - ~~**P2-03 체결 중복 제거 결함**~~ — 완료(PR #221). 누적 체결수량이 체결의 정체, 주문 행 잠금, 누적을 모르면 과체결만 거부
   - **P3-02 웹 자격증명 폼** ⚠️ — 웹 앱 폼에 계좌번호·HTS ID가 없다(API Key/Secret/Passphrase 그대로). 웹에서 저장한 KIS 자격증명은 계좌번호가 없어 매매할 수 없다(API는 이미 받는다 — 폼만 고치면 된다). 모바일은 정상
   - **P5-03 리스크 시스템 이원화** ❌ — 앱 퀵트레이드의 정지 게이트가 레거시 `strategy/risk.py` Redis 키를 읽는데 **실행 중인 서비스 어디도 그 키를 쓰지 않는다** — 손실로는 절대 정지하지 않는다(퀵트레이드는 사용자 자신의 계좌라 워커의 `DailyRiskState`와는 별개 — 설계 결정 필요)
   - ~~**P1-10 체결 기록 실패**~~ — 완료(PR #222). 기록 못 한 체결은 신규 진입 차단(재시작까지, 청산 허용) + 긴급 경보 1회
   - P6-02 배포 게이팅 ⚠️ — `deploy.yml`이 꺼져 있어 수동 배포는 테스트에 묶이지 않는다
   - P0-02/P1-03 사전 기록(워커 경로) · P0-09 포지션 upsert · P2-06 폴링 회로 · P1-12 수량 허용치 · P0-13 FK · P6-01 전이 전수 테스트 · P6-03 헬스체크 · P1-08 `bot/` 정리
5. 열린 이슈:
   - **#173** 브로커 조회가 30일 창에서 첫 `odno` 일치 행 반환(#168의 브로커 쪽 절반). KIS가 같은
     번호를 여러 날짜로 돌려주는지 라이브 없이 확인 불가 — 모의투자 관측 필요
   - **#178** MDD 총자산(`get_balance().total_eval_krw`)이 낮게 읽힐 수 있음 — 자동 청산 활성화 전제. `equity_verified` + 진단 로그 + 미검증 시 청산 보류는 완료(PR #181). **남은 것**: 필드명·`NASD` 조회 범위 확정은 모의투자 로그로
   - ~~#166~~ 완료(PR #215) · ~~#189~~ 완료(PR #202) · ~~#127~~ 완료(PR #201) · ~~#188~~ 완료(PR #191) · ~~#185~~ 완료(PR #186) · ~~#182~~ 완료(PR #184) · ~~#176~~ 완료(PR #180) · ~~#164~~ 완료(PR #177) · ~~#172~~ 완료(PR #175) · ~~#168~~ 완료(PR #174) · ~~#170~~ 완료(PR #171) · ~~#161~~ 완료(PR #169) · ~~#160~~ 완료(PR #165) · ~~#158~~ 완료(PR #163) · ~~#167~~ 완료(PR #165)

> ~~`P0-10` SIGTERM 핸들러~~ — 완료(PR #159, `install_signal_handlers` +
> `StrategyWorker.shutdown`, 종료 예산 8초).

> ~~P6-05~~ — 완료(PR #220). `ROADMAP.md`의 51개 항목 모두에 상태(✅·⚠️·❌·⏸)와 **근거 파일:행**(Audit 노트)이 있다.
> 처방과 다르게 구현된 항목은 노트가 어떻게 다른지 적는다. 새 작업을 끝내면 해당 항목의 표기·근거도 함께 고칠 것.
