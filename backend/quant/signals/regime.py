"""
레짐 탐지 엔진 — ADX, ATR percentile, EMA slope 기반 3-class 분류.
TREND / RANGE / STRESS

순수 pandas 구현 (pandas_ta 의존 없음).
"""
import logging
from dataclasses import dataclass, field

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class RegimeOutput:
    regime: str           # "trend" | "range" | "stress"
    score: float          # 0.0~1.0 (trend 강도)
    adx_pct: float        # ADX 백분위 (0~1)
    atr_pct: float        # ATR 백분위 (0~1)
    ema_slope: float      # -1~1 정규화 EMA 기울기
    meta: dict = field(default_factory=dict)


class RegimeDetector:
    """
    3-class 레짐 탐지기.

    TREND  : score > trend_threshold  (추세추종 전략 활성화)
    STRESS : atr_pct > stress_threshold  (현금 보유, 모든 매수 차단)
    RANGE  : 나머지  (평균회귀 전략 활성화)

    사용 예:
        detector = RegimeDetector()
        output = detector.detect(df_spy)
    """

    TREND = "trend"
    RANGE = "range"
    STRESS = "stress"

    def __init__(
        self,
        adx_window: int = 252,
        atr_window: int = 252,
        ema_span: int = 20,
        trend_threshold: float = 0.6,
        stress_threshold: float = 0.8,
        adx_weight: float = 0.4,
        atr_weight: float = 0.3,
        slope_weight: float = 0.3,
    ):
        self.adx_window = adx_window
        self.atr_window = atr_window
        self.ema_span = ema_span
        self.trend_threshold = trend_threshold
        self.stress_threshold = stress_threshold
        self.adx_weight = adx_weight
        self.atr_weight = atr_weight
        self.slope_weight = slope_weight

    def detect(self, df: pd.DataFrame) -> RegimeOutput:
        """
        df: OHLCV DataFrame (최소 atr_window+50 bars 권장)
        반환: RegimeOutput
        """
        if len(df) < 60:
            return RegimeOutput(regime=self.RANGE, score=0.5,
                                adx_pct=0.5, atr_pct=0.5, ema_slope=0.0,
                                meta={"reason": "insufficient_data"})
        try:
            adx_pct = self._adx_percentile(df)
            atr_pct = self._atr_percentile(df)
            ema_slope = self._ema_slope(df)

            # STRESS: 변동성 극단 → 현금 보유
            if atr_pct > self.stress_threshold:
                return RegimeOutput(
                    regime=self.STRESS, score=0.0,
                    adx_pct=adx_pct, atr_pct=atr_pct, ema_slope=ema_slope,
                    meta={"reason": "high_volatility"}
                )

            # 종합 score (높을수록 추세 강함)
            slope_pct = (ema_slope + 1) / 2  # -1~1 → 0~1
            score = (
                self.adx_weight * adx_pct
                + self.atr_weight * (1 - atr_pct)   # 낮은 ATR = 안정적 추세
                + self.slope_weight * slope_pct
            )
            score = float(np.clip(score, 0.0, 1.0))

            regime = self.TREND if score > self.trend_threshold else self.RANGE
            return RegimeOutput(
                regime=regime, score=round(score, 4),
                adx_pct=round(adx_pct, 4),
                atr_pct=round(atr_pct, 4),
                ema_slope=round(ema_slope, 4),
            )
        except Exception as e:
            logger.warning("RegimeDetector.detect 오류: %s", e)
            return RegimeOutput(regime=self.RANGE, score=0.5,
                                adx_pct=0.5, atr_pct=0.5, ema_slope=0.0,
                                meta={"error": str(e)})

    # ── 내부 계산 ────────────────────────────────────────────────────────

    def _adx_percentile(self, df: pd.DataFrame) -> float:
        """현재 ADX 값이 최근 adx_window 기간에서 몇 백분위인지."""
        adx = self._calc_adx(df["High"], df["Low"], df["Close"])
        if adx.dropna().empty:
            return 0.5
        window = min(self.adx_window, len(adx.dropna()))
        recent = adx.dropna().iloc[-window:]
        current = recent.iloc[-1]
        return float((recent <= current).mean())

    def _atr_percentile(self, df: pd.DataFrame) -> float:
        """현재 ATR 값이 최근 atr_window 기간에서 몇 백분위인지."""
        atr = self._calc_atr(df["High"], df["Low"], df["Close"])
        if atr.dropna().empty:
            return 0.5
        window = min(self.atr_window, len(atr.dropna()))
        recent = atr.dropna().iloc[-window:]
        current = recent.iloc[-1]
        return float((recent <= current).mean())

    def _ema_slope(self, df: pd.DataFrame) -> float:
        """EMA 기울기를 [-1, 1]로 정규화. 양수=상승, 음수=하락."""
        close = df["Close"]
        ema = close.ewm(span=self.ema_span, adjust=False).mean()
        if len(ema) < 2:
            return 0.0
        # 5일 기울기 (가격 대비 %)
        slope = (ema.iloc[-1] - ema.iloc[-5]) / (ema.iloc[-5] + 1e-9)
        # ±5% 범위로 클리핑 후 정규화
        return float(np.clip(slope / 0.05, -1.0, 1.0))

    @staticmethod
    def _calc_adx(high: pd.Series, low: pd.Series, close: pd.Series,
                  length: int = 14) -> pd.Series:
        """순수 pandas ADX 계산."""
        prev_high = high.shift(1)
        prev_low = low.shift(1)
        prev_close = close.shift(1)

        dm_plus = (high - prev_high).clip(lower=0)
        dm_minus = (prev_low - low).clip(lower=0)
        # DM+ > DM-인 경우만 DM+ 유효
        dm_plus = dm_plus.where(dm_plus > dm_minus, 0.0)
        dm_minus = dm_minus.where(dm_minus > dm_plus.where(dm_plus > dm_minus, 0.0), 0.0)

        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        atr = tr.ewm(alpha=1 / length, adjust=False).mean()
        di_plus = 100 * dm_plus.ewm(alpha=1 / length, adjust=False).mean() / (atr + 1e-9)
        di_minus = 100 * dm_minus.ewm(alpha=1 / length, adjust=False).mean() / (atr + 1e-9)
        dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus + 1e-9)
        return dx.ewm(alpha=1 / length, adjust=False).mean()

    @staticmethod
    def _calc_atr(high: pd.Series, low: pd.Series, close: pd.Series,
                  length: int = 14) -> pd.Series:
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(length).mean()
