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
