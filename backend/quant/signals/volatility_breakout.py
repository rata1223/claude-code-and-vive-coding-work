"""
변동성 돌파 전략 신호 — Bollinger Squeeze + ATR 채널 돌파.
"""
import pandas as pd
from backend.quant.signals.base import SignalBase, SignalOutput
from backend.quant.indicators.volatility import bollinger_squeeze, bollinger_bands


class VolatilityBreakoutSignal(SignalBase):
    """
    Bollinger Squeeze 해제 시 방향성 추종.
    BB Squeeze + 모멘텀 방향으로 진입.
    """

    def compute(self, df: pd.DataFrame, symbol: str = "") -> SignalOutput:
        if len(df) < 50:
            return SignalOutput(symbol=symbol, signal=0, strength=0.0)

        squeeze = bollinger_squeeze(df)
        bb = bollinger_bands(df)

        sq_signal = squeeze.signal.dropna().iloc[-1] if not squeeze.signal.dropna().empty else 0
        bb_signal = bb.signal.dropna().iloc[-1] if not bb.signal.dropna().empty else 0

        # Squeeze 해제 신호가 primary, BB 터치가 보조
        if sq_signal == 1:
            return SignalOutput(symbol=symbol, signal=1, strength=0.8,
                                meta={"trigger": "squeeze_breakout_up"})
        if sq_signal == -1:
            return SignalOutput(symbol=symbol, signal=-1, strength=0.8,
                                meta={"trigger": "squeeze_breakout_down"})
        if bb_signal == 1:
            return SignalOutput(symbol=symbol, signal=1, strength=0.5,
                                meta={"trigger": "bb_lower_touch"})

        return SignalOutput(symbol=symbol, signal=0, strength=0.0)
