# 페이퍼 트레이딩 런타임 교정 (PAPER_TRADING_REMEDIATION)

> TASK **P3-02B** — P3-01 리뷰(P3-01C)와 교정 감사(P3-02A)를 권위 컨텍스트로 사용.
> 런타임 정확성 결함만 수정. 새 기능/재설계 없음.

---

## 1. Findings addressed

| ID | 심각도 | 내용 | 상태 |
|---|---|---|---|
| **C1** | CRITICAL | CANCELLED/REJECTED/EXPIRED 브로커 이벤트가 런타임에 도달하지 않음 (poller 에 `on_canceled/on_rejected/on_expired` 미배선) | ✅ 수정 |
| **H1** | HIGH | 타임아웃 콜백이 pending 락만 해제하고 상태머신을 전환하지 않음 | ✅ 수정 |
| **H3** | HIGH | broker_order_id 없는 주문이 오펀(추적 불가 + pending 락 점유) | ✅ 수정(fail-closed + 감사) |
| **M1** | MEDIUM | async 취소/거부 후 pending 락이 30분 TTL 까지 유지 | ✅ 즉시 해제 |
| **H2** | HIGH | reconciler 가 DB 는 고치지만 인메모리 상태머신은 못 고침 | 🟡 잔존(아래 5) |
| **M2/M3** | MEDIUM | reconcile 전용 체결의 손익 누락 / flatten 후 tracker 동기화 | 🟡 범위 밖(아래 5) |

전이 지원 범위: NEW(submit) · PARTIAL_FILL · FILLED 는 기존 `on_filled` 경로로 처리,
**CANCELLED · REJECTED · EXPIRED** 를 이번에 추가해 6개 브로커 이벤트가 모두 런타임에 도달한다.

## 2. Root causes

- **RC1 (C1/H1):** 터미널 브로커 이벤트에 런타임 싱크가 없었다. `OrderFillPoller` 는
  `on_canceled/on_rejected/on_expired` 를 지원하지만 프로덕션(`IndicatorStrategy._register_order`,
  `runner._register_recovered_order`)이 이를 전달하지 않았고, 타임아웃 콜백도 `machine.transition`
  을 호출하지 않았다 → 상태머신/DB 주문이 `SUBMITTED` 로, pending 락이 TTL 까지 고착.
- **RC3 (H3):** 주문 추적 키가 broker_order_id 하나뿐이라, id 가 비면 추적/폴링 모두 스킵되어
  브로커엔 살아있고 런타임엔 보이지 않는 오펀이 됐다.

## 3. Modified files

| 파일 | 변경 |
|---|---|
| `backend/execution/order_events.py` (신규) | `apply_terminal_event(machine, tracker, order, …)` — 멱등 공유 핸들러: 상태머신 전환 + pending 락 해제 + 감사. PARTIAL_FILLED→REJECTED 는 CANCELLED 로 수렴(체결분 보존). |
| `backend/strategy/indicator/strategy.py` | `on_terminal_cb` 파라미터 추가, `poller.register` 에 `on_canceled/on_rejected/on_expired` 전달. broker_order_id 없는 주문은 **fail-closed**(pending 락 유지 → 중복 방지) + `on_orphan_cb` 감사(리뷰 Finding 1 반영). |
| `backend/worker/runner.py` | 터미널 콜백 생성·주입(`on_terminal_cb`), 타임아웃 시 `apply_terminal_event(target=CANCELLED)` 로 상태머신 전환, 복구 경로(`_restore_pending_to_tracker`/`_register_recovered_order`)까지 배선. |
| `backend/testing/paper_harness.py` | 터미널 콜백을 프로덕션과 동일한 `apply_terminal_event` 로 위임(하니스=런타임 경로 일치). |

아키텍처 불변: poller→콜백→machine/tracker 계약 그대로. 재설계 없음.

## 4. Added tests

- **`backend/execution/tests/test_order_events.py`** (8) — 공유 핸들러 단위: cancel/reject/expired 전환, PARTIAL→REJECTED→CANCELLED 수렴(체결분 보존), 멱등(반복), 미등록 주문도 락 해제, 타임아웃 target_status, 비터미널 무시.
- **`backend/testing/tests/test_paper_remediation.py`** (6) — 실제 poller+broker 구동: **cancel · async reject · partial→cancel · partial→reject · repeated cancel · duplicate broker event**. 각 케이스에서 상태머신 전이 + 포지션 일관성 + pending 해제 검증.
- **`tests/postgres/test_paper_remediation_db.py`** (2, CI) — **reconciliation after cancel**(브로커 취소를 DB 로 동기화) · **worker restart after cancel**(취소 주문은 pending 복원 대상에서 제외 → 스테일 락 없음).

로컬 실행: `pytest backend/testing/tests/ backend/brokers/tests/test_paper_broker.py backend/execution/tests/test_order_events.py -q` → **61 passed**. (runner/strategy 는 pandas/redis 미설치로 로컬 미실행 → compile + 코드 검증 + CI.)

## 5. Remaining risks

- **H2 (인메모리 reconciler 복원):** `PositionReconciler` 는 tracker/machine 핸들이 없어 DB 만 교정한다. 이번 수정으로 **라이브 경로는 poller 콜백이 인메모리 상태머신을 즉시 갱신**하므로, H2 는 "poller 콜백을 통째로 놓친 뒤 reconcile 만 잡는" 드문 경우에만 남는다(다음 재시작에 수렴). 별도 후속 과제.
- **M2:** reconcile 로만 발견된 체결은 여전히 손익/kill-switch 평가를 우회(별 경로). 범위 밖.
- **M3:** 비상청산은 tracker/poller 를 우회 → 청산 직후 인메모리 tracker 스테일(다음 reconcile 수렴). 범위 밖.
- **프로덕션 배선 검증:** runner/strategy 는 로컬에서 실행 불가(의존성) → CI 의 postgres/pytest 잡에서 최종 검증.
- **Kiwoom:** 어댑터 미구현 스텁 — 본 교정과 무관하게 NOT READY.

## 6. READY / NOT READY

| 브로커 | 판정 | 근거 |
|---|---|---|
| **KIS (paper)** | **READY (CI green 조건부)** | C1/H1/H3 해소 — 6개 브로커 이벤트 모두 런타임 도달, 취소/거부/부분체결 후 상태머신·포지션·pending 일관성 검증, 오펀 방지. 잔존 위험(H2/M2/M3)은 self-heal 되거나 범위 밖. |
| **Kiwoom (paper)** | **NOT READY** | 어댑터 미구현(모든 메서드 `NotImplementedError`), 커버리지 0. |

**종합:** KIS 페이퍼 트레이딩은 이번 교정으로 **READY**(전체 스위트 CI green 확인 후). Kiwoom 은 구현 전까지 **NOT READY**.
