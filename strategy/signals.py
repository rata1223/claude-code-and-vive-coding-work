import logging
import os
import pandas as pd
import yfinance as yf

try:
    import pandas_ta as ta
    _HAS_PANDAS_TA = True
except ImportError:
    ta = None
    _HAS_PANDAS_TA = False

logger = logging.getLogger(__name__)

US_ETF = ["SPY", "QQQ", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE"]
US_LARGE = ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "JPM", "V"]
KR_ETF = ["069500", "360750", "091160"]

UNIVERSE = US_ETF + US_LARGE + KR_ETF

EXCD_MAP = {s: "NASD" for s in ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "QQQ", "XLK", "XLRE"]}
EXCD_MAP.update({s: "NYSE" for s in ["SPY", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "JPM", "V"]})

SECTOR_MAP = {
    "SPY": "broad", "QQQ": "tech", "XLK": "tech", "XLF": "finance",
    "XLE": "energy", "XLV": "health", "XLI": "industrial", "XLY": "consumer_disc",
    "XLP": "consumer_staple", "XLU": "utility", "XLRE": "realestate",
    "AAPL": "tech", "NVDA": "tech", "MSFT": "tech", "GOOGL": "tech",
    "AMZN": "consumer_disc", "META": "tech", "TSLA": "consumer_disc",
    "AVGO": "tech", "JPM": "finance", "V": "finance",
    "069500": "broad_kr", "360750": "broad_kr", "091160": "tech_kr",
}

_YF_TICKER = {k: k + ".KS" for k in KR_ETF}


def _fetch(symbol: str, period: str = "2y", interval: str = "1d") -> pd.DataFrame:
    yf_symbol = _YF_TICKER.get(symbol, symbol)
    df = yf.Ticker(yf_symbol).history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        raise ValueError(f"No data: {symbol} (yf={yf_symbol})")
    return df


class TradingSignals:
    """기본 일봉 매매 신호 (4조건 매수 / 3조건 매도)."""

    def fetch_ohlcv(self, symbol: str, period: str = "2y") -> pd.DataFrame:
        return _fetch(symbol, period)

    @staticmethod
    def _sma(series: pd.Series, length: int) -> pd.Series:
        return series.rolling(window=length).mean()

    @staticmethod
    def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(window=length).mean()
        loss = (-delta.clip(upper=0)).rolling(window=length).mean()
        rs = gain / loss.replace(0, float("nan"))
        return 100 - (100 / (1 + rs))

    def buy_signal(self, df: pd.DataFrame) -> bool:
        close = df["Close"]
        volume = df["Volume"]
        if len(close) < 200:
            return False

        if _HAS_PANDAS_TA:
            sma200 = ta.sma(close, length=200)
            rsi = ta.rsi(close, length=14)
            vol_ma20 = ta.sma(volume, length=20)
        else:
            sma200 = self._sma(close, 200)
            rsi = self._rsi(close, 14)
            vol_ma20 = self._sma(volume, 20)

        if sma200 is None or rsi is None or vol_ma20 is None:
            return False

        last_close = close.iloc[-1]
        last_sma200 = sma200.iloc[-1]
        last_rsi = rsi.iloc[-1]
        last_vol = volume.iloc[-1]
        last_vol_ma = vol_ma20.iloc[-1]

        if len(close) < 63:
            return False
        momentum = (last_close - close.iloc[-63]) / close.iloc[-63]

        return all([
            last_close > last_sma200,
            momentum > 0,
            last_rsi < 70,
            last_vol > last_vol_ma,
        ])

    def sell_signal(self, df: pd.DataFrame, entry_price: float) -> tuple[bool, str]:
        """(should_sell, reason) 반환."""
        close = df["Close"]
        if len(close) < 200:
            return False, ""

        if _HAS_PANDAS_TA:
            sma200 = ta.sma(close, length=200)
            rsi = ta.rsi(close, length=14)
        else:
            sma200 = self._sma(close, 200)
            rsi = self._rsi(close, 14)

        if sma200 is None or rsi is None:
            return False, ""

        last_close = close.iloc[-1]
        stop_pct = float(os.environ.get("STOP_LOSS_PCT", 0.07))

        if last_close <= entry_price * (1 - stop_pct):
            return True, f"손절 (진입가 {entry_price:.2f} → 현재 {last_close:.2f})"
        if last_close < sma200.iloc[-1]:
            return True, "200일 이평 하향 돌파"
        if rsi.iloc[-1] > 80:
            return True, f"RSI 과매수 ({rsi.iloc[-1]:.1f})"
        return False, ""

    def scan_universe(self, portfolio_positions: dict = None) -> dict:
        buy_candidates = []
        sell_candidates = []
        portfolio_positions = portfolio_positions or {}

        for symbol in UNIVERSE:
            try:
                df = self.fetch_ohlcv(symbol)
                if symbol in portfolio_positions:
                    entry = portfolio_positions[symbol]
                    should_sell, reason = self.sell_signal(df, entry)
                    if should_sell:
                        sell_candidates.append((symbol, reason))
                        logger.info("SELL signal: %s — %s", symbol, reason)
                    continue
                if self.buy_signal(df):
                    buy_candidates.append(symbol)
                    logger.info("BUY signal: %s", symbol)
            except Exception as e:
                logger.warning("Signal scan failed for %s: %s", symbol, e)

        return {"buy": buy_candidates, "sell": sell_candidates}


