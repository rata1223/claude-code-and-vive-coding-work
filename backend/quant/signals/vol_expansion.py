"""
변동성 확장 돌파 신호 — ATR 급증 + 가격 돌파 조합.

에지:
- 저변동성 압축(squeeze) 이후 ATR 급증은 방향성 이동 선행
- 변동성이 낮을 때 진입 → 스탑 좁음 → 유리한 R:R
- no-lookahead: 전봉 값만 사용
"""
import pandas as pd
import numpy as np

from backend.quant.signals.base import SignalBase, SignalOutput


class VolExpansionSignal(SignalBase):
    """
    ATR 확장 돌파 신호.

    조건:
    - 현재 ATR > 최근 atr_lookback 기간 평균 ATR × expansion_factor
    - 종가 > n일 고점 (매수) / 종가 < n일 저점 (매도)
    """

    def __init__(self, atr_period: int = 14, atr_lookback: int = 20,
                 expansion_factor: float = 1.5, breakout_period: int = 10):
        self._atr_period = atr_period
        self._atr_lookback = atr_lookback
        self._expansion_factor = expansion_factor
        self._breakout_period = breakout_period

    def name(self) -> str:
        return f"VolExpansion_{self._atr_period}"

    def compute(self, df: pd.DataFrame, symbol: str = "") -> SignalOutput:
        min_bars = self._atr_period + self._atr_lookback + 5
        if len(df) < min_bars:
            return SignalOutput(symbol=symbol, signal=0, strength=0.0)

        close = df["Close"]
        high = df["High"]
        low = df["Low"]

        # ATR 계산 (순수 pandas)
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(self._atr_period).mean()

        # ATR 확장 여부 (전봉 기준)
        current_atr = atr.iloc[-2]
        avg_atr = atr.iloc[-(self._atr_lookback + 2):-2].mean()
        if avg_atr <= 0 or pd.isna(current_atr) or pd.isna(avg_atr):
            return SignalOutput(symbol=symbol, signal=0, strength=0.0)

        expansion_ratio = current_atr / avg_atr
        is_expanding = expansion_ratio >= self._expansion_factor

        # 가격 돌파 (전봉 기준)
        breakout_high = high.shift(1).rolling(self._breakout_period).max().iloc[-2]
        breakout_low = low.shift(1).rolling(self._breakout_period).min().iloc[-2]
        last_close = close.iloc[-2]

        if is_expanding and last_close > breakout_high:
            strength = float(np.clip((expansion_ratio - 1) / 2, 0.0, 1.0))
            return SignalOutput(symbol=symbol, signal=1, strength=round(strength, 4),
                                indicators={"expansion_ratio": round(expansion_ratio, 3),
                                            "atr": round(current_atr, 4)})

        if is_expanding and last_close < breakout_low:
            strength = float(np.clip((expansion_ratio - 1) / 2, 0.0, 1.0))
            return SignalOutput(symbol=symbol, signal=-1, strength=round(strength, 4),
                                indicators={"expansion_ratio": round(expansion_ratio, 3),
                                            "atr": round(current_atr, 4)})

        return SignalOutput(symbol=symbol, signal=0, strength=0.0)
