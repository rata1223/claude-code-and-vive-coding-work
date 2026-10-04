"""The apps came from QuantDinger and kept its servers as defaults: the mobile
dev server proxied /api (logins, credentials) to api.quantdinger.com, and the
About page offered the upstream APK, website and support email. Nothing in
either app may point at the upstream project's hosts.
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
