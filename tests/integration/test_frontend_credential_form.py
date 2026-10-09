"""P3-02 — the credential form, identical in both apps.

The web form still had the upstream crypto fields (API Key / Secret /
Passphrase) and no account number, so a KIS credential saved from the web
could not trade. The mobile form had the fields but demanded a 12-character
account (KIS accounts are 10 digits: CANO 8 + product code 2) and hardcoded
Korean. One form now, internationalised, checked here statically.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APPS = ("frontend", "mobile")
LOCALES = ("en-US", "ko-KR", "ja-JP", "zh-CN", "zh-TW")
FORM = "src/views/profile/CredentialForm.vue"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_both_apps_carry_the_same_form():
    assert _read(f"frontend/{FORM}") == _read(f"mobile/{FORM}")


def test_the_form_sends_what_trading_needs():
    src = _read(f"frontend/{FORM}")
    create = src.split("credentialsApi.create(", 1)[1].split("})", 1)[0]
    for field in ("api_key", "secret_key", "account_no", "hts_id", "enable_demo_trading"):
        assert f"{field}:" in create, field
    assert "account_no: normalizeAccountNo(" in create
    assert "passphrase" not in src


def test_paper_is_the_default():
    assert re.search(r"enable_demo_trading:\s*true", _read(f"frontend/{FORM}"))


def test_the_account_check_is_ten_digits_after_hyphens_and_spaces():
    src = _read(f"frontend/{FORM}")
    assert "replace(/[-\\s]/g, '')" in src
    assert "/^\\d{10}$/" in src
    validate = src.split("validate()", 1)[1].split("async submit", 1)[0]
    assert "isKisAccountNo(this.form.account_no)" in validate
    assert "credentials.account_no_required" in validate


def test_only_kis_can_be_saved():
    src = _read(f"frontend/{FORM}")
    assert ":disabled=\"form.exchange_id !== 'kis'\"" in src
    assert "this.form.exchange_id !== 'kis'" in src.split("validate()", 1)[1]


@pytest.mark.parametrize("app", APPS)
def test_every_key_the_form_uses_exists_in_every_locale(app):
    used = set(re.findall(r"\$t\('credentials\.([a-z_]+)'\)", _read(f"{app}/{FORM}")))
    used |= set(re.findall(r"fail\('credentials\.([a-z_]+)'\)", _read(f"{app}/{FORM}")))
    assert {"account_no", "account_no_format", "hts_id", "kiwoom_unsupported"} <= used
    for loc in LOCALES:
        block = _read(f"{app}/src/locales/{loc}.js").split("\n  credentials: {", 1)
        assert len(block) == 2, f"{app}/{loc}"
        defined = set(re.findall(r"^    ([a-z_]+):", block[1].split("\n  }", 1)[0], re.M))
        missing = used - defined
        assert not missing, f"{app}/{loc}: {sorted(missing)}"
