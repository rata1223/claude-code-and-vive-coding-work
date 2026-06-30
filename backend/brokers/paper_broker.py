"""
ScriptedPaperBroker — 결정론적 인프로세스 BrokerAdapter (페이퍼 트레이딩 하니스용).

네트워크 없이 BrokerAdapter 계약을 그대로 구현한다. 체결은 스크립트 가능한
스케줄에 따라 get_order_status() 호출마다 점진적으로 노출되므로, 실제
OrderFillPoller가 KIS를 상대할 때와 동일한 비동기 체결 경로를 그대로 구동한다.

backend/strategy/runtime/simulator.py 의 SimulatedBroker 와는 목적이 다르다:
- SimulatedBroker: 백테스트용. place_order 내부에서 비용모델로 즉시 체결하고
  자체 OrderStateMachine/PositionTracker 를 소유한다(폴러 경로를 타지 않음).
- ScriptedPaperBroker(이 클래스): 페이퍼 트레이딩 감사용. 체결을 노출만 하는
  "멍청한" 주문 엔드포인트로, Worker 의 실제 machine/tracker/poller 파이프라인이
  KIS 와 동일하게 비동기로 구동되게 한다. 부분체결·거부·타임아웃·수량점프(기업행위)를
  스크립트로 결정론적으로 재현할 수 있다.

설계 포인트
- capabilities는 기존 SIMULATOR_CAPABILITIES(backend/brokers/capabilities.py)를 재사용.
- is_live 기본값 False → backend/strategy/base.py의 SAFE_MODE / ENABLE_LIVE_TRADING /
  freshness 게이트를 건너뛴다(백테스트·드라이런과 동일). 그 게이트 자체를 결정론적으로
  검증하려면 is_live=True 로 생성한다.
- 브로커 자체 포지션 장부를 유지한다 → get_positions()가 실제 체결을 반영하므로
  PositionReconciler 가 신뢰할 수 있는 ground-truth 소스로 쓸 수 있다.
- 새로운 데이터 모델을 만들지 않는다. brokers/models.py 의 Order/Position/Balance/
  OrderStatus 만 사용한다.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Optional

from .base import BrokerAdapter
from .capabilities import SIMULATOR_CAPABILITIES
from .models import Balance, BrokerCapabilities, Order, OrderStatus, Position

logger = logging.getLogger(__name__)


@dataclass
class FillStep:
    """get_order_status() N번째 호출 시 브로커가 노출할 한 관측치.

    cumulative_filled: 지금까지 누적 체결 수량.
    status: 브로커가 보고하는 상태.
    avg_price: 누적 평균 체결가. None이면 주문 지정가를 사용.
    """
    cumulative_filled: int
    status: OrderStatus
    avg_price: Optional[float] = None


@dataclass
class _SimOrder:
    order: Order
    market: str
    plan: list[FillStep]
    cursor: int = 0           # 다음에 노출할 step 인덱스
    revealed_filled: int = 0  # 브로커 장부에 이미 반영한 체결 수량


@dataclass
class _Scripted:
    """다음 매칭 place_order 에 적용할 스크립트."""
    plan: Optional[list[FillStep]] = None
    reject: bool = False


class ScriptedPaperBroker(BrokerAdapter):
    """결정론적·스크립트형 페이퍼 트레이딩 브로커.

    사용 예::

        sim = ScriptedPaperBroker(initial_cash_krw=2_000_000)
        sim.set_price("SPY", 100.0)
        # 부분체결 후 완전체결 스크립트
        sim.script_fills("SPY", "buy", [
            FillStep(40, OrderStatus.PARTIAL_FILLED),
            FillStep(100, OrderStatus.FILLED),
        ])
        order = sim.place_order("SPY", "buy", 100, 100.0)
        # 이후 poller 가 get_order_status 를 호출하면 위 스케줄대로 체결을 노출
    """

    def __init__(
        self,
        initial_cash_krw: float = 2_000_000.0,
        initial_cash_usd: float = 0.0,
        fx_usdkrw: float = 1_300.0,
        default_price: float = 100.0,
        is_live: bool = False,
    ):
        self.is_live = is_live
        self._cash_krw = float(initial_cash_krw)
        self._cash_usd = float(initial_cash_usd)
        self._fx = float(fx_usdkrw)
        self._default_price = float(default_price)

        self._prices: dict[str, float] = {}
        self._positions: dict[str, Position] = {}          # symbol → Position(ground truth)
        self._orders: dict[str, _SimOrder] = {}            # broker_order_id → _SimOrder
        # 심볼+사이드 별로 대기 중인 스크립트 큐
        self._scripts: dict[tuple[str, str], deque[_Scripted]] = {}
        self._seq = 0
        self._lock = threading.RLock()

        # 외부에서 주입/검사 가능한 실패 플래그(브로커 장애 시뮬레이션).
        self.fail_next_status = False   # 다음 get_order_status 가 예외를 던지게 한다
        self.fail_next_order = False    # 다음 place_order 가 예외를 던지게 한다

    # ── 설정 헬퍼 ───────────────────────────────────────────────────────────
    @property
    def capabilities(self) -> BrokerCapabilities:
        return SIMULATOR_CAPABILITIES

    def set_price(self, symbol: str, price: float) -> None:
        with self._lock:
            self._prices[symbol] = float(price)

    def set_prices(self, prices: dict[str, float]) -> None:
        with self._lock:
            self._prices.update({k: float(v) for k, v in prices.items()})

    def _market_for(self, symbol: str) -> str:
        """심볼로 시장 추정: 6자리 숫자면 KR, 아니면 US."""
        return "KR" if symbol.isdigit() and len(symbol) == 6 else "US"

    def script_fills(self, symbol: str, side: str, steps: list[FillStep]) -> None:
        """다음 (symbol, side) place_order 에 적용할 점진적 체결 스케줄을 큐에 넣는다."""
        with self._lock:
            self._scripts.setdefault((symbol, side), deque()).append(_Scripted(plan=list(steps)))

    def script_reject(self, symbol: str, side: str) -> None:
        """다음 (symbol, side) place_order 가 즉시 REJECTED 를 반환하도록 큐에 넣는다."""
        with self._lock:
            self._scripts.setdefault((symbol, side), deque()).append(_Scripted(reject=True))

    def script_no_fill(self, symbol: str, side: str) -> None:
        """다음 (symbol, side) 주문이 영원히 체결되지 않게 한다(타임아웃 시나리오)."""
        self.script_fills(symbol, side, [FillStep(0, OrderStatus.SUBMITTED)])

    def set_position(self, symbol: str, qty: int, avg_price: float, market: str = "") -> None:
        """브로커 장부 포지션을 직접 설정(기업행위 수량 점프/외부 체결 시뮬레이션)."""
        with self._lock:
            mkt = market or self._market_for(symbol)
            if qty <= 0:
                self._positions.pop(symbol, None)
            else:
                cur = self._prices.get(symbol, avg_price)
                self._positions[symbol] = Position(symbol=symbol, qty=qty,
                                                    avg_price=avg_price, market=mkt,
                                                    current_price=cur)

    def apply_split(self, symbol: str, ratio: float) -> None:
        """N:1 액면분할을 브로커 장부에 적용(예: ratio=2.0 → 수량 2배, 평단 1/2)."""
        with self._lock:
            pos = self._positions.get(symbol)
            if pos is None:
                return
            pos.qty = int(round(pos.qty * ratio))
            pos.avg_price = pos.avg_price / ratio

    # ── BrokerAdapter 구현 ─────────────────────────────────────────────────
    def get_balance(self) -> Balance:
        with self._lock:
            pos_val_krw = 0.0
            for p in self._positions.values():
                price = self._prices.get(p.symbol, p.avg_price)
                val = p.qty * price
                pos_val_krw += val * (self._fx if p.market == "US" else 1.0)
            total = self._cash_krw + self._cash_usd * self._fx + pos_val_krw
            return Balance(cash_krw=self._cash_krw, cash_usd=self._cash_usd,
                           total_eval_krw=total)

    def get_positions(self) -> list[Position]:
        with self._lock:
            out = []
            for p in self._positions.values():
                price = self._prices.get(p.symbol, p.avg_price)
                out.append(replace(p, current_price=price))
            return out

    def get_price(self, symbol: str) -> float:
        with self._lock:
            return self._prices.get(symbol, self._default_price)

    def place_order(self, symbol: str, side: str, qty: int, price: float,
                    order_type: str = "limit") -> Order:
        if self.fail_next_order:
            self.fail_next_order = False
            raise RuntimeError("SimulatedBroker: place_order 강제 실패")

        with self._lock:
            self._seq += 1
            oid = f"SIM-{self._seq:06d}"
            scripted = self._pop_script(symbol, side)

            if scripted is not None and scripted.reject:
                order = Order(id=oid, symbol=symbol, side=side, qty=qty, price=price,
                              status=OrderStatus.REJECTED)
                self._orders[oid] = _SimOrder(order=order, market=self._market_for(symbol),
                                              plan=[FillStep(0, OrderStatus.REJECTED)])
                logger.info("[SIM] 주문 거부: %s %s qty=%d", side, symbol, qty)
                return replace(order)

            plan = scripted.plan if (scripted and scripted.plan) else \
                [FillStep(qty, OrderStatus.FILLED)]  # 기본: 첫 폴링에 전량 체결

            order = Order(id=oid, symbol=symbol, side=side, qty=qty, price=price,
                          status=OrderStatus.SUBMITTED)
            self._orders[oid] = _SimOrder(order=order, market=self._market_for(symbol),
                                          plan=plan)
            logger.info("[SIM] 주문 접수: %s %s %s qty=%d price=%.4f", oid, side, symbol, qty, price)
            return replace(order)

    def cancel_order(self, order_id: str, symbol: str = "", qty: int = 0,
                     price: float = 0.0) -> bool:
        with self._lock:
            so = self._orders.get(order_id)
            if so is None:
                return False
            if so.order.status in (OrderStatus.FILLED, OrderStatus.CANCELED,
                                    OrderStatus.REJECTED, OrderStatus.EXPIRED):
                return False
            so.order.status = OrderStatus.CANCELED
            logger.info("[SIM] 주문 취소: %s", order_id)
            return True

    def get_order_status(self, order_id: str, symbol: str = "") -> Optional[Order]:
        if self.fail_next_status:
            self.fail_next_status = False
            raise RuntimeError("SimulatedBroker: get_order_status 강제 실패")

        with self._lock:
            so = self._orders.get(order_id)
            if so is None:
                return None

            # 터미널 상태면 그대로 반환(취소/거부 등은 더 진행하지 않음)
            if so.order.status in (OrderStatus.CANCELED, OrderStatus.REJECTED,
                                   OrderStatus.EXPIRED):
                return replace(so.order)

            # 다음 step 노출(마지막 step 에 고정)
            step = so.plan[min(so.cursor, len(so.plan) - 1)]
            if so.cursor < len(so.plan):
                so.cursor += 1

            avg = step.avg_price if step.avg_price is not None else so.order.price
            so.order.filled_qty = step.cumulative_filled
            so.order.avg_fill_price = avg if step.cumulative_filled > 0 else 0.0
            so.order.status = step.status

            # 새로 노출된 체결분을 브로커 장부(ground truth)에 반영
            inc = step.cumulative_filled - so.revealed_filled
            if inc > 0:
                self._apply_to_book(so, inc, avg)
                so.revealed_filled = step.cumulative_filled

            return replace(so.order)

    def settle_all_open(self) -> int:
        """테스트 헬퍼: 미체결(SUBMITTED/PARTIAL) 주문을 전량 즉시 체결한다.

        EmergencyFlattenManager 가 제출한 매도처럼 OrderFillPoller 에 등록되지
        않은 주문을 결제시켜 포지션 청산을 검증할 때 사용한다. 반환값은 결제된
        주문 수."""
        settled = 0
        with self._lock:
            for so in self._orders.values():
                if so.order.status in (OrderStatus.FILLED, OrderStatus.CANCELED,
                                       OrderStatus.REJECTED, OrderStatus.EXPIRED):
                    continue
                remaining = so.order.qty - so.revealed_filled
                if remaining > 0:
                    self._apply_to_book(so, remaining, so.order.price)
                    so.revealed_filled = so.order.qty
                so.order.filled_qty = so.order.qty
                so.order.avg_fill_price = so.order.price
                so.order.status = OrderStatus.FILLED
                settled += 1
        return settled

    # ── 내부 ────────────────────────────────────────────────────────────────
    def _pop_script(self, symbol: str, side: str) -> Optional[_Scripted]:
        q = self._scripts.get((symbol, side))
        if q:
            return q.popleft()
        return None

    def _apply_to_book(self, so: _SimOrder, inc_qty: int, fill_price: float) -> None:
        """체결 증분을 브로커 포지션 장부 + 현금에 반영(lock 보유 상태에서 호출)."""
        symbol = so.order.symbol
        mkt = so.market
        cash_delta = inc_qty * fill_price
        if so.order.side == "buy":
            if mkt == "US":
                self._cash_usd -= cash_delta
            else:
                self._cash_krw -= cash_delta
            pos = self._positions.get(symbol)
            if pos is None:
                self._positions[symbol] = Position(symbol=symbol, qty=inc_qty,
                                                    avg_price=fill_price, market=mkt,
                                                    current_price=fill_price)
            else:
                total = pos.qty + inc_qty
                pos.avg_price = (pos.avg_price * pos.qty + fill_price * inc_qty) / total
                pos.qty = total
        else:  # sell
            if mkt == "US":
                self._cash_usd += cash_delta
            else:
                self._cash_krw += cash_delta
            pos = self._positions.get(symbol)
            if pos is not None:
                pos.qty -= inc_qty
                if pos.qty <= 0:
                    self._positions.pop(symbol, None)
