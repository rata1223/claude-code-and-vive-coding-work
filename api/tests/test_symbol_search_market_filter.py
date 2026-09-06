"""Blocker B — ``symbols/search`` must honour the market the caller asked for.

The catalogue branch filters by market correctly. The yfinance fallback did not:
it fired whenever the catalogue produced no match, and appended whatever the
provider returned. Ask for ``market=NYSE&q=AAPL`` and the catalogue rightly
excludes AAPL (it is NASD), the fallback then finds it anyway, and the picker —
which passes the active market — offers a NASD symbol on the NYSE tab.

That matters more than a cosmetic mislabel: the row a search returns is what the
client hands to order entry.

yfinance is mocked throughout. No network calls.
"""
import pytest

from api.routers import watchlist


class _FakeTicker:
    def __init__(self, symbol, infos):
        self.symbol = symbol
        self._infos = infos

    @property
    def info(self):
        return self._infos.get(self.symbol, {})


@pytest.fixture
def yf_search(monkeypatch):
    """Patch yfinance and record which provider symbols were looked up."""
    asked = []
    infos = {}

    def factory(symbol):
        asked.append(symbol)
        return _FakeTicker(symbol, infos)

    monkeypatch.setattr(watchlist.yf, "Ticker", factory)
    return asked, infos


def _items(resp):
    return resp.data["items"]


# ── the fallback must respect the requested market ────────────────────────────

def test_a_nyse_search_does_not_return_a_nasd_symbol(yf_search):
    """AAPL is NASD. Asking the NYSE tab for it must come back empty rather
    than offering a symbol from the market the caller filtered out."""
    _asked, infos = yf_search
    infos["AAPL"] = {"symbol": "AAPL", "shortName": "Apple Inc."}

    resp = watchlist.search_symbols(q="AAPL", market="NYSE", limit=20)

    assert _items(resp) == [], "a NYSE-scoped search must not offer a NASD symbol"


def test_a_matching_market_still_returns_the_symbol(yf_search):
    """The guard must not break the case it is protecting."""
    _asked, infos = yf_search
    infos["PLTR"] = {"symbol": "PLTR", "shortName": "Palantir"}

    resp = watchlist.search_symbols(q="PLTR", market="NASD", limit=20)

    items = _items(resp)
    assert len(items) == 1
    assert items[0]["symbol"] == "PLTR"
    assert items[0]["market"] == "NASD"


def test_an_unscoped_search_is_unaffected(yf_search):
    """No market filter means the caller has no opinion — the fallback should
    still answer, with the exchange it resolved."""
    _asked, infos = yf_search
    infos["PLTR"] = {"symbol": "PLTR", "shortName": "Palantir"}

    resp = watchlist.search_symbols(q="PLTR", market=None, limit=20)

    items = _items(resp)
    assert len(items) == 1
    assert items[0]["market"] == "NASD"


def test_a_kr_search_scoped_to_krx_resolves_through_the_provider_spelling(yf_search):
    """The KR case this fallback was fixed for must keep working under a
    market filter: a bare six-digit code is looked up as .KS/.KQ and reported
    back raw."""
    asked, infos = yf_search
    infos["247540.KQ"] = {"symbol": "247540.KQ", "shortName": "에코프로비엠"}

    resp = watchlist.search_symbols(q="247540", market="KRX", limit=20)

    items = _items(resp)
    assert asked == ["247540.KS", "247540.KQ"]
    assert len(items) == 1
    assert items[0]["symbol"] == "247540", "the raw symbol, not the provider spelling"
    assert items[0]["market"] == "KRX"


def test_a_kr_search_scoped_to_a_us_market_returns_nothing(yf_search):
    """A six-digit KR code resolves to KRX, so it must not surface on a NASD
    tab even though the provider would happily answer for it."""
    _asked, infos = yf_search
    infos["005930.KS"] = {"symbol": "005930.KS", "shortName": "Samsung Electronics"}

    resp = watchlist.search_symbols(q="005930", market="NASD", limit=20)

    assert _items(resp) == []


def test_the_catalogue_branch_is_untouched_by_the_guard(yf_search):
    """A catalogue hit never reaches the fallback, so the guard must not
    change it."""
    asked, _infos = yf_search

    resp = watchlist.search_symbols(q="AAPL", market="NASD", limit=20)

    items = _items(resp)
    assert [i["symbol"] for i in items] == ["AAPL"]
    assert asked == [], "a catalogue hit must not spend a provider lookup"
