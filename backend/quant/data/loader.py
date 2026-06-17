"""
Unified OHLCV data loader.
Supports: yfinance (US/ETF), PyKRX (KR stocks), BrokerAdapter (live).
All returned DataFrames have columns: Open, High, Low, Close, Volume
with DatetimeIndex (UTC-naive, exchange local time).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class StaleDataError(ValueError):
    """yfinance 데이터가 stale_hours 이상 오래된 경우."""
    pass

# KR 종목 코드 패턴 (6자리 숫자)
_KR_CODE_RE = __import__("re").compile(r"^\d{6}$")


def _is_kr(symbol: str) -> bool:
    return bool(_KR_CODE_RE.match(symbol))


class DataLoader:
    """
    단일 인터페이스로 US/KR 데이터 로드.
    캐시 없음 — 호출자가 캐싱 책임.
    """

    def fetch(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
        period: str = "2y",
        interval: str = "1d",
    ) -> pd.DataFrame:
        """
        OHLCV DataFrame 반환.
        start/end가 있으면 해당 범위, 없으면 period 사용.
        """
        if _is_kr(symbol):
            return self._fetch_kr(symbol, start, end, period, interval)
        return self._fetch_us(symbol, start, end, period, interval)

    def fetch_multi(
        self,
        symbols: list[str],
        start: Optional[str] = None,
        end: Optional[str] = None,
        period: str = "2y",
        interval: str = "1d",
    ) -> dict[str, pd.DataFrame]:
        """복수 종목 로드. 실패한 종목은 제외."""
        result = {}
        for sym in symbols:
            try:
                df = self.fetch(sym, start=start, end=end, period=period, interval=interval)
                if not df.empty:
                    result[sym] = df
            except Exception as e:
                logger.warning("DataLoader.fetch_multi failed %s: %s", sym, e)
        return result

    def _fetch_us(self, symbol, start, end, period, interval) -> pd.DataFrame:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        if start and end:
            df = ticker.history(start=start, end=end, interval=interval, auto_adjust=True)
        else:
            df = ticker.history(period=period, interval=interval, auto_adjust=True)
        if df.empty:
            raise ValueError(f"yfinance returned no data for {symbol}")
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index)
        df.sort_index(inplace=True)
        # NOTE (R-11): freshness is NOT judged here anymore. The loader serves
        # both live scans and intentionally-historical backtests, so a blanket
        # staleness check here was either wrong (backtests) or toothless (the
        # old WARN-only 26h check). The single authoritative gate now lives at
        # the execution boundary — backend/data/freshness_gate.FreshnessGate.
        return df

    def _fetch_kr(self, symbol, start, end, period, interval) -> pd.DataFrame:
        try:
            return self._fetch_kr_pykrx(symbol, start, end, period)
        except Exception as e:
            logger.warning("PyKRX failed for %s (%s), falling back to yfinance", symbol, e)
            # yfinance KR fallback: append .KS
            return self._fetch_us(f"{symbol}.KS", start, end, period, interval)

    def _fetch_kr_pykrx(self, symbol, start, end, period) -> pd.DataFrame:
        from pykrx import stock as krx
        if start and end:
            s = start.replace("-", "")
            e = end.replace("-", "")
        else:
            days = {"1y": 365, "2y": 730, "3y": 1095, "6m": 182, "3m": 91}.get(period, 730)
            e = datetime.today().strftime("%Y%m%d")
            s = (datetime.today() - timedelta(days=days)).strftime("%Y%m%d")

        df = krx.get_market_ohlcv_by_date(s, e, symbol)
        if df.empty:
            raise ValueError(f"PyKRX returned no data for {symbol}")
        df = df.rename(columns={"시가": "Open", "고가": "High", "저가": "Low",
                                 "종가": "Close", "거래량": "Volume"})
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index)
        df.sort_index(inplace=True)
        return df

    def fetch_from_broker(self, broker, symbol: str, bars: int = 200) -> pd.DataFrame:
        """
        라이브 브로커에서 최근 N봉 로드.
        broker: BrokerAdapter 구현체.
        KIS는 market_data 모듈로 실시간 OHLCV 제공.
        """
        try:
            from kis_adapter.market_data import get_ohlcv
            raw = get_ohlcv(symbol, count=bars)
            return pd.DataFrame(raw).set_index("date").sort_index()
        except Exception as e:
            logger.warning("broker fetch failed for %s: %s — falling back to yfinance", symbol, e)
            return self.fetch(symbol, period="1y")
