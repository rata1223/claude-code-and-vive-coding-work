"""P0 UI safety — the two Vue apps must not drift on the trading surface.

``frontend/`` is the canonical deploy UI: it is the only one wired into
``docker-compose.yml``. ``mobile/`` is the Capacitor build of the same product
and is **not** deprecated — the two are 50-of-57-file identical forks, and
``views/quick-trade/index.vue`` is byte-identical between them.

That duplication is the hazard this guard exists for. A safety fix applied to
one app and not the other ships a *fixed* web UI and a *still-dangerous* mobile
UI, with nothing in CI to say so. Every P0 change to the trading surface must
therefore land in both files in the same commit.

De-duplicating the two apps is the real fix and is deliberately out of scope
here (audit P3). Until then, this test is what keeps them honest.
"""
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

#: Files that must stay byte-identical across both apps. Scoped to the trading
#: surface — the apps legitimately differ in config, locales, router and
#: credential form, so a blanket comparison would be wrong.
_MIRRORED = (
    "src/views/quick-trade/index.vue",
)


@pytest.mark.parametrize("relative", _MIRRORED)
def test_trading_surface_is_identical_in_both_apps(relative):
    canonical = _REPO / "frontend" / relative
    mirror = _REPO / "mobile" / relative

    assert canonical.is_file(), f"canonical app missing {relative}"
    assert mirror.is_file(), f"mobile app missing {relative}"

    if canonical.read_bytes() != mirror.read_bytes():
        pytest.fail(
            f"{relative} differs between frontend/ and mobile/.\n"
            "A P0 trading-safety change must be applied to BOTH apps in the "
            "same commit — shipping it to one leaves the other dangerous."
        )
