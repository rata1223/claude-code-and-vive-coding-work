"""
Worker 하트비트 + 데드-워커 감시.

WorkerHeartbeat : 30s 마다 Redis에 TTL 키 갱신
HeartbeatMonitor: 90s 무응답 시 경보 + SAFE_MODE disable
"""
import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_HB_KEY = "worker:heartbeat"
_HB_TTL_SEC = 90      # 3 beats 연속 실패 = dead
_HB_INTERVAL_SEC = 30  # publish interval


class WorkerHeartbeat:
    """Worker 프로세스가 살아있음을 Redis에 기록."""

    def __init__(self, redis_client, worker_id: str = "kis-worker"):
        self._redis = redis_client
        self._worker_id = worker_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._beat()  # immediate first beat
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="worker-heartbeat"
        )
        self._thread.start()
        logger.info("WorkerHeartbeat 시작 (TTL=%ds interval=%ds)", _HB_TTL_SEC, _HB_INTERVAL_SEC)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(_HB_INTERVAL_SEC):
            self._beat()

    def _beat(self) -> None:
        try:
            payload = f"{self._worker_id}:{datetime.now(timezone.utc).isoformat()}"
            self._redis.setex(_HB_KEY, _HB_TTL_SEC, payload)
        except Exception as e:
            logger.warning("하트비트 기록 실패: %s", e)


class HeartbeatMonitor:
    """
    외부 프로세스(kis-api 등)에서 worker가 살아있는지 감시.
    TTL 만료 = worker 죽음 → 경보 발송.

    사용 예: scheduler 혹은 health-check endpoint 에서 호출.
    """

    @staticmethod
    def is_alive(redis_client) -> bool:
        try:
            return redis_client.exists(_HB_KEY) == 1
        except Exception:
            return False

    @staticmethod
    def last_beat(redis_client) -> str | None:
        """마지막 하트비트 타임스탬프 문자열."""
        try:
            val = redis_client.get(_HB_KEY)
            if val:
                return val.decode()
        except Exception:
            pass
        return None

    @staticmethod
    def ttl_seconds(redis_client) -> int:
        """남은 TTL(초). -2 = 만료(dead), -1 = TTL 없음."""
        try:
            return redis_client.ttl(_HB_KEY)
        except Exception:
            return -2


class WorkerWatchdog:
    """
    주기적으로 heartbeat 를 확인하고 죽은 워커를 감지하면:
    1. SAFE_MODE 비활성화 (새 전략 실행 차단)
    2. Telegram 경보
    3. WebSocket alert 발행

    scheduler.py 에서 주기적으로 호출 또는 독립 스레드 실행.
    """

    def __init__(self, redis_client, check_interval_sec: int = 60):
        self._redis = redis_client
        self._interval = check_interval_sec
        self._was_dead = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="worker-watchdog"
        )
        self._thread.start()
        logger.info("WorkerWatchdog 시작 (interval=%ds)", self._interval)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._check()

    def _check(self) -> None:
        alive = HeartbeatMonitor.is_alive(self._redis)
        if not alive and not self._was_dead:
            self._was_dead = True
            logger.critical("Worker 하트비트 없음 — 죽은 Worker 감지")
            self._alert_dead_worker()
        elif alive and self._was_dead:
            self._was_dead = False
            logger.info("Worker 하트비트 복구")
            self._alert_recovery()

    def _alert_dead_worker(self) -> None:
        try:
            from backend.worker.recovery import SAFE_MODE
            SAFE_MODE.disable("Worker 하트비트 없음 — 프로세스 재시작 필요")
        except Exception:
            pass
        try:
            from bot.notifier import alert_emergency
            alert_emergency("⚠️ kis-worker 하트비트 없음 — 프로세스 확인 필요")
        except Exception:
            pass
        try:
            from backend.websocket.server import publish_alert
            publish_alert("Worker 하트비트 없음 — 프로세스 재시작 필요", level="critical")
        except Exception:
            pass

    def _alert_recovery(self) -> None:
        try:
            from backend.websocket.server import publish_alert
            publish_alert("Worker 하트비트 복구됨", level="info")
        except Exception:
            pass
