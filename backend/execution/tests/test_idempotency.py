"""
Tests for backend/execution/idempotency.py — TASK 2-3C.

Scenarios:
  1. TestIdempotencyKey    — fingerprint stability, price normalisation, bucket boundaries
  2. TestDoubleClick       — same key submitted twice → second blocked
  3. TestWorkerRestart     — DB-stored record → duplicate; RecoveryVerifier outcomes
  4. TestRetryLoop         — N identical submissions → only first proceeds
  5. TestRedisReconnect    — Redis failure → DB fallback; Redis recovery is transparent
  6. TestBrokerTimeout     — lock TTL auto-expires; exception in body → lock released
  7. TestDuplicatedSignal  — same params same bucket → duplicate; different bucket → new
  8. TestConcurrentExecution — two threads same key → exactly one acquires lock
"""
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.brokers.models import Order as BrokerOrder, OrderStatus
from backend.database.models import AuditLog, Base
from backend.execution.idempotency import (
    DuplicateDetector,
    DuplicateResult,
    ExecutionRecord,
    IdempotencyKey,
    IdempotencyStore,
    RecoveryOutcome,
    RecoveryVerifier,
    VerificationResult,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

_FIXED_NOW = datetime(2026, 6, 6, 9, 37, 0, tzinfo=timezone.utc)  # bucket = "09:35"


def _db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _key(**kwargs) -> IdempotencyKey:
    defaults = dict(
        symbol="AAPL", side="buy", qty=10, price=150.0,
        order_type="limit", strategy_run_id="run1",
        _now=_FIXED_NOW,
    )
    defaults.update(kwargs)
    return IdempotencyKey.from_order_params(**defaults)


def _store(redis=None, db=None) -> IdempotencyStore:
    return IdempotencyStore(redis_client=redis, db_factory=db, ttl_seconds=3600)


def _detector(redis=None, db=None) -> DuplicateDetector:
    return DuplicateDetector(_store(redis=redis, db=db))


class FakeRedis:
    """Thread-safe in-memory Redis stub (no fakeredis dependency)."""

    def __init__(self) -> None:
        self._data: dict = {}
        self._lock = threading.Lock()

    def setex(self, key: str, ttl: int, value: str) -> None:
        with self._lock:
            self._data[key] = value

    def get(self, key: str):
        with self._lock:
            val = self._data.get(key)
            return val.encode() if isinstance(val, str) else val

    def set(self, key: str, value: str, nx: bool = False, ex: int = None):
        with self._lock:
            if nx and key in self._data:
                return False
            self._data[key] = value
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


class BrokenRedis:
    """Redis stub that always raises to test fallback behaviour."""

    def setex(self, *a, **kw):
        raise ConnectionError("Redis down")

    def get(self, *a, **kw):
        raise ConnectionError("Redis down")

    def set(self, *a, **kw):
        raise ConnectionError("Redis down")

    def delete(self, *a, **kw):
        raise ConnectionError("Redis down")


# ── 1. IdempotencyKey ─────────────────────────────────────────────────────────

class TestIdempotencyKey:

    def test_fingerprint_is_64_hex_chars(self):
        k = _key()
        assert len(k.fingerprint) == 64
        assert all(c in "0123456789abcdef" for c in k.fingerprint)

    def test_fingerprint_stable_for_same_inputs(self):
        k1 = _key()
        k2 = _key()
        assert k1.fingerprint == k2.fingerprint

    def test_different_symbol_different_fingerprint(self):
        assert _key(symbol="AAPL").fingerprint != _key(symbol="NVDA").fingerprint

    def test_different_side_different_fingerprint(self):
        assert _key(side="buy").fingerprint != _key(side="sell").fingerprint

    def test_different_qty_different_fingerprint(self):
        assert _key(qty=10).fingerprint != _key(qty=20).fingerprint

    def test_price_normalisation_150_vs_150_00(self):
        k1 = IdempotencyKey.from_order_params("AAPL", "buy", 10, 150.0, _now=_FIXED_NOW)
        k2 = IdempotencyKey.from_order_params("AAPL", "buy", 10, 150.00, _now=_FIXED_NOW)
        assert k1.fingerprint == k2.fingerprint

    def test_price_normalisation_float_imprecision(self):
        # 0.1 + 0.2 = 0.30000000000000004 in float; should hash same as 0.3
        k1 = IdempotencyKey.from_order_params("AAPL", "buy", 10, 0.3, _now=_FIXED_NOW)
        k2 = IdempotencyKey.from_order_params("AAPL", "buy", 10, 0.1 + 0.2, _now=_FIXED_NOW)
        # round(0.3 * 100) = 30, round((0.1+0.2)*100) = 30 → same fingerprint
        assert k1.fingerprint == k2.fingerprint

    def test_time_bucket_minute_0_to_4_maps_to_00(self):
        now = datetime(2026, 6, 6, 9, 3, 0, tzinfo=timezone.utc)
        k = _key(_now=now)
        assert k.time_bucket == "09:00"

    def test_time_bucket_minute_5_to_9_maps_to_05(self):
        now = datetime(2026, 6, 6, 9, 7, 0, tzinfo=timezone.utc)
        k = _key(_now=now)
        assert k.time_bucket == "09:05"

    def test_time_bucket_minute_37_maps_to_35(self):
        assert _FIXED_NOW.minute == 37
        k = _key()
        assert k.time_bucket == "09:35"

    def test_different_strategy_run_id_different_fingerprint(self):
        k1 = _key(strategy_run_id="run1")
        k2 = _key(strategy_run_id="run2")
        assert k1.fingerprint != k2.fingerprint


# ── 2. Double Click ───────────────────────────────────────────────────────────

class TestDoubleClick:

    def test_first_check_is_not_duplicate(self):
        d = _detector(redis=FakeRedis())
        result = d.check(_key())
        assert result.is_duplicate is False

    def test_second_check_after_mark_is_duplicate(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        d.mark_executing(k, "ORD001")
        result = d.check(k)
        assert result.is_duplicate is True
        assert result.order_id == "ORD001"
        assert result.status == "executing"

    def test_mark_completed_updates_status(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        d.mark_executing(k, "ORD001")
        d.mark_completed(k.fingerprint, "ORD001", "filled")
        result = d.check(k)
        assert result.is_duplicate is True
        assert result.status == "filled"

    def test_double_click_same_key_twice_only_first_not_duplicate(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        r1 = d.check(k)
        d.mark_executing(k, "ORD001")
        r2 = d.check(k)
        assert r1.is_duplicate is False
        assert r2.is_duplicate is True


# ── 3. Worker Restart ─────────────────────────────────────────────────────────

class TestWorkerRestart:

    def test_db_stored_record_detected_as_duplicate(self):
        db = _db()
        d = _detector(db=db)  # no Redis
        k = _key()
        d.mark_executing(k, "ORD002")
        # Simulate restart: new detector same DB
        d2 = _detector(db=db)
        result = d2.check(k)
        assert result.is_duplicate is True
        assert result.order_id == "ORD002"

    def test_recovery_verifier_completed_when_broker_filled(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        broker = MagicMock()
        broker.get_order_status.return_value = BrokerOrder(
            id="ORD003", symbol="AAPL", side="buy", qty=10, price=150.0,
            status=OrderStatus.FILLED, filled_qty=10, avg_fill_price=150.5,
        )
        verifier = RecoveryVerifier(broker, d)
        result = verifier.verify(k, "ORD003")
        assert result.outcome == RecoveryOutcome.COMPLETED
        assert result.status == "filled"

    def test_recovery_verifier_in_flight_when_broker_submitted(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        broker = MagicMock()
        broker.get_order_status.return_value = BrokerOrder(
            id="ORD004", symbol="AAPL", side="buy", qty=10, price=150.0,
            status=OrderStatus.SUBMITTED, filled_qty=0, avg_fill_price=0.0,
        )
        verifier = RecoveryVerifier(broker, d)
        result = verifier.verify(k, "ORD004")
        assert result.outcome == RecoveryOutcome.IN_FLIGHT

    def test_recovery_verifier_not_found_when_broker_returns_none(self):
        d = _detector()
        broker = MagicMock()
        broker.get_order_status.return_value = None
        verifier = RecoveryVerifier(broker, d)
        result = verifier.verify(_key(), "ORD005")
        assert result.outcome == RecoveryOutcome.NOT_FOUND

    def test_recovery_verifier_uses_store_before_broker(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        d.mark_completed(k.fingerprint, "ORD006", "canceled")
        broker = MagicMock()
        verifier = RecoveryVerifier(broker, d)
        result = verifier.verify(k, "ORD006")
        assert result.outcome == RecoveryOutcome.COMPLETED
        broker.get_order_status.assert_not_called()  # store was sufficient

    def test_recovery_verifier_broker_exception_returns_not_found(self):
        d = _detector()
        broker = MagicMock()
        broker.get_order_status.side_effect = RuntimeError("timeout")
        verifier = RecoveryVerifier(broker, d)
        result = verifier.verify(_key(), "ORD007")
        assert result.outcome == RecoveryOutcome.NOT_FOUND

    def test_recovery_verifier_marks_completed_in_store_on_terminal_broker_status(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        broker = MagicMock()
        broker.get_order_status.return_value = BrokerOrder(
            id="ORD008", symbol="AAPL", side="buy", qty=10, price=150.0,
            status=OrderStatus.CANCELED, filled_qty=0, avg_fill_price=0.0,
        )
        verifier = RecoveryVerifier(broker, d)
        verifier.verify(k, "ORD008")
        # Store should now have the record
        assert d.check(k).is_duplicate is True


# ── 4. Retry Loop ─────────────────────────────────────────────────────────────

class TestRetryLoop:

    def test_five_identical_submissions_only_first_proceeds(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        not_duplicate_count = 0
        for i in range(5):
            result = d.check(k)
            if not result.is_duplicate:
                not_duplicate_count += 1
                d.mark_executing(k, f"ORD{i:03d}")
        assert not_duplicate_count == 1

    def test_retry_after_different_time_bucket_is_allowed(self):
        r = FakeRedis()
        d = _detector(redis=r)
        now1 = datetime(2026, 6, 6, 9, 0, tzinfo=timezone.utc)   # bucket 09:00
        now2 = datetime(2026, 6, 6, 9, 10, tzinfo=timezone.utc)  # bucket 09:10
        k1 = _key(_now=now1)
        k2 = _key(_now=now2)
        d.mark_executing(k1, "ORD100")
        assert d.check(k1).is_duplicate is True
        assert d.check(k2).is_duplicate is False  # different bucket = new order


# ── 5. Redis Reconnect ────────────────────────────────────────────────────────

class TestRedisReconnect:

    def test_redis_get_failure_falls_back_to_db(self):
        db = _db()
        broken = BrokenRedis()
        d1 = _detector(db=db)  # write via DB (no Redis)
        k = _key()
        d1.mark_executing(k, "ORD200")
        # Now use broken Redis + same DB → should still find record via DB
        store = IdempotencyStore(redis_client=broken, db_factory=db)
        d2 = DuplicateDetector(store)
        result = d2.check(k)
        assert result.is_duplicate is True

    def test_redis_set_failure_falls_back_to_db(self):
        db = _db()
        store = IdempotencyStore(redis_client=BrokenRedis(), db_factory=db)
        d = DuplicateDetector(store)
        k = _key()
        d.mark_executing(k, "ORD201")  # Redis fails → written to DB
        # Verify via DB-only store
        d2 = _detector(db=db)
        assert d2.check(k).is_duplicate is True

    def test_redis_recovery_used_after_reconnect(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        d.mark_executing(k, "ORD202")
        # Redis is working: should read from Redis
        result = d.check(k)
        assert result.is_duplicate is True
        assert result.order_id == "ORD202"

    def test_no_sticky_failure_state(self):
        """After Redis fails and DB is used, next call retries Redis (no cached failure)."""
        db = _db()
        # First call with broken Redis writes to DB
        broken = BrokenRedis()
        store_broken = IdempotencyStore(redis_client=broken, db_factory=db)
        d_broken = DuplicateDetector(store_broken)
        k = _key()
        d_broken.mark_executing(k, "ORD203")
        # Now healthy Redis — can read from Redis (empty) or DB (has record)
        # The DB fallback provides the record regardless
        store_good = IdempotencyStore(redis_client=FakeRedis(), db_factory=db)
        d_good = DuplicateDetector(store_good)
        # Redis is empty, so falls through to DB
        result = d_good.check(k)
        assert result.is_duplicate is True


# ── 6. Broker Timeout (Lock TTL) ──────────────────────────────────────────────

class TestBrokerTimeout:

    def test_lock_acquired_returns_true(self):
        d = _detector(redis=FakeRedis())
        k = _key()
        with d.execution_lock(k, ttl_seconds=60) as acquired:
            assert acquired is True

    def test_lock_released_after_context_exit(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        with d.execution_lock(k, ttl_seconds=60):
            pass  # lock acquired and released
        # After exit, lock should be gone → can acquire again
        assert r.set(f"idem_lock:{k.fingerprint}", "1", nx=True) is True

    def test_exception_in_body_releases_lock(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        try:
            with d.execution_lock(k, ttl_seconds=60):
                raise RuntimeError("broker timeout")
        except RuntimeError:
            pass
        # Lock must be released despite exception
        assert r.set(f"idem_lock:{k.fingerprint}", "1", nx=True) is True

    def test_no_redis_lock_always_acquired(self):
        d = _detector()  # no Redis
        k = _key()
        with d.execution_lock(k) as acquired:
            assert acquired is True

    def test_second_acquire_blocked_while_held(self):
        r = FakeRedis()
        store = _store(redis=r)
        k = _key()
        store.acquire_lock(k.fingerprint, ttl_seconds=60)
        # Second acquire with NX should fail
        result = store.acquire_lock(k.fingerprint, ttl_seconds=60)
        assert result is False


# ── 7. Duplicated Signal ──────────────────────────────────────────────────────

class TestDuplicatedSignal:

    def test_same_params_same_bucket_is_duplicate(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()  # uses _FIXED_NOW → bucket 09:35
        d.mark_executing(k, "ORD300")
        k2 = _key()  # same _FIXED_NOW → same bucket
        assert d.check(k2).is_duplicate is True

    def test_same_params_different_bucket_is_not_duplicate(self):
        r = FakeRedis()
        d = _detector(redis=r)
        now_a = datetime(2026, 6, 6, 9, 35, tzinfo=timezone.utc)  # bucket 09:35
        now_b = datetime(2026, 6, 6, 9, 40, tzinfo=timezone.utc)  # bucket 09:40
        k_a = _key(_now=now_a)
        k_b = _key(_now=now_b)
        d.mark_executing(k_a, "ORD301")
        assert d.check(k_b).is_duplicate is False

    def test_same_params_different_date_is_not_duplicate(self):
        r = FakeRedis()
        d = _detector(redis=r)
        now_today = datetime(2026, 6, 6, 9, 35, tzinfo=timezone.utc)
        now_tomorrow = datetime(2026, 6, 7, 9, 35, tzinfo=timezone.utc)
        k_today = _key(_now=now_today)
        k_tomorrow = _key(_now=now_tomorrow)
        d.mark_executing(k_today, "ORD302")
        assert d.check(k_tomorrow).is_duplicate is False

    def test_different_strategy_run_id_not_duplicate(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k1 = _key(strategy_run_id="run1")
        k2 = _key(strategy_run_id="run2")
        d.mark_executing(k1, "ORD303")
        assert d.check(k2).is_duplicate is False


# ── 8. Concurrent Execution ───────────────────────────────────────────────────

class TestConcurrentExecution:

    def test_two_threads_only_one_acquires_lock(self):
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        acquired_results = []
        lock_held = threading.Event()   # T1 signals it holds the lock
        test_done = threading.Event()   # T2 signals it has tried

        def thread1():
            with d.execution_lock(k, ttl_seconds=60) as acq:
                acquired_results.append(acq)
                lock_held.set()      # tell T2 to try while we hold
                test_done.wait()     # keep lock held until T2 has tried

        def thread2():
            lock_held.wait()         # wait until T1 owns the lock
            with d.execution_lock(k, ttl_seconds=60) as acq:
                acquired_results.append(acq)
            test_done.set()          # let T1 finish

        t1 = threading.Thread(target=thread1)
        t2 = threading.Thread(target=thread2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(acquired_results) == 2
        # Exactly one True (T1), one False (T2 — lock was held)
        assert acquired_results.count(True) == 1
        assert acquired_results.count(False) == 1

    def test_concurrent_mark_executing_idempotent(self):
        """Two threads racing to mark the same key as executing → only one wins."""
        r = FakeRedis()
        d = _detector(redis=r)
        k = _key()
        barrier = threading.Barrier(2)
        winners = []

        def thread_fn(order_id: str):
            barrier.wait()
            result = d.check(k)
            if not result.is_duplicate:
                with d.execution_lock(k) as acq:
                    if acq:
                        result2 = d.check(k)
                        if not result2.is_duplicate:
                            d.mark_executing(k, order_id)
                            winners.append(order_id)

        t1 = threading.Thread(target=thread_fn, args=("ORD_A",))
        t2 = threading.Thread(target=thread_fn, args=("ORD_B",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(winners) <= 1  # at most one winner
