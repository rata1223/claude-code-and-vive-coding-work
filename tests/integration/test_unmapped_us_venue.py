"""Held US positions sell on the venue the broker reports (known issue 5).

``EXCD_MAP`` covers the trading universe; a holding outside it — bought by
hand, say — used to be sold as ``NASD`` and rejected if it trades on NYSE.
Emergency flatten sells every holding, so that rejection is a flatten that
cannot finish. ``KISBroker`` now remembers each US balance row's
``ovrs_excg_cd`` and routes sells (and cancels, inquiries, quotes) of that
symbol there. No network: portfolio, quotes and orders are fakes.
"""
import logging

import pytest

from backend.brokers.kis import KISBroker
from backend.market.symbols import broker_exchange, is_mapped


class TestHelpers:
    @pytest.mark.parametrize("code, expected", [
        ("NASD", "NASD"), ("NAS", "NASD"), ("NYSE", "NYSE"), (" nys ", "NYSE"),
        ("AMEX", "AMEX"), ("AMS", "AMEX"), ("", None), ("SEHK", None), (None, None),
    ])
    def test_broker_exchange(self, code, expected):
        assert broker_exchange(code) == expected

    def test_is_mapped(self):
        assert is_mapped("AAPL") and is_mapped("SPY") and is_mapped("005930")
        assert not is_mapped("KO") and not is_mapped("AMD") and not is_mapped("")


class _Portfolio:
    def __init__(self, us_rows):
        self.us_rows = us_rows

    def get_kr_balance(self):
        return {"positions": [], "summary": {}}

    def get_us_balance(self):
        return {"positions": self.us_rows, "summary": {}}


class _Market:
    def __init__(self):
        self.quotes = []

    def get_price_us(self, symbol, excd):
        self.quotes.append((symbol, excd))
        return 50.0


class _Orders:
    def __init__(self):
        self.calls = []

    def sell_us(self, symbol, excd, qty, price):
        self.calls.append(("sell_us", symbol, excd))
        return {"output": {"ODNO": "1"}}

    buy_us = sell_us


class _Breaker:
    def is_open(self):
        return False

    def record_success(self):
        pass

    def record_failure(self):
        pass


def _broker(us_rows):
    b = KISBroker.__new__(KISBroker)
    b._paper = True
    b._account = "123456789012"
    b._portfolio = _Portfolio(us_rows)
    b._market = _Market()
    b._orders = _Orders()
    b._breaker = _Breaker()
    return b


def _row(symbol, venue=None):
    row = {"ovrs_pdno": symbol, "ovrs_cblc_qty": "3", "ord_psbl_qty": "3",
           "pchs_avg_pric": "40"}
    if venue is not None:
        row["ovrs_excg_cd"] = venue
    return row


@pytest.fixture(autouse=True)
def _tradeable(monkeypatch):
    """place_order checks the market calendar first; it is not under test."""
    from backend.data import calendar
    monkeypatch.setattr(calendar, "get_calendar_service",
                        lambda: type("C", (), {"assert_tradeable": lambda *a: None})())


def _sell(broker, symbol):
    broker.place_order(symbol, "sell", 3, 41.0)
    return broker._orders.calls[-1][2]


def test_an_unmapped_nyse_holding_sells_on_nyse():
    b = _broker([_row("KO", "NYSE")])
    b.get_positions()
    assert _sell(b, "KO") == "NYSE"            # was NASD → rejected
    assert ("KO", "NYS") in b._market.quotes   # the quote uses the row's venue too


def test_the_broker_wins_over_the_map_and_says_so(caplog):
    b = _broker([_row("SPY", "AMEX")])
    with caplog.at_level(logging.WARNING, logger="backend.brokers.kis"):
        b.get_positions()
        b.get_positions()                       # warned once, not every refresh
    assert _sell(b, "SPY") == "AMEX"
    assert sum("거래소 불일치 SPY" in r.message for r in caplog.records) == 1


def test_an_unmapped_symbol_not_held_keeps_the_nasd_fallback():
    b = _broker([])
    b.get_positions()
    assert _sell(b, "AMD") == "NASD"


def test_a_row_without_a_venue_changes_nothing():
    b = _broker([_row("KO")])
    b.get_positions()
    assert _sell(b, "KO") == "NASD"
    assert _sell(b, "AAPL") == "NASD"           # mapped: unchanged
