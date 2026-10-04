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
from flask_socketio import ConnectionRefusedError, SocketIO, emit

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


def _authorized_payload(auth=None):
    """The token's payload when it is a valid app token (api/auth.py) whose user
    is a listed operator; otherwise ``None``.

    Every channel this server relays — orders, positions, equity, alerts — is the
    worker's single ``.env`` account, and app signup is open, so a valid token
    proves nothing about who may see it. Only ``OPERATOR_USER_IDS``
    (``backend/security/operators.py``) may; empty means every connection is
    refused. The list is read from the environment, so changing it means
    restarting kis-ws — which also drops every open connection.

    A missing JWT library or JWT_SECRET_KEY is a deployment error and raises: it
    used to be swallowed here, so the container rejected every client as
    "unauthenticated" (#189). start_ws_server() checks both before accepting
    connections.
    """
    # The Socket.IO ``auth`` payload first: it travels in the handshake body,
    # not the URL, so it stays out of proxy and access logs. ``?token=`` is
    # still accepted for clients that cannot send ``auth``.
    token = auth.get("token") if isinstance(auth, dict) else None
    if not isinstance(token, str) or not token:
        token = request.args.get("token", "")
    if not token:
        return None
    from backend.security.jwt_tokens import decode_access_token
    payload = decode_access_token(token)
    if payload is None:
        return None
    from backend.security.operators import is_operator_id
    if not is_operator_id(payload.get("sub")):
        return None
    return payload


def _verify_ws_token(auth=None) -> bool:
    return _authorized_payload(auth) is not None


# A socket outlives the token it connected with; without this an operator
# whose token expired would keep receiving the feed for as long as the socket
# stayed open. sid -> token expiry (epoch seconds); swept by _session_sweeper.
_SESSION_SWEEP_SEC = 30
_session_expiry: dict = {}
_session_lock = threading.Lock()


def _expire_sessions(now: float) -> list:
    """Disconnect every socket whose token has expired; returns their sids."""
    with _session_lock:
        expired = [sid for sid, exp in _session_expiry.items() if exp <= now]
        for sid in expired:
            del _session_expiry[sid]
    for sid in expired:
        try:
            socketio.server.disconnect(sid, namespace="/")
        except Exception as e:  # already gone
            logger.debug("만료 세션 끊기 실패 %s: %s", sid, e)
    if expired:
        logger.info("토큰 만료로 WS 연결 %d개 종료", len(expired))
    return expired


def _session_sweeper():
    while True:
        time.sleep(_SESSION_SWEEP_SEC)
        try:
            _expire_sessions(time.time())
        except Exception as e:
            logger.error("WS 세션 만료 점검 실패: %s", e)


# ── 클라이언트 이벤트 ─────────────────────────────────────────────────────
@socketio.on("connect")
def on_connect(auth=None):
    payload = _authorized_payload(auth)
    if payload is None:
        logger.warning("WS 인증 실패 — 연결 거부: %s", request.remote_addr)
        # A refused handshake (CONNECT_ERROR), not a disconnect after accepting:
        # the client then knows it was turned away and does not retry, rather
        # than reading it as the server ending a live session (expired token).
        raise ConnectionRefusedError("unauthorized")
    with _session_lock:
        _session_expiry[request.sid] = float(payload["exp"])
    logger.info("WS 클라이언트 연결: %s", request.remote_addr)
    emit("connected", {"status": "ok"})
    _send_snapshot(request.sid)


@socketio.on("disconnect")
def on_disconnect():
    with _session_lock:
        _session_expiry.pop(getattr(request, "sid", None), None)
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


#: State channels: the latest payload is also kept, so an operator who connects
#: between publishes sees the account at once (``_send_snapshot``). Events
#: (orders, alerts) are not replayed.
_SNAPSHOT_CHANNELS = ("position:update", "equity:update")
_SNAPSHOT_TTL_SEC = 24 * 3600


def _snapshot_key(channel: str) -> str:
    return f"ws:last:{channel}"


def _publish_state(channel: str, payload) -> None:
    data = json.dumps(payload)
    _r.publish(channel, data)
    try:
        _r.set(_snapshot_key(channel), data, ex=_SNAPSHOT_TTL_SEC)
    except Exception as e:  # the live publish went out; only the replay is lost
        logger.warning("WS 스냅샷 저장 실패 (%s): %s", channel, e)


def publish_position_update(positions):
    _publish_state("position:update", positions)


def publish_equity_update(equity: dict):
    _publish_state("equity:update", equity)


def _send_snapshot(sid: str) -> None:
    """Send the latest position/equity payloads to one just-accepted socket."""
    for channel in _SNAPSHOT_CHANNELS:
        try:
            raw = _r.get(_snapshot_key(channel))
            if raw:
                socketio.emit(channel, json.loads(raw), to=sid)
        except Exception as e:
            logger.warning("WS 스냅샷 전송 실패 (%s): %s", channel, e)


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


def _require_token_verifier() -> None:
    """Fail fast if client tokens cannot be verified (no PyJWT or JWT_SECRET_KEY).
    Without this the server starts and refuses every connection (#189)."""
    from backend.security.jwt_tokens import jwt_secret
    jwt_secret()
    from backend.security.operators import warn_legacy_operator_env
    warn_legacy_operator_env()


def start_ws_server() -> None:
    """Bootstrap and run the WS server.
    Call this from any entrypoint (gunicorn WSGI app factory, __main__, etc.)
    so the secret check is never bypassed by non-__main__ launch paths.
    """
    _require_ws_secret()
    _require_token_verifier()
    start_redis_listener()
    threading.Thread(target=_session_sweeper, daemon=True, name="ws-session-sweeper").start()
    port = int(os.environ.get("WS_PORT", 5002))
    # Flask-SocketIO refuses the Werkzeug server unless stdin is a TTY — which it
    # never is in a container — so without this kis-ws exited at startup. One
    # process with threads (async_mode="threading", WebSocket via
    # simple-websocket) is enough for the handful of operator sockets this
    # serves. The port is published on host loopback only (docker-compose.yml);
    # browsers reach it through the web server's /socket.io proxy.
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    start_ws_server()
