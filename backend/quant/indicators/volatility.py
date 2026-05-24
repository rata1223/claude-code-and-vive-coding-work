"""
변동성 지표 — ATR, Bollinger Bands, Keltner Channel, Squeeze.
"""
import pandas as pd
from backend.quant.indicators.base import no_lookahead, IndicatorResult


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    """ATR 시계열 (no-lookahead 적용)."""
    import pandas_ta as ta
    result = ta.atr(df["High"], df["Low"], df["Close"], length=length)
    if result is None:
        return pd.Series(dtype=float, index=df.index)
    return no_lookahead(result)


def bollinger_squeeze(df: pd.DataFrame, bb_length: int = 20, kc_length: int = 20) -> IndicatorResult:  # noqa: E501
    """
    Bollinger Squeeze — BB가 KC 안에 있을 때 압축(squeeze).
    squeeze 해제 시 방향성 포착 신호.
    signal: 1=squeeze 해제 후 상승, -1=하강, 0=squeeze 중
    """
    import pandas_ta as ta
    close = df["Close"]

    bb = ta.bbands(close, length=bb_length)
    if bb is None:
        return IndicatorResult(values=pd.Series(dtype=float), signal=pd.Series(dtype=float),
                               name="BB_Squeeze")

    bb_upper = bb[f"BBU_{bb_length}_2.0"]
    bb_lower = bb[f"BBL_{bb_length}_2.0"]

    # Keltner Channel (ATR 기반)
    atr_val = ta.atr(df["High"], df["Low"], close, length=kc_length)
    ema_mid = close.ewm(span=kc_length, adjust=False).mean()
    kc_upper = ema_mid + 1.5 * atr_val
    kc_lower = ema_mid - 1.5 * atr_val

    in_squeeze = (bb_upper < kc_upper) & (bb_lower > kc_lower)
    prev_squeeze = in_squeeze.shift(1)
    squeeze_fire = prev_squeeze & ~in_squeeze  # 직전 봉 squeeze, 현재 봉 해제

    momentum = close - close.rolling(bb_length).mean()
    raw_signal = pd.Series(0, index=close.index)
    raw_signal[squeeze_fire & (momentum > 0)] = 1
    raw_signal[squeeze_fire & (momentum < 0)] = -1

    return IndicatorResult(values=no_lookahead(momentum),
                           signal=no_lookahead(raw_signal),
                           name="BB_Squeeze")


def bollinger_bands(df: pd.DataFrame, length: int = 20, std: float = 2.0) -> IndicatorResult:
    """가격이 하단 밴드 터치(매수) / 상단 밴드 터치(매도)."""
    import pandas_ta as ta
    close = df["Close"]
    bb = ta.bbands(close, length=length, std=std)
    if bb is None:
        return IndicatorResult(values=pd.Series(dtype=float), signal=pd.Series(dtype=float),
                               name="BB")
    upper = bb[f"BBU_{length}_{std}"]
    lower = bb[f"BBL_{length}_{std}"]
    mid = bb[f"BBM_{length}_{std}"]

    pct_b = (close - lower) / (upper - lower).replace(0, float("nan"))
    raw_signal = pd.Series(0, index=close.index)
    raw_signal[close <= lower] = 1
    raw_signal[close >= upper] = -1

    return IndicatorResult(values=no_lookahead(pct_b),
                           signal=no_lookahead(raw_signal),
                           name=f"BB_{length}")


def atr_stop(df: pd.DataFrame, entry_price: float, side: str = "long",
             multiplier: float = 2.0, length: int = 14) -> float:
    """ATR 기반 스탑 가격 계산."""
    import pandas_ta as ta
    atr_val = ta.atr(df["High"], df["Low"], df["Close"], length=length)
    if atr_val is None or atr_val.empty:
        return entry_price * 0.93
    last_atr = atr_val.iloc[-2]  # no-lookahead: 전봉 ATR 사용
    if side == "long":
        return entry_price - multiplier * last_atr
    return entry_price + multiplier * last_atr
