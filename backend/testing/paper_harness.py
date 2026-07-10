"""
PaperHarness — 페이퍼 트레이딩 종단간(E2E) 검증 드라이버 (TASK P3-01A Phase B).

실제 실행 계층 컴포넌트(OrderStateMachine / PositionTracker / OrderFillPoller /
KillSwitch / CorporateActionRuntime)를 ScriptedPaperBroker 에 그대로 연결해,
KIS 를 상대할 때와 동일한 비동기 체결 경로를 결정론적으로 구동한다.

원칙
- 실제 클래스를 그대로 쓴다(목 아님). 검증 대상이 곧 프로덕션 코드 경로다.
- 체결 콜백 체인은 worker.runner._make_fill_callback 과 동일한 순서:
    machine.process_fill → tracker.on_fill → (매도 시) 실현손익 기록
- 폴링은 백그라운드 스레드의 sleep/backoff 대신 pump() 로 결정론적으로 구동한다
  (poller._poll_one / _handle_timeout 를 직접 호출 — 테스트 하니스 한정).
- DB 는 선택. db_factory 가 없으면 인메모리로만 동작(현금·포지션은 브로커 장부 기준).
  reconciler·영속 fills 등 DB 의존 시나리오는 db_factory 주입 시에만 켜진다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from backend.brokers.models import Order, OrderStatus
from backend.brokers.paper_broker import ScriptedPaperBroker
from backend.execution.order_events import apply_terminal_event
from backend.execution.order_machine import FillEvent, OrderStateMachine
from backend.execution.order_poller import OrderFillPoller
from backend.execution.position_tracker import Fill, PositionTracker
from backend.risk.kill_switch import KillSwitch, OrderIntent, TradingState
from backend.testing.metrics import ValidationMetrics

logger = logging.getLogger(__name__)

_UNSET = object()  # submit_order(bar_ts=...) sentinel: "do not run freshness gate"


def market_of(symbol: str) -> str:
    return "KR" if symbol.isdigit() and len(symbol) == 6 else "US"


class _SimCancelMapper:
    """OrderFillPoller 가 타임아웃 자동취소 시 호출하는 cancel_kwargs 만 제공.

    실제 BrokerSemanticMapper 와 동일한 역할이지만 시뮬레이터의 cancel_order
    시그니처에 맞춘 최소 구현이다(타임아웃→자동취소 경로를 그대로 구동하기 위함)."""

    @staticmethod
    def cancel_kwargs(order) -> dict:
        return {"order_id": order.id, "symbol": order.symbol,
                "qty": order.qty, "price": order.price}


@dataclass
class FillRecord:
    order_id: str
    symbol: str
    side: str
    qty: int
    price: float
    realized_pnl: float = 0.0


@dataclass
class SubmitResult:
    """submit_order 결과. order 가 None 이면 게이트에서 차단된 것."""
    order: Optional[Order]
    blocked_by: Optional[str] = None  # "pending_or_ca" | "kill_switch" | None


class PaperHarness:
    def __init__(
        self,
        broker: Optional[ScriptedPaperBroker] = None,
        *,
        db_factory: Optional[Callable] = None,
        corporate_action_runtime=None,
        kill_switch: Optional[KillSwitch] = None,
        freshness_gate=None,
        on_state_change: Optional[Callable[[Order], None]] = None,
    ):
        self.broker = broker or ScriptedPaperBroker()
        self.db_factory = db_factory
        self._ca = corporate_action_runtime
        self._freshness_gate = freshness_gate
        self._external_state_cb = on_state_change

        self.machine = OrderStateMachine(on_state_change=self._on_state_change)
        self.tracker = PositionTracker(self.machine, corporate_action_runtime)
        self.poller = OrderFillPoller(self.broker, db_factory=db_factory,
                                      semantic_mapper=_SimCancelMapper())
        self.kill_switch = kill_switch or KillSwitch(db_factory=db_factory)

        # 관측 가능한 기록
        self.metrics = ValidationMetrics()
        self.fills: list[FillRecord] = []
        self.realized_pnl: float = 0.0
        self.state_changes: list[tuple[str, str]] = []  # (order_id, status)
        self.timeouts: list[str] = []
        self.rejects: list[str] = []
        self.cancels: list[str] = []
        self._counted_fills: set[str] = set()   # FILLED 1회만 successful_orders 집계
        self._seen_signals: set[tuple] = set()   # 중복 신호 dedup 키

    # ── 주문 제출 (전략 buy/sell 게이트를 그대로 재현) ──────────────────────
    def submit_order(self, symbol: str, side: str, qty: int, price: float,
                     order_type: str = "limit",
                     fill_steps=None, reject: bool = False, no_fill: bool = False,
                     bar_ts=_UNSET, tier=None, _dup_metric: str = "duplicate_orders") -> SubmitResult:
        """게이트 → 브로커 제출 → machine 등록 → poller 등록.

        게이트 순서(모두 fail-closed): freshness → 중복/기업행위 → kill-switch.
        fill_steps/reject/no_fill 로 이 주문의 체결 시나리오를 스크립트한다.
        bar_ts 를 명시하면(예: None 또는 과거 시각) freshness 게이트가 평가된다.
        """
        # 0. 시세 신선도 게이트 (gate 가 구성되고 bar_ts 가 명시된 경우에만; fail-closed)
        if self._freshness_gate is not None and bar_ts is not _UNSET:
            if not self._is_fresh(symbol, bar_ts, tier):
                self.metrics.stale_data_blocks += 1
                return SubmitResult(order=None, blocked_by="stale_data")

        # 1. 중복주문 / 기업행위 게이트 (PositionTracker)
        if not self.tracker.try_mark_pending(symbol):
            if self._ca_blocking(symbol):
                self.metrics.corporate_action_events += 1
                return SubmitResult(order=None, blocked_by="corporate_action")
            setattr(self.metrics, _dup_metric, getattr(self.metrics, _dup_metric) + 1)
            return SubmitResult(order=None, blocked_by="pending_or_ca")

        # 2. kill-switch 게이트 (HALTED 시 NEW 차단)
        if not self.kill_switch.check_order(OrderIntent.NEW).allowed:
            self.tracker.unmark_pending(symbol)
            self.metrics.kill_switch_blocks += 1
            return SubmitResult(order=None, blocked_by="kill_switch")

        # 3. 체결 스크립트 등록
        if reject:
            self.broker.script_reject(symbol, side)
        elif no_fill:
            self.broker.script_no_fill(symbol, side)
        elif fill_steps is not None:
            self.broker.script_fills(symbol, side, fill_steps)

        order = self.broker.place_order(symbol, side, qty, price, order_type)

        if order.status == OrderStatus.REJECTED:
            self.tracker.unmark_pending(symbol)
            self.rejects.append(order.id)
            self.metrics.rejected_orders += 1
            return SubmitResult(order=order)

        # 4. 상태머신 등록 + 폴러 등록
        self.machine.register(order)
        self.poller.register(
            order,
            on_filled=self._on_fill,
            on_timeout=self._on_timeout,
            on_canceled=self._on_canceled,
            on_rejected=self._on_rejected,
        )
        return SubmitResult(order=order)

    # ── 결정론적 폴링 구동 ──────────────────────────────────────────────────
    def pump(self, max_rounds: int = 30) -> None:
        """poller 의 백그라운드 루프를 sleep 없이 결정론적으로 대체.

        대기 중인 모든 주문을 라운드마다 1회 폴링한다(스크립트 step 1개 노출).
        더 이상 대기 주문이 없거나 진행이 멈추면 종료한다.
        타임아웃으로 표시된 주문은 _handle_timeout 으로 처리한다."""
        for _ in range(max_rounds):
            with self.poller._lock:  # noqa: SLF001 - test harness drives poller deterministically
                entries = list(self.poller._entries.values())
            if not entries:
                return
            for entry in entries:
                if entry.is_timed_out:
                    self.poller._handle_timeout(entry)  # noqa: SLF001
                else:
                    self.poller._poll_one(entry)         # noqa: SLF001

    def expire_pending(self, order_id: str) -> None:
        """주문의 등록 시각을 과거로 돌려 다음 pump() 에서 타임아웃되게 한다."""
        from datetime import datetime, timezone, timedelta
        with self.poller._lock:  # noqa: SLF001
            entry = self.poller._entries.get(order_id)
            if entry is not None:
                entry.registered_at = datetime.now(timezone.utc) - timedelta(minutes=31)

    # ── 신호 레벨 진입 (중복 신호 dedup) ───────────────────────────────────
    def submit_signal(self, symbol: str, side: str, qty: int, price: float,
                      *, signal_key=None, **kwargs) -> SubmitResult:
        """전략 신호 1건을 주문으로 변환. 동일 (symbol, side, signal_key) 가
        이미 처리됐으면 중복 신호로 차단한다(idempotency 의 신호 레벨 모사).

        in-flight 중복(이미 pending)은 submit_order 의 pending 게이트가 잡고
        duplicate_signals 로 집계한다."""
        key = (symbol, side, signal_key)
        if key in self._seen_signals:
            self.metrics.duplicate_signals += 1
            return SubmitResult(order=None, blocked_by="duplicate_signal")
        self._seen_signals.add(key)
        return self.submit_order(symbol, side, qty, price,
                                 _dup_metric="duplicate_signals", **kwargs)

    # ── 비상 청산 (실제 EmergencyFlattenManager 사용) ──────────────────────
    def emergency_flatten(self, *, dry_run: bool = False, reason: str = "검증",
                          settle: bool = True) -> dict:
        """실제 EmergencyFlattenManager 로 전 포지션 시장가 청산.

        dry_run=False 면 브로커에 매도 주문이 실제 제출된다. settle=True 면
        브로커의 미체결 주문을 모두 체결시켜 포지션이 청산됐는지 검증할 수 있다."""
        from backend.worker.emergency import EmergencyFlattenManager
        mgr = EmergencyFlattenManager(self.broker, db_factory=self.db_factory, dry_run=dry_run)
        result = mgr.flatten_all(reason)
        if not dry_run and result.get("attempted", 0) > 0:
            self.metrics.emergency_flatten_executions += 1
        if not dry_run and settle:
            self.broker.settle_all_open()
        return result

    def record_reconciliation(self, result) -> None:
        """reconciler 결과의 갭 수를 지표에 반영."""
        try:
            self.metrics.reconciliation_mismatches += len(result.gaps)
            self.metrics.reconciliation_repairs += len(result.repairs)
        except Exception:
            pass

    # ── 내부 게이트 헬퍼 ────────────────────────────────────────────────────
    def _is_fresh(self, symbol: str, bar_ts, tier) -> bool:
        from backend.data.freshness_config import FreshnessTier
        t = tier or FreshnessTier.INTRADAY_BAR
        result = self._freshness_gate.validate_timestamp(
            symbol, bar_ts, tier=t, source="paper_harness", raise_on_block=False)
        return not self._freshness_gate.is_blocking(result)

    def _ca_blocking(self, symbol: str) -> bool:
        if self._ca is None:
            return False
        try:
            return bool(self._ca.is_blocked(symbol))
        except Exception:  # fail-closed: an unverifiable gate blocks
            return True

    # ── 체결 콜백 체인 (runner._make_fill_callback 미러) ────────────────────
    def _on_fill(self, order: Order) -> None:
        # poller 규약: order.filled_qty 는 '증분' 수량
        inc = order.filled_qty
        if inc <= 0:
            self.metrics.duplicate_event_suppression += 1   # 중복 체결 이벤트 억제
            return
        self.machine.process_fill(FillEvent(order.id, inc, order.avg_fill_price))
        # 완전 체결 1회만 successful_orders 로 집계
        m = self.machine.get(order.id)
        if m is not None and m.status == OrderStatus.FILLED and order.id not in self._counted_fills:
            self._counted_fills.add(order.id)
            self.metrics.successful_orders += 1

        # 매도 실현손익: 포지션 평단 기준(체결 반영 전)
        realized = 0.0
        if order.side == "sell":
            pos = self.tracker.get_position(order.symbol)
            if pos is not None:
                realized = (order.avg_fill_price - pos.avg_price) * inc
                self.realized_pnl += realized

        fill = Fill(order_id=order.id, symbol=order.symbol, side=order.side,
                    qty=inc, price=order.avg_fill_price, market=market_of(order.symbol))
        self.tracker.on_fill(fill)
        self.fills.append(FillRecord(order.id, order.symbol, order.side, inc,
                                     order.avg_fill_price, realized))

    # 터미널 콜백은 프로덕션과 동일한 공유 핸들러(apply_terminal_event)로 위임한다
    # → 하니스가 실제 런타임 경로를 그대로 검증한다(P3-02B).
    def _on_timeout(self, order: Order) -> None:
        # 타임아웃 시 poller 가 브로커 취소를 시도 → 상태머신도 CANCELED 로 강제.
        # CodeRabbit Finding A: bookkeeping은 실제 전이가 일어난 경우에만 기록한다
        # (중복 브로커 이벤트는 apply_terminal_event 가 False 를 반환 → 중복 집계 방지).
        transitioned = apply_terminal_event(
            self.machine, self.tracker, order,
            target_status=OrderStatus.CANCELED, db_factory=self.db_factory)
        if transitioned:
            self.timeouts.append(order.id)
            self.metrics.timeout_recovery += 1
        else:
            self.metrics.duplicate_event_suppression += 1

    def _on_rejected(self, order: Order) -> None:
        transitioned = apply_terminal_event(
            self.machine, self.tracker, order, db_factory=self.db_factory)
        if transitioned:
            # A PARTIAL_FILLED order reported REJECTED converges to CANCELED
            # (order_events voids only the unfilled remainder). Book it by the
            # order's true final status, not the incoming event label.
            final = self.machine.get(order.id)
            if final is not None and final.status == OrderStatus.CANCELED:
                self.cancels.append(order.id)
            else:
                self.rejects.append(order.id)
                self.metrics.rejected_orders += 1
        else:
            self.metrics.duplicate_event_suppression += 1

    def _on_canceled(self, order: Order) -> None:
        transitioned = apply_terminal_event(
            self.machine, self.tracker, order, db_factory=self.db_factory)
        if transitioned:
            self.cancels.append(order.id)
        else:
            self.metrics.duplicate_event_suppression += 1

    def _on_state_change(self, order: Order) -> None:
        self.state_changes.append((order.id, order.status.value))
        if self._external_state_cb is not None:
            self._external_state_cb(order)

    # ── 위험 이벤트 보고 (전략/loss-tracker 가 하던 호출을 직접 트리거) ─────
    def report_loss(self, daily_pnl_pct: float, mdd_pct: float = 0.0):
        before = self.kill_switch.state
        event = self.kill_switch.report_loss_breach(daily_pnl_pct, mdd_pct)
        if (before is not TradingState.HALTED
                and self.kill_switch.state is TradingState.HALTED):
            self.metrics.kill_switch_activations += 1
        return event

    # ── 검증 헬퍼 ────────────────────────────────────────────────────────────
    def position_qty(self, symbol: str) -> int:
        pos = self.tracker.get_position(symbol)
        return pos.qty if pos else 0

    def position_avg(self, symbol: str) -> float:
        pos = self.tracker.get_position(symbol)
        return pos.avg_price if pos else 0.0

    def order(self, order_id: str) -> Optional[Order]:
        return self.machine.get(order_id)

    def total_filled(self, order_id: str) -> int:
        o = self.machine.get(order_id)
        return o.filled_qty if o else 0
