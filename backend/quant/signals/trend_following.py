"""
추세추종 전략 신호 — CTA 스타일.
SMA200 + MACD + Ichimoku 3중 확인.
"""
import pandas as pd
from backend.quant.signals.base import SignalBase, SignalOutput
from backend.quant.indicators.trend import sma_cross, ema_trend, macd_signal, ichimoku_regime


class TrendFollowingSignal(SignalBase):
    """
    3중 추세 확인:
    1) SMA50 > SMA200 (골든크로스)
    2) MACD 히스토그램 양수
    3) Ichimoku: 가격 > 구름 위
    3개 모두 동의 → 강한 매수 (strength=1.0)
    2개 동의 → 약한 매수 (strength=0.6)
    """

    def compute(self, df: pd.DataFrame, symbol: str = "") -> SignalOutput:
        if len(df) < 260:
            return SignalOutput(symbol=symbol, signal=0, strength=0.0,
                                meta={"reason": "data_insufficient"})

        sma = sma_cross(df, fast=50, slow=200)
        macd = macd_signal(df)
        ichi = ichimoku_regime(df)

        votes = []
        for ind in [sma, macd, ichi]:
            if not ind.signal.empty and len(ind.signal.dropna()) > 0:
                last = ind.signal.dropna().iloc[-1]
                votes.append(int(last))

        if not votes:
            return SignalOutput(symbol=symbol, signal=0, strength=0.0)

        bull_votes = votes.count(1)
        bear_votes = votes.count(-1)

        if bull_votes >= 3:
            return SignalOutput(symbol=symbol, signal=1, strength=1.0,
                                indicators={"sma": votes[0], "macd": votes[1], "ichi": votes[2]})
        if bull_votes == 2 and bear_votes == 0:
            return SignalOutput(symbol=symbol, signal=1, strength=0.6,
                                indicators={"votes": votes})
        if bear_votes >= 2:
            return SignalOutput(symbol=symbol, signal=-1, strength=0.7,
                                indicators={"votes": votes})
        return SignalOutput(symbol=symbol, signal=0, strength=0.0,
                            indicators={"votes": votes})
