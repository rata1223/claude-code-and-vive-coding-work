"""B1 step 2 — the operator screen (views/profile/OperatorStrategy.vue) in both
apps. Static checks: the server is the authority (api/routers/operator.py);
these keep the two apps and the picker from drifting away from it.
"""
import re
from pathlib import Path

import pytest

from backend.quant.data.universe import UNIVERSE

ROOT = Path(__file__).resolve().parents[2]
APPS = ("frontend", "mobile")
LOCALES = ("en-US", "ko-KR", "ja-JP", "zh-CN", "zh-TW")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_both_apps_carry_the_same_screen_and_picker():
    for rel in ("src/views/profile/OperatorStrategy.vue", "src/constants/tradingUniverse.js"):
        assert _read(f"frontend/{rel}") == _read(f"mobile/{rel}"), rel


def test_the_picker_offers_exactly_the_server_universe():
    src = _read("frontend/src/constants/tradingUniverse.js")
    symbols = re.findall(r"'([A-Z0-9]+)'", src.split("UNIVERSE_GROUPS", 1)[1])
    assert symbols == list(UNIVERSE)


@pytest.mark.parametrize("app", APPS)
def test_every_operator_key_the_screen_uses_exists_in_every_locale(app):
    used = set(re.findall(r"\$t\('operator\.([a-z_]*[a-z])'", _read(f"{app}/src/views/profile/OperatorStrategy.vue")))
    used |= {"group_" + g for g in ("us_etf", "us_large", "kr_etf")}   # built as 'operator.group_' + key
    assert used
    for loc in LOCALES:
        block = _read(f"{app}/src/locales/{loc}.js").split("\n  operator: {", 1)
        assert len(block) == 2, f"{app}/{loc}: no operator block"
        defined = set(re.findall(r"^    ([a-z_]+):", block[1].split("\n  }", 1)[0], re.M))
        missing = used - defined
        assert not missing, f"{app}/{loc}: {sorted(missing)}"


@pytest.mark.parametrize("app", APPS)
def test_the_menu_entry_is_shown_to_operators_only(app):
    src = _read(f"{app}/src/views/profile/index.vue")
    m = re.search(r"<div ([^>]*)@click=\"\$router\.push\('/profile/operator-strategy'\)\"", src)
    assert m and 'v-if="userInfo?.is_operator"' in m.group(1)


@pytest.mark.parametrize("app", APPS)
def test_the_route_and_client_exist(app):
    assert "path: '/profile/operator-strategy'" in _read(f"{app}/src/router/index.js")
    api = _read(f"{app}/src/api/index.js")
    for path in ("/api/operator/strategies'", "/api/operator/strategies/start'",
                 "/api/operator/strategies/stop'"):
        assert path in api, path


def test_the_screen_sends_fractions_not_percentages():
    """The form edits percent; the API takes fractions (position_size_pct ≤ 0.05)."""
    src = _read("frontend/src/views/profile/OperatorStrategy.vue")
    assert "position_size_pct: Number(this.form.sizePct) / 100" in src
    assert "stop_loss_pct: Number(this.form.stopPct) / 100" in src


def test_a_run_stuck_in_stopping_can_be_stopped_again():
    """A stop the worker never recorded keeps the slot; the screen must offer
    stop for every occupying run, not only active ones (re-sending is safe)."""
    src = _read("frontend/src/views/profile/OperatorStrategy.vue")
    m = re.search(r'<van-button\s+v-if="([^"]+)"[^>]*@click="stopRun\(run\)"', src, re.S)
    assert m and m.group(1).strip() == "run.occupying"


def test_start_and_stop_are_claimed_before_the_confirm_dialog():
    """A double tap must not open two dialogs and send two requests: the busy
    flag is set before the dialog is awaited and released if it is cancelled."""
    src = _read("frontend/src/views/profile/OperatorStrategy.vue")
    for method, flag in (("startRun()", "this.starting = true"), ("stopRun(run)", "this.stopping = run.id")):
        body = src.split(f"async {method} {{", 1)[1]
        assert body.index(flag) < body.index("showConfirmDialog"), method


