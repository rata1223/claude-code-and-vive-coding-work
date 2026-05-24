"""
모멘텀 전략 신호 — Fama-French 12-1 + RSI 필터.
"""
import pandas as pd
from backend.quant.signals.base import SignalBase, SignalOutput
from backend.quant.indicators.momentum import momentum_12_1, rsi_signal


class MomentumSignal(SignalBase):
    """
    12-1 모멘텀 팩터 양수 AND RSI 30~70 (과열/침체 구간 제외).
    """

    def __init__(self, rsi_low: float = 30, rsi_high: float = 70):
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high

    def compute(self, df: pd.DataFrame, symbol: str = "") -> SignalOutput:
        if len(df) < 252:
            return SignalOutput(symbol=symbol, signal=0, strength=0.0,
                                meta={"reason": "data_insufficient"})

        mom = momentum_12_1(df)
        rsi = rsi_signal(df, length=14, oversold=self.rsi_low, overbought=self.rsi_high)

        if mom.values.empty or rsi.values.empty:
            return SignalOutput(symbol=symbol, signal=0, strength=0.0)

        mom_val = mom.values.dropna().iloc[-1] if not mom.values.dropna().empty else 0
        rsi_val = rsi.values.dropna().iloc[-1] if not rsi.values.dropna().empty else 50

        in_rsi_zone = self.rsi_low < rsi_val < self.rsi_high

        if mom_val > 0 and in_rsi_zone:
            # 강도: 모멘텀 크기를 0~1로 정규화 (상한 100%)
            strength = min(abs(mom_val) * 5, 1.0)
            return SignalOutput(symbol=symbol, signal=1, strength=strength,
                                indicators={"momentum_12_1": round(mom_val, 4), "rsi": round(rsi_val, 1)})
        if mom_val < -0.05 and rsi_val < 40:
            return SignalOutput(symbol=symbol, signal=-1, strength=0.6,
                                indicators={"momentum_12_1": round(mom_val, 4), "rsi": round(rsi_val, 1)})

        return SignalOutput(symbol=symbol, signal=0, strength=0.0,
                            indicators={"momentum_12_1": round(mom_val, 4), "rsi": round(rsi_val, 1)})
