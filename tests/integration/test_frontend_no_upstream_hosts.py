"""The apps came from QuantDinger and kept its servers as defaults: the mobile
dev server proxied /api (logins, credentials) to api.quantdinger.com, and the
About page offered the upstream APK, website and support email. Nothing in
either app may point at the upstream project's hosts.

The name stays (#214). The apps are a copy of QuantDinger-Mobile under its
source-available license, and §3.1 of that license forbids removing or
altering its branding and attribution notices without the copyright holder's
written permission. The guards below keep them in place: the license text in
both apps, the name in the login terms, the About text, the page titles and the
native app name.
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


# ── the upstream branding stays (#214, LICENSE §3.1) ─────────────────────

def _node_eval(module_rel, expr):
    import json
    import subprocess
    uri = json.dumps((ROOT / module_rel).as_uri())
    script = f"import({uri}).then(m => process.stdout.write(JSON.stringify({expr})))"
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True,
                                     check=True).stdout)


@pytest.mark.parametrize("app", APPS)
def test_both_apps_carry_the_license_text(app):
    assert _read(f"{app}/LICENSE") == _read("mobile/LICENSE")
    assert "3.1 Attribution and Branding" in _read(f"{app}/LICENSE")


@pytest.mark.parametrize("app", APPS)
def test_the_page_title_and_native_app_name_keep_the_brand(app):
    assert "<title>QuantDinger</title>" in _read(f"{app}/index.html")
    assert '"appName": "QuantDinger"' in _read(f"{app}/capacitor.config.json")
    # The router rewrites document.title on every navigation, so the brand has
    # to be there too or the static <title> is never seen.
    router = _read(f"{app}/src/router/index.js")
    assert "document.title = title ? `${title} | QuantDinger` : 'QuantDinger'" in router


LOCALES = ("ko-KR", "en-US", "ja-JP", "zh-CN", "zh-TW")


@pytest.mark.parametrize("locale", LOCALES)
def test_the_terms_keep_the_brand_and_name_the_market(locale):
    """The brand stays; what changed is the market — the terms described a
    digital-asset service, and this one trades stocks and ETFs."""
    legal = _node_eval("frontend/src/constants/legal.js", f"m.getLegal('{locale}')")
    assert "QuantDinger" in legal["terms"]
    assert "ETF" in legal["terms"]
    assert not re.search(r"digital asset|数字资产|數位資產|デジタル資産|디지털 자산", legal["terms"])


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("locale", LOCALES)
def test_the_about_text_keeps_the_brand_and_names_the_market(app, locale):
    intro = _node_eval(f"{app}/src/locales/{locale}.js", "m.default.about.intro")
    assert intro.startswith("QuantDinger")
    assert "ETF" in intro
    assert not re.search(r"digital asset|数字资产|數位資產|デジタル資産|暗号資産|디지털 자산", intro, re.I)


def test_the_readme_keeps_the_upstream_license_section():
    readme = _read("mobile/README.md")
    assert "## License" in readme and "Section 3.1" in readme
    assert (ROOT / "mobile/banner.png").is_file()


@pytest.mark.parametrize("rel", ["src/views/login/index.vue", "src/main.js",
                                 "src/constants/legal.js"])
def test_both_apps_carry_the_same_login_and_terms(rel):
    assert _read(f"frontend/{rel}") == _read(f"mobile/{rel}"), rel


def test_both_capacitor_configs_are_the_same():
    assert _read("frontend/capacitor.config.json") == _read("mobile/capacitor.config.json")


# ── no OAuth login (#214) ─────────────────────────────────────────────────
#
# The backend has no OAuth (no /api/auth/oauth route, security-config never
# enables it), yet the login page took an ``oauth_token`` from the URL and
# logged in with it: a link could sign the visitor into someone else's account,
# where they might then store their KIS credentials.

@pytest.mark.parametrize("app", APPS)
def test_no_login_token_is_taken_from_the_url(app):
    """The invariant, not the word: a login token comes only from the login API
    response — never from the address the visitor was sent to."""
    login = _read(f"{app}/src/views/login/index.vue")
    calls = re.findall(r"this\.finalizeLogin\(([^)]*)\)", login)
    assert calls and all(c.startswith("res.data.token") for c in calls), calls
    for rel in ("src/views/login/index.vue", "src/main.js", "src/router/index.js"):
        src = _read(f"{app}/{rel}")
        assert not re.search(r"location\.(search|hash)|URLSearchParams|getLaunchUrl|appUrlOpen",
                             src), rel
        # the only query value the login page reads is where to go next
        assert set(re.findall(r"\$route\.query\??\.(\w+)", src)) <= {"redirect"}, rel


@pytest.mark.parametrize("app", APPS)
def test_the_oauth_code_is_gone(app):
    """OAuth needs a backend this platform does not have; bringing any of it back
    should be a deliberate change, so this fails on the word anywhere in src."""
    hits = [f"{path.relative_to(ROOT)}:{n}"
            for path in (ROOT / app / "src").rglob("*")
            if path.is_file() and path.suffix in {".js", ".vue", ".ts"}
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"oauth", line, re.I)]
    assert not hits, hits
    assert not (ROOT / app / "src/utils/oauthRedirect.js").exists()
