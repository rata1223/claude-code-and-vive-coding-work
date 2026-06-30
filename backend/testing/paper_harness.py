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
from backend.execution.order_machine import FillEvent, OrderStateMachine
from backend.execution.order_poller import OrderFillPoller
from backend.execution.position_tracker import Fill, PositionTracker
from backend.risk.kill_switch import KillSwitch, OrderIntent

logger = logging.getLogger(__name__)


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
        on_state_change: Optional[Callable[[Order], None]] = None,
    ):
        self.broker = broker or ScriptedPaperBroker()
        self.db_factory = db_factory
        self._external_state_cb = on_state_change

        self.machine = OrderStateMachine(on_state_change=self._on_state_change)
        self.tracker = PositionTracker(self.machine, corporate_action_runtime)
        self.poller = OrderFillPoller(self.broker, db_factory=db_factory,
                                      semantic_mapper=_SimCancelMapper())
        self.kill_switch = kill_switch or KillSwitch(db_factory=db_factory)

        # 관측 가능한 기록
        self.fills: list[FillRecord] = []
        self.realized_pnl: float = 0.0
        self.state_changes: list[tuple[str, str]] = []  # (order_id, status)
        self.timeouts: list[str] = []
        self.rejects: list[str] = []
        self.cancels: list[str] = []

    # ── 주문 제출 (전략 buy/sell 게이트를 그대로 재현) ──────────────────────
    def submit_order(self, symbol: str, side: str, qty: int, price: float,
                     order_type: str = "limit",
                     fill_steps=None, reject: bool = False, no_fill: bool = False) -> SubmitResult:
        """게이트 → 브로커 제출 → machine 등록 → poller 등록.

        fill_steps/reject/no_fill 로 이 주문의 체결 시나리오를 스크립트한다.
        """
        # 1. 중복주문 / 기업행위 게이트 (PositionTracker)
        if not self.tracker.try_mark_pending(symbol):
            return SubmitResult(order=None, blocked_by="pending_or_ca")

        # 2. kill-switch 게이트 (HALTED 시 NEW 차단)
        if not self.kill_switch.check_order(OrderIntent.NEW).allowed:
            self.tracker.unmark_pending(symbol)
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

    # ── 체결 콜백 체인 (runner._make_fill_callback 미러) ────────────────────
    def _on_fill(self, order: Order) -> None:
        # poller 규약: order.filled_qty 는 '증분' 수량
        inc = order.filled_qty
        if inc <= 0:
            return
        self.machine.process_fill(FillEvent(order.id, inc, order.avg_fill_price))

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

    def _on_timeout(self, order: Order) -> None:
        self.timeouts.append(order.id)
        self.tracker.unmark_pending(order.symbol)

    def _on_rejected(self, order: Order) -> None:
        self.rejects.append(order.id)
        self.tracker.unmark_pending(order.symbol)

    def _on_canceled(self, order: Order) -> None:
        self.cancels.append(order.id)
        self.tracker.unmark_pending(order.symbol)

    def _on_state_change(self, order: Order) -> None:
        self.state_changes.append((order.id, order.status.value))
        if self._external_state_cb is not None:
            self._external_state_cb(order)

    # ── 위험 이벤트 보고 (전략/loss-tracker 가 하던 호출을 직접 트리거) ─────
    def report_loss(self, daily_pnl_pct: float, mdd_pct: float = 0.0):
        return self.kill_switch.report_loss_breach(daily_pnl_pct, mdd_pct)

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
