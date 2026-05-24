"""
모멘텀 지표 — RSI, Stochastic, Williams %R, ROC, 12-1 팩터.
"""
import pandas as pd
from backend.quant.indicators.base import no_lookahead, IndicatorResult


def rsi_signal(df: pd.DataFrame, length: int = 14,
               oversold: float = 30, overbought: float = 70) -> IndicatorResult:
    import pandas_ta as ta
    close = df["Close"]
    rsi_val = ta.rsi(close, length=length)
    if rsi_val is None:
        return IndicatorResult(values=pd.Series(dtype=float), signal=pd.Series(dtype=float),
                               name=f"RSI_{length}")
    raw_signal = pd.Series(0, index=close.index)
    raw_signal[rsi_val < oversold] = 1
    raw_signal[rsi_val > overbought] = -1
    return IndicatorResult(values=no_lookahead(rsi_val),
                           signal=no_lookahead(raw_signal),
                           name=f"RSI_{length}")


def stoch_signal(df: pd.DataFrame, k: int = 14, d: int = 3,
                 oversold: float = 20, overbought: float = 80) -> IndicatorResult:
    import pandas_ta as ta
    stoch = ta.stoch(df["High"], df["Low"], df["Close"], k=k, d=d)
    if stoch is None or stoch.empty:
        return IndicatorResult(values=pd.Series(dtype=float), signal=pd.Series(dtype=float),
                               name="Stoch")
    k_col = stoch.columns[0]
    d_col = stoch.columns[1]
    k_line = stoch[k_col]
    d_line = stoch[d_col]

    # K가 D를 아래서 위로 교차 + 과매도 구간 = 매수
    cross_up = (k_line > d_line) & (k_line.shift(1) <= d_line.shift(1))
    cross_dn = (k_line < d_line) & (k_line.shift(1) >= d_line.shift(1))

    raw_signal = pd.Series(0, index=k_line.index)
    raw_signal[cross_up & (k_line < oversold)] = 1
    raw_signal[cross_dn & (k_line > overbought)] = -1

    return IndicatorResult(values=no_lookahead(k_line),
                           signal=no_lookahead(raw_signal),
                           name=f"Stoch_{k}_{d}")


def momentum_12_1(df: pd.DataFrame) -> IndicatorResult:
    """
    Fama-French 12-1 모멘텀 팩터.
    12개월 수익률 - 1개월 수익률 (반전 효과 제거).
    신호: 값 > 0 = 매수, < 0 = 매도.
    """
    close = df["Close"]
    if len(close) < 252:
        empty = pd.Series(dtype=float)
        return IndicatorResult(values=empty, signal=empty, name="Mom_12_1")

    ret_12m = close / close.shift(252) - 1
    ret_1m = close / close.shift(21) - 1
    factor = ret_12m - ret_1m

    raw_signal = pd.Series(0, index=close.index)
    raw_signal[factor > 0] = 1
    raw_signal[factor < 0] = -1
    return IndicatorResult(values=no_lookahead(factor),
                           signal=no_lookahead(raw_signal),
                           name="Mom_12_1")


def roc_signal(df: pd.DataFrame, length: int = 20, threshold: float = 5.0) -> IndicatorResult:
    """Rate of Change 기반 신호."""
    import pandas_ta as ta
    close = df["Close"]
    roc = ta.roc(close, length=length)
    if roc is None:
        return IndicatorResult(values=pd.Series(dtype=float), signal=pd.Series(dtype=float),
                               name=f"ROC_{length}")
    raw_signal = pd.Series(0, index=close.index)
    raw_signal[roc > threshold] = 1
    raw_signal[roc < -threshold] = -1
    return IndicatorResult(values=no_lookahead(roc),
                           signal=no_lookahead(raw_signal),
                           name=f"ROC_{length}")