def test_only_the_latest_list_response_updates_the_screen():
    """Overlapping loads can finish out of order; a stale "slot free" answer
    must not replace a newer "occupied" one and re-enable Start."""
    src = _read("frontend/src/views/profile/OperatorStrategy.vue")
    body = src.split("async load() {", 1)[1].split("\n    },", 1)[0]
    assert "const seq = ++this.loadSeq" in body
    assert body.index("if (seq !== this.loadSeq) return") < body.index("this.runs = res")


def test_the_screen_explains_every_gate_reason():
    """Each reason the gate can give (promotion_guard.paper_gate_status) has its
    own text; ``duration`` falls through to the date / "not met" lines."""
    import inspect
    from backend.worker import promotion_guard
    reasons = set(re.findall(r'return False, "([a-z_]+)"',
                             inspect.getsource(promotion_guard.paper_gate_status)))
    assert reasons == {"env_unknown", "env_not_paper", "duration", "no_fills"}
    src = _read("frontend/src/views/profile/OperatorStrategy.vue")
    for reason in reasons - {"duration"}:
        assert f"reason === '{reason}'" in src, reason
    assert "operator.env_fills" in src
    assert 'v-if="run.orders_enabled === false"' in src, "a shadow run is labelled"


# ── live feed (kis-ws) ─────────────────────────────────────────────────────

def test_both_apps_carry_the_same_feed_client():
    assert _read("frontend/src/services/operatorFeed.js") == _read("mobile/src/services/operatorFeed.js")


def test_the_feed_sends_the_token_in_auth_not_the_url():
    src = _read("frontend/src/services/operatorFeed.js")
    assert "auth: (cb) => cb({ token:" in src
    assert "?token" not in src and "query:" not in src


def test_the_feed_listens_to_channels_kis_ws_relays():
    """Every event the client listens to is one the server bridges from Redis."""
    feed = _read("frontend/src/services/operatorFeed.js")
    server = _read("backend/websocket/server.py")
    relayed = set(re.findall(r'"([a-z:]+)"', server.split("_CHANNELS = [", 1)[1].split("]", 1)[0]))
    listened = set(re.findall(r"socket\.on\('([a-z:]+)'", feed)) - {"connect", "disconnect", "connect_error"}
    assert listened and listened <= relayed, listened - relayed


@pytest.mark.parametrize("app", APPS)
def test_the_dev_server_proxies_the_socket(app):
    cfg = _read(f"{app}/vite.config.js")
    block = cfg.split("'/socket.io': {", 1)[1].split("}", 1)[0]
    assert "target: wsTarget" in block and "ws: true" in block
    assert '"socket.io-client"' in _read(f"{app}/package.json")


def test_the_screen_closes_the_feed_when_it_leaves():
    src = _read("frontend/src/views/profile/OperatorStrategy.vue")
    unmount = src.split("beforeUnmount() {", 1)[1].split("},", 1)[0]
    assert "this.feed.close()" in unmount


def test_every_alert_level_the_backend_sends_is_styled():
    """``info`` is plain text; every other level any ``publish_alert`` caller
    uses must stand out (a failed reconcile sends ``error``)."""
    levels = set()
    for path in (ROOT / "backend").rglob("*.py"):
        if "/tests/" in str(path):
            continue
        src = path.read_text(encoding="utf-8")
        if "publish_alert" not in src:
            continue
        levels |= set(re.findall(r'level\s*=\s*"([a-z]+)"', src))
    assert {"critical", "error", "warning"} <= levels
    css = _read("frontend/src/views/profile/OperatorStrategy.vue").split("<style", 1)[1]
    for level in levels - {"info"}:
        assert f".live-row.alert-{level}" in css, level


def test_the_feed_falls_back_to_polling_when_websocket_fails():
    """socket.io-client 4.8 does not try the next transport unless told to: a
    proxy that blocks WebSocket upgrades would leave the feed disconnected."""
    src = _read("frontend/src/services/operatorFeed.js")
    assert "transports: ['websocket', 'polling']" in src
    assert "tryAllTransports: true" in src


def test_a_reconnect_reloads_the_list():
    """Socket.IO does not replay what was published while the socket was down."""
    src = _read("frontend/src/views/profile/OperatorStrategy.vue")
    handler = src.split("onFeedStatus(s) {", 1)[1].split("\n    },", 1)[0]
    assert "this.liveEverConnected) this.scheduleReload()" in handler
