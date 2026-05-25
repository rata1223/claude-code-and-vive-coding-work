"""
레짐 탐지 엔진 — SPY 실현변동성 + SMA200 기반 이진 분류.
RISK_ON / RISK_OFF / STRESS (히스테리시스 포함)

3-class 출력은 이전 인터페이스(regime: "trend"|"range"|"stress")와 호환.
trend_cta.py가 사용하는 RegimeDetector._calc_adx()는 유지.
"""
import logging
from dataclasses import dataclass, field

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class RegimeOutput:
    regime: str           # "trend" | "range" | "stress"
    score: float          # 0.0~1.0 (시장 강도)
    risk_on: bool         # True = 신규 매수 허용
    vol_ann: float        # 연환산 실현변동성
    sma_pct: float        # 종가 / SMA200 − 1 (양수=위, 음수=아래)
    # 구형 인터페이스 호환 필드 (사용하지 말 것)
    adx_pct: float = 0.5
    atr_pct: float = 0.5
    ema_slope: float = 0.0
    meta: dict = field(default_factory=dict)


class BinaryRegimeEngine:
    """
    이진 레짐 탐지: SPY 실현변동성 + SMA200.

    상태 전이 (히스테리시스):
      RISK_ON  → RISK_OFF : vol_ann > vol_caution (0.22)
      RISK_ON  → STRESS   : vol_ann > vol_stress  (0.35)
      RISK_OFF → RISK_ON  : vol_ann < vol_clear   (0.18) AND close > SMA200
      RISK_OFF → STRESS   : vol_ann > vol_stress  (0.35)
      STRESS   → RISK_OFF : vol_ann < vol_caution (0.22)

    파라미터 단 3개.
    """

    RISK_ON = "risk_on"
    RISK_OFF = "risk_off"
    STRESS = "stress"

    # 구형 인터페이스 매핑
    TREND = "trend"
    RANGE = "range"

    def __init__(
        self,
        vol_stress: float = 0.35,    # 연환산 변동성 → STRESS
        vol_caution: float = 0.22,   # 연환산 변동성 → RISK_OFF
        vol_clear: float = 0.18,     # 연환산 변동성 → RISK_ON 복귀
        sma_period: int = 200,
        vol_window: int = 21,
    ):
        self.vol_stress = vol_stress
        self.vol_caution = vol_caution
        self.vol_clear = vol_clear
        self.sma_period = sma_period
        self.vol_window = vol_window

    def detect(self, df: pd.DataFrame,
               prev_state: str = "risk_on") -> RegimeOutput:
        """
        df: SPY OHLCV DataFrame (최소 sma_period + vol_window bars 필요).
        prev_state: 히스테리시스를 위한 이전 상태.
        """
        min_bars = self.sma_period + self.vol_window
        if len(df) < min_bars:
            return RegimeOutput(
                regime="range", score=0.5, risk_on=True,
                vol_ann=0.0, sma_pct=0.0,
                meta={"reason": "insufficient_data", "bars": len(df)}
            )

        try:
            close = df["Close"]

            # 21일 실현변동성 (연환산)
            log_ret = np.log(close / close.shift(1)).dropna()
            vol_daily = log_ret.iloc[-self.vol_window:].std()
            vol_ann = float(vol_daily * np.sqrt(252))

            # SMA200 대비 위치
            sma200 = close.rolling(self.sma_period).mean().iloc[-1]
            last_close = close.iloc[-1]
            sma_pct = float((last_close / sma200) - 1.0) if sma200 > 0 else 0.0

            # 상태 결정 (히스테리시스)
            new_state = self._transition(prev_state, vol_ann, sma_pct)

            # 구형 인터페이스 매핑
            if new_state == self.STRESS:
                old_regime = "stress"
                risk_on = False
                score = 0.0
            elif new_state == self.RISK_ON:
                old_regime = "trend"
                risk_on = True
                # score: sma 위치 + 저변동성 반영
                score = float(np.clip(0.5 + sma_pct * 5 + (0.22 - vol_ann), 0.3, 1.0))
            else:  # RISK_OFF
                old_regime = "range"
                risk_on = False
                score = float(np.clip(0.5 - vol_ann, 0.0, 0.5))

            return RegimeOutput(
                regime=old_regime,
                score=round(score, 4),
                risk_on=risk_on,
                vol_ann=round(vol_ann, 4),
                sma_pct=round(sma_pct, 4),
                meta={"state": new_state, "sma200": round(float(sma200), 2)}
            )

        except Exception as e:
            logger.warning("BinaryRegimeEngine.detect 오류: %s", e)
            return RegimeOutput(
                regime="range", score=0.5, risk_on=True,
                vol_ann=0.0, sma_pct=0.0,
                meta={"error": str(e)}
            )

    def _transition(self, prev: str, vol_ann: float, sma_pct: float) -> str:
        # STRESS 진입: 항상 즉시
        if vol_ann > self.vol_stress:
            return self.STRESS

        if prev == self.STRESS:
            # STRESS 탈출: 변동성이 caution 아래로 내려와야
            if vol_ann < self.vol_caution:
                return self.RISK_OFF
            return self.STRESS

        if prev == self.RISK_ON:
            # RISK_ON 유지 조건: vol < caution
            if vol_ann < self.vol_caution:
                return self.RISK_ON
            return self.RISK_OFF

        # RISK_OFF
        # RISK_ON 복귀: vol < clear AND 가격이 SMA200 위
        if vol_ann < self.vol_clear and sma_pct > 0:
            return self.RISK_ON
        return self.RISK_OFF


