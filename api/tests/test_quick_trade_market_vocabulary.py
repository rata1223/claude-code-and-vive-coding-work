"""Blocker B — the trading UI must speak the backend's exchange vocabulary.

The picker offered ``Crypto / USStock / HKStock / Forex / Futures``; the backend
catalogue (``api/routers/watchlist.py:HOT_SYMBOLS``) is keyed
``NASD / NYSE / KRX``. **The two sets intersect nowhere**, so ``symbols/hot``
returned an empty list for every tab and ``symbols/search`` survived only on its
yfinance fallback — while the share-based limit order PR #150 built sat behind a
picker that could not name a single instrument it could route.

These are source-level guards rather than a browser test. There is no JS test
runner in this repo, and the failure mode is a *vocabulary* mismatch between two
files — exactly what a source assertion catches and a unit test of either side
alone does not.
"""
import re
from pathlib import Path

import pytest

from api.routers.watchlist import HOT_SYMBOLS
from backend.market.symbols import CANONICAL_EXCHANGES

_REPO = Path(__file__).resolve().parents[2]
_APPS = ("frontend", "mobile")
_QUICK_TRADE = "src/views/quick-trade/index.vue"
_PICKER = "src/components/SymbolPicker.vue"

#: The crypto-era vocabulary. None of these may appear as a market value on the
#: trading surface.
_RETIRED_MARKETS = ("USStock", "HKStock", "Forex", "Futures")


def _read(app, relative):
    path = _REPO / app / relative
    assert path.is_file(), f"missing {app}/{relative}"
    return path.read_text(encoding="utf-8")


# ── the two vocabularies must be the same one ─────────────────────────────────

def test_the_backend_catalogue_is_keyed_by_the_canonical_exchanges():
    """If these ever diverge, the picker goes quietly empty again."""
    assert set(HOT_SYMBOLS) == set(CANONICAL_EXCHANGES)


def test_every_canonical_exchange_has_symbols_to_offer():
    for exchange in CANONICAL_EXCHANGES:
        assert HOT_SYMBOLS[exchange], f"{exchange} has no symbols; its tab would be empty"


# ── the quick-trade surface offers exactly the tradable exchanges ─────────────

@pytest.mark.parametrize("app", _APPS)
def test_quick_trade_declares_the_canonical_markets(app):
    source = _read(app, _QUICK_TRADE)
    for exchange in CANONICAL_EXCHANGES:
        assert f"'{exchange}'" in source, f"{app}: quick trade cannot select {exchange}"


@pytest.mark.parametrize("app", _APPS)
def test_quick_trade_no_longer_locks_the_picker_to_crypto(app):
    """``:only-crypto="true"`` is what made the KIS order form unreachable."""
    assert "only-crypto" not in _read(app, _QUICK_TRADE)


@pytest.mark.parametrize("app", _APPS)
def test_quick_trade_offers_no_retired_market(app):
    """Unlocking the picker without narrowing it would expose HK / Forex /
    Futures symbols the KIS equities backend cannot route — worse than the dead
    end it replaced."""
    source = _read(app, _QUICK_TRADE)
    for retired in _RETIRED_MARKETS:
        # Quoted, so this matches an actual option *value* and not prose. A
        # guard a comment can trip is a guard that gets deleted.
        assert f"'{retired}'" not in source, \
            f"{app}: retired market {retired} still offered"


@pytest.mark.parametrize("app", _APPS)
def test_quick_trade_does_not_default_a_symbol_to_crypto(app):
    """Three places used to hardcode ``'Crypto'``: the pick handler, the
    watchlist-select handler and the chart's ``:market`` binding. Each would
    stamp a crypto market onto a KIS equity."""
    assert "'Crypto'" not in _read(app, _QUICK_TRADE)


# ── the shared picker stays usable by its other callers ───────────────────────

@pytest.mark.parametrize("app", _APPS)
def test_the_picker_takes_a_caller_supplied_market_list(app):
    assert re.search(r"markets:\s*\{", _read(app, _PICKER)), \
        f"{app}: SymbolPicker has no `markets` prop"


