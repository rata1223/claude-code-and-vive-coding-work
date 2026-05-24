"""
평균회귀 전략 신호 — 쌍 거래(Pairs Trading) + 공적분 검정.
"""
import logging
import numpy as np
import pandas as pd
from backend.quant.signals.base import SignalBase, SignalOutput

logger = logging.getLogger(__name__)


def _spread(s1: pd.Series, s2: pd.Series) -> tuple[pd.Series, float]:
    """OLS 헤지 비율 계산 + 스프레드 시계열 반환."""
    from numpy.linalg import lstsq
    X = np.column_stack([s2.values, np.ones(len(s2))])
    (beta, intercept), _, _, _ = lstsq(X, s1.values, rcond=None)
    spread = s1 - beta * s2 - intercept
    return spread, beta


def cointegration_test(s1: pd.Series, s2: pd.Series, pvalue_threshold: float = 0.05) -> bool:
    """Engle-Granger 공적분 검정. True = 공적분 관계 있음."""
    try:
        from statsmodels.tsa.stattools import coint
        _, pvalue, _ = coint(s1, s2)
        return pvalue < pvalue_threshold
    except Exception as e:
        logger.warning("Cointegration test failed: %s", e)
        return False


class MeanReversionSignal(SignalBase):
    """
    단일 종목 평균회귀 신호 — RSI 과매도/과매수 + 볼린저 밴드 반전.

    RANGE 레짐에서 사용. 추세장에서는 신호 품질 저하.
    """

    def __init__(self, rsi_period: int = 14, bb_period: int = 20,
                 rsi_oversold: float = 35, rsi_overbought: float = 65):
        self._rsi_period = rsi_period
        self._bb_period = bb_period
        self._rsi_oversold = rsi_oversold
        self._rsi_overbought = rsi_overbought

    def name(self) -> str:
        return "MeanReversion"

    def compute(self, df: pd.DataFrame, symbol: str = "") -> SignalOutput:
        min_bars = max(self._rsi_period, self._bb_period) + 10
        if len(df) < min_bars:
            return SignalOutput(symbol=symbol, signal=0, strength=0.0)

        close = df["Close"]

        # RSI (순수 pandas)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(self._rsi_period).mean()
        loss = (-delta.clip(upper=0)).rolling(self._rsi_period).mean()
        rs = gain / (loss + 1e-9)
        rsi = 100 - 100 / (1 + rs)

        # Bollinger Bands
        sma = close.rolling(self._bb_period).mean()
        std = close.rolling(self._bb_period).std()
        bb_upper = sma + 2 * std
        bb_lower = sma - 2 * std

        # 전봉 기준 (no-lookahead)
        rsi_prev = rsi.iloc[-2]
        close_prev = close.iloc[-2]
        bb_low_prev = bb_lower.iloc[-2]
        bb_hi_prev = bb_upper.iloc[-2]

        if pd.isna(rsi_prev) or pd.isna(bb_low_prev):
            return SignalOutput(symbol=symbol, signal=0, strength=0.0)

        # 매수: RSI 과매도 + 하단 밴드 근접
        if rsi_prev < self._rsi_oversold and close_prev <= bb_low_prev * 1.02:
            strength = min((self._rsi_oversold - rsi_prev) / self._rsi_oversold, 1.0)
            return SignalOutput(symbol=symbol, signal=1, strength=round(strength, 4),
                                indicators={"rsi": round(rsi_prev, 2)})

        # 매도: RSI 과매수 + 상단 밴드 근접
        if rsi_prev > self._rsi_overbought and close_prev >= bb_hi_prev * 0.98:
            strength = min((rsi_prev - self._rsi_overbought) / (100 - self._rsi_overbought), 1.0)
            return SignalOutput(symbol=symbol, signal=-1, strength=round(strength, 4),
                                indicators={"rsi": round(rsi_prev, 2)})

        return SignalOutput(symbol=symbol, signal=0, strength=0.0,
                            indicators={"rsi": round(rsi_prev, 2)})


class PairsSignal(SignalBase):
    """
    두 종목의 공적분 관계를 이용한 평균회귀 신호.
    z-score > entry_z → 매도 s1, 매수 s2
    z-score < -entry_z → 매수 s1, 매도 s2
    """

    def __init__(self, symbol2_df: pd.DataFrame, entry_z: float = 2.0,
                 exit_z: float = 0.5, lookback: int = 60):
        self.symbol2_df = symbol2_df
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.lookback = lookback

    def compute(self, df: pd.DataFrame, symbol: str = "") -> SignalOutput:
        s1 = df["Close"]
        s2 = self.symbol2_df["Close"]

        # 공통 인덱스 정렬
        common = s1.index.intersection(s2.index)
        if len(common) < self.lookback + 20:
            return SignalOutput(symbol=symbol, signal=0, strength=0.0,
                                meta={"reason": "data_insufficient"})

        s1 = s1.loc[common]
        s2 = s2.loc[common]

        # lookback 구간으로 제한
        s1_w = s1.iloc[-self.lookback - 1:-1]  # no-lookahead
        s2_w = s2.iloc[-self.lookback - 1:-1]

        if not cointegration_test(s1_w, s2_w):
            return SignalOutput(symbol=symbol, signal=0, strength=0.0,
                                meta={"reason": "no_cointegration"})

        spread, beta = _spread(s1_w, s2_w)
        mean = spread.mean()
        std = spread.std()
        if std == 0:
            return SignalOutput(symbol=symbol, signal=0, strength=0.0)

        z = (spread.iloc[-1] - mean) / std

        if z > self.entry_z:
            # s1 과매수, 매도 신호
            return SignalOutput(symbol=symbol, signal=-1,
                                strength=min(abs(z) / 4, 1.0),
                                indicators={"z_score": round(z, 2), "beta": round(beta, 4)})
        if z < -self.entry_z:
            return SignalOutput(symbol=symbol, signal=1,
                                strength=min(abs(z) / 4, 1.0),
                                indicators={"z_score": round(z, 2), "beta": round(beta, 4)})
        return SignalOutput(symbol=symbol, signal=0, strength=0.0,
                            indicators={"z_score": round(z, 2)})
