"""kis-ws can verify the app's access tokens (#189).

The WS server checked tokens by importing ``api.auth``, but its image
(``Dockerfile.kis-bot``) ships neither ``api/`` nor a JWT library nor
``JWT_SECRET_KEY``. The ``ImportError`` was swallowed, so every client was
refused as unauthenticated. Verification now lives in
``backend/security/jwt_tokens.py``; PyJWT is in ``requirements.txt``; compose
passes the API's ``JWT_SECRET_KEY`` to kis-ws; and a missing secret stops the
server at start instead of failing every connection.

A valid token alone is not enough: the relayed data is the operator's single
``.env`` account and signup is open, so only ``OPERATOR_USER_IDS`` may connect
— user ids, because signup does not verify mailboxes and an operator's email
could be registered by someone else.
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest

from backend.security import jwt_tokens

ROOT = Path(__file__).resolve().parents[2]
SECRET = "ws-test-secret-ws-test-secret-ws-test-secret-0123"
OPERATOR_ID = "42"


def _token(secret=SECRET, alg="HS256", minutes=30, sub=OPERATOR_ID, email="ops@example.com"):
    exp = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    claims = {"exp": exp, "email": email}
    if sub is not None:
        claims["sub"] = sub
    return jwt.encode(claims, secret, algorithm=alg)


@pytest.fixture()
def secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", SECRET)
    monkeypatch.setenv("OPERATOR_USER_IDS", f"1, {OPERATOR_ID} ")
    return SECRET


# ── the shared verifier ────────────────────────────────────────────────────

class TestVerifier:
    def test_a_valid_token_decodes(self, secret):
        assert jwt_tokens.decode_access_token(_token())["sub"] == OPERATOR_ID

    def test_a_signed_token_without_exp_is_rejected(self, secret):
        """A token with no expiry would never expire (CodeRabbit)."""
        token = jwt.encode({"sub": OPERATOR_ID}, SECRET, algorithm="HS256")
        assert jwt_tokens.decode_access_token(token) is None

    @pytest.mark.parametrize("token", [
        "not-a-jwt",
        _token(secret="some-other-secret-some-other-secret-0000"),
        _token(minutes=-1),
        _token(alg="HS512"),
        jwt.encode({"sub": "7"}, None, algorithm="none"),
    ], ids=["malformed", "wrong-secret", "expired", "hs512", "alg-none"])
    def test_anything_else_is_none(self, secret, token):
        assert jwt_tokens.decode_access_token(token) is None

    def test_a_missing_secret_is_a_configuration_error(self, monkeypatch):
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
            jwt_tokens.decode_access_token(_token())


# ── the WS server ──────────────────────────────────────────────────────────

def _ws_check(token):
    from backend.websocket import server
    with server.app.test_request_context(f"/?token={token}"):
        return server._verify_ws_token()


class TestWsServer:
    def test_a_valid_token_is_accepted(self, secret):
        assert _ws_check(_token()) is True

    def test_a_bad_or_absent_token_is_refused(self, secret):
        assert _ws_check(_token(minutes=-1)) is False
        assert _ws_check("") is False

    def test_a_valid_token_from_a_non_operator_is_refused(self, secret):
        """Signup is open: any account can mint a valid token."""
        assert _ws_check(_token(sub="7")) is False
        assert _ws_check(_token(sub=None)) is False

    def test_registering_an_operators_email_gains_nothing(self, secret, monkeypatch):
        """Signup does not verify mailboxes: a newcomer may hold the operator's
        address, but not the operator's id."""
        monkeypatch.setenv("OPERATOR_USER_IDS", OPERATOR_ID)
        assert _ws_check(_token(sub="999", email="ops@example.com")) is False
        assert _ws_check(_token(sub=OPERATOR_ID, email="anything@else.com")) is True

    def test_the_list_takes_ids_with_spaces(self, secret):
        assert _ws_check(_token(sub="1")) is True

    def test_no_operators_configured_refuses_everyone(self, secret, monkeypatch, caplog):
        from backend.websocket import server
        monkeypatch.setenv("OPERATOR_USER_IDS", " , ")
        assert _ws_check(_token()) is False
        with caplog.at_level("WARNING"):
            server._require_token_verifier()          # starts, but says why
        assert "OPERATOR_USER_IDS" in caplog.text

    def test_a_missing_secret_raises_instead_of_refusing_everyone(self, monkeypatch):
        """The old path turned a deployment error into "unauthenticated"."""
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        with pytest.raises(RuntimeError):
            _ws_check(_token())

    def test_the_server_will_not_start_without_a_secret(self, monkeypatch):
        from backend.websocket import server
        monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
        with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
            server._require_token_verifier()

    def test_start_runs_the_check(self):
        from backend.websocket import server
        import inspect
        src = inspect.getsource(server.start_ws_server)
        assert "_require_token_verifier()" in src