@pytest.mark.parametrize("app", _APPS)
def test_the_picker_still_supports_its_crypto_callers(app):
    """``BotForm``, ``BotFromIndicator`` and ``BotAIRecommend`` all pass
    ``:only-crypto="true"``. Narrowing the picker for quick trade must not
    change what they see."""
    source = _read(app, _PICKER)
    assert "onlyCrypto" in source, f"{app}: onlyCrypto removed; bot views would break"
    assert "'Crypto'" in source, f"{app}: Crypto option removed from the shared picker"


@pytest.mark.parametrize("app", _APPS)
def test_the_bot_views_are_untouched(app):
    for view in ("BotForm", "BotFromIndicator", "BotAIRecommend"):
        source = _read(app, f"src/views/trading/{view}.vue")
        assert 'only-crypto="true"' in source, f"{app}/{view}: crypto lock lost"


# ── the saved-watchlist list must actually filter ─────────────────────────────

@pytest.mark.parametrize("app", _APPS)
def test_the_picker_filters_saved_items_when_not_crypto_only(app):
    """``displayedList`` returned ``items`` unfiltered whenever ``onlyCrypto``
    was false, so a saved crypto symbol stayed pickable on an equities-only
    screen. Filtering must not depend on that flag."""
    source = _read(app, _PICKER)
    body = source[source.index("displayedList()"):]
    body = body[:body.index("\n    }")]

    assert "markets" in body.lower(), (
        f"{app}: displayedList ignores the allowed markets — saved crypto "
        "symbols would remain selectable on the equities screen"
    )


# ── regressions found in review ───────────────────────────────────────────────

def test_the_catalogue_and_excd_map_agree_per_symbol():
    """Key sets matching is not enough. SPY sat under NASD in the catalogue
    while EXCD_MAP — the canonical source — says NYSE, so one instrument had
    two exchanges depending on which path you came in through. SPY trades on
    NYSE Arca, so the catalogue was the wrong one."""
    from backend.quant.data.universe import EXCD_MAP

    conflicts = [
        (row["symbol"], exchange, EXCD_MAP[row["symbol"]])
        for exchange, rows in HOT_SYMBOLS.items()
        for row in rows
        if row["symbol"] in EXCD_MAP and EXCD_MAP[row["symbol"]] != exchange
    ]
    assert conflicts == [], f"catalogue disagrees with EXCD_MAP: {conflicts}"


@pytest.mark.parametrize("app", _APPS)
def test_a_picker_with_no_market_list_filters_nothing(app):
    """``home`` and ``ai-analysis`` pass neither ``markets`` nor ``only-crypto``.
    Before this change they saw every saved row. If the new filter applies its
    own default to them, every NASD/NYSE/KRX row vanishes from their list —
    a regression in screens this task never meant to touch."""
    source = _read(app, _PICKER)

    # Two halves of one guarantee: an absent list resolves to "unconstrained"
    # rather than an empty allow-list, and the filter passes everything through
    # in that case. An empty list would read as "nothing is allowed" and hide
    # every saved row — the exact regression this guards.
    assert "if (!this.markets) return null" in source, (
        f"{app}: an absent `markets` prop must mean unconstrained, not empty"
    )
    assert "if (!allowed) return items" in source, (
        f"{app}: displayedList must pass everything through when unconstrained"
    )


@pytest.mark.parametrize("app", _APPS)
def test_quick_trade_only_prefills_a_tradable_symbol(app):
    """``activeSymbol`` persists in localStorage and the watchlist store's
    ``setItems`` explicitly prefers a crypto item, so the remembered symbol can
    be crypto. Prefilling it lands a crypto symbol in the now-unlocked KIS order
    form, where ``validateOrder`` only checks that it is non-empty."""
    source = _read(app, _QUICK_TRADE)
    block = source[source.index("activeSymbol"):]
    block = block[:400]

    assert "watchlistTradable" in block, (
        f"{app}: the remembered symbol is prefilled without checking it is "
        "tradable — a crypto symbol can reach the KIS order form"
    )


@pytest.mark.parametrize("app", _APPS)
def test_the_auto_selected_symbol_keeps_its_own_market(app):
    """Auto-selecting the first tradable row recorded DEFAULT_MARKET rather
    than the row's market, so a KRX or NYSE symbol was persisted as NASD."""
    source = _read(app, _QUICK_TRADE)
    block = source[source.index("watchlistTradable.length > 0"):]
    block = block[:400]

    assert "first.market" in block, \
        f"{app}: auto-select overwrites the symbol's real market with a default"
