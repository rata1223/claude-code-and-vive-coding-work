"""
주문 체결 폴링 엔진.

KIS API에는 실시간 체결 푸시가 없으므로 주문 제출 후 주기적으로
get_order_status()를 호출해 체결 여부를 확인한다.

백오프 스케줄: 10s → 30s → 60s → 120s → 300s (이후 300s 고정)
타임아웃: 30분 후 미체결 주문은 자동 취소 시도 후 콜백 호출.

추가 기능:
- Terminal-state callbacks: on_canceled, on_rejected, on_expired
- PollingHealthMonitor: in-memory metrics (fills, errors, timeouts, ...)
- 회로 차단기(P2-06): 연속 폴링 실패 _BREAKER_THRESHOLD회면 조회를 멈추고
  냉각 뒤 주문 하나로 시험 조회한다. 차단 중에는 타임아웃도 하지 않는다 —
  상태를 모르는 주문을 취소로 수렴시키지 않게.
"""
import copy
import dataclasses
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

from backend.brokers.base import BrokerAdapter
from backend.brokers.models import Order, OrderStatus

logger = logging.getLogger(__name__)

_POLL_INTERVALS = [10, 30, 60, 120, 300]
_TIMEOUT_MINUTES = 30

# Circuit breaker (P2-06). Consecutive poll failures across all orders: a KIS
# outage fails every lookup, and without a breaker the loop keeps calling a
# broker that cannot answer — and, worse, times orders out whose status it
# cannot read (the worker then converges them to CANCELED).
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SEC = 60          # doubled after each failed probe …
_BREAKER_MAX_COOLDOWN_SEC = 300     # … up to the longest poll interval


def _monotonic() -> float:
    """The poller's clock (one place for tests to replace)."""
    return time.monotonic()

# Valid status transitions the poller may observe on the wire.
# Defined locally to avoid importing from order_machine (circular-import risk).
# Unexpected transitions are logged as warnings but never block processing —
# the OrderStateMachine enforces hard validation at callback time.
_VALID_POLLER_TRANSITIONS: dict[OrderStatus, frozenset] = {
    OrderStatus.PENDING:        frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED,
                                           OrderStatus.CANCELED}),
    OrderStatus.SUBMITTED:      frozenset({OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED,
                                           OrderStatus.CANCELED, OrderStatus.REJECTED,
                                           OrderStatus.EXPIRED, OrderStatus.UNKNOWN}),
    OrderStatus.PARTIAL_FILLED: frozenset({OrderStatus.FILLED, OrderStatus.CANCELED,
                                           OrderStatus.EXPIRED, OrderStatus.UNKNOWN}),
    OrderStatus.UNKNOWN:        frozenset({OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED,
                                           OrderStatus.FILLED, OrderStatus.CANCELED,
                                           OrderStatus.REJECTED, OrderStatus.EXPIRED}),
}


# ── Health ────────────────────────────────────────────────────────────────────

@dataclass
class PollingHealth:
    """Snapshot of poller metrics (returned by copy — safe to read without locks)."""
    total_registered: int = 0
    total_fills_detected: int = 0
    total_partial_fills: int = 0
    total_timeouts: int = 0
    total_cancels: int = 0
    total_rejects: int = 0
    total_expired: int = 0
    total_poll_errors: int = 0
    consecutive_poll_errors: int = 0
    last_successful_poll_at: Optional[datetime] = None
    pending_count: int = 0
    circuit_open: bool = False
    circuit_opened_at: Optional[datetime] = None

    @property
    def is_healthy(self) -> bool:
        return not self.circuit_open and self.consecutive_poll_errors < _BREAKER_THRESHOLD


