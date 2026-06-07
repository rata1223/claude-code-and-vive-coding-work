"""
Idempotent execution system for order submission.

Provides pre-submission duplicate detection using SHA256 fingerprinting,
Redis-backed distributed locking, and broker-side recovery verification.

Usage:
    key = IdempotencyKey.from_order_params("AAPL", "buy", 10, 150.0, strategy_run_id="42")
    result = detector.check(key)
    if result.is_duplicate:
        return  # skip

    with detector.execution_lock(key) as acquired:
        if not acquired:
            return  # concurrent duplicate blocked
        order = broker.place_order(...)
        detector.mark_executing(key, order.id)
"""
import hashlib
import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterator, Optional

from backend.brokers.base import BrokerAdapter
from backend.brokers.models import Order, OrderStatus

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({
    OrderStatus.FILLED, OrderStatus.CANCELED,
    OrderStatus.REJECTED, OrderStatus.EXPIRED,
})


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IdempotencyKey:
    """Immutable order-intent descriptor. `fingerprint` is the canonical SHA256 hash."""

    strategy_run_id: str
    symbol: str
    side: str          # "buy" | "sell"
    qty: int
    price: float
    order_type: str    # "limit" | "market"
    date: str          # YYYY-MM-DD (UTC)
    time_bucket: str   # "HH:MM" at bucket_minutes boundary

    @property
    def fingerprint(self) -> str:
        """64-char hex SHA256 of canonical JSON representation."""
        canonical = json.dumps(
            {
                "v": 1,
                "s": self.strategy_run_id,
                "sym": self.symbol,
                "side": self.side,
                "qty": self.qty,
                "price_cents": round(self.price * 100),  # avoids float imprecision
                "ot": self.order_type,
                "d": self.date,
                "tb": self.time_bucket,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @classmethod
    def from_order_params(
        cls,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        order_type: str = "limit",
        strategy_run_id: str = "",
        bucket_minutes: int = 5,
        _now: Optional[datetime] = None,  # test injection point
    ) -> "IdempotencyKey":
        """Build key from raw order parameters. bucket_minutes windows duplicate signals."""
        now = _now or datetime.now(timezone.utc)
        bucket = (now.minute // bucket_minutes) * bucket_minutes
        return cls(
            strategy_run_id=str(strategy_run_id or ""),
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            order_type=order_type,
            date=now.strftime("%Y-%m-%d"),
            time_bucket=f"{now.hour:02d}:{bucket:02d}",
        )


@dataclass(frozen=True)
class ExecutionRecord:
    """Result of an idempotency store lookup."""
    fingerprint: str
    order_id: str
    status: str
    recorded_at: Optional[datetime] = None


@dataclass(frozen=True)
class DuplicateResult:
    """Output of DuplicateDetector.check()."""
    is_duplicate: bool
    fingerprint: str = ""
    order_id: str = ""
    status: str = ""


class RecoveryOutcome(Enum):
    NOT_FOUND = "not_found"    # broker has no record → safe to re-submit
    IN_FLIGHT = "in_flight"    # order still open → re-register with poller
    COMPLETED = "completed"    # order terminal → skip re-submission


@dataclass(frozen=True)
class VerificationResult:
    """Output of RecoveryVerifier.verify()."""
    outcome: RecoveryOutcome
    order_id: str = ""
    status: str = ""


# ── Idempotency Store ─────────────────────────────────────────────────────────

class IdempotencyStore:
    """
    Redis-primary, AuditLog-fallback storage for execution fingerprints.

    Redis key format:
        record: "idem:{fingerprint}"  value: "{order_id}:{status}"
        lock:   "idem_lock:{fingerprint}"  value: "1"

    DB fallback: AuditLog rows with event_type="idempotency_record",
        order_id = fingerprint[:50] (fits String(50) column),
        detail = JSON with full fingerprint, order_id, status.
    """

    _RECORD_PREFIX = "idem"
    _LOCK_PREFIX = "idem_lock"
    _DB_EVENT_TYPE = "idempotency_record"
    _DB_KEY_LEN = 50  # AuditLog.order_id column length

    def __init__(
        self,
        redis_client=None,
        db_factory=None,
        ttl_seconds: int = 86400,
    ) -> None:
        self._r = redis_client
        self._db = db_factory
        self._ttl = ttl_seconds

    # ── public API ────────────────────────────────────────────────────────

    def set(self, fingerprint: str, order_id: str, status: str) -> None:
        """Record a fingerprint → (order_id, status) mapping."""
        value = f"{order_id}:{status}"
        if self._r is not None:
            try:
                self._r.setex(self._record_key(fingerprint), self._ttl, value)
                return
            except Exception as e:
                logger.warning("Redis set 실패, DB 폴백: %s", e)
        self._db_set(fingerprint, order_id, status)

    def get(self, fingerprint: str) -> Optional[ExecutionRecord]:
        """Return ExecutionRecord if fingerprint was seen, else None."""
        if self._r is not None:
            try:
                raw = self._r.get(self._record_key(fingerprint))
                if raw is not None:
                    val = raw.decode() if isinstance(raw, bytes) else raw
                    oid, _, st = val.partition(":")
                    return ExecutionRecord(fingerprint=fingerprint, order_id=oid, status=st)
            except Exception as e:
                logger.warning("Redis get 실패, DB 폴백: %s", e)
        return self._db_get(fingerprint)

    def acquire_lock(self, fingerprint: str, ttl_seconds: int = 60) -> bool:
        """
        Acquire distributed lock via Redis SET NX EX.
        Returns True if lock acquired. Fail-open when Redis unavailable
        (single-worker deployments continue safely without Redis).
        """
        if self._r is None:
            return True
        try:
            result = self._r.set(
                self._lock_key(fingerprint),
                "1",
                nx=True,
                ex=ttl_seconds,
            )
            return bool(result)
        except Exception as e:
            logger.warning("Redis lock 실패 (fail-open): %s", e)
            return True  # fail-open

    def release_lock(self, fingerprint: str) -> None:
        """Release distributed lock. Swallows all errors."""
        if self._r is None:
            return
        try:
            self._r.delete(self._lock_key(fingerprint))
        except Exception as e:
            logger.warning("Redis lock 해제 실패 (무시): %s", e)

    # ── private helpers ───────────────────────────────────────────────────

    def _record_key(self, fingerprint: str) -> str:
        return f"{self._RECORD_PREFIX}:{fingerprint}"

    def _lock_key(self, fingerprint: str) -> str:
        return f"{self._LOCK_PREFIX}:{fingerprint}"

    def _db_key(self, fingerprint: str) -> str:
        return fingerprint[: self._DB_KEY_LEN]

    def _db_set(self, fingerprint: str, order_id: str, status: str) -> None:
        if self._db is None:
            return
        try:
            from backend.database.models import AuditLog
            sess = self._db()
            try:
                db_key = self._db_key(fingerprint)
                existing = (
                    sess.query(AuditLog)
                    .filter(
                        AuditLog.event_type == self._DB_EVENT_TYPE,
                        AuditLog.order_id == db_key,
                    )
                    .first()
                )
                if existing is None:
                    sess.add(AuditLog(
                        event_type=self._DB_EVENT_TYPE,
                        order_id=db_key,
                        actor="idempotency",
                        detail=json.dumps({
                            "fingerprint": fingerprint,
                            "order_id": order_id,
                            "status": status,
                        }),
                    ))
                else:
                    detail = json.loads(existing.detail or "{}")
                    detail["status"] = status
                    detail["order_id"] = order_id
                    existing.detail = json.dumps(detail)
                sess.commit()
            finally:
                sess.close()
        except Exception as e:
            logger.warning("DB idempotency 기록 실패: %s", e)

    def _db_get(self, fingerprint: str) -> Optional[ExecutionRecord]:
        if self._db is None:
            return None
        try:
            from backend.database.models import AuditLog
            sess = self._db()
            try:
                db_key = self._db_key(fingerprint)
                row = (
                    sess.query(AuditLog)
                    .filter(
                        AuditLog.event_type == self._DB_EVENT_TYPE,
                        AuditLog.order_id == db_key,
                    )
                    .first()
                )
                if row is None:
                    return None
                detail = json.loads(row.detail or "{}")
                if detail.get("fingerprint") != fingerprint:
                    return None  # truncation collision guard
                return ExecutionRecord(
                    fingerprint=fingerprint,
                    order_id=detail.get("order_id", ""),
                    status=detail.get("status", "unknown"),
                    recorded_at=row.created_at,
                )
            finally:
                sess.close()
        except Exception as e:
            logger.warning("DB idempotency 조회 실패: %s", e)
            return None


# ── Duplicate Detector ────────────────────────────────────────────────────────

class DuplicateDetector:
    """High-level idempotency API. Wraps IdempotencyStore."""

    def __init__(self, store: IdempotencyStore) -> None:
        self._store = store

    def check(self, key: IdempotencyKey) -> DuplicateResult:
        """Return DuplicateResult. is_duplicate=True if fingerprint already seen."""
        record = self._store.get(key.fingerprint)
        if record is None:
            return DuplicateResult(is_duplicate=False, fingerprint=key.fingerprint)
        return DuplicateResult(
            is_duplicate=True,
            fingerprint=key.fingerprint,
            order_id=record.order_id,
            status=record.status,
        )

    def mark_executing(self, key: IdempotencyKey, order_id: str) -> None:
        """Call immediately after broker.place_order() succeeds."""
        self._store.set(key.fingerprint, order_id, "executing")

    def mark_completed(self, fingerprint: str, order_id: str, status: str) -> None:
        """Update to terminal status (filled/canceled/rejected/expired)."""
        self._store.set(fingerprint, order_id, status)

    @contextmanager
    def execution_lock(
        self, key: IdempotencyKey, ttl_seconds: int = 60
    ) -> Iterator[bool]:
        """
        Context manager for distributed execution lock.
        Yields True if lock acquired, False if already held by another worker.
        Lock is always released on exit, even on exception.
        """
        acquired = self._store.acquire_lock(key.fingerprint, ttl_seconds)
        try:
            yield acquired
        finally:
            if acquired:
                self._store.release_lock(key.fingerprint)


# ── Recovery Verifier ─────────────────────────────────────────────────────────

class RecoveryVerifier:
    """
    On worker restart, determines what happened to a pending order.

    Priority:
    1. Idempotency store (fast, no broker call)
    2. Broker query (ground truth, network call)
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        detector: DuplicateDetector,
    ) -> None:
        self._broker = broker
        self._detector = detector

    def verify(self, key: IdempotencyKey, order_id: str) -> VerificationResult:
        """
        Determine the recovery outcome for an order.

        Returns:
            NOT_FOUND  — broker has no record; safe to re-submit
            IN_FLIGHT  — order still open; re-register with poller
            COMPLETED  — order reached terminal state; skip re-submission
        """
        # 1. Fast path: idempotency store
        record = self._detector._store.get(key.fingerprint)
        if record is not None and record.status not in ("executing",):
            return VerificationResult(
                outcome=RecoveryOutcome.COMPLETED,
                order_id=record.order_id,
                status=record.status,
            )

        # 2. Broker ground truth
        broker_order: Optional[Order] = None
        try:
            broker_order = self._broker.get_order_status(order_id, key.symbol)
        except Exception as e:
            logger.warning("RecoveryVerifier: 브로커 조회 실패 %s: %s", order_id, e)

        if broker_order is None:
            return VerificationResult(outcome=RecoveryOutcome.NOT_FOUND)

        if broker_order.status in _TERMINAL_STATUSES:
            self._detector.mark_completed(
                key.fingerprint, order_id, broker_order.status.value
            )
            return VerificationResult(
                outcome=RecoveryOutcome.COMPLETED,
                order_id=order_id,
                status=broker_order.status.value,
            )

        return VerificationResult(
            outcome=RecoveryOutcome.IN_FLIGHT,
            order_id=order_id,
            status=broker_order.status.value,
        )