# ── deployment ─────────────────────────────────────────────────────────────

def _service(name: str) -> str:
    text = (ROOT / "docker-compose.yml").read_text()
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|^\S|\Z)", text, re.M | re.S)
    assert m, name
    return m.group(1)


def test_kis_ws_gets_the_same_secret_the_api_signs_with():
    jwt_line = re.compile(r"^\s+JWT_SECRET_KEY:\s*\$\{JWT_SECRET_KEY:\?", re.M)
    assert jwt_line.search(_service("api"))
    assert jwt_line.search(_service("kis-ws"))


def test_kis_ws_reads_the_operator_list_and_defaults_to_nobody():
    line = re.compile(r"^\s+OPERATOR_USER_IDS:\s*\$\{OPERATOR_USER_IDS:-\}\s*$", re.M)
    assert line.search(_service("kis-ws"))
    assert line.search(_service("api")), "one operator list for the api controls and the feed"
    assert "WS_OPERATOR_USER_IDS" not in (ROOT / "docker-compose.yml").read_text()


def test_requirements_pin_pyjwt_like_the_api():
    pin = re.compile(r"^PyJWT==([\d.]+)$", re.M)
    ws = pin.search((ROOT / "requirements.txt").read_text())
    api = pin.search((ROOT / "requirements-api.txt").read_text())
    assert ws and api and ws.group(1) == api.group(1)


def test_the_ws_image_verifies_tokens_from_only_what_it_copies(tmp_path):
    """Rebuild the kis-ws image's /app from Dockerfile.kis-bot's COPY lines and
    verify a token there — with ``api/`` unreachable."""
    dirs = re.findall(r"^COPY\s+(\S+)/\s+\./\1/\s*$",
                      (ROOT / "Dockerfile.kis-bot").read_text(), re.M)
    assert "backend" in dirs and "api" not in dirs
    for name in dirs:
        (tmp_path / name).symlink_to(ROOT / name, target_is_directory=True)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(JWT_SECRET_KEY=SECRET, OPERATOR_USER_IDS=OPERATOR_ID, PYTHONDONTWRITEBYTECODE="1")
    code = (
        "import sys, importlib.util; sys.path.insert(0, '.');"
        "assert not any(p.startswith(%r) for p in sys.path if p), sys.path;"
        "assert importlib.util.find_spec('api') is None;"
        "from backend.websocket import server;"
        "server._require_token_verifier();"
        # minted here, on the child's real clock (the kst_* plugins freeze ours)
        "import jwt, time;"
        "token = jwt.encode({'sub': %r, 'exp': int(time.time()) + 600}, %r, algorithm='HS256');"
        "ctx = server.app.test_request_context('/?token=' + token);"
        "ctx.push(); assert server._verify_ws_token() is True"
    ) % (str(ROOT), OPERATOR_ID, SECRET)
    proc = subprocess.run([sys.executable, "-P", "-c", code],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]


# ── a socket does not outlive its token ────────────────────────────────────

