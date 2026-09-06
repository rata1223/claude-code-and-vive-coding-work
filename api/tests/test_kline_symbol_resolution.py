"""Blocker B — KR symbols must reach the chart provider under a name it knows.

``_fetch_kline_yf`` handed the raw symbol straight to yfinance, so every Korean
symbol charted as an empty panel: yfinance has never heard of a bare ``005930``.
It wants ``005930.KS`` (KOSPI) or ``005930.KQ`` (KOSDAQ), and a six-digit code
does not say which — so the provider symbol is resolved through
``backend.market.symbols``, which returns both candidates in try-order, rather
than by appending a guessed suffix here.

Every test mocks yfinance. No network calls.
"""
import pandas as pd
import pytest

from api.routers import indicators


class _FakeTicker:
    """Records the symbol it was constructed with; returns a canned frame."""

    def __init__(self, symbol, frames):
        self.symbol = symbol
        self._frames = frames

    def history(self, **_kwargs):
        return self._frames.get(self.symbol, pd.DataFrame())


def _bars(n=3):
    idx = pd.date_range("2026-01-02", periods=n, freq="D", tz="UTC")
    return pd.DataFrame(
        {"Open": [1.0] * n, "High": [2.0] * n, "Low": [0.5] * n,
         "Close": [1.5] * n, "Volume": [100] * n},
        index=idx,
    )


@pytest.fixture
def yf_spy(monkeypatch):
    """Patch yfinance and record every symbol asked for, in order."""
    asked = []
    frames = {}

    def factory(symbol):
        asked.append(symbol)
        return _FakeTicker(symbol, frames)

    monkeypatch.setattr(indicators.yf, "Ticker", factory)
    return asked, frames


# ── KR symbol chart resolution ────────────────────────────────────────────────

def test_a_kospi_symbol_is_requested_with_the_ks_suffix(yf_spy):
    asked, frames = yf_spy
    frames["005930.KS"] = _bars()

    bars = indicators._fetch_kline_yf("005930", "1d", 200)

    assert asked[0] == "005930.KS", "KOSPI candidate must be tried first"
    assert len(bars) == 3
    assert "005930" not in asked, "the bare code must never reach the provider"


def test_a_kosdaq_symbol_falls_back_to_the_kq_suffix(yf_spy):
    """``.KS`` returning nothing is not an error — it means the code trades on
    the other board. Without the fallback, every KOSDAQ symbol charts empty."""
    asked, frames = yf_spy
    frames["247540.KQ"] = _bars()

    bars = indicators._fetch_kline_yf("247540", "1d", 200)

    assert asked == ["247540.KS", "247540.KQ"]
    assert len(bars) == 3


def test_a_kr_symbol_on_neither_board_returns_no_bars(yf_spy):
    asked, _frames = yf_spy

    assert indicators._fetch_kline_yf("999999", "1d", 200) == []
    assert asked == ["999999.KS", "999999.KQ"], "both boards must be tried"


def test_the_kq_fallback_is_not_attempted_once_ks_has_data(yf_spy):
    """A second provider call per chart load, for every KOSPI symbol, is waste."""
    asked, frames = yf_spy
    frames["005930.KS"] = _bars()

    indicators._fetch_kline_yf("005930", "1d", 200)

    assert asked == ["005930.KS"]


# ── US symbols are unaffected ─────────────────────────────────────────────────

def test_a_us_symbol_is_requested_unchanged(yf_spy):
    asked, frames = yf_spy
    frames["AAPL"] = _bars()

    bars = indicators._fetch_kline_yf("AAPL", "1d", 200)

    assert asked == ["AAPL"]
    assert len(bars) == 3


def test_a_dotted_us_ticker_uses_the_provider_spelling(yf_spy):
    """Yahoo spells class shares with a hyphen. BRK.B is in the backend
    catalogue, so this is reachable from the picker."""
    asked, frames = yf_spy
    frames["BRK-B"] = _bars()

    bars = indicators._fetch_kline_yf("BRK.B", "1d", 200)

    assert asked == ["BRK-B"]
    assert len(bars) == 3


# ── unsupported symbols never reach the provider ──────────────────────────────

@pytest.mark.parametrize("raw", ["BTC/USDT", "ETHUSDT", ""])
def test_an_unsupported_symbol_is_not_sent_to_the_provider(yf_spy, raw):
    """A crypto pair has no equity chart to fetch. Asking anyway spends a
    network round trip to be told nothing."""
    asked, _frames = yf_spy

    assert indicators._fetch_kline_yf(raw, "1d", 200) == []
    assert asked == []


# ── a raising candidate must not kill the fallback (CodeRabbit #1) ────────────

class _RaisingTicker:
    """Raises on history() — a provider error for one board, not for the symbol."""

    def __init__(self, symbol, frames, raising):
        self.symbol = symbol
        self._frames = frames
        self._raising = raising

    def history(self, **_kwargs):
        if self.symbol in self._raising:
            raise RuntimeError(f"provider blew up on {self.symbol}")
        return self._frames.get(self.symbol, pd.DataFrame())


@pytest.fixture
def yf_flaky(monkeypatch):
    """Patch yfinance so named symbols raise instead of returning a frame."""
    asked = []
    frames = {}
    raising = set()

    def factory(symbol):
        asked.append(symbol)
        return _RaisingTicker(symbol, frames, raising)

    monkeypatch.setattr(indicators.yf, "Ticker", factory)
    return asked, frames, raising


def test_a_raising_ks_still_falls_back_to_kq(yf_flaky):
    """An exception on the KOSPI candidate is a *provider* failure for that
    board, not evidence the symbol has no data. Letting it escape the loop
    means every KOSDAQ symbol charts empty whenever the .KS lookup errors —
    the same empty-chart bug this file exists to prevent, by another route."""
    asked, frames, raising = yf_flaky
    raising.add("247540.KS")
    frames["247540.KQ"] = _bars()

    bars = indicators._fetch_kline_yf("247540", "1d", 200)

    assert asked == ["247540.KS", "247540.KQ"], "the .KQ candidate must still be tried"
    assert len(bars) == 3


def test_every_candidate_raising_returns_no_bars(yf_flaky):
    """Still no exception out of the helper — the caller gets an empty chart,
    not a 500."""
    asked, _frames, raising = yf_flaky
    raising.update({"005930.KS", "005930.KQ"})

    assert indicators._fetch_kline_yf("005930", "1d", 200) == []
    assert asked == ["005930.KS", "005930.KQ"]
