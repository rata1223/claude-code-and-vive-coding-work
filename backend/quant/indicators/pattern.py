"""
패턴 인식 — Fibonacci 되돌림, Harmonic 패턴 탐지.
"""
import pandas as pd
import numpy as np
from backend.quant.indicators.base import no_lookahead, IndicatorResult


def fibonacci_levels(df: pd.DataFrame, lookback: int = 50) -> dict[str, float]:
    """
    최근 N봉 고가/저가 기반 Fibonacci 되돌림 레벨 계산.
    전봉 데이터 기준 (no-lookahead).
    반환: {'0': low, '0.236': ..., '0.382': ..., '0.5': ..., '0.618': ..., '1': high}
    """
    window = df.iloc[-lookback - 1:-1]  # 전봉까지
    high = window["High"].max()
    low = window["Low"].min()
    diff = high - low
    ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    return {str(r): round(high - r * diff, 4) for r in ratios}


def fibonacci_signal(df: pd.DataFrame, lookback: int = 50,
                     tolerance: float = 0.005) -> IndicatorResult:
    """
    현재가가 0.618 또는 0.5 되돌림 레벨 근처에서 반등 시 매수 신호.
    tolerance: 레벨과의 허용 오차 비율.
    """
    close = df["Close"]
    raw_signal = pd.Series(0, index=close.index)

    for i in range(lookback + 1, len(df)):
        window = df.iloc[i - lookback:i]
        high = window["High"].max()
        low = window["Low"].min()
        diff = high - low
        if diff == 0:
            continue
        price = df["Close"].iloc[i - 1]  # 전봉 종가 (no-lookahead)
        for ratio in [0.5, 0.618, 0.786]:
            level = high - ratio * diff
            if abs(price - level) / level < tolerance:
                # 전봉이 레벨 근처에서 닫힘 → 당일 매수 신호
                raw_signal.iloc[i] = 1
                break

    return IndicatorResult(values=no_lookahead(raw_signal.astype(float)),
                           signal=no_lookahead(raw_signal),
                           name="Fibonacci_618")


def _find_swings(df: pd.DataFrame, n: int = 5) -> tuple[pd.Series, pd.Series]:
    """로컬 고점/저점 탐지 (n봉 좌우 비교)."""
    highs = df["High"]
    lows = df["Low"]
    swing_hi = pd.Series(np.nan, index=df.index)
    swing_lo = pd.Series(np.nan, index=df.index)
    for i in range(n, len(df) - n):
        if highs.iloc[i] == highs.iloc[i - n:i + n + 1].max():
            swing_hi.iloc[i] = highs.iloc[i]
        if lows.iloc[i] == lows.iloc[i - n:i + n + 1].min():
            swing_lo.iloc[i] = lows.iloc[i]
    return swing_hi, swing_lo


def harmonic_gartley(df: pd.DataFrame, tolerance: float = 0.05) -> IndicatorResult:
    """
    Gartley 패턴 탐지 (단순화 버전).
    XABCD 비율: XA=1, AB=0.618, BC=0.382~0.886, CD=1.272~1.618.
    실제 운용 시 더 정교한 라이브러리(HarmonicPatterns) 사용 권장.
    """
    close = df["Close"]
    raw_signal = pd.Series(0, index=close.index)
    swing_hi, swing_lo = _find_swings(df, n=5)

    hi_idx = swing_hi.dropna().index.tolist()
    lo_idx = swing_lo.dropna().index.tolist()

    if len(hi_idx) < 2 or len(lo_idx) < 2:
        return IndicatorResult(values=raw_signal.astype(float),
                               signal=raw_signal, name="Gartley")

    # 마지막 확인된 스윙 포인트 기준 비율 체크
    x_lo = swing_lo.dropna().iloc[-2] if len(swing_lo.dropna()) >= 2 else None
    a_hi = swing_hi.dropna().iloc[-1] if len(swing_hi.dropna()) >= 1 else None
    b_lo = swing_lo.dropna().iloc[-1] if len(swing_lo.dropna()) >= 1 else None

    if x_lo and a_hi and b_lo and x_lo < a_hi:
        xa = a_hi - x_lo
        ab = a_hi - b_lo
        ab_ratio = ab / xa if xa else 0
        if abs(ab_ratio - 0.618) < tolerance:
            last_idx = df.index[-1]
            raw_signal[last_idx] = 1  # 불리시 Gartley 완성 신호

    return IndicatorResult(values=no_lookahead(raw_signal.astype(float)),
                           signal=no_lookahead(raw_signal),
                           name="Gartley")
