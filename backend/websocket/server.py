"""
WebSocket 실시간 push 서버.
Redis Pub/Sub에서 이벤트를 받아 연결된 클라이언트에 브로드캐스트.
Flask-SocketIO 기반 (단일 프로세스에서 API와 함께 실행 가능).
"""
import json
import logging
import os
import threading
import time

import redis
from flask import Flask, request
from flask_socketio import SocketIO, emit, disconnect

logger = logging.getLogger(__name__)

_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

# Importing this module must NOT require QUANTDINGER_SECRET_KEY: worker processes
# (e.g. kis-worker, which has no such env var) do `from backend.websocket.server import
# publish_alert/publish_order_update`, and those helpers use only the Redis client below.
# The secret is enforced at server-start (see _require_ws_secret / __main__) so the
# standalone WS server still refuses to run with an insecure Flask session secret.
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("QUANTDINGER_SECRET_KEY") or None

# Restrict CORS to explicit origins when WS_CORS_ORIGINS is set.
_ws_cors = os.environ.get("WS_CORS_ORIGINS", "*")
socketio = SocketIO(app, cors_allowed_origins=_ws_cors, async_mode="threading")

_r = redis.from_url(_REDIS_URL)


def _verify_ws_token() -> bool:
    """Validate JWT token passed as query param ?token=<jwt>.
    Returns True if valid, False otherwise.
    """
    token = request.args.get("token", "")
    if not token:
        return False
    try:
        # Import here to avoid circular dependency
        import sys
        import importlib
        # Try api.auth first (FastAPI stack), then fallback
        for mod_name in ("api.auth", "backend.api.auth"):
            try:
                mod = importlib.import_module(mod_name)
                payload = mod.decode_access_token(token)
                return payload is not None
            except (ImportError, AttributeError):
                continue
        return False
    except Exception:
        return False


# ── 클라이언트 이벤트 ─────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    if not _verify_ws_token():
        logger.warning("WS 인증 실패 — 연결 거부: %s", request.remote_addr)
        disconnect()
        return False
    logger.info("WS 클라이언트 연결: %s", request.remote_addr)
    emit("connected", {"status": "ok"})


@socketio.on("disconnect")
def on_disconnect():
    logger.info("WS 클라이언트 연결 해제")


@socketio.on("subscribe")
def on_subscribe(data):
    """클라이언트가 구독할 채널 등록 (현재는 전체 브로드캐스트)."""
    emit("subscribed", {"channels": data.get("channels", [])})


# ── Redis → WebSocket 브리지 ─────────────────────────────────────────────
def _redis_listener():
    _CHANNELS = ["order:update", "position:update", "equity:update", "alert"]
    backoff = 2.0
    while True:
        try:
            pubsub = _r.pubsub()
            pubsub.subscribe(*_CHANNELS)
            logger.info("Redis 리스너 시작 (%s)", _CHANNELS)
            backoff = 2.0

            for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                channel = message["channel"].decode()
                try:
                    data = json.loads(message["data"])
                except Exception:
                    data = {"raw": message["data"].decode()}
                socketio.emit(channel, data)

        except Exception as e:
            logger.error("Redis 리스너 끊김: %s — %.1fs 후 재연결", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


def start_redis_listener():
    t = threading.Thread(target=_redis_listener, daemon=True, name="ws-redis-listener")
    t.start()


def publish_order_update(order_data: dict):
    _r.publish("order:update", json.dumps(order_data))


def publish_position_update(positions: list):
    _r.publish("position:update", json.dumps(positions))


def publish_equity_update(equity: dict):
    _r.publish("equity:update", json.dumps(equity))


def publish_alert(message: str, level: str = "info"):
    _r.publish("alert", json.dumps({"message": message, "level": level}))


def _require_ws_secret() -> None:
    """Fail fast if the WS server would run with an insecure/empty Flask session secret.
    Enforced only when starting the server process — never on import."""
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError(
            "QUANTDINGER_SECRET_KEY environment variable is not set. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _require_ws_secret()
    start_redis_listener()
    port = int(os.environ.get("WS_PORT", 5002))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