class TestSessionExpiry:
    """CodeRabbit: an accepted socket stayed authorized after its token expired."""

    @staticmethod
    def _client(token):
        from backend.websocket import server
        return server.socketio.test_client(server.app, query_string=f"token={token}")

    def test_a_real_connect_is_accepted_and_tracked(self, secret):
        """Also the Flask/flask-socketio compatibility check: 5.3.6 raised on
        every event under Flask 3.1."""
        from backend.websocket import server
        client = self._client(_token(minutes=5))
        try:
            assert client.is_connected()
            assert client.eio_sid in server.socketio.server.environ  # sanity: live socket
            with server._session_lock:
                assert len(server._session_expiry) >= 1
        finally:
            client.disconnect()

    def test_a_non_operator_connect_is_refused(self, secret):
        client = self._client(_token(sub="7"))
        assert not client.is_connected()

    def test_the_sweep_drops_a_socket_whose_token_expired(self, secret):
        import time as _time
        from backend.websocket import server
        client = self._client(_token(minutes=5))
        assert client.is_connected()
        with server._session_lock:
            tracked = dict(server._session_expiry)
        assert tracked
        server._expire_sessions(_time.time())          # nothing expired yet
        assert client.is_connected()
        dropped = server._expire_sessions(max(tracked.values()) + 1)
        assert set(tracked) <= set(dropped)
        assert not client.is_connected()

    def test_disconnect_forgets_the_socket(self, secret):
        from backend.websocket import server
        client = self._client(_token(minutes=5))
        with server._session_lock:
            before = set(server._session_expiry)
        client.disconnect()
        with server._session_lock:
            assert len(server._session_expiry) == len(before) - 1

    def test_start_runs_the_sweeper(self):
        import inspect
        from backend.websocket import server
        assert "_session_sweeper" in inspect.getsource(server.start_ws_server)


class TestHandshakeAuth:
    """The app client sends its token in the Socket.IO ``auth`` payload, which
    stays out of URLs (proxy and access logs); ``?token=`` still works."""

    def test_a_token_in_auth_is_accepted(self, secret):
        from backend.websocket import server
        client = server.socketio.test_client(server.app, auth={"token": _token(minutes=5)})
        try:
            assert client.is_connected()
        finally:
            client.disconnect()

    def test_a_non_operator_token_in_auth_is_refused(self, secret):
        from backend.websocket import server
        client = server.socketio.test_client(server.app, auth={"token": _token(sub="7")})
        assert not client.is_connected()

    @pytest.mark.parametrize("auth", [{}, {"token": ""}, {"token": 5}, ["x"], "x"])
    def test_a_malformed_auth_falls_back_to_the_query(self, secret, auth):
        from backend.websocket import server
        with server.app.test_request_context(f"/?token={_token()}"):
            assert server._verify_ws_token(auth) is True
        with server.app.test_request_context("/"):
            assert server._verify_ws_token(auth) is False


def _free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_server_starts_without_a_tty():
    """In a container stdin is not a TTY, and Flask-SocketIO then refused the
    Werkzeug server: kis-ws exited at startup. Start it the way compose does
    (no TTY) and wait for the Engine.IO handshake."""
    import subprocess
    import sys
    import time as _time
    import urllib.request
    port = _free_port()
    env = dict(os.environ, QUANTDINGER_SECRET_KEY="ws-start-test", JWT_SECRET_KEY="ws-start-test",
               OPERATOR_USER_IDS="3", WS_PORT=str(port),
               REDIS_URL="redis://127.0.0.1:1")      # unreachable: the listener retries, startup must not care
    proc = subprocess.Popen([sys.executable, "-m", "backend.websocket.server"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, cwd=str(ROOT))
    try:
        deadline = _time.monotonic() + 20
        body = None
        while _time.monotonic() < deadline and proc.poll() is None:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/socket.io/?EIO=4&transport=polling", timeout=2) as r:
                    body = r.read().decode()
                break
            except OSError:
                _time.sleep(0.3)
        if body is None:
            proc.kill()
            out = proc.communicate(timeout=5)[0].decode(errors="replace")
            pytest.fail(f"kis-ws did not come up (exit={proc.returncode}):\n{out[-2000:]}")
        assert '"sid"' in body
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_kis_ws_is_published_on_loopback_only():
    """The Werkzeug server must not face the internet; browsers go through the
    web server's /socket.io proxy."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    block = compose.split("\n  kis-ws:\n", 1)[1].split("\n  volumes:", 1)[0].split("\nvolumes:", 1)[0]
    ports = re.findall(r'^\s+- "([^"]+)"', block.split("ports:", 1)[1].split("environment:", 1)[0], re.M)
    assert ports == ["127.0.0.1:5002:5002"]


def test_a_refused_token_is_a_handshake_refusal_not_a_disconnect(secret):
    """The client tells "refused" (connect_error, not retried) from "ended"
    (the server dropped a live socket — an expired token). Disconnecting inside
    the connect handler made every refusal look like the latter."""
    from flask_socketio import ConnectionRefusedError
    from backend.websocket import server
    with server.app.test_request_context("/"):
        with pytest.raises(ConnectionRefusedError):
            server.on_connect({"token": _token(sub="7")})