class PollingHealthMonitor:
    """Thread-safe in-memory metrics accumulator for OrderFillPoller."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._h = PollingHealth()
        self._cooldown_sec = _BREAKER_COOLDOWN_SEC
        self._next_probe_at = 0.0
        self._opened_mono = 0.0

    def record_register(self) -> None:
        with self._lock:
            self._h.total_registered += 1

    def record_fill(self) -> None:
        with self._lock:
            self._h.total_fills_detected += 1

    def record_partial_fill(self) -> None:
        with self._lock:
            self._h.total_partial_fills += 1

    def record_timeout(self) -> None:
        with self._lock:
            self._h.total_timeouts += 1

    def record_cancel(self) -> None:
        with self._lock:
            self._h.total_cancels += 1

    def record_reject(self) -> None:
        with self._lock:
            self._h.total_rejects += 1

    def record_expired(self) -> None:
        with self._lock:
            self._h.total_expired += 1

    def record_poll_success(self) -> Optional[float]:
        """A lookup answered. Closes the breaker if it was open and returns how
        long it was open (seconds) — else ``None``."""
        with self._lock:
            self._h.consecutive_poll_errors = 0
            self._h.last_successful_poll_at = datetime.now(timezone.utc)
            if not self._h.circuit_open:
                return None
            self._h.circuit_open = False
            self._h.circuit_opened_at = None
            down = _monotonic() - self._opened_mono
        logger.info("OrderFillPoller: 회로 복구 — 주문 조회 재개 (차단 %.0f초)", down)
        return down

    def record_poll_error(self) -> Optional[tuple[int, int]]:
        """A lookup failed. Returns ``(consecutive, cooldown_sec)`` when this
        failure opened the breaker — else ``None``."""
        with self._lock:
            self._h.total_poll_errors += 1
            self._h.consecutive_poll_errors += 1
            if self._h.circuit_open or self._h.consecutive_poll_errors < _BREAKER_THRESHOLD:
                return None
            self._h.circuit_open = True
            self._h.circuit_opened_at = datetime.now(timezone.utc)
            self._opened_mono = _monotonic()
            self._cooldown_sec = _BREAKER_COOLDOWN_SEC
            self._next_probe_at = self._opened_mono + self._cooldown_sec
            opened = (self._h.consecutive_poll_errors, self._cooldown_sec)
        logger.error("OrderFillPoller: %d 연속 폴링 실패 — 회로 차단, %d초 뒤 시험 조회 "
                     "(차단 중 타임아웃 보류)", *opened)
        return opened

    def record_probe_failure(self) -> None:
        """The half-open probe failed: stay open, wait twice as long."""
        with self._lock:
            self._h.total_poll_errors += 1
            self._h.consecutive_poll_errors += 1
            self._cooldown_sec = min(self._cooldown_sec * 2, _BREAKER_MAX_COOLDOWN_SEC)
            self._next_probe_at = _monotonic() + self._cooldown_sec
            cooldown = self._cooldown_sec
        logger.warning("OrderFillPoller: 시험 조회 실패 — 회로 유지, %d초 뒤 재시도", cooldown)

    def circuit_state(self, now: float) -> str:
        """``closed``, ``open`` (cooling down) or ``probe`` (cooldown over —
        one lookup may test the broker)."""
        with self._lock:
            if not self._h.circuit_open:
                return "closed"
            return "probe" if now >= self._next_probe_at else "open"

    def is_circuit_open(self) -> bool:
        with self._lock:
            return self._h.circuit_open

    def set_pending_count(self, n: int) -> None:
        with self._lock:
            self._h.pending_count = n

    def get_health(self) -> PollingHealth:
        with self._lock:
            return copy.copy(self._h)


# ── Poll entry ────────────────────────────────────────────────────────────────

@dataclass
class _PollEntry:
    order: Order
    on_filled: Callable[[Order], None]
    on_timeout: Callable[[Order], None]
    on_canceled: Optional[Callable[[Order], None]] = None
    on_rejected: Optional[Callable[[Order], None]] = None
    on_expired: Optional[Callable[[Order], None]] = None
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    poll_index: int = 0
    next_poll_at: float = field(default_factory=_monotonic)
    last_reported_qty: int = 0  # prevents double-counting on replay
    # Serializes _apply_update for THIS entry so a reconciler resync() and the
    # background poll loop can never process the same broker update concurrently
    # and double-apply an increment.
    processing_lock: threading.Lock = field(default_factory=threading.Lock)
    # F7: set once the terminal (cancel/reject/expire) branch has fired for this
    # entry, so a resync() racing the poll loop on the same terminal event can't
    # re-run the terminal callback or re-write its audit rows.
    terminal_fired: bool = False

    @property
    def is_timed_out(self) -> bool:
        elapsed = datetime.now(timezone.utc) - self.registered_at
        return elapsed > timedelta(minutes=_TIMEOUT_MINUTES)

    def advance(self) -> float:
        """Advance poll schedule; return seconds until next poll."""
        idx = min(self.poll_index, len(_POLL_INTERVALS) - 1)
        wait = _POLL_INTERVALS[idx]
        self.poll_index += 1
        self.next_poll_at = _monotonic() + wait
        return wait


# ── Poller ────────────────────────────────────────────────────────────────────

class OrderFillPoller:
    """
    백그라운드 스레드에서 pending 주문들을 주기적으로 폴링.

    사용 예:
        poller = OrderFillPoller(broker, db_factory=session_factory)
        poller.start()
        poller.register(order, on_filled=my_fill_handler,
                        on_canceled=my_cancel_handler, on_timeout=my_timeout_handler)
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        db_factory: Optional[Callable] = None,
        semantic_mapper=None,  # Optional[BrokerSemanticMapper] — avoids circular import
        on_circuit_open: Optional[Callable[[int, int], None]] = None,
        on_circuit_close: Optional[Callable[[float], None]] = None,
    ):
        """``on_circuit_open(consecutive, cooldown_sec)`` / ``on_circuit_close(down_sec)``
        are called once per transition, outside every lock; an exception from
        either is logged and ignored (an alert must not stop the loop)."""
        self._broker = broker
        self._on_circuit_open = on_circuit_open
        self._on_circuit_close = on_circuit_close
        self._db_factory = db_factory
        self._semantic_mapper = semantic_mapper
        self._entries: dict[str, _PollEntry] = {}  # order_id → entry
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._health = PollingHealthMonitor()

    # ── public API ────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="order-poller")
        self._thread.start()
        logger.info("OrderFillPoller 시작")

    def stop(self) -> None:
        self._stop.set()

    def register(
        self,
        order: Order,
        on_filled: Callable[[Order], None],
        on_timeout: Optional[Callable[[Order], None]] = None,
        on_canceled: Optional[Callable[[Order], None]] = None,
        on_rejected: Optional[Callable[[Order], None]] = None,
        on_expired: Optional[Callable[[Order], None]] = None,
        initial_reported_qty: int = 0,
    ) -> None:
        if not order.id:
            logger.warning("주문 ID 없음 — 폴링 등록 스킵: %s %s", order.side, order.symbol)
            return
        entry = _PollEntry(
            order=order,
            on_filled=on_filled,
            on_timeout=on_timeout or self._default_timeout_handler,
            on_canceled=on_canceled,
            on_rejected=on_rejected,
            on_expired=on_expired,
            # Seed the replay high-water mark. Live registrations pass 0 (an immediate
            # broker fill must still be reported — see IndicatorStrategy._register_order).
            # Recovery/replay passes the DB-persisted filled_qty so a re-poll after a
            # restart never re-reports shares already processed pre-crash (persistent
            # watermark; the only fill-dedup for a recovered PARTIAL order).
            last_reported_qty=initial_reported_qty,
        )
        with self._lock:
            existing = self._entries.get(order.id)
            if existing is None:
                self._entries[order.id] = entry
            self._health.set_pending_count(len(self._entries))
        if existing is not None:
            # Re-registration of a still-tracked order.id (e.g. startup recovery while
            # the poll loop already runs): mutate the EXISTING entry in place instead of
            # replacing it — one entry per id keeps a single mutex + monotonic watermark.
            # Do it under the entry's OWN processing_lock (the mutex _apply_update uses)
            # so a concurrent poll/resync can't read a half-updated entry or lose the
            # watermark advance (F4). Acquired OUTSIDE self._lock — register never nests
            # self._lock inside processing_lock — so there is no ordering inversion.
            with existing.processing_lock:
                existing.order = order
                existing.on_filled = on_filled
                existing.on_timeout = on_timeout or self._default_timeout_handler
                existing.on_canceled = on_canceled
                existing.on_rejected = on_rejected
                existing.on_expired = on_expired
                existing.last_reported_qty = max(existing.last_reported_qty,
                                                 initial_reported_qty)
        self._health.record_register()
        self._audit("poller_register", order,
                    {"symbol": order.symbol, "side": order.side, "qty": order.qty})
        logger.info("폴링 등록: %s %s %s", order.id, order.side, order.symbol)

    def unregister(self, order_id: str) -> None:
        with self._lock:
            self._entries.pop(order_id, None)
            self._health.set_pending_count(len(self._entries))

    def pending_count(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def health(self) -> PollingHealth:
        return self._health.get_health()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._poll_due(_monotonic())
            self._stop.wait(timeout=5)

    def _poll_due(self, now: float) -> None:
        """One tick. With the breaker closed: poll every due entry, timing out
        the expired ones. Open: nothing — no lookups and **no timeouts** (a
        timeout cancels and converges an order whose status nobody could read).
        Cooldown over: one lookup, the earliest-due entry, tests the broker."""
        state = self._health.circuit_state(now)
        with self._lock:
            entries = list(self._entries.values())
            self._health.set_pending_count(len(self._entries))
        if state == "open" or not entries:
            return
        if state == "probe":
            self._poll_one(min(entries, key=lambda e: e.next_poll_at), probe=True)
            return
        for entry in [e for e in entries if e.next_poll_at <= now]:
            if self._health.is_circuit_open():
                return          # opened during this tick: stop here, time nothing out
            if entry.is_timed_out:
                self._handle_timeout(entry)
                continue
            self._poll_one(entry)

    def _poll_one(self, entry: _PollEntry, probe: bool = False) -> bool:
        try:
            updated = self._broker.get_order_status(entry.order.id, entry.order.symbol)
        except Exception as e:
            logger.warning("폴링 실패 %s: %s", entry.order.id, e)
            if probe:
                self._health.record_probe_failure()
            else:
                opened = self._health.record_poll_error()
                if opened is not None:
                    self._notify(self._on_circuit_open, *opened)
            entry.advance()
            return False

        down = self._health.record_poll_success()
        if down is not None:
            self._notify(self._on_circuit_close, down)

        if updated is None:
            entry.advance()
            return True

        self._apply_update(entry, updated)
        return True

    @staticmethod
    def _notify(callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as e:  # an alert must never stop the poll loop
            logger.warning("회로 알림 콜백 오류: %s", e)

    def resync(self, broker_order: Order) -> tuple[bool, bool]:
        """Reconciler entry point — repair a missed callback WITHOUT a restart.

        Re-drives a broker-confirmed order through the SAME processing pipeline as
        live polling (`_apply_update` → the registered `on_filled` / terminal
        callbacks), so there is exactly one fill processor. Idempotent: the
        increment is computed against the entry's persistent watermark, so a state
        already applied is a no-op.

        Returns ``(owned, applied)``:
        - ``owned`` — True if a live entry exists for this order and was driven.
          When False the caller should fall back to a DB-only sync (no runtime
          pipeline is bound here).
        - ``applied`` — True if the update settled. False means the fill callback
          raised and the entry was RETAINED for the poller to retry; the caller
          must NOT DB-write (a write now + the retry would double-count) and must
          NOT record the order as repaired.
        """
        with self._lock:
            entry = self._entries.get(broker_order.id)
        if entry is None:
            return (False, False)
        applied = self._apply_update(entry, broker_order)
        return (True, applied)

    def _apply_update(self, entry: _PollEntry, updated: Order) -> bool:
        """Serialize per-entry, then apply. resync() (reconciler) and the background
        poll loop can both target the same entry; the per-entry lock guarantees exactly
        one in-flight update so an increment is never computed twice against a stale
        watermark (double-apply). Returns True if the update settled; False if a fill
        callback raised and the entry was RETAINED for a later retry."""
        with entry.processing_lock:
            return self._apply_update_locked(entry, updated)

    def _apply_update_locked(self, entry: _PollEntry, updated: Order) -> bool:
        """Shared processing core for a broker status update (poll + resync).

        This is the single place where a broker-confirmed state is turned into a
        runtime effect. On a FILLED/PARTIAL increment the watermark is advanced and
        the entry popped ONLY after the callback succeeds — a callback that raises
        leaves the entry registered so the next poll re-drives the same increment
        (lost-callback self-heal), and never double-counts thanks to the watermark.
        """
        # Transition validation — warn on unexpected broker status regression.
        prev_status = entry.order.status
        if updated.status != prev_status:
            valid = _VALID_POLLER_TRANSITIONS.get(prev_status, frozenset())
            if updated.status not in valid:
                logger.warning(
                    "예상치 못한 상태 전환 %s → %s (order=%s)",
                    prev_status.value, updated.status.value, updated.id,
                )

        entry.order = updated

        if updated.status == OrderStatus.FILLED:
            incremental = (updated.filled_qty or 0) - entry.last_reported_qty
            logger.info("체결 확인 %s: %s qty=%d (증분=%d) avg=%.4f",
                        updated.id, updated.symbol, updated.filled_qty,
                        incremental, updated.avg_fill_price or 0)
            if incremental > 0:
                # Pass a copy with filled_qty=incremental so the callback records
                # only the new quantity — same convention as PARTIAL_FILLED.
                final_fill = dataclasses.replace(updated, filled_qty=incremental,
                                                 cumulative_filled_qty=updated.filled_qty)
                try:
                    entry.on_filled(final_fill)
                except Exception as e:
                    # Callback lost — keep the entry so the next poll retries. Do NOT
                    # advance the watermark or pop, so the same increment recomputes.
                    logger.error("on_filled 콜백 오류 — 재시도 위해 유지: %s", e)
                    entry.advance()
                    return False
                entry.last_reported_qty = updated.filled_qty
                self._health.record_fill()
                self._audit("poller_filled", updated,
                            {"incremental": incremental, "total": updated.filled_qty,
                             "avg": updated.avg_fill_price or 0})
            else:
                logger.warning("체결 중복 감지 — 콜백 스킵: %s", updated.id)
            # FILLED is terminal: unregister once the increment is settled.
            with self._lock:
                self._entries.pop(updated.id, None)
                self._health.set_pending_count(len(self._entries))

        elif updated.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            if entry.terminal_fired:
                # A poll and a resync() can both deliver the same terminal event; the
                # first fired the callback + audit, so the second is a no-op (F7).
                return True
            entry.terminal_fired = True
            logger.warning("주문 취소/거부/만료: %s status=%s", updated.id, updated.status.value)
            with self._lock:
                self._entries.pop(updated.id, None)
                self._health.set_pending_count(len(self._entries))
            self._audit(f"poller_{updated.status.value}", updated,
                        {"prev_status": prev_status.value,
                         "partial_qty": entry.last_reported_qty})

            _terminal_cb = {
                OrderStatus.CANCELED: entry.on_canceled,
                OrderStatus.REJECTED: entry.on_rejected,
                OrderStatus.EXPIRED:  entry.on_expired,
            }.get(updated.status)

            _health_record = {
                OrderStatus.CANCELED: self._health.record_cancel,
                OrderStatus.REJECTED: self._health.record_reject,
                OrderStatus.EXPIRED:  self._health.record_expired,
            }.get(updated.status)
            if _health_record:
                _health_record()

            if _terminal_cb is not None:
                try:
                    _terminal_cb(updated)
                except Exception as e:
                    logger.warning("terminal callback error [%s]: %s", updated.status, e)

        elif updated.status == OrderStatus.PARTIAL_FILLED:
            incremental = (updated.filled_qty or 0) - entry.last_reported_qty
            if incremental > 0:
                logger.info("부분체결 %s: 증분=%d (누적=%d/%d)",
                            updated.id, incremental, updated.filled_qty, updated.qty)
                # Pass a copy with filled_qty=incremental so the callback records
                # only the new quantity without double-counting prior partials.
                partial = dataclasses.replace(updated, filled_qty=incremental,
                                              cumulative_filled_qty=updated.filled_qty)
                try:
                    entry.on_filled(partial)
                except Exception as e:
                    # Callback lost — keep watermark unchanged so the next poll
                    # recomputes the same increment (self-heal, no double count).
                    logger.error("on_filled 콜백 오류 (부분체결) — 재시도 위해 유지: %s", e)
                    entry.advance()
                    return False
                entry.last_reported_qty = updated.filled_qty
                self._health.record_partial_fill()
                self._audit("poller_partial_filled", updated,
                            {"incremental": incremental, "cumulative": updated.filled_qty})
            entry.advance()

        else:
            entry.advance()

        return True

    def _handle_timeout(self, entry: _PollEntry) -> None:
        # Serialize with _apply_update: since P3-02C a reconciler resync() is a second
        # thread that can be applying a fill on this same entry. Take the entry mutex so
        # a timeout can't cancel an order a resync is concurrently filling (F5).
        with entry.processing_lock:
            self._handle_timeout_locked(entry)

    def _handle_timeout_locked(self, entry: _PollEntry) -> None:
        # A concurrent/preceding resync() may have already driven this entry terminal;
        # never cancel an order that already filled or terminated.
        if entry.order.status in (OrderStatus.FILLED, OrderStatus.CANCELED,
                                  OrderStatus.REJECTED, OrderStatus.EXPIRED):
            return
        logger.warning("주문 타임아웃 (%dm): %s %s %s",
                       _TIMEOUT_MINUTES, entry.order.id, entry.order.side, entry.order.symbol)
        with self._lock:
            self._entries.pop(entry.order.id, None)
            self._health.set_pending_count(len(self._entries))

        # Auto-cancel via broker using market-appropriate kwargs.
        if self._semantic_mapper is not None:
            try:
                kwargs = self._semantic_mapper.cancel_kwargs(entry.order)
                self._broker.cancel_order(**kwargs)
                logger.info("타임아웃 자동취소 성공: %s", entry.order.id)
            except Exception as e:
                logger.warning("타임아웃 자동취소 실패 %s: %s", entry.order.id, e)

        self._health.record_timeout()
        self._audit("poller_timeout", entry.order,
                    {"elapsed_minutes": _TIMEOUT_MINUTES,
                     "last_reported_qty": entry.last_reported_qty})
        try:
            entry.on_timeout(entry.order)
        except Exception as e:
            logger.error("on_timeout 콜백 오류: %s", e)

    def _audit(self, event_type: str, order: Order, detail: dict) -> None:
        """Fire-and-forget AuditLog write. Never raises."""
        if self._db_factory is None:
            return
        try:
            from backend.database.models import AuditLog
            sess = self._db_factory()
            try:
                sess.add(AuditLog(
                    event_type=event_type,
                    symbol=order.symbol,
                    order_id=order.id,
                    actor="poller",
                    detail=json.dumps(detail, ensure_ascii=False),
                ))
                sess.commit()
            finally:
                sess.close()
        except Exception as e:
            logger.warning("AuditLog 쓰기 실패 (%s): %s", event_type, e)

    @staticmethod
    def _default_timeout_handler(order: Order) -> None:
        logger.error("주문 타임아웃 미처리: %s %s %s — 수동 취소 필요",
                     order.id, order.side, order.symbol)
