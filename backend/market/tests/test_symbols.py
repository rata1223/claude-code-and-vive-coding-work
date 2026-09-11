"""Blocker B — the symbol abstraction layer.

Three representations of one instrument, kept explicitly distinct:

    raw_symbol      what the user or UI supplies       "005930"
    provider_symbol what a market-data vendor wants    "005930.KS"
    backend_symbol  what order routing uses            "005930"

They coincide for US equities and diverge for KR, which is exactly why the
distinction has to be named. Collapsing them is what made every Korean symbol
render an empty chart: ``_fetch_kline_yf`` handed a raw symbol to yfinance,
which has never heard of a bare ``005930``.
"""
import pytest

from backend.market import symbols as S


# ── canonical vocabulary ──────────────────────────────────────────────────────

def test_the_canonical_exchanges_are_the_three_kis_supports():
    """KRX / NASD / NYSE. Not the crypto-era Crypto/USStock/HKStock/Forex/Futures
    vocabulary the picker used to show, which intersected this set nowhere."""
    assert S.CANONICAL_EXCHANGES == ("KRX", "NASD", "NYSE")


@pytest.mark.parametrize("exchange", ["KRX", "NASD", "NYSE"])
def test_supported_exchanges_are_accepted(exchange):
    assert S.is_supported_exchange(exchange) is True


@pytest.mark.parametrize("exchange", ["Crypto", "USStock", "HKStock", "Forex",
                                      "Futures", "", None, "krx ", "KOSPI"])
def test_unsupported_markets_are_rejected(exchange):
    """The rejection list is the old picker vocabulary verbatim. ``USStock``
    matters most: it *looks* supported and is not — it never mapped to anything
    the backend catalogue holds."""
    assert S.is_supported_exchange(exchange) is False


# ── raw_symbol → exchange ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["005930", "000660", "069500", "360750"])
def test_six_digit_codes_resolve_to_krx(raw):
    """Structural, not a lookup: KIS domestic codes are six digits. 069500 and
    360750 are also in KR_ETF, so this covers both routes to KRX."""
    assert S.resolve_exchange(raw) == "KRX"


def test_known_nyse_tickers_resolve_to_nyse():
    """EXCD_MAP is the canonical source (backend/quant/data/universe.py) and
    already distinguishes NYSE from NASD. This layer must defer to it rather
    than invent a second mapping."""
    assert S.resolve_exchange("SPY") == "NYSE"
    assert S.resolve_exchange("JPM") == "NYSE"


def test_known_nasdaq_tickers_resolve_to_nasd():
    assert S.resolve_exchange("AAPL") == "NASD"
    assert S.resolve_exchange("NVDA") == "NASD"


def test_an_unmapped_us_ticker_defaults_to_nasd():
    """Mirrors ``EXCD_MAP.get(symbol, "NASD")``, the fallback already used
    throughout backend/brokers/kis.py. A new default here would route orders
    differently from the broker adapter."""
    assert S.resolve_exchange("PLTR") == "NASD"


def test_resolution_is_case_insensitive_and_trims():
    assert S.resolve_exchange("  aapl ") == "NASD"


# ── crypto must not leak into the equities path ───────────────────────────────

@pytest.mark.parametrize("raw", ["BTC/USDT", "ETH/USD", "BTCUSDT", "ETHUSDC",
                                 "", "   ", None])
def test_crypto_and_empty_symbols_resolve_to_no_exchange(raw):
    """``None`` means "not a KIS-tradable equity" and must never be coerced to
    a default. The picker previously returned saved crypto symbols unfiltered;
    if one reached order entry it would resolve to *some* exchange and be
    submitted. Refusing to classify it is what stops that."""
    assert S.resolve_exchange(raw) is None


# ── raw_symbol → provider_symbol ──────────────────────────────────────────────

def test_a_kr_symbol_offers_both_kospi_and_kosdaq_candidates():
    """A six-digit code does not say which board it trades on: KOSPI is ``.KS``
    and KOSDAQ is ``.KQ``. Guessing one is how you get a silently empty chart
    for every symbol on the other board, so the layer returns both, in the order
    to try them."""
    assert S.provider_symbol_candidates("005930") == ["005930.KS", "005930.KQ"]


