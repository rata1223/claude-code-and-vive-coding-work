# P0-07B: closePosition — Minimum Safe Implementation Plan

**Date:** 2026-07-27
**Type:** Implementation plan (approval gate). No code changed by this document.
**Baseline:** `main` @ `738976d` + `docs/P0_07_CLOSE_POSITION_AUDIT.md` (PR #142).
**Precondition:** P0-03 (request-scoped credentials), P0-04 (durable reservation + idempotency), P0-05 (RiskManager pre-submit gate), PR #140 (reserved-order reconciliation) — all merged.

This plan is bound by the audit's findings: `closePosition` is an **execution-domain** problem, `reserve_and_submit()` is **side-agnostic and reusable as-is**, and the implementation surface must be **minimal**. Pagination, compat middleware, and the frontend are **out of scope and must not be touched**.

---

## 1. closePosition Semantic Decision

**Decision: `closePosition` is a server-resolved, position-derived LIMIT sell, submitted through the hardened Quick Trade order path.**

It is defined as an execution-domain operation with these binding semantics:

| Property | Decision | Rationale (audit-grounded) |
|---|---|---|
| Order side | Always `sell` — never inferred from client input | The endpoint's only legitimate meaning; `side` is passed to `reserve_and_submit` verbatim (`quick_trade_service.py:152`) |
| Order type | **LIMIT** (`ORD_DVSN "00"`) | KIS declares `supports_market_sell=False` (`backend/brokers/capabilities.py:6,38`); no market/IOC/FOK primitive exists in the adapter. A "market close" is not implementable and must not be promised. |
| Quantity | **Computed by the backend from live position state.** Client-supplied qty is at most an upper-bound request, never authoritative | Audit G1: today's caller-supplied `int(body.qty)` reaches the broker with no holding validation and no oversell guard (`validator.py:109-115` is a no-op) |
| Price | **Computed by the backend from live market data**, with an explicit, bounded fallback policy (§5) | Audit G2: EmergencyFlatten's cost-basis fallback can post a deeply off-market limit — this plan must not repeat it |
| Scope of a close | Single symbol, single credential, single market. **No portfolio-wide flatten** | Portfolio-wide liquidation already has an owner (`EmergencyFlattenManager`); duplicating it here would create a second unguarded flatten path |
| Guarantee | Submission guarantee only — `submitted` ≠ filled | Same documented DEFER as `place-order` since P0-04; a resting limit close may remain unfilled |

**Explicitly rejected alternatives:** broker-native close (does not exist on `BrokerAdapter` — `base.py` has only `place_order`/`cancel_order`), market liquidation (unsupported by the KIS adapter), reduce-only flags (no such concept), and any client-priced close (the exact defect this task removes).

---

## 2. Runtime Flow

```text
POST /api/quick-trade/close-position
        │
        │  body: {credential_id, symbol, market, exchange, qty?}
        │  (qty optional = "close all"; unknown fields ignored by Pydantic)
        ↓
[1] Auth + tenant scoping        get_current_user → _get_cred(credential_id, user.id, db)
                                 quick_trade.py:68-73 — UNCHANGED, already correct
        ↓
[2] Request-scoped KIS clients   _load_kis(cred) → (client, orders, portfolio)
                                 quick_trade.py:46-66 — UNCHANGED (P0-03 preserved).
                                 KISMarketData(client) constructed in-handler from the
                                 SAME client — no new credential path, no env mutation.
        ↓
[3] LIVE POSITION RESOLUTION     portfolio.get_kr_balance() | get_us_balance()
    (backend owns quantity)      match symbol on pdno (KR) / ovrs_pdno (US)
                                 held_qty = int(hldg_qty | ovrs_cblc_qty)
                                 → no position or held_qty <= 0  → REJECT, broker never called
                                 → close_qty = held_qty            (qty omitted = close all)
                                 → qty supplied: requested > held_qty → REJECT
                                   (never silently clamp); else close_qty = requested
        ↓
[4] LIVE PRICE RESOLUTION        market_data.get_price_kr(symbol) | get_price_us(symbol, excd)
    (backend owns price)         → price <= 0 or exception → REJECT (stale/unavailable).
                                 NEVER fall back to pchs_avg_pric (cost basis).
                                 Optional protective offset applied here (§5, stale price).
        ↓
[5] Idempotency derivation       key = Idempotency-Key header or derive_idempotency_key(...)
                                 req_hash = request_fingerprint(...)
                                 over the RESOLVED (server-side) qty/price + side="sell"
                                 quick_trade_service.py:68-116 — REUSED UNCHANGED
        ↓
[6] reserve_and_submit(          quick_trade_service.py:118 — REUSED UNCHANGED
      db, user_id, credential_id,
      request={... "side": "sell", "qty": close_qty, "price": price, "order_type": "limit"},
      idempotency_key=key, request_hash=req_hash,
      risk_gate=risk_gate,        ← Depends(get_risk_gate), quick_trade.py:29-43
      broker_submit=<closure calling orders.sell_kr | sell_us>,
      extract_order_id=mapper.extract_broker_order_id)
        │
        ├─ [6a] INSERT quick_trade_orders(status=RESERVED) + COMMIT   ← durable, pre-broker
        ├─ [6b] IntegrityError → duplicate key: return existing row, broker NOT called
        ├─ [6c] risk_gate() → RiskDenied or ANY error → QT_BLOCKED, broker NOT called
        ├─ [6d] broker_submit() → KIS limit sell
        ├─ [6e] RuntimeError (rt_cd≠0) → QT_REJECTED (terminal)
        ├─ [6f] network/timeout       → stays QT_RESERVED (recoverable, never blind-retried)
        └─ [6g] success               → broker_order_id set, QT_SUBMITTED
        ↓
[7] Response                     real order.status returned (submitted/blocked/rejected/reserved)
                                 mirroring place_order:211-223 — NEVER hardcoded "submitted"
        ↓
[8] Reconciliation (automatic)   recover_on_startup → _claim_reserved_ids filters only
                                 status==QT_RESERVED (no side predicate,
                                 quick_trade_recovery.py:117-128); _classify is side-aware
                                 via extract_side. ZERO changes required.
```

Steps [1], [2], [5], [6], [8] are **existing code reused verbatim**. Only steps [3], [4], [7] are new logic, and all of it lives inside the one handler.

---

## 3. Ownership Map

| 컴포넌트 / 영역 | 소유권 (State Ownership) | 주요 역할 및 책임 |
| :--- | :--- | :--- |
| **Order Lifecycle** | `reserve_and_submit()` (`api/services/quick_trade_service.py:118`) + `qt_transition()` (`api/models.py:31`) | 유일한 상태 소유자. RESERVED 예약을 브로커 호출 **전에** 커밋하고, `QT_VALID_TRANSITIONS`에 따라 SUBMITTED/REJECTED/FAILED/BLOCKED로만 전이시킨다. 핸들러는 상태를 직접 쓰지 않으며 반환된 `order.status`를 읽기만 한다. |
| **Quantity (close_qty)** | `close_position` 핸들러 — 라이브 포지션(`KISPortfolio`)에서 산출 | 브로커 잔고의 `hldg_qty`/`ovrs_cblc_qty`가 유일한 진실. 클라이언트 수량은 상한 요청일 뿐 권위가 없다. 초과 요청은 조용히 잘라내지 않고 거절한다. |
| **Price** | `close_position` 핸들러 — `KISMarketData` 라이브 시세에서 산출 | 지정가 산정과 폴백 정책의 단일 소유자. 평균매입단가(`pchs_avg_pric`) 폴백은 금지(감사 G2 재발 방지). |
| **Risk Validation** | `get_risk_gate` → `RiskManager.is_trading_halted()` (`quick_trade.py:29-43`) | 예약 커밋 후·브로커 제출 전에 호출. 거부·오류 모두 fail-closed(`QT_BLOCKED`)로 수렴하며 브로커는 호출되지 않는다. **RiskManager 자체는 수정하지 않는다.** |
| **Idempotency / Reservation** | `derive_idempotency_key`/`request_fingerprint` + DB 유니크 제약 `(user_id, idempotency_key)` (`api/models.py:180-182`) | 중복 청산 요청 차단. 서버가 해석한 qty/price 기준으로 지문을 만들어, 동일 의도의 재클릭은 단일 주문으로 수렴한다. |
| **Position Update** | **브로커(KIS)가 진실의 원천. 이 경로는 포지션을 직접 변경하지 않는다.** | Quick Trade 도메인에는 `PositionTracker`가 없고(P0-02 SEPARATE 결정), 포지션은 다음 `/position` 조회 시 브로커에서 재조회된다. `backend/execution/position_tracker.py`는 이 경로와 무관하며 **수정 금지**. |
| **Reconciliation** | `reconcile_reserved` + `recover_on_startup` (`api/services/quick_trade_recovery.py`) | 미확정(RESERVED) 청산 주문을 기동 시 스윕으로 정리. side 필터가 없어 매도 주문도 자동 포함 — **무변경**. |
| **Audit Logging** | `quick_trade_orders` 행 자체가 감사 기록 | 예약·차단·거절·실패·제출 상태와 `error` 필드가 영속 감사 추적을 구성한다. 별도 감사 테이블·로거를 신설하지 않는다. |

---

## 4. Minimum Implementation Surface

**수정 파일: 정확히 2개.** (구현 1개 + 테스트 1개)

| 파일 | 변경 범위 | 비고 |
|---|---|---|
| `api/routers/quick_trade.py` | `close_position` 핸들러(`:233-266`) **본문 교체**. 추가: `risk_gate=Depends(get_risk_gate)`, `idempotency_key` 헤더 파라미터, 라이브 포지션 조회, 라이브 시세 조회, `reserve_and_submit` 호출, 실제 상태 반환. 파일 상단에 `KISMarketData` 임포트(또는 `_load_kis`가 반환한 `client`로 핸들러 내 생성) | **이 핸들러 외 다른 라우트·헬퍼(`_load_kis`, `_get_cred`, `get_risk_gate`)는 손대지 않는다.** 라우터 파일 자체는 대상 핸들러 경로이므로 제약을 충족한다. |
| `api/schemas.py` | `ClosePositionRequest`에서 `price` 제거, `qty`를 `Optional[float] = None`(미지정 = 전량 청산)로 완화 | 스키마 클래스 1개의 필드 정의만 변경. **DB 스키마 변경 없음** — `QuickTradeOrder`는 이미 `side/qty/price/market/exchange/order_type`를 보유(감사 §3). |
| `api/tests/test_quick_trade_close_position.py` | **신규 테스트 파일**(기존 테스트 파일 수정 없음) | §5의 6개 실패 시나리오 + 정상 경로 커버 |

**변경하지 않는 것 (제약 준수 재확인):**

- ❌ `api/services/quick_trade_service.py` — `reserve_and_submit`는 side-agnostic으로 검증됨, 무변경
- ❌ `api/services/quick_trade_recovery.py` — RESERVED 스윕이 매도를 이미 포함, 무변경
- ❌ `api/models.py` — 필요한 컬럼이 모두 존재, **마이그레이션 불요**
- ❌ `api/compat.py` — close-position 제외는 의도적 설계(`:387-391`)이며 이 계획은 서버가 가격/수량을 결정함으로써 그 배제 사유를 **해소**한다. 미들웨어 확장 없음
- ❌ 프론트엔드(`mobile/`, `frontend/`) — 무변경. `qty`/`price` 없이 보내는 기존 페이로드가 스키마 완화로 **그대로 유효**해진다(422 해소는 부수 효과이며 프론트 수정 없이 달성)
- ❌ `strategy/risk.py`, `backend/execution/*`, `backend/worker/*` — 무변경
- ❌ 페이지네이션 로직 — 무변경

---

## 5. Failure Handling

| 실패 시나리오 | 감지 매커니즘 | 대응 및 처리 동작 |
| :--- | :--- | :--- |
| **Over-close request** | 라이브 잔고 조회 후 `requested_qty > held_qty` 비교 (`hldg_qty`/`ovrs_cblc_qty`) | **거절. 절대 조용히 클램프하지 않는다.** `Resp.err("requested qty exceeds held quantity: N > M")` 반환, 예약 미생성, 브로커 미호출. 사용자가 의도한 수량과 실제 체결 수량이 달라지는 사고를 원천 차단. *주의: 미결제·대기 매도로 잠긴 수량을 구분하는 "매도가능수량" 개념이 저장소 전체에 부재(감사 S2)하므로, `held_qty` 이내라도 브로커가 거절할 수 있다 — 이는 브로커 거절 경로로 처리된다.* |
| **Missing live position** | 심볼 매칭 실패 또는 `held_qty <= 0` | **거절.** `Resp.err("no open position for {symbol}")`. 예약 미생성, 브로커 미호출. 존재하지 않는 포지션에 대한 매도(=신규 공매도)를 구조적으로 불가능하게 만든다. |
| **Stale price** | `get_price_kr/us` 예외, 비수치 응답, 또는 `price <= 0` | **거절, 폴백 없음.** `Resp.err("live price unavailable for {symbol}")`. **평균매입단가 폴백 금지** — 감사 G2가 지적한 EmergencyFlatten의 결함을 복제하지 않는다. 체결성 확보를 위해 보수적 오프셋(예: KR 호가단위 정합 범위 내 하향 지정가)을 적용할 수 있으나, 오프셋은 **라이브 시세가 유효할 때만** 적용되며 폴백 수단이 아니다. |
| **Broker rejection** | `broker_submit()`이 `RuntimeError` 발생 (`rt_cd != "0"`) | `reserve_and_submit`이 `QT_REJECTED`(터미널)로 전이하고 `error`에 사유 저장. 재시도 없음. 핸들러는 `Resp.err(f"Order rejected: ...")`로 실제 상태를 반환. |
| **Partial fill** | 이 경로에서 **감지하지 않는다** | 설계상 범위 밖. `QT_SUBMITTED`는 제출 확인일 뿐 체결이 아니며(P0-04 이래 문서화된 DEFER), 부분 체결 추적은 QT 도메인에 존재하지 않는다. 잔여 수량은 **다음 close 요청 시 라이브 잔고 재조회로 자연 해소**된다 — 서버가 매번 실제 보유량에서 수량을 재계산하기 때문. 사용자 대상 문구에서 "청산 완료"를 주장해서는 안 된다. |
| **Repeated close request** | `(user_id, idempotency_key)` 유니크 제약 위반 → `IntegrityError` (`quick_trade_service.py:163-178`) | 동일 파라미터 재요청: **기존 주문 행을 반환하고 브로커를 재호출하지 않는다.** 동일 키·다른 파라미터: `IdempotencyConflict` → `Resp.err`. 키는 서버가 해석한 qty/price로 생성되므로, 더블클릭은 10초 버킷 내에서 단일 주문으로 수렴한다. 첫 요청 체결 후의 재요청은 잔고가 0이 되어 *missing live position*으로 거절된다. |

**공통 원칙:** 위 6개 중 예약(reservation)을 생성하는 것은 broker rejection·partial fill·repeated request뿐이다. over-close, missing position, stale price는 **예약 이전 단계에서 종료**되어 DB에 흔적을 남기지 않으며 브로커를 호출하지 않는다.

---

## 6. Rollback Strategy

| 계층 | 롤백 수단 |
|---|---|
| **코드** | 단일 커밋 revert. 변경이 핸들러 1개 + 스키마 1개에 국한되므로 `git revert`가 결합 부작용 없이 이전 동작(직접 브로커 호출)으로 되돌린다. |
| **DB** | **롤백 불필요.** 스키마 변경이 없고 마이그레이션도 없다. 이미 기록된 `quick_trade_orders` 청산 행은 revert 후에도 유효한 감사 기록으로 남는다. |
| **런타임 킬 스위치** | 배포 후 이상 시 `RiskManager` 정지 플래그(Redis)를 세우면 **청산 경로도 즉시 fail-closed(`QT_BLOCKED`)** 된다. 단, 이는 청산까지 막는다는 뜻이므로(감사 S1) 긴급 청산이 필요하면 `POST /api/admin/flatten`(SAFE_MODE 우회 경로)을 사용한다. |
| **부분 배포 상태** | 스키마 완화(`price` 제거, `qty` 선택)는 기존 클라이언트 페이로드를 **더 넓게 수용**하므로, 구버전 프런트/신버전 백엔드 조합에서 파손이 발생하지 않는다. 역방향(구버전 백엔드/신버전 요청)은 프런트 변경이 없으므로 발생하지 않는다. |
| **검증 게이트** | 배포 전 페이퍼 계정(`KIS_ENV=paper`)에서 6개 실패 시나리오 + 정상 청산 1건을 실거래 없이 확인한 뒤에만 실전 전환. |

---

## 7. Implementation Order

1. **스키마 완화** — `api/schemas.py`의 `ClosePositionRequest`에서 `price` 제거, `qty`를 선택 필드로 변경. (이 단계만으로는 런타임 동작이 바뀌지 않도록, 핸들러 교체와 동일 커밋에 포함한다.)
2. **핸들러 재작성 — 해석 단계** — `close_position`에 라이브 포지션 조회(수량 산출)와 라이브 시세 조회(가격 산출)를 추가하고, over-close·missing position·stale price 3개 거절 경로를 먼저 완성한다. 이 시점까지 브로커 호출 방식은 그대로 두어 변경을 격리한다.
3. **핸들러 재작성 — 라우팅 단계** — 직접 `sell_kr`/`sell_us` 호출을 `broker_submit` 클로저로 감싸고 `reserve_and_submit(side="sell", risk_gate=...)` 경유로 전환. `risk_gate=Depends(get_risk_gate)`와 `Idempotency-Key` 헤더 파라미터를 시그니처에 추가.
4. **응답 정직화** — 하드코딩된 `"status": "submitted"`를 제거하고 `place_order:211-223`와 동일하게 실제 `order.status`를 반환.
5. **테스트 추가** — `api/tests/test_quick_trade_close_position.py` 신규 작성: 정상 전량 청산, 부분 수량 청산, over-close 거절, 포지션 부재 거절, 시세 실패 거절, 리스크 차단(`QT_BLOCKED`), 브로커 거절(`QT_REJECTED`), 중복 요청(단일 주문 수렴) — 기존 QT 테스트 파일은 수정하지 않는다.
6. **페이퍼 환경 검증 후 배포** — §6의 검증 게이트 통과 후 머지.

---

## 8. Risks and Exclusions

**리스크**

| # | 리스크 | 완화 |
|---|---|---|
| R1 | **지정가 청산은 체결을 보장하지 않는다.** KIS 어댑터에 시장가·IOC/FOK가 없어(감사 S3) 급락장에서 청산 주문이 미체결로 남을 수 있다 | 계약서(§1)에 "제출 보장, 체결 미보장"을 명시. 보수적 오프셋으로 체결 확률을 높이되 보장으로 표현하지 않는다. 진정한 시장가 청산은 어댑터 확장이 필요한 별도 과제. |
| R2 | **매도가능수량 개념 부재(감사 S2).** `hldg_qty` 이내라도 미결제·대기주문으로 잠긴 수량은 브로커가 거절 | 브로커 거절 경로로 안전하게 수렴(예약은 `QT_REJECTED`로 종결). 사용자에게 브로커 사유를 그대로 노출. 근본 해결은 별도 과제. |
| R3 | **추가 API 호출 2회(잔고+시세)로 지연 증가 및 rate limit 소비** | Quick Trade는 사용자 트리거 단발 요청이며 기존 `/position`·`/balance`와 동일한 호출 패턴. 실전 15req/s 한도 대비 무시 가능. |
| R4 | **시세 조회와 주문 제출 사이의 가격 변동(TOCTOU)** | 지정가 특성상 불리한 체결은 발생하지 않는다(가격이 고정됨). 대신 미체결 위험으로 전환되며 이는 R1과 동일하게 다룬다. |
| R5 | **리스크 정지 상태에서 청산까지 차단됨(감사 S1)** | 의도된 fail-closed 동작이나 정책 판단이 필요한 사안. 이 과제에서 `RiskManager`를 수정하지 않으며, 긴급 청산은 기존 admin flatten 경로가 담당함을 §6에 명시. |

**제외 항목 (이번 과제에서 다루지 않음)**

- 페이지네이션 관련 일체 (P0-04/PR #140 영역)
- CompatMiddleware 확장 — close-position은 여전히 compat 미적용으로 유지
- 프론트엔드 변경 — `mobile/`·`frontend/` 무수정
- 주문 취소(Cancel)·주문 상세/상태 조회 엔드포인트 신설
- 체결 영속화·부분 체결 추적 재설계
- 포트폴리오 전체 청산(flatten) — 기존 `EmergencyFlattenManager` 소유
- G2/G3/G4 우회 경로 개선(EmergencyFlatten 가격 하드닝, 레거시 `bot/main.py`, LivePipeline)
- S1(정지 시 청산 정책)·S2(매도가능수량)·S3(시장가/IOC 지원) 구조적 과제
- DB 스키마·마이그레이션 변경
