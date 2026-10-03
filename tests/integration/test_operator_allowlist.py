"""One operator allow-list of user ids (backend/security/operators.py).

Emergency flatten, kill-switch reset and the live feed all act on the worker's
single ``.env`` account, while app signup is open. They used to read three lists,
two of them email lists that compared case-insensitively against a signup whose
duplicate check is case-sensitive — so a listed address protected nothing.
"""
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.security import operators

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (operators.OPERATOR_ENV, *operators.LEGACY_OPERATOR_ENVS):
        monkeypatch.delenv(name, raising=False)


def test_unset_means_nobody():
    assert operators.operator_user_ids() == frozenset()
    assert operators.is_operator(SimpleNamespace(id=1)) is False


def test_listed_ids_match_with_spacing(monkeypatch):
    monkeypatch.setenv("OPERATOR_USER_IDS", " 3 , 42,,")
    assert operators.operator_user_ids() == {"3", "42"}
    assert operators.is_operator(SimpleNamespace(id=42)) is True
    assert operators.is_operator_id("3") is True
    assert operators.is_operator(SimpleNamespace(id=4)) is False


@pytest.mark.parametrize("entry", ["ops@example.com", "OPS@EXAMPLE.COM", "0", "-1", "1.0",
                                   "abc", "１"])   # last: full-width digit one
def test_entries_that_are_not_ids_match_nothing(monkeypatch, entry):
    monkeypatch.setenv("OPERATOR_USER_IDS", entry)
    assert operators.operator_user_ids() == frozenset()
    assert operators.is_operator(SimpleNamespace(id=1, email="ops@example.com")) is False


def test_leading_zeros_name_the_same_id(monkeypatch):
    monkeypatch.setenv("OPERATOR_USER_IDS", "007")
    assert operators.is_operator_id(7) is True
    assert operators.is_operator_id("7") is True


@pytest.mark.parametrize("value", [None, True, False, "", " ", "x", "7a", 7.5])
def test_odd_user_ids_are_not_operators(monkeypatch, value):
    monkeypatch.setenv("OPERATOR_USER_IDS", "1,7")
    assert operators.is_operator_id(value) is False


def test_an_email_never_authorizes_even_its_owner(monkeypatch):
    """Two accounts whose addresses differ only in case are distinct rows; the
    list cannot be satisfied by an address at all."""
    monkeypatch.setenv("OPERATOR_USER_IDS", "5")
    owner = SimpleNamespace(id=5, email="ops@example.com")
    lookalike = SimpleNamespace(id=6, email="Ops@example.com")
    assert operators.is_operator(owner) is True
    assert operators.is_operator(lookalike) is False


@pytest.mark.parametrize("legacy", operators.LEGACY_OPERATOR_ENVS)
def test_retired_variables_grant_nothing_and_are_reported(monkeypatch, caplog, legacy):
    monkeypatch.setenv(legacy, "1,ops@example.com")
    assert operators.is_operator(SimpleNamespace(id=1, email="ops@example.com")) is False
    with caplog.at_level(logging.WARNING, logger=operators.__name__):
        found = operators.warn_legacy_operator_env()
    assert found == [legacy]
    assert legacy in caplog.text and "OPERATOR_USER_IDS" in caplog.text


def test_startup_report_counts_rejected_entries_without_echoing_them(monkeypatch, caplog):
    monkeypatch.setenv("OPERATOR_USER_IDS", "3,ops@example.com")
    with caplog.at_level(logging.WARNING, logger=operators.__name__):
        assert operators.warn_legacy_operator_env() == []
    assert "1개" in caplog.text
    assert "ops@example.com" not in caplog.text


def test_startup_report_says_when_nobody_is_an_operator(caplog):
    """In a container the retired variables are never set (compose passes only
    declared ones), so this warning is the one an upgraded deployment sees: it
    must name them and point at the new list."""
    with caplog.at_level(logging.WARNING, logger=operators.__name__):
        operators.warn_legacy_operator_env()
    assert "비어 있음" in caplog.text
    for legacy in operators.LEGACY_OPERATOR_ENVS:
        assert legacy in caplog.text, legacy


# ── wiring ─────────────────────────────────────────────────────────────────

def _service(name: str) -> str:
    text = (ROOT / "docker-compose.yml").read_text()
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|^\S|\Z)", text, re.M | re.S)
    assert m, name
    return m.group(1)


def test_compose_passes_one_list_to_the_api_and_the_feed():
    line = re.compile(r"^\s+OPERATOR_USER_IDS:\s*\$\{OPERATOR_USER_IDS:-\}\s*$", re.M)
    assert line.search(_service("api"))
    assert line.search(_service("kis-ws"))
    compose = (ROOT / "docker-compose.yml").read_text()
    for legacy in operators.LEGACY_OPERATOR_ENVS:
        assert legacy not in compose, legacy


def test_no_code_reads_the_retired_variables():
    """Only the operators module may name them (to warn); a reader elsewhere
    would bring an email list back."""
    allowed = {ROOT / "backend/security/operators.py"}
    offenders = []
    for path in [*ROOT.glob("api/**/*.py"), *ROOT.glob("backend/**/*.py")]:
        if path in allowed or "/tests/" in str(path) or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        for legacy in operators.LEGACY_OPERATOR_ENVS:
            if legacy in text:
                offenders.append(f"{path.relative_to(ROOT)}: {legacy}")
    assert offenders == []


def test_both_api_controls_ask_the_operators_module():
    for rel, fn in (("api/routers/quick_trade.py", "_flatten_authorized"),
                    ("api/routers/risk.py", "_is_risk_admin")):
        src = (ROOT / rel).read_text(encoding="utf-8")
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\n\n\n")]
        assert "is_operator(user)" in body, rel
        assert "email" not in body.split('"""')[-1], f"{rel}: {fn} must not compare emails"


def test_both_processes_report_at_startup():
    assert "warn_legacy_operator_env()" in (ROOT / "api/main.py").read_text(encoding="utf-8")
    ws = (ROOT / "backend/websocket/server.py").read_text(encoding="utf-8")
    verifier = ws[ws.index("def _require_token_verifier"):]
    verifier = verifier[:verifier.index("\n\n\n")]
    assert "warn_legacy_operator_env()" in verifier