def test_a_us_symbol_is_its_own_provider_symbol():
    assert S.provider_symbol_candidates("AAPL") == ["AAPL"]


def test_a_dotted_us_ticker_is_converted_to_the_provider_spelling():
    """Yahoo spells class shares with a hyphen: BRK.B is BRK-B. BRK.B is in the
    backend catalogue (watchlist.py HOT_SYMBOLS, NYSE), so this is reachable."""
    assert S.provider_symbol_candidates("BRK.B") == ["BRK-B"]


def test_an_unsupported_symbol_has_no_provider_candidates():
    assert S.provider_symbol_candidates("BTC/USDT") == []


# ── raw_symbol → backend_symbol ───────────────────────────────────────────────

def test_the_backend_symbol_keeps_the_kis_spelling():
    """Order routing must not receive the provider's spelling. ``005930.KS``
    is not a thing KIS will accept."""
    assert S.to_backend_symbol("005930") == "005930"
    assert S.to_backend_symbol("  aapl ") == "AAPL"


def test_an_unsupported_symbol_has_no_backend_symbol():
    assert S.to_backend_symbol("BTC/USDT") is None


# ── the KR/US split stays consistent with the broker adapter ──────────────────

def test_is_kr_agrees_with_the_broker_adapter():
    """``KISBroker._is_kr`` already decides KR-vs-US for cancels and quotes.
    Two definitions of "is this Korean" that can disagree is a routing bug
    waiting to happen, so this layer must match it exactly.

    Skipped where the broker stack's deps are absent: importing it pulls in
    ``redis``, and this module is deliberately pure — requiring a broker stack
    to test a naming function would be the wrong dependency. CI has redis, so
    the invariant is still enforced where it counts.
    """
    pytest.importorskip("redis", reason="broker adapter import needs redis")
    from backend.brokers.kis import KISBroker

    for raw in ["005930", "069500", "AAPL", "SPY", "BRK.B"]:
        assert S.is_kr(raw) == KISBroker._is_kr(raw), raw


# ── order codes vs quote codes ────────────────────────────────────────────────

def test_the_quote_code_differs_from_the_order_code():
    """KIS names the same venue two ways. From its own examples
    (``koreainvestment/open-trading-api``)::

        order(ovrs_excg_cd="NASD", pdno="AAPL")     # trading
        price(auth="", excd="NAS", symb="AAPL")     # quotes

    Passing the order code to a quote endpoint asks for an exchange it does
    not name, and the caller reads the empty answer as "no price".
    """
    assert S.to_quote_excd("NASD") == "NAS"
    assert S.to_quote_excd("NYSE") == "NYS"
    assert S.to_quote_excd("AMEX") == "AMS"


def test_krx_has_no_quote_exchange_code():
    """Domestic quotes go to a different endpoint that takes no exchange code.
    Returning some string would let a caller send it anyway."""
    assert S.to_quote_excd("KRX") is None


def test_an_unknown_exchange_has_no_quote_code():
    assert S.to_quote_excd("SEHK") is None
    assert S.to_quote_excd(None) is None
    assert S.to_quote_excd("") is None


def test_every_us_exchange_we_can_route_to_has_a_quote_code():
    """A venue we can place an order on but cannot price is a venue whose
    close-position path is broken — ``_live_close_price`` returns ``None`` and
    the close is refused for "no live price"."""
    for exchange in S.CANONICAL_EXCHANGES:
        if exchange == S.KR_EXCHANGE:
            continue
        assert S.to_quote_excd(exchange), f"{exchange} has no quote code"


# ── the symbols the app offers must all be routable ──────────────────────────

def test_the_nyse_names_in_the_catalogue_resolve_to_nyse():
    """These are offered by the picker's NYSE tab and are listed on NYSE
    proper, so the expected code is not in doubt. Before this mapping existed
    they resolved to the US default and KIS rejected the order.

    SPY and the XL* sector ETFs are excluded on purpose: they are NYSE Arca,
    and whether KIS files Arca under NYSE or AMEX is unverified — see the note
    in ``backend/quant/data/universe.py``. Asserting it here would turn an
    assumption into a fact by writing it down twice.
    """
    for symbol in ["JPM", "V", "BRK.B", "XOM", "WMT"]:
        assert S.resolve_exchange(symbol) == "NYSE", symbol
