"""
WebSocket 실시간 push 서버.
Redis Pub/Sub에서 이벤트를 받아 연결된 클라이언트에 브로드캐스트.
Flask-SocketIO 기반 (단일 프로세스에서 API와 함께 실행 가능).
"""
import json
import logging
import os
import threading

import redis
from flask import Flask
from flask_socketio import SocketIO, emit

logger = logging.getLogger(__name__)

_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("QUANTDINGER_SECRET_KEY", "dev-secret")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

_r = redis.from_url(_REDIS_URL)


# ── 클라이언트 이벤트 ─────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    logger.info("WS 클라이언트 연결")
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
    pubsub = _r.pubsub()
    pubsub.subscribe("order:update", "position:update", "equity:update", "alert")
    logger.info("Redis 리스너 시작 (order/position/equity/alert)")

    for message in pubsub.listen():
        if message["type"] != "message":
            continue
        channel = message["channel"].decode()
        try:
            data = json.loads(message["data"])
        except Exception:
            data = {"raw": message["data"].decode()}

        socketio.emit(channel, data)


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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    start_redis_listener()
    port = int(os.environ.get("WS_PORT", 5002))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
