"""When the worker was alive, for the 4-week paper gate.

The gate counts worker uptime, not calendar days
(``promotion_guard.run_uptime``): six hours down means six more hours to go. The
Redis heartbeat (``heartbeat.py``) expires after 90 s and keeps no history, so
this keeps one ``worker_uptime`` row per process lifetime: inserted when the
worker is up, ``last_beat_at`` moved every minute, ``ended_at`` set on a clean
shutdown. A crash leaves the row ending at its last beat.

Every failure is logged and swallowed — this never touches trading. A beat that
could not be written is time not counted (fail-closed): when the next beat lands
more than :data:`UPTIME_GRACE` after the last one that was written, it opens a
new row instead of stretching the old one over the gap. The same goes for a
process that was frozen (container paused, host suspended) — it was not running.
"""
import logging
import threading
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

#: How often ``last_beat_at`` moves.
BEAT_INTERVAL_SEC = 60
#: How far past its last beat a row still counts, and the longest gap between two
#: written beats that one row may span. Both sides use this one value: the gate
#: (``promotion_guard.run_uptime``) ends a row at ``last_beat_at + UPTIME_GRACE``,
#: and the recorder starts a new row after a longer gap. Above the beat interval,
#: so the time between two beats is never downtime.
UPTIME_GRACE = timedelta(seconds=2 * BEAT_INTERVAL_SEC)


class UptimeRecorder:
    def __init__(self, db_factory, worker_id: str = "kis-worker",
                 interval_sec: float = BEAT_INTERVAL_SEC, clock=datetime.utcnow):
        self._factory = db_factory
        self._worker_id = worker_id
        self._interval = interval_sec
        self._clock = clock
        self._row_id: int | None = None
        self._last_ok: datetime | None = None  # when the last beat was written
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.beat()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="worker-uptime")
        self._thread.start()
        logger.info("가동 기록 시작 (worker_uptime id=%s, %ds 주기)", self._row_id, self._interval)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.beat()

    def beat(self, ended: bool = False) -> None:
        """Move ``last_beat_at`` to now (and ``ended_at`` when ``ended``). Never raises."""
        from backend.database.models import WorkerUptime
        with self._lock:
            now = self._clock()
            try:
                db = self._factory()
                try:
                    row = None
                    if (self._row_id is not None and self._last_ok is not None
                            and now - self._last_ok <= UPTIME_GRACE):
                        row = db.get(WorkerUptime, self._row_id)
                    if row is None:
                        row = WorkerUptime(worker_id=self._worker_id, boot_at=now)
                        db.add(row)
                    row.last_beat_at = now
                    if ended:
                        row.ended_at = now
                    db.commit()
                    self._row_id = row.id
                    self._last_ok = now
                finally:
                    db.close()
            except Exception as e:
                logger.warning("가동 기록 실패 — 이 시간은 4주 관문에 세지 않는다: %s", e)

    def stop(self) -> None:
        """Stop beating and record a clean end. Never raises."""
        self._stop.set()
        self.beat(ended=True)
