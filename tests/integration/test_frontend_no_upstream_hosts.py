"""The apps came from QuantDinger and kept its servers as defaults: the mobile
dev server proxied /api (logins, credentials) to api.quantdinger.com, and the
About page offered the upstream APK, website and support email. Nothing in
either app may point at the upstream project's hosts.

The name went too (#214): the login terms, titles, app ids and storage keys
said QuantDinger. What stays is the license (``mobile/LICENSE``) and one
attribution line in each README, which that license asks for.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APPS = ("frontend", "mobile")
UPSTREAM_HOST = re.compile(r"quantdinger\.com", re.I)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("app", APPS)
def test_no_source_file_points_at_the_upstream_hosts(app):
    hits = []
    for path in [ROOT / app / "vite.config.js", *(ROOT / app / "src").rglob("*")]:
        if path.is_file() and path.suffix in {".js", ".vue", ".ts", ".json", ".html", ".css"}:
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if UPSTREAM_HOST.search(line):
                    hits.append(f"{path.relative_to(ROOT)}:{n}")
    assert not hits, hits


@pytest.mark.parametrize("app", APPS)
def test_the_dev_api_proxy_is_configurable(app):
    assert "process.env.VITE_API_TARGET" in _read(f"{app}/vite.config.js")


@pytest.mark.parametrize("rel", ["src/views/profile/About.vue", "src/config/index.js"])
def test_both_apps_carry_the_same_copy(rel):
    assert _read(f"frontend/{rel}") == _read(f"mobile/{rel}"), rel


def _safe_download_url_source() -> str:
    src = _read("frontend/src/views/profile/About.vue")
    start = src.index("const safeDownloadUrl = ")
    return src[start:src.index("\n}\n", start) + 3]


@pytest.mark.parametrize("value, expected", [
    ("https://downloads.example.test/kis.apk", "https://downloads.example.test/kis.apk"),
    ("  https://downloads.example.test/a b.apk ", "https://downloads.example.test/a%20b.apk"),
    ("https://example.com:bad/app.apk", ""),        # not a URL (CodeRabbit)
    ("http://downloads.example.test/kis.apk", ""),
    ("javascript:alert(1)", ""),
    ("https://", ""),
    ("", ""),
    (None, ""),
])
def test_about_accepts_only_parseable_https_addresses(value, expected):
    """Run the page's own validator in Node: a hand-written regex accepted
    `https://example.com:bad`, an address no browser can open."""
    import json
    import subprocess
    script = _safe_download_url_source() + f"\nprocess.stdout.write(JSON.stringify(safeDownloadUrl({json.dumps(value)})))"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == expected


def test_about_opens_only_an_https_address_from_the_server():
    src = _read("frontend/src/views/profile/About.vue")
    assert "url.protocol === 'https:' && url.hostname" in src
    confirm = src.split("async onConfirmUpdate() {", 1)[1].split("\n    }", 1)[0]
    assert "const url = safeDownloadUrl(this.downloadUrl)" in confirm and "if (!url) return" in confirm
    # no built-in fallback address of any kind
    assert not re.search(r"https?://", src.split("<script>", 1)[1].split("const safeDownloadUrl", 1)[0])


# ── the upstream name (#214) ──────────────────────────────────────────────

UPSTREAM_NAME = re.compile(r"quantdinger", re.I)


def _app_files(app):
    root = ROOT / app
    yield from (root / n for n in ("index.html", "capacitor.config.json", "package.json",
                                   "vite.config.js") if (root / n).exists())
    for path in (root / "src").rglob("*"):
        if path.is_file() and path.suffix in {".js", ".vue", ".ts", ".json", ".html", ".css"}:
            yield path


@pytest.mark.parametrize("app", APPS)
def test_no_app_file_carries_the_upstream_name(app):
    hits = [f"{path.relative_to(ROOT)}:{n}"
            for path in _app_files(app)
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if UPSTREAM_NAME.search(line)]
    assert not hits, hits


@pytest.mark.parametrize("readme", ["mobile/README.md", "mobile/README_CN.md"])
def test_a_readme_names_the_upstream_once_as_attribution(readme):
    """The license asks for attribution; the README keeps exactly that, nothing
    of the upstream product (no hosts, no upstream repository links)."""
    lines = [line for line in _read(readme).splitlines() if UPSTREAM_NAME.search(line)]
    assert len(lines) == 1 and "LICENSE" in lines[0], lines
    assert not re.search(r"brokermr810|quantdinger\.com", _read(readme), re.I)


def test_the_license_is_kept():
    assert (ROOT / "mobile/LICENSE").is_file()


@pytest.mark.parametrize("rel", ["src/views/login/index.vue", "src/main.js",
                                 "src/constants/legal.js"])
def test_both_apps_carry_the_same_login_and_terms(rel):
    assert _read(f"frontend/{rel}") == _read(f"mobile/{rel}"), rel


def test_both_capacitor_configs_name_the_same_app():
    assert _read("frontend/capacitor.config.json") == _read("mobile/capacitor.config.json")
    assert '"appId": "com.kistrade.mobile"' in _read("mobile/capacitor.config.json")


@pytest.mark.parametrize("locale, name", [
    ("ko-KR", "KIS 자동매매"), ("en-US", "KIS Trading"), ("ja-JP", "KIS Trading"),
    ("zh-CN", "KIS Trading"), ("zh-TW", "KIS Trading"),
])
def test_the_terms_name_this_service_and_its_market(locale, name):
    """The terms described a digital-asset service under the upstream name."""
    import json
    import subprocess
    script = (f"import('file://{ROOT}/frontend/src/constants/legal.js')"
              f".then(m => process.stdout.write(JSON.stringify(m.getLegal({json.dumps(locale)}))))")
    legal = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True,
                                      check=True).stdout)
    assert name in legal["terms"]
    assert "ETF" in legal["terms"]
    assert not re.search(r"digital asset|数字资产|數位資產|デジタル資産|디지털 자산", legal["terms"])


# ── no OAuth login (#214) ─────────────────────────────────────────────────
#
# The backend has no OAuth (no /api/auth/oauth route, security-config never
# enables it), yet the login page took an ``oauth_token`` from the URL and
# logged in with it: a link could sign the visitor into someone else's account,
# where they might then store their KIS credentials.

@pytest.mark.parametrize("app", APPS)
def test_no_login_token_is_taken_from_the_url(app):
    hits = [f"{path.relative_to(ROOT)}:{n}"
            for path in (ROOT / app / "src").rglob("*")
            if path.is_file() and path.suffix in {".js", ".vue", ".ts"}
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"oauth", line, re.I)]
    assert not hits, hits
    assert not (ROOT / app / "src/utils/oauthRedirect.js").exists()


@pytest.mark.parametrize("app", APPS)
def test_no_upstream_storage_keys(app):
    hits = [f"{path.relative_to(ROOT)}:{n}"
            for path in (ROOT / app / "src").rglob("*.vue")
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"['\"]qd_", line)]
    assert not hits, hits
