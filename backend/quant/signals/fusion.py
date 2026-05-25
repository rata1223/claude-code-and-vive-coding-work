"""
Deliverable 4: 전략 스코어링 모델 — Signal Fusion.

여러 SignalBase 모듈의 출력을 가중합산해 최종 신호 생성.
각 신호의 weight × strength × direction 을 합산.
threshold 이상이면 매수, 이하이면 매도.

레짐 인식: RegimeDetector를 이용해 TREND/RANGE/STRESS에 따라
전략 가중치를 자동 조정.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd

from backend.quant.signals.base import SignalBase, SignalOutput

logger = logging.getLogger(__name__)


@dataclass
class FusionResult:
    symbol: str
    signal: int              # 1=매수, -1=매도, 0=중립
    score: float             # -1.0 ~ 1.0 (합산 스코어)
    strength: float          # 확신도 0.0~1.0
    individual: dict = field(default_factory=dict)  # {strategy_name: SignalOutput}
    regime_blocked: bool = False


class SignalFusion:
    """
    복수 전략 신호 합산기.

    사용 예:
        fusion = SignalFusion(buy_threshold=0.3, sell_threshold=-0.3)
        fusion.add(TrendFollowingSignal(), weight=0.4)
        fusion.add(MomentumSignal(), weight=0.4)
        fusion.add(VolatilityBreakoutSignal(), weight=0.2)
        result = fusion.evaluate(df, symbol="SPY")
    """

    def __init__(self, buy_threshold: float = 0.3, sell_threshold: float = -0.3):
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self._signals: list[tuple[SignalBase, float]] = []  # (signal, weight)
        self._regime_filter: Optional[callable] = None

    def add(self, signal: SignalBase, weight: float = 1.0) -> "SignalFusion":
        self._signals.append((signal, weight))
        return self

    def set_regime_filter(self, fn: callable) -> "SignalFusion":
        """fn(df) → bool: True면 신규 매수 허용, False면 차단."""
        self._regime_filter = fn
        return self

    def evaluate(self, df: pd.DataFrame, symbol: str = "") -> FusionResult:
        if not self._signals:
            return FusionResult(symbol=symbol, signal=0, score=0.0, strength=0.0)

        # 레짐 필터
        regime_blocked = False
        if self._regime_filter is not None:
            try:
                if not self._regime_filter(df):
                    regime_blocked = True
            except Exception as e:
                logger.warning("Regime filter error: %s", e)

        total_weight = sum(w for _, w in self._signals)
        if total_weight == 0:
            return FusionResult(symbol=symbol, signal=0, score=0.0, strength=0.0)

        weighted_score = 0.0
        individual = {}

        for sig, w in self._signals:
            try:
                out: SignalOutput = sig.compute(df, symbol=symbol)
                contribution = (w / total_weight) * out.signal * out.strength
                weighted_score += contribution
                individual[sig.name()] = out
            except Exception as e:
                logger.warning("Signal %s failed for %s: %s", sig.name(), symbol, e)

        if regime_blocked and weighted_score > 0:
            return FusionResult(symbol=symbol, signal=0, score=weighted_score,
                                strength=abs(weighted_score), individual=individual,
                                regime_blocked=True)

        if weighted_score >= self.buy_threshold:
            final_signal = 1
        elif weighted_score <= self.sell_threshold:
            final_signal = -1
        else:
            final_signal = 0

        return FusionResult(symbol=symbol, signal=final_signal,
                            score=round(weighted_score, 4),
                            strength=round(abs(weighted_score), 4),
                            individual=individual,
                            regime_blocked=False)

    def scan(self, dfs: dict[str, pd.DataFrame]) -> list[FusionResult]:
        """
        복수 종목 일괄 평가. 신호 강도 기준 내림차순 정렬.
        dfs: {symbol: df}
        """
        results = []
        for symbol, df in dfs.items():
            try:
                results.append(self.evaluate(df, symbol=symbol))
            except Exception as e:
                logger.warning("Fusion scan failed for %s: %s", symbol, e)
        results.sort(key=lambda r: r.score, reverse=True)
        return results


def default_fusion() -> SignalFusion:
    """
    기본 합산 설정: 추세추종 40% + 모멘텀 40% + 변동성돌파 20%.
    종목별 SMA200 기반 레짐 필터 (Ichimoku 대체 — 파라미터 1개).
    """
    from backend.quant.signals.trend_following import TrendFollowingSignal
    from backend.quant.signals.momentum import MomentumSignal
    from backend.quant.signals.volatility_breakout import VolatilityBreakoutSignal

    def regime_ok(df):
        """종목 자체의 SMA200 위에 있을 때만 신규 매수 허용."""
        if len(df) < 200:
            return True
        close = df["Close"]
        sma200 = close.rolling(200).mean().iloc[-1]
        return float(close.iloc[-1]) > float(sma200)

    fusion = SignalFusion(buy_threshold=0.25, sell_threshold=-0.25)
    fusion.add(TrendFollowingSignal(), weight=0.4)
    fusion.add(MomentumSignal(), weight=0.4)
    fusion.add(VolatilityBreakoutSignal(), weight=0.2)
    fusion.set_regime_filter(regime_ok)
    return fusion


def regime_aware_fusion(df_market: pd.DataFrame,
                        buy_threshold: float = 0.25,
                        sell_threshold: float = -0.25) -> SignalFusion:
    """
    레짐 인식 자동 가중치 조정 팩토리.

    TREND  → TrendCTA 50% + Momentum 30% + TrendFollowing 20%
    RANGE  → MeanReversion 60% + VolExpansion 40%
    STRESS → 신호 없음 (빈 fusion 반환)
    """
    from backend.quant.signals.regime import RegimeDetector
    from backend.quant.signals.trend_following import TrendFollowingSignal
    from backend.quant.signals.momentum import MomentumSignal
    from backend.quant.signals.trend_cta import TrendCTASignal
    from backend.quant.signals.vol_expansion import VolExpansionSignal
    from backend.quant.signals.mean_reversion import MeanReversionSignal

    detector = RegimeDetector()
    regime_out = detector.detect(df_market)
    logger.info("레짐 탐지: %s (score=%.3f)", regime_out.regime, regime_out.score)

    fusion = SignalFusion(buy_threshold=buy_threshold, sell_threshold=sell_threshold)

    if regime_out.regime == RegimeDetector.STRESS:
        # 모든 신호 비활성화 — 빈 fusion
        logger.warning("STRESS 레짐: 신규 매수 전면 차단")
        fusion.set_regime_filter(lambda _: False)
        return fusion

    if regime_out.regime == RegimeDetector.TREND:
        fusion.add(TrendCTASignal(), weight=0.5)
        fusion.add(MomentumSignal(), weight=0.3)
        fusion.add(TrendFollowingSignal(), weight=0.2)
    else:  # RANGE
        try:
            fusion.add(MeanReversionSignal(), weight=0.6)
        except Exception:
            pass
        fusion.add(VolExpansionSignal(), weight=0.4)

    return fusion