class MultiTimeframeSignals:
    """전문 전략: 일봉 + 주봉 다중 타임프레임 + 시장 레짐 탐지."""

    def __init__(self):
        self._base = TradingSignals()

    def weekly_trend_ok(self, symbol: str) -> bool:
        """주봉 20주 이평 위 = 중기 추세 양호."""
        try:
            df_w = _fetch(symbol, period="3y", interval="1wk")
            close = df_w["Close"]
            if len(close) < 20:
                return False
            sma20w = ta.sma(close, length=20) if _HAS_PANDAS_TA else self._base._sma(close, 20)
            return sma20w is not None and close.iloc[-1] > sma20w.iloc[-1]
        except Exception as e:
            logger.warning("Weekly trend check failed %s: %s", symbol, e)
            return False

    def regime_is_bullish(self) -> bool:
        """SPY 20일 실현변동성 < 25% = 정상 시장."""
        try:
            df = _fetch("SPY", period="60d", interval="1d")
            returns = df["Close"].pct_change().dropna()
            realized_vol = returns.std() * (252 ** 0.5)
            is_bullish = realized_vol < 0.25
            logger.info("Market regime: vol=%.2f%%, bullish=%s", realized_vol * 100, is_bullish)
            return is_bullish
        except Exception as e:
            logger.warning("Regime check failed: %s — assuming bullish", e)
            return True

    def momentum_factor(self, df: pd.DataFrame) -> float:
        """12-1 모멘텀 팩터."""
        close = df["Close"]
        if len(close) < 252:
            return 0.0
        ret_12m = (close.iloc[-1] - close.iloc[-252]) / close.iloc[-252]
        ret_1m = (close.iloc[-1] - close.iloc[-21]) / close.iloc[-21]
        return ret_12m - ret_1m

    def atr_position_size(self, df: pd.DataFrame, capital: float, risk_pct: float = 0.01) -> float:
        """ATR 기반 포지션 크기: 자본의 risk_pct를 1ATR 손실로 제한."""
        close = df["Close"]
        if _HAS_PANDAS_TA:
            atr = ta.atr(df["High"], df["Low"], close, length=14)
        else:
            atr = (df["High"] - df["Low"]).rolling(14).mean()
        if atr is None or atr.iloc[-1] == 0:
            return capital * 0.05
        risk_amount = capital * risk_pct
        shares = risk_amount / atr.iloc[-1]
        position_value = shares * close.iloc[-1]
        return min(position_value, capital * 0.05)

    def sector_diversified(self, candidate: str, already_selected: list) -> bool:
        """동일 섹터 종목이 이미 2개 있으면 차단."""
        sector = SECTOR_MAP.get(candidate)
        if sector is None:
            return True
        count = sum(1 for s in already_selected if SECTOR_MAP.get(s) == sector)
        return count < 2

    def scan_universe(self, portfolio_positions: dict = None) -> dict:
        """3중 필터: 일봉 신호 AND 주봉 추세 AND 시장 레짐."""
        portfolio_positions = portfolio_positions or {}
        base_result = self._base.scan_universe(portfolio_positions)

        if not self.regime_is_bullish():
            logger.warning("시장 레짐 비관적 — 신규 매수 차단 (매도 신호는 유지)")
            return {"buy": [], "sell": base_result["sell"], "regime": "bearish"}

        filtered = []
        for symbol in base_result["buy"]:
            if self.weekly_trend_ok(symbol):
                filtered.append(symbol)
            else:
                logger.info("WEEKLY FILTER blocked: %s", symbol)

        ranked = []
        for symbol in filtered:
            try:
                df = self._base.fetch_ohlcv(symbol)
                mom = self.momentum_factor(df)
                ranked.append((symbol, mom))
            except Exception:
                ranked.append((symbol, 0.0))
        ranked.sort(key=lambda x: x[1], reverse=True)

        selected = []
        for symbol, _ in ranked:
            if self.sector_diversified(symbol, selected):
                selected.append(symbol)

        return {"buy": selected, "sell": base_result["sell"], "regime": "bullish"}
