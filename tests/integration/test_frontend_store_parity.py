"""Static guards for the Pinia stores (P3-04).

``frontend/src/stores`` and ``mobile/src/stores`` used to be one hand-copied
file each, and the copies drifted: the web picker offered Kiwoom credentials
the backend cannot use (#150), and mobile's Profile page crashed on a getter
only the web copy had. The stores are now one module per store, and these
checks keep the two apps on the same code and keep logout clearing every
account-scoped store.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "frontend" / "src" / "stores"
MOBILE = ROOT / "mobile" / "src" / "stores"

#: Stores that hold per-device settings, not account data, and so survive a
#: logout. Every other store is account-scoped and must be reset by logout().
DEVICE_SCOPED = {"settings"}


def _store_files(d: Path) -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(d.glob("*.js"))}


def _store_ids(files: dict[str, str]) -> list[str]:
    return [i for src in files.values() for i in re.findall(r"defineStore\('([^']+)'", src)]


def test_web_and_mobile_carry_the_same_stores():
    web, mobile = _store_files(WEB), _store_files(MOBILE)
    assert set(web) == set(mobile)
    for name in web:
        assert web[name] == mobile[name], f"{name} differs between frontend/ and mobile/"


def test_each_store_is_defined_once_and_exported():
    files = _store_files(WEB)
    ids = _store_ids(files)
    assert len(ids) == len(set(ids)), ids
    barrel = files["index.js"]
    for name, src in files.items():
        for hook in re.findall(r"export const (use\w+Store) = defineStore", src):
            assert f"export {{ {hook} }} from './{name[:-3]}'" in barrel, hook


def test_logout_resets_every_account_scoped_store():
    files = _store_files(WEB)
    hook_by_id = {}
    for src in files.values():
        for hook, sid in re.findall(r"export const (use\w+Store) = defineStore\('([^']+)'", src):
            hook_by_id[sid] = hook
    m = re.search(r"logout\(\) \{.*?for \(const useStore of \[(.*?)\]\)", files["user.js"], re.S)
    assert m, "logout() no longer resets stores in a loop"
    reset = {h.strip() for h in m.group(1).split(",") if h.strip()}
    expected = {hook for sid, hook in hook_by_id.items() if sid not in DEVICE_SCOPED | {"user"}}
    assert reset == expected
