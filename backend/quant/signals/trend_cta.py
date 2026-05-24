"""
CTA 추세추종 신호 — Donchian Channel + ADX 확인.

에지:
- 신고가 돌파 시 추세 지속 확률 높음 (Turtle Traders 연구)
- ADX > 25 확인으로 횡보장 오진 제거
- no-lookahead: 전봉 데이터만 사용
"""
import pandas as pd
import numpy as np

from backend.quant.signals.base import SignalBase, SignalOutput
from backend.quant.signals.regime import RegimeDetector


class TrendCTASignal(SignalBase):
    """
    Donchian Channel 돌파 + ADX 필터 추세추종 신호.

    TREND 레짐에서만 유의미 (SignalFusion 레짐 필터와 함께 사용).
    """

    def __init__(self, dc_period: int = 20, adx_period: int = 14,
                 adx_threshold: float = 25.0):
        self._dc_period = dc_period
        self._adx_period = adx_period
        self._adx_threshold = adx_threshold

    def name(self) -> str:
        return f"TrendCTA_{self._dc_period}"

    def compute(self, df: pd.DataFrame, symbol: str = "") -> SignalOutput:
        if len(df) < self._dc_period + 20:
            return SignalOutput(symbol=symbol, signal=0, strength=0.0)

        close = df["Close"]
        high = df["High"]
        low = df["Low"]

        # Donchian Channel (전봉 기준 no-lookahead)
        dc_high = high.shift(1).rolling(self._dc_period).max()
        dc_low = low.shift(1).rolling(self._dc_period).min()

        breakout_up = close > dc_high  # 신고가 돌파
        breakout_dn = close < dc_low   # 신저가 돌파

        # ADX 필터 (순수 pandas)
        adx = RegimeDetector._calc_adx(high, low, close, self._adx_period)
        adx_filtered = adx.iloc[-2] >= self._adx_threshold if len(adx) > 1 else False

        last_up = bool(breakout_up.iloc[-2]) if len(breakout_up) > 1 else False
        last_dn = bool(breakout_dn.iloc[-2]) if len(breakout_dn) > 1 else False

        if last_up and adx_filtered:
            # 강도: ADX 수치를 25~50 구간에서 0.5~1.0으로 정규화
            strength = float(np.clip((adx.iloc[-2] - 25) / 25, 0.0, 1.0)) * 0.5 + 0.5
            return SignalOutput(symbol=symbol, signal=1, strength=round(strength, 4),
                                indicators={"adx": round(float(adx.iloc[-2]), 2),
                                            "dc_period": self._dc_period})
        elif last_dn and adx_filtered:
            strength = float(np.clip((adx.iloc[-2] - 25) / 25, 0.0, 1.0)) * 0.5 + 0.5
            return SignalOutput(symbol=symbol, signal=-1, strength=round(strength, 4),
                                indicators={"adx": round(float(adx.iloc[-2]), 2),
                                            "dc_period": self._dc_period})

        return SignalOutput(symbol=symbol, signal=0, strength=0.0)
