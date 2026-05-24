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
