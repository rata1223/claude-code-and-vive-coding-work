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


def test_about_opens_only_an_https_address_from_the_server():
    src = _read("frontend/src/views/profile/About.vue")
    assert "return /^https:\\/\\/[^\\s]+$/i.test(url) ? url : ''" in src
    confirm = src.split("async onConfirmUpdate() {", 1)[1].split("\n    }", 1)[0]
    assert "const url = safeDownloadUrl(this.downloadUrl)" in confirm and "if (!url) return" in confirm
    # no built-in fallback address of any kind
    assert not re.search(r"https?://", src.split("<script>", 1)[1].split("const safeDownloadUrl", 1)[0])
