"""When a strategy run was actually running, for the 4-week paper gate.

The gate counts this, not calendar days (``promotion_guard.uptime_by_run``): six
hours without a worker running the run means six more hours to go. The Redis
heartbeat (``heartbeat.py``) expires after 90 s and keeps no history, and "the
worker is up" is not the same as "the run is running" — a run whose restore
failed stays active for the next boot with nothing running it. So each run's
session records its own ``run_uptime`` rows: from a successful
``strategy.start()``, ``last_beat_at`` moved every minute, ``ended_at`` set when
the session ends. A crash leaves the row ending at its last beat.

Recording is switched on by the worker only once startup recovery has succeeded
(:func:`backend.worker.runner.enable_run_uptime`): a worker still recovering, or
left in SafeMode, cannot trade.

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
#: (``promotion_guard.uptime_by_run``) ends a row at ``last_beat_at + UPTIME_GRACE``,
#: and the recorder starts a new row after a longer gap. Above the beat interval,
#: so the time between two beats is never downtime.
UPTIME_GRACE = timedelta(seconds=2 * BEAT_INTERVAL_SEC)
#: How long :meth:`UptimeRecorder.stop` waits for its final write. A stalled DB
#: must not hold up the session's exit (or the worker's shutdown budget); without
#: the write the row ends at its last beat, which is counted the same as a crash.
STOP_WRITE_TIMEOUT_SEC = 2.0


class UptimeRecorder:
    def __init__(self, db_factory, run_id: int,
                 interval_sec: float = BEAT_INTERVAL_SEC, clock=datetime.utcnow):
        self._factory = db_factory
        self._run_id = run_id
        self._interval = interval_sec
        self._clock = clock
        self._row_id: int | None = None
        self._last_ok: datetime | None = None  # when the last beat was written
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.beat()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"run-uptime-{self._run_id}")
        self._thread.start()
        logger.info("[run_id=%d] 가동 기록 시작 (run_uptime id=%s, %ds 주기)",
                    self._run_id, self._row_id, self._interval)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self.beat()

    def beat(self, ended: bool = False) -> None:
        """Move ``last_beat_at`` to now (and ``ended_at`` when ``ended``). Never raises."""
        from backend.database.models import RunUptime
        with self._lock:
            now = self._clock()
            try:
                db = self._factory()
                try:
                    row = None
                    if (self._row_id is not None and self._last_ok is not None
                            and now - self._last_ok <= UPTIME_GRACE):
                        row = db.get(RunUptime, self._row_id)
                    if row is None:
                        row = RunUptime(run_id=self._run_id, boot_at=now)
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
                logger.warning("[run_id=%d] 가동 기록 실패 — 이 시간은 4주 관문에 세지 않는다: %s",
                               self._run_id, e)

    def stop(self, timeout: float = STOP_WRITE_TIMEOUT_SEC) -> bool:
        """Stop beating and record a clean end, waiting at most ``timeout`` for
        the write. Never raises. ``False`` if the write did not finish in time."""
        self._stop.set()
        writer = threading.Thread(target=self.beat, kwargs={"ended": True}, daemon=True,
                                  name=f"run-uptime-{self._run_id}-end")
        writer.start()
        writer.join(timeout)
        if writer.is_alive():
            logger.warning("[run_id=%d] 가동 종료 기록이 %.1fs 안에 끝나지 않음 — "
                           "마지막 박동에서 끝난 것으로 센다", self._run_id, timeout)
            return False
        return True
