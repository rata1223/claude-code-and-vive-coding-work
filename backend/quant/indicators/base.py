"""
No-lookahead indicator base.
모든 지표는 shift(1)로 현재 봉에서 참조 금지.
신호는 전봉 종가 기준으로만 생성됨 (repaint 방지).
"""
from dataclasses import dataclass
from typing import Optional
import pandas as pd


@dataclass
class IndicatorResult:
    """단일 지표 계산 결과."""
    values: pd.Series          # 지표값 시계열
    signal: pd.Series          # 1=buy, -1=sell, 0=neutral
    name: str


def no_lookahead(series: pd.Series) -> pd.Series:
    """
    시계열을 1봉 shift — 현재 봉에서 당일 확정 전 값을 참조하는 것을 방지.
    모든 지표 계산 후 반드시 적용.
    """
    return series.shift(1)


def safe_sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(length).mean()


def safe_ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()