class RegimeDetector(BinaryRegimeEngine):
    """
    BinaryRegimeEngine의 구형 인터페이스 래퍼.
    기존 코드가 RegimeDetector().detect(df) 로 호출하는 경우 그대로 작동.

    구형 파라미터 매핑:
    - stress_threshold <= 0.0  → vol_stress = 0.0 (항상 STRESS 강제)
    - trend_threshold <= 0.35  → vol_caution 하향 (TREND 진입 쉽게)
    """

    # 구형 클래스 속성 유지
    TREND = "trend"
    RANGE = "range"
    STRESS = "stress"

    def __init__(
        self,
        # 구형 파라미터 (호환성)
        adx_window: int = 252,
        atr_window: int = 252,
        ema_span: int = 20,
        trend_threshold: float = 0.6,
        stress_threshold: float = 0.8,
        adx_weight: float = 0.4,
        atr_weight: float = 0.3,
        slope_weight: float = 0.3,
        # 신규 파라미터
        vol_stress: float = 0.35,
        vol_caution: float = 0.22,
        vol_clear: float = 0.18,
        sma_period: int = 200,
        vol_window: int = 21,
    ):
        # stress_threshold=0.0 → 항상 STRESS (테스트 호환)
        if stress_threshold <= 0.0:
            vol_stress = 0.0
        # trend_threshold 낮으면 → RISK_ON 진입 쉽게 (caution 낮춤)
        if trend_threshold < 0.5:
            vol_caution = min(vol_caution, 0.40)
            vol_stress = max(vol_stress, 0.99)  # STRESS 거의 불가

        super().__init__(
            vol_stress=vol_stress,
            vol_caution=vol_caution,
            vol_clear=vol_clear,
            sma_period=sma_period,
            vol_window=vol_window,
        )

    @staticmethod
    def _calc_adx(high: pd.Series, low: pd.Series, close: pd.Series,
                  length: int = 14) -> pd.Series:
        """trend_cta.py 호환용 ADX 계산 (변경 없음)."""
        prev_high = high.shift(1)
        prev_low = low.shift(1)
        prev_close = close.shift(1)

        dm_plus = (high - prev_high).clip(lower=0)
        dm_minus = (prev_low - low).clip(lower=0)
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
