"""
추세 지표 — SMA/EMA 크로스, MACD, Ichimoku 레짐 필터.
모든 신호는 no_lookahead() 적용 후 반환.
"""
import pandas as pd
from backend.quant.indicators.base import no_lookahead, safe_sma, safe_ema, IndicatorResult


def sma_cross(df: pd.DataFrame, fast: int = 50, slow: int = 200) -> IndicatorResult:
    """골든/데드 크로스 신호."""
    close = df["Close"]
    fast_line = safe_sma(close, fast)
    slow_line = safe_sma(close, slow)

    raw_signal = pd.Series(0, index=close.index)
    raw_signal[fast_line > slow_line] = 1
    raw_signal[fast_line < slow_line] = -1

    signal = no_lookahead(raw_signal)
    return IndicatorResult(values=no_lookahead(fast_line - slow_line), signal=signal,
                           name=f"SMA_{fast}_{slow}_cross")


def ema_trend(df: pd.DataFrame, length: int = 200) -> IndicatorResult:
    """가격이 EMA 위/아래 기반 추세 방향."""
    close = df["Close"]
    ema = safe_ema(close, length)
    raw_signal = pd.Series(0, index=close.index)
    raw_signal[close > ema] = 1
    raw_signal[close < ema] = -1
    return IndicatorResult(values=no_lookahead(close - ema),
                           signal=no_lookahead(raw_signal),
                           name=f"EMA_{length}_trend")


def macd_signal(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> IndicatorResult:
    """MACD 히스토그램 부호 기반 신호."""
    import pandas_ta as ta
    close = df["Close"]
    macd_df = ta.macd(close, fast=fast, slow=slow, signal=signal)
    if macd_df is None or macd_df.empty:
        return IndicatorResult(values=pd.Series(dtype=float), signal=pd.Series(dtype=float),
                               name="MACD")
    hist_col = [c for c in macd_df.columns if "MACDh" in c][0]
    hist = macd_df[hist_col]

    raw_signal = pd.Series(0, index=close.index)
    raw_signal[hist > 0] = 1
    raw_signal[hist < 0] = -1
    return IndicatorResult(values=no_lookahead(hist),
                           signal=no_lookahead(raw_signal),
                           name=f"MACD_{fast}_{slow}_{signal}")


def ichimoku_regime(df: pd.DataFrame) -> IndicatorResult:
    """
    Ichimoku 레짐 필터.
    가격 > 구름 위 AND 전환선 > 기준선 = 강세 (1)
    가격 < 구름 아래 = 약세 (-1)
    구름 안 = 중립 (0)
    """
    high, low, close = df["High"], df["Low"], df["Close"]

    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2       # 전환선
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2      # 기준선
    senkou_a = ((tenkan + kijun) / 2).shift(26)                        # 선행스팬A
    senkou_b = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)  # 선행스팬B

    cloud_top = pd.concat([senkou_a, senkou_b], axis=1).max(axis=1)
    cloud_bot = pd.concat([senkou_a, senkou_b], axis=1).min(axis=1)

    raw_signal = pd.Series(0, index=close.index)
    bullish = (close > cloud_top) & (tenkan >= kijun)
    bearish = close < cloud_bot
    raw_signal[bullish] = 1
    raw_signal[bearish] = -1

    return IndicatorResult(values=no_lookahead(close - cloud_top),
                           signal=no_lookahead(raw_signal),
                           name="Ichimoku_regime")
