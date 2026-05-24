import logging
import pandas as pd
import pandas_ta as ta
import yfinance as yf

logger = logging.getLogger(__name__)

US_ETF = ["SPY", "QQQ", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE"]
US_LARGE = ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "JPM", "V"]
KR_ETF = ["069500", "360750", "091160"]

UNIVERSE = US_ETF + US_LARGE + KR_ETF

# 거래소 코드 매핑
EXCD_MAP = {s: "NASD" for s in ["AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "QQQ", "XLK", "XLRE"]}
EXCD_MAP.update({s: "NYSE" for s in ["SPY", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "JPM", "V"]})
EXCD_MAP.update({s: "AMEX" for s in ["AVGO"]})


class TradingSignals:
    def fetch_ohlcv(self, symbol: str, period: str = "1y") -> pd.DataFrame:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period)
        if df.empty:
            raise ValueError(f"No data for {symbol}")
        return df

    def buy_signal(self, df: pd.DataFrame) -> bool:
        close = df["Close"]
        volume = df["Volume"]

        sma200 = ta.sma(close, length=200)
        rsi = ta.rsi(close, length=14)
        vol_ma20 = ta.sma(volume, length=20)

        if sma200 is None or rsi is None or vol_ma20 is None:
            return False

        last_close = close.iloc[-1]
        last_sma200 = sma200.iloc[-1]
        last_rsi = rsi.iloc[-1]
        last_vol = volume.iloc[-1]
        last_vol_ma = vol_ma20.iloc[-1]

        # 3개월 수익률
        three_months_ago = close.iloc[-63] if len(close) >= 63 else close.iloc[0]
        momentum = (last_close - three_months_ago) / three_months_ago

        conditions = [
            last_close > last_sma200,
            momentum > 0,
            last_rsi < 70,
            last_vol > last_vol_ma,
        ]
        return all(conditions)

    def sell_signal(self, df: pd.DataFrame, entry_price: float) -> bool:
        close = df["Close"]
        sma200 = ta.sma(close, length=200)
        rsi = ta.rsi(close, length=14)

        if sma200 is None or rsi is None:
            return False

        last_close = close.iloc[-1]
        last_sma200 = sma200.iloc[-1]
        last_rsi = rsi.iloc[-1]

        stop_loss_hit = last_close <= entry_price * 0.93
        trend_broken = last_close < last_sma200
        overbought = last_rsi > 80

        return stop_loss_hit or trend_broken or overbought

    def scan_universe(self) -> dict:
        buy_candidates = []
        sell_candidates = []

        for symbol in UNIVERSE:
            try:
                df = self.fetch_ohlcv(symbol)
                if self.buy_signal(df):
                    buy_candidates.append(symbol)
                    logger.info("BUY signal: %s", symbol)
            except Exception as e:
                logger.warning("Signal scan failed for %s: %s", symbol, e)

        return {"buy": buy_candidates, "sell": sell_candidates}
