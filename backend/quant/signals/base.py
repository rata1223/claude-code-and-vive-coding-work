"""
Signal ABC — 모든 전략 신호 모듈의 기본 인터페이스.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class SignalOutput:
    """단일 전략 신호 출력."""
    symbol: str
    signal: int              # 1=매수, -1=매도, 0=중립
    strength: float          # 0.0~1.0 신뢰도
    indicators: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


class SignalBase(ABC):
    """모든 신호 모듈의 공통 인터페이스."""

    @abstractmethod
    def compute(self, df: pd.DataFrame, symbol: str = "") -> SignalOutput:
        """
        OHLCV DataFrame을 받아 SignalOutput 반환.
        df: DatetimeIndex, columns=[Open,High,Low,Close,Volume]
        신호는 반드시 전봉 기준 (no-lookahead).
        """
        ...

    def name(self) -> str:
        return self.__class__.__name__
