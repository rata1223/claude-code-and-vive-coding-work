# KIS Trading Platform — 인수인계 문서 (Claude 새 세션용)

> 이 문서를 읽으면 이전 대화 없이도 프로젝트 전체를 파악하고 바로 이어서 작업할 수 있다.

---

## 프로젝트 한 줄 요약

한국투자증권(KIS) + 키움증권 전용 자동매매 플랫폼.
**모바일 앱**(Vue 3 + Capacitor) + **백엔드 봇** (Python + Docker) 구조.
운용 자금 200만원, 모의투자 4주 검증 후 실전 전환.

---

## 프로젝트 진행 현황 (2026-10-03 기준, main `294d6af` = PR #203)

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
| WS 토큰 검증 | #202 | **#189** — `kis-ws`(`Dockerfile.kis-bot`)는 `api.auth`를 import해 토큰을 검증했는데 이미지에 `api/`·JWT 라이브러리·`JWT_SECRET_KEY`가 없었고, `ImportError`를 삼켜 **모든 WS 클라이언트를 인증 실패로 거부**했다. 검증을 `backend/security/jwt_tokens.py`로 옮기고 `api.auth.decode_access_token`이 그것을 쓴다(검증기 하나). `requirements.txt`에 PyJWT(API와 같은 버전), compose `kis-ws`에 API와 같은 `JWT_SECRET_KEY`. 설정 누락은 이제 연결마다 거부가 아니라 **기동 실패**(`_require_token_verifier`). **유효한 토큰만으로는 안 된다** — 중계 데이터(주문·포지션·자산·경보)는 `.env` 단일 운영 계좌이고 가입은 열려 있어서 `WS_OPERATOR_USER_IDS`(compose·`.env`, 비우면 전원 거부 — **#205에서 `OPERATOR_USER_IDS`로 통합**)에 있는 사용자 id(토큰 `sub`)만 연결 — 이메일이 아닌 이유: 가입이 메일 소유를 확인하지 않아 등록 안 된 운영자 주소를 남이 가입할 수 있다(코드 리뷰·CodeRabbit 지적). 토큰에 `exp` 필수, 소켓은 토큰 만료 시 끊긴다(30초 주기 점검). **별건 수정**: 고정된 flask-socketio 5.3.6이 Flask 3.1과 비호환이라 모든 Socket.IO 이벤트가 `AttributeError`로 실패했다 → 5.6.1. **주의: 웹·모바일 앱에는 WS 클라이언트가 아직 없다** — 서버는 동작하지만 붙는 곳이 없다 |
| 운영 API 노출·경보 | #203 | 이미지 감사(#201·#202 후속). 이미지 구성·의존성은 문제없음(kis-api·kis-worker·kis-ws 진입점이 각자 COPY한 것만으로 import). 대신 compose에서: **kis-api(:5001)가 열려 있었다** — `KIS_API_KEY` 기본값이 빈 값이면 `_check_api_key`가 인증을 통째로 껐고 포트는 모든 인터페이스에 게시돼, 접근 가능한 누구나 `POST /api/admin/flatten {"confirm":true}`(운영 계좌 전량 매도)·전략 시작/중지·조정·잔고를 인증 없이 호출할 수 있었다. 이제 키가 없으면 열린 경로(`/api/health`·`status`·`metrics`) 외 503, gunicorn 기동 거부(`on_starting`→`require_api_key`), compose는 `KIS_API_KEY:?`로 시작 거부·포트는 `127.0.0.1:5001`만, 키 비교는 `hmac.compare_digest`. **워커 경보 유실**: kis-worker·kis-api에 `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID`가 전달되지 않아 킬스위치·MDD 청산·워치독·복구 경보가 전부 사라졌다 → 전달 |
| 전략 시작 B 설계 | #204 | **설계 문서만**(`docs/STRATEGY_START_B_DESIGN.md`). 워커 지표 전략은 설정의 조건을 읽지 않고 하우스 신호(`default_fusion`)만 쓴다(설정은 `universe`·`position_size_pct`·`stop_loss_pct`만) → "앱 조건 번역" 대신 **운영자 전용 하우스 전략 제어판(B1)**: `kis-api` 프록시(비상청산과 같은 방식), 입력 제한(지표 고정·유니버스 부분집합·비중 ≤5%), 활성 실행 1개. 발견: 비상청산·킬스위치 해제 허용 목록이 **이메일**이다. 가입이 메일을 확인하지 않고, 목록 비교는 대소문자를 무시하는데 가입 중복 검사는 구분한다 — **운영자가 이미 가입했어도 안전하지 않다**(CodeRabbit). 0단계(사용자 id `OPERATOR_USER_IDS` 통일)를 B1과 상관없이 먼저, 그때까지 `EMERGENCY_FLATTEN_ADMINS`는 비워 둘 것. 활성 실행 1개는 프록시가 아니라 `kis-api` `start_strategy`가 원자적으로 강제. 4주 관문은 "28일 전에 시작한 행이 있는가"만 본다(1분 만에 중지해도 통과) — 별도 결정 |
| 운영자 id 통일 | #205 | **B 설계 0단계(F1)** — 운영 계좌에 작용하는 제어 셋(비상청산·킬스위치 해제·kis-ws 피드)이 목록 셋을 읽었고 둘은 **이메일**이었다: 가입이 메일을 확인하지 않고, 목록 비교는 대소문자를 무시하는데 가입 중복 검사는 구분해 **운영자가 이미 가입했어도** 대소문자만 다른 주소로 일치할 수 있었다. 이제 하나의 `OPERATOR_USER_IDS`(사용자 id, `backend/security/operators.py`, 비우면 아무도 허용 안 함). `EMERGENCY_FLATTEN_ADMINS`·`KILL_SWITCH_ADMINS`·`WS_OPERATOR_USER_IDS`는 읽지 않고 기동 시 경고(api lifespan·kis-ws). compose `api`·`kis-ws` 모두 `OPERATOR_USER_IDS`. **동작 변화**: 킬스위치 해제는 `KILL_SWITCH_ADMINS`가 compose에 없어 늘 비활성이었는데, 이제 운영자 id가 있으면 앱에서 동작한다 |

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
   **4주 모의투자 시계(`strategy_runs`)는 운영자 `kis-api`(`POST /api/strategies/start`, `X-API-Key`)로만 시작된다.**
   예전 방식으로 `running`이 된 앱 행은 정지 버튼 또는 `UPDATE strategies SET status = 'stopped' WHERE status = 'running';`.
   앱→워커 연결(계좌·설정 스키마·스크립트 격리)의 선택지 B/C는 `docs/STRATEGY_START_AUDIT.md`
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
- **현재 열린 PR 0건.** main = `294d6af` (PR #203)

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
2. `P0-12` — ⚠️ **부분 완료**. 위 알려진 이슈 8번 참고. 남은 건 (a) 인메모리 `SAFE_MODE`
   무재시작 해제(P0-04 의존), (b) 위반 조건이 유효할 때의 재개 의미 정의(리스크 정책 판단)
3. `P2-01` `order_events` append-only 테이블 (큰 변경)
4. ~~`P3-04` Pinia 스토어 분리~~ — 완료(PR #200). 웹·모바일 스토어 파일은 동일해야 한다(정적 가드)
5. 열린 이슈:
   - **#173** 브로커 조회가 30일 창에서 첫 `odno` 일치 행 반환(#168의 브로커 쪽 절반). KIS가 같은
     번호를 여러 날짜로 돌려주는지 라이브 없이 확인 불가 — 모의투자 관측 필요
   - **#178** MDD 총자산(`get_balance().total_eval_krw`)이 낮게 읽힐 수 있음 — 자동 청산 활성화 전제. `equity_verified` + 진단 로그 + 미검증 시 청산 보류는 완료(PR #181). **남은 것**: 필드명·`NASD` 조회 범위 확정은 모의투자 로그로
   - **#166** `daily_pnl`의 날 경계를 어디에 둘 것인가 — 미국 세션이 서울 자정을 가로지르므로
     한 야간 세션의 손익이 두 거래일로 쪼개진다. 리스크 정책 판단
   - ~~#189~~ 완료(PR #202) · ~~#127~~ 완료(PR #201) · ~~#188~~ 완료(PR #191) · ~~#185~~ 완료(PR #186) · ~~#182~~ 완료(PR #184) · ~~#176~~ 완료(PR #180) · ~~#164~~ 완료(PR #177) · ~~#172~~ 완료(PR #175) · ~~#168~~ 완료(PR #174) · ~~#170~~ 완료(PR #171) · ~~#161~~ 완료(PR #169) · ~~#160~~ 완료(PR #165) · ~~#158~~ 완료(PR #163) · ~~#167~~ 완료(PR #165)

> ~~`P0-10` SIGTERM 핸들러~~ — 완료(PR #159, `install_signal_handlers` +
> `StrategyWorker.shutdown`, 종료 예산 8초).

> `ROADMAP.md`의 각 항목에 `✅ DONE` / `❌ OPEN` 표기와 **근거 파일:행**을 달아두는 작업이
> 진행 중이다(P6-05). 표기가 없는 항목은 아직 검증되지 않은 것이지 미완이라는 뜻이 아니다.
