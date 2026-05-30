"""
gunicorn 설정 파일.

kis-api 컨테이너에서 gunicorn 실행 시 이 파일을 자동 로드:
    gunicorn -c backend/api/gunicorn_conf.py backend.api.server:app

post_fork 훅에서 WorkerWatchdog를 각 gunicorn worker 프로세스에 시작.
__main__ 블록에서 시작하면 gunicorn fork 후 watchdog가 부모 프로세스에만 남아 죽은 상태가 됨.
"""
import logging
import os

# gunicorn 기본 설정
bind = f"0.0.0.0:{os.environ.get('API_PORT', '5001')}"
workers = int(os.environ.get("GUNICORN_WORKERS", "2"))
worker_class = "sync"
timeout = 120
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"


def post_fork(server, worker):
    """각 worker 프로세스 fork 후 WorkerWatchdog 시작."""
    log = logging.getLogger("gunicorn.error")
    try:
        import redis as _redis
        r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        from backend.worker.heartbeat import WorkerWatchdog
        WorkerWatchdog(r).start()
        log.info("WorkerWatchdog 시작 (gunicorn post_fork pid=%d)", worker.pid)
    except Exception as e:
        log.warning("WorkerWatchdog 시작 실패 (post_fork): %s", e)
