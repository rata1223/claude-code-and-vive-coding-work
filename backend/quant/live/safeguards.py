"""
라이브 트레이딩 안전장치.

1. SignalDeduplicator  — 동일 신호 중복 주문 방지
2. PartialFillTracker  — 부분체결 추적 + 미체결 타임아웃
3. OHLCVRecovery       — 주 데이터소스 실패 시 폴백 복구
4. SpreadGuard         — 스프레드 과도 시 주문 차단
5. APIThrottleGuard    — API 레이트리밋 전 사전 대기
"""
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ── 1. 중복 신호 방지 ────────────────────────────────────────────────────────

class SignalDeduplicator:
    """
    동일 (symbol, side) 신호가 window_minutes 내에 재발생하면 차단.

    KIS API는 실시간이 아니라 봉마다 호출하므로
    봉 간격이 짧거나 재시작 후 같은 신호가 반복 실행되는 경우를 방지.
    """

    def __init__(self, window_minutes: int = 120):
        self.window = timedelta(minutes=window_minutes)
        self._seen: dict[tuple[str, str], datetime] = {}  # {(symbol, side): last_seen}

    def is_duplicate(self, symbol: str, side: str) -> bool:
        key = (symbol, side)
        now = datetime.now(timezone.utc)
        last = self._seen.get(key)
        if last and (now - last) < self.window:
            logger.warning(
                "중복 신호 차단: %s %s (마지막: %s, 윈도우: %dm)",
                symbol, side, last.isoformat(), self.window.seconds // 60,
            )
            return True
        self._seen[key] = now
        return False

    def clear(self, symbol: Optional[str] = None) -> None:
        if symbol:
            self._seen = {k: v for k, v in self._seen.items() if k[0] != symbol}
        else:
            self._seen.clear()

    def prune_expired(self) -> None:
        now = datetime.now(timezone.utc)
        self._seen = {k: v for k, v in self._seen.items()
                      if (now - v) < self.window}


# ── 2. 부분체결 추적 ─────────────────────────────────────────────────────────

@dataclass
class PendingOrder:
    order_id: str
    symbol: str
    side: str
    requested_qty: int
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    timeout_minutes: int = 30

    @property
    def remaining_qty(self) -> int:
        return self.requested_qty - self.filled_qty

    @property
    def is_timed_out(self) -> bool:
        elapsed = datetime.now(timezone.utc) - self.created_at
        return elapsed > timedelta(minutes=self.timeout_minutes)

    @property
    def is_complete(self) -> bool:
        return self.filled_qty >= self.requested_qty


class PartialFillTracker:
    """
    미체결·부분체결 주문 추적.

    - 30분 경과 미완료 주문 → 타임아웃 처리 (cancel 권고)
    - 부분체결 후 추가 체결 이벤트로 accumulated avg 가격 갱신
    """

    def __init__(self, timeout_minutes: int = 30):
        self.timeout_minutes = timeout_minutes
        self._orders: dict[str, PendingOrder] = {}  # {order_id: PendingOrder}

    def register(self, order_id: str, symbol: str, side: str, qty: int) -> None:
        self._orders[order_id] = PendingOrder(
            order_id=order_id, symbol=symbol, side=side,
            requested_qty=qty, timeout_minutes=self.timeout_minutes,
        )
        logger.info("주문 등록: %s %s %s qty=%d", order_id, side, symbol, qty)

    def record_fill(self, order_id: str, fill_qty: int, fill_price: float) -> PendingOrder:
        """체결 이벤트 처리. 가중평균 체결가 갱신."""
        order = self._orders.get(order_id)
        if order is None:
            logger.warning("알 수 없는 주문 ID: %s", order_id)
            return None

        prev_value = order.avg_fill_price * order.filled_qty
        new_value = fill_price * fill_qty
        order.filled_qty += fill_qty
        if order.filled_qty > 0:
            order.avg_fill_price = (prev_value + new_value) / order.filled_qty

        logger.info(
            "체결 기록 %s: +%d shares @ %.2f (합계 %d/%d, 평균 %.2f)",
            order_id, fill_qty, fill_price,
            order.filled_qty, order.requested_qty, order.avg_fill_price,
        )

        if order.is_complete:
            logger.info("주문 완전체결: %s", order_id)
            self._orders.pop(order_id, None)
        return order

    def check_timeouts(self) -> list[PendingOrder]:
        """타임아웃된 주문 목록 반환 (cancel 요청 필요)."""
        timed_out = [o for o in self._orders.values() if o.is_timed_out]
        for o in timed_out:
            logger.warning(
                "주문 타임아웃: %s %s %s (미체결 %d/%d)",
                o.order_id, o.side, o.symbol, o.remaining_qty, o.requested_qty,
            )
        return timed_out

    def cancel_timed_out(self, cancel_fn=None) -> list[str]:
        """
        타임아웃 주문을 cancel_fn으로 취소 후 내부 제거.
        cancel_fn: callable(order_id) → bool
        """
        timed_out = self.check_timeouts()
        cancelled = []
        for o in timed_out:
            try:
                if cancel_fn:
                    cancel_fn(o.order_id)
                self._orders.pop(o.order_id, None)
                cancelled.append(o.order_id)
            except Exception as e:
                logger.error("주문 취소 실패 %s: %s", o.order_id, e)
        return cancelled

    def get_pending(self) -> list[PendingOrder]:
        return list(self._orders.values())


