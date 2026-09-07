"""Blocker B — the symbol abstraction layer.

One instrument has three names, and conflating them is a bug:

===============  ==============================  ===========
representation   who wants it                    ``005930``
===============  ==============================  ===========
``raw_symbol``   the user, the UI, the DB row    ``005930``
provider symbol  a market-data vendor            ``005930.KS``
backend symbol   KIS order routing               ``005930``
===============  ==============================  ===========

They coincide for US equities, which is why the distinction went unnoticed, and
diverge for KR, which is why every Korean symbol rendered an empty chart:
``_fetch_kline_yf`` handed yfinance a bare ``005930``, a ticker it has never
heard of.

This module is pure and stateless, like ``backend/risk/sellable_qty``. It talks
to no provider, broker, database or cache — it only decides names.

It is deliberately **not** a second source of truth. The symbol→exchange
mapping lives in ``backend/quant/data/universe`` (``EXCD_MAP``, ``KR_ETF``),
which that file documents as canonical and which ``backend/brokers/kis`` already
imports. A second mapping that could disagree with the broker adapter would be
a routing bug waiting to happen, so this module reuses it.
"""
from typing import List, Optional

from backend.quant.data.universe import EXCD_MAP, KR_ETF

#: The exchanges this platform can actually route an order to. This is the
#: whole supported vocabulary — the UI picker previously offered
#: ``Crypto / USStock / HKStock / Forex / Futures``, which intersects this set
#: nowhere, so its "hot symbols" list came back empty and its search survived
#: only on a yfinance fallback.
CANONICAL_EXCHANGES = ("KRX", "NASD", "NYSE")

#: KIS domestic. Six-digit codes trade here.
KR_EXCHANGE = "KRX"

#: The exchange assumed for a US ticker absent from ``EXCD_MAP``. Mirrors
#: ``EXCD_MAP.get(symbol, "NASD")``, the fallback already used throughout
#: ``backend/brokers/kis``; a different default here would route orders
#: differently from the broker adapter.
_US_DEFAULT = "NASD"

#: Quote currencies that mark a symbol as a crypto pair rather than an equity.
#: Pair separators (``/``) are caught separately.
_CRYPTO_QUOTES = ("USDT", "USDC", "BUSD")

#: Yahoo Finance board suffixes for KIS domestic codes. A six-digit code does
#: not say which board it trades on — KOSPI is ``.KS``, KOSDAQ is ``.KQ`` — so
#: both are offered, in the order to try them. Picking one and hoping is how
#: every symbol on the other board silently charts empty.
_KR_PROVIDER_SUFFIXES = (".KS", ".KQ")


def _clean(raw_symbol) -> str:
    """Uppercased, trimmed symbol, or ``""`` if there is nothing usable."""
    if not isinstance(raw_symbol, str):
        return ""
    return raw_symbol.strip().upper()


def is_supported_exchange(exchange) -> bool:
    """Whether ``exchange`` is one this platform can route to.

    Compared **exactly** — no trimming, no case folding. This gates a market
    vocabulary, not user input: a client sending ``"krx "`` is sending
    something other than the canonical value, and quietly accepting it would
    let a second spelling into a contract that has exactly three legal values.
    """
    return exchange in CANONICAL_EXCHANGES


def is_kr(raw_symbol) -> bool:
    """Whether this is a KIS domestic symbol.

    Matches ``KISBroker._is_kr`` exactly — six digits, or a member of
    ``KR_ETF``. That function already decides KR-vs-US for cancels and quotes;
    two definitions that could disagree is a routing bug.
    """
    symbol = _clean(raw_symbol)
    return symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit())


def _is_crypto(symbol: str) -> bool:
    """Whether ``symbol`` names a crypto pair rather than an equity."""
    if "/" in symbol:
        return True
    return any(symbol.endswith(quote) for quote in _CRYPTO_QUOTES)


def resolve_exchange(raw_symbol) -> Optional[str]:
    """The canonical exchange for ``raw_symbol``, or ``None``.

    ``None`` means *this is not a KIS-tradable equity* and must never be
    coerced to a default. The watchlist picker used to return saved crypto
    symbols unfiltered; if one reached order entry and were assigned some
    exchange anyway, it would be submitted. Refusing to classify it is what
    stops that.
    """
    symbol = _clean(raw_symbol)
    if not symbol or _is_crypto(symbol):
        return None
    if is_kr(symbol):
        return KR_EXCHANGE
    # A US ticker is letters, optionally with a class suffix (BRK.B) or a
    # hyphen. Anything else is not something this platform trades.
    core = symbol.replace(".", "").replace("-", "")
    if not core.isalpha():
        return None
    return EXCD_MAP.get(symbol, _US_DEFAULT)


def provider_symbol_candidates(raw_symbol) -> List[str]:
    """Market-data vendor spellings to try, best first.

    A list rather than a single value because a six-digit KR code is genuinely
    ambiguous between KOSPI and KOSDAQ; the caller tries each until one returns
    data. Empty when the symbol is not a supported equity — there is nothing
    legitimate to ask a provider for.
    """
    exchange = resolve_exchange(raw_symbol)
    if exchange is None:
        return []

    symbol = _clean(raw_symbol)
    if exchange == KR_EXCHANGE:
        return [f"{symbol}{suffix}" for suffix in _KR_PROVIDER_SUFFIXES]
    # Yahoo spells class shares with a hyphen: BRK.B is BRK-B.
    return [symbol.replace(".", "-")]


def to_backend_symbol(raw_symbol) -> Optional[str]:
    """The spelling KIS order routing expects, or ``None`` if unsupported.

    Deliberately *not* the provider spelling: ``005930.KS`` is not something
    KIS will accept, and letting a provider suffix reach the order path is the
    mirror image of the bug this module exists to fix.
    """
    if resolve_exchange(raw_symbol) is None:
        return None
    return _clean(raw_symbol)