# ── 3. OHLCV 데이터 복구 ─────────────────────────────────────────────────────

class OHLCVRecovery:
    """
    주 데이터소스(yfinance/PyKRX) 실패 시 폴백 복구.

    우선순위: KIS 브로커 실시간 → yfinance → PyKRX → 캐시된 마지막 데이터
    """

    def __init__(self, max_cache_age_hours: int = 26):
        self.max_cache_age_hours = max_cache_age_hours
        self._cache: dict[str, tuple[pd.DataFrame, datetime]] = {}

    def fetch(self, symbol: str, period: str = "1y",
              broker=None) -> Optional[pd.DataFrame]:
        """
        복구 우선순위대로 데이터 취득 시도.
        broker: BrokerAdapter (선택 — 있으면 실시간 우선)
        """
        # 1순위: 브로커 실시간
        if broker:
            df = self._try_broker(symbol, broker)
            if df is not None:
                self._update_cache(symbol, df)
                return df

        # 2순위: yfinance
        df = self._try_yfinance(symbol, period)
        if df is not None:
            self._update_cache(symbol, df)
            return df

        # 3순위: PyKRX (한국 종목만)
        if symbol.isdigit() and len(symbol) == 6:
            df = self._try_pykrx(symbol)
            if df is not None:
                self._update_cache(symbol, df)
                return df

        # 4순위: 캐시
        cached = self._get_cache(symbol)
        if cached is not None:
            logger.warning("OHLCV 캐시 사용: %s (신선도 불보장)", symbol)
            return cached

        logger.error("OHLCV 데이터 전체 실패: %s", symbol)
        return None

    def _try_yfinance(self, symbol: str, period: str) -> Optional[pd.DataFrame]:
        try:
            import yfinance as yf
            suffix = ".KS" if (symbol.isdigit() and len(symbol) == 6) else ""
            df = yf.Ticker(f"{symbol}{suffix}").history(period=period, auto_adjust=True)
            if df.empty:
                raise ValueError("empty")
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.index = pd.to_datetime(df.index)
            return df.sort_index()
        except Exception as e:
            logger.warning("yfinance 실패 %s: %s", symbol, e)
            return None

    def _try_pykrx(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            from pykrx import stock as krx
            from datetime import datetime, timedelta
            e = datetime.today().strftime("%Y%m%d")
            s = (datetime.today() - timedelta(days=365)).strftime("%Y%m%d")
            df = krx.get_market_ohlcv_by_date(s, e, symbol)
            if df.empty:
                raise ValueError("empty")
            df = df.rename(columns={"시가": "Open", "고가": "High", "저가": "Low",
                                     "종가": "Close", "거래량": "Volume"})
            return df[["Open", "High", "Low", "Close", "Volume"]].sort_index()
        except Exception as e:
            logger.warning("PyKRX 실패 %s: %s", symbol, e)
            return None

    def _try_broker(self, symbol: str, broker) -> Optional[pd.DataFrame]:
        try:
            from kis_adapter.market_data import get_ohlcv
            raw = get_ohlcv(symbol, count=250)
            df = pd.DataFrame(raw).set_index("date").sort_index()
            df.index = pd.to_datetime(df.index)
            return df
        except Exception as e:
            logger.warning("브로커 OHLCV 실패 %s: %s", symbol, e)
            return None

    def _update_cache(self, symbol: str, df: pd.DataFrame) -> None:
        self._cache[symbol] = (df.copy(), datetime.now(timezone.utc))

    def _get_cache(self, symbol: str) -> Optional[pd.DataFrame]:
        entry = self._cache.get(symbol)
        if entry is None:
            return None
        df, ts = entry
        age = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        if age > self.max_cache_age_hours:
            logger.warning("캐시 만료: %s (%.0fh)", symbol, age)
            return None
        return df


# ── 4. 스프레드 가드 ──────────────────────────────────────────────────────────

class SpreadGuard:
    """
    매수/매도 스프레드가 max_spread_pct 초과 시 주문 차단.
    한국 ETF의 경우 장 시작 직후·종료 직전 스프레드 확대 방어.
    """

    def __init__(self, max_spread_pct: float = 0.005):  # 기본 0.5%
        self.max_spread_pct = max_spread_pct

    def check(self, bid: float, ask: float) -> tuple[bool, float]:
        """
        스프레드 체크.
        반환: (허용 여부, 스프레드 분율)
        """
        if bid <= 0 or ask <= 0:
            return False, 0.0
        spread_pct = (ask - bid) / bid
        ok = spread_pct <= self.max_spread_pct
        if not ok:
            logger.warning("스프레드 과다: bid=%.2f ask=%.2f spread=%.3f%%",
                           bid, ask, spread_pct * 100)
        return ok, round(spread_pct, 6)

    def effective_cost(self, bid: float, ask: float, side: str,
                       is_kr: bool = True) -> float:
        """
        스프레드 포함 실효 비용 (왕복 환산).
        """
        from backend.quant.risk.position_sizer import DEFAULT_COMMISSION, KR_SECURITIES_TAX
        spread = (ask - bid) / bid if bid > 0 else 0.0
        half_spread = spread / 2
        if side == "buy":
            return half_spread + DEFAULT_COMMISSION
        return half_spread + DEFAULT_COMMISSION + (KR_SECURITIES_TAX if is_kr else 0.0)


# ── 5. API 레이트리밋 가드 ───────────────────────────────────────────────────

class APIThrottleGuard:
    """
    KIS API 레이트리밋 사전 대기.
    - 모의투자: 초당 5건
    - 실전투자: 초당 15건

    deque로 최근 호출 기록 관리, 초과 시 sleep.
    """

    def __init__(self, is_paper: bool = True):
        self.rate_limit = 5 if is_paper else 15  # 초당 최대 호출
        self._call_times: deque = deque(maxlen=self.rate_limit)

    def wait_if_needed(self) -> float:
        """호출 전 레이트리밋 대기. 실제 대기 시간(초) 반환."""
        now = time.monotonic()

        if len(self._call_times) < self.rate_limit:
            self._call_times.append(now)
            return 0.0

        oldest = self._call_times[0]
        elapsed = now - oldest
        if elapsed < 1.0:
            wait = 1.0 - elapsed
            logger.debug("API 레이트리밋: %.3fs 대기", wait)
            time.sleep(wait)
            now = time.monotonic()

        self._call_times.append(now)
        return max(0.0, 1.0 - elapsed)

    def record(self) -> None:
        self._call_times.append(time.monotonic())


# ── 6. WebSocket 재연결 가드 ─────────────────────────────────────────────────

class WSReconnectGuard:
    """
    WebSocket 연결 끊김 감지 + 지수 백오프 재연결 정책.

    사용 예:
        guard = WSReconnectGuard()
        while True:
            if ws.is_connected():
                guard.reset()
                # ... 정상 처리
            else:
                wait = guard.next_backoff()
                time.sleep(wait)
                ws.reconnect()
    """

    def __init__(self, max_retries: int = 10, base_delay: float = 2.0):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self._retry_count = 0
        self._last_disconnect: Optional[datetime] = None

    def reset(self) -> None:
        if self._retry_count > 0:
            logger.info("WS 재연결 성공 (%d회 시도)", self._retry_count)
        self._retry_count = 0
        self._last_disconnect = None

    def next_backoff(self) -> float:
        """지수 백오프 대기시간 반환 (최대 64초)."""
        if self._retry_count == 0:
            self._last_disconnect = datetime.now(timezone.utc)
        self._retry_count += 1

        if self._retry_count > self.max_retries:
            logger.error("WS 재연결 최대 시도 초과 (%d회)", self.max_retries)
            raise ConnectionError(f"WS 재연결 실패: {self.max_retries}회 초과")

        delay = min(self.base_delay ** self._retry_count, 64.0)
        logger.warning("WS 재연결 시도 %d/%d, %.1fs 대기",
                       self._retry_count, self.max_retries, delay)
        return delay

    @property
    def is_disconnected_long(self) -> bool:
        if self._last_disconnect is None:
            return False
        return (datetime.now(timezone.utc) - self._last_disconnect) > timedelta(minutes=5)
