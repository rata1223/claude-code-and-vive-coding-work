"""
전략 스코어링 + Signal Fusion.

SignalFusion: 가중합산 + 레짐 필터 (기본)
RobustFusion: 신뢰도 가중 + 약신호 억제 + 충돌 해소 + 드로다운 디스카운트

각 신호의 weight × strength × direction 을 합산.
threshold 이상이면 매수, 이하이면 매도.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
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
    meta: dict = field(default_factory=dict)        # 디버그 정보


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


class RobustFusion:
    """
    신뢰도 가중 + 약신호 억제 + 충돌 해소 + 드로다운 디스카운트 Fusion.

    기존 SignalFusion 대비 개선:
    1. 정규화 스코어: 각 신호 strength를 z-score 정규화 (지배적 신호 방지)
    2. 신뢰도 가중: 과거 신호 정확도 기반 가중치 보정
    3. 약신호 억제: |score| < min_score_threshold → 0 처리
    4. 충돌 해소: 매수-매도 신호가 동시에 강하면 중립 처리
    5. 드로다운 디스카운트: MDD 심할수록 매수 신호 축소

    사용 예:
        fusion = RobustFusion()
        fusion.add(TrendFollowingSignal(), weight=0.4)
        fusion.add(MomentumSignal(), weight=0.4)
        fusion.add(VolatilityBreakoutSignal(), weight=0.2)
        result = fusion.evaluate(df, symbol="069500")
    """

    def __init__(
        self,
        buy_threshold: float = 0.30,
        sell_threshold: float = -0.25,
        min_score_threshold: float = 0.10,   # 약신호 억제 (이 이하는 중립)
        conflict_ratio: float = 0.70,        # 매수·매도 비율이 이상이면 충돌
        dd_discount_floor: float = 0.50,     # 드로다운 매수 최소 배율
        mdd_full_discount: float = 0.15,     # 이 MDD에서 dd_discount_floor 적용
    ):
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold
        self.min_score_threshold = min_score_threshold
        self.conflict_ratio = conflict_ratio
        self.dd_discount_floor = dd_discount_floor
        self.mdd_full_discount = mdd_full_discount

        self._signals: list[tuple[SignalBase, float]] = []
        self._regime_filter: Optional[callable] = None
        # 신뢰도 이력: {strategy_name: [hit (1) or miss (0), ...]}
        self._accuracy_history: dict[str, list[int]] = {}

    def add(self, signal: SignalBase, weight: float = 1.0) -> "RobustFusion":
        self._signals.append((signal, weight))
        return self

    def set_regime_filter(self, fn: callable) -> "RobustFusion":
        self._regime_filter = fn
        return self

    def record_outcome(self, strategy_name: str, was_correct: bool) -> None:
        """체결 후 신호 정확도 기록 (선택적 온라인 학습)."""
        hist = self._accuracy_history.setdefault(strategy_name, [])
        hist.append(1 if was_correct else 0)
        if len(hist) > 100:
            hist.pop(0)

    def _confidence_weight(self, strategy_name: str, base_weight: float) -> float:
        """과거 정확도로 가중치 보정. 데이터 없으면 base_weight 그대로."""
        hist = self._accuracy_history.get(strategy_name, [])
        if len(hist) < 10:
            return base_weight
        acc = np.mean(hist[-50:])  # 최근 50회 정확도
        # 정확도 50%(랜덤)~80%(우수) → 0.5×~1.5× 보정
        boost = np.clip((acc - 0.5) * 2.0, -0.5, 0.5)
        return base_weight * (1.0 + boost)

    def _normalize_strengths(
        self, outputs: list[tuple[str, SignalOutput, float]]
    ) -> list[tuple[str, float, float]]:
        """
        각 신호 strength를 z-score 정규화.
        Returns: [(name, normalized_strength, weight), ...]
        """
        strengths = np.array([abs(o.strength) for _, o, _ in outputs])
        direction = lambda out: (1 if out.signal >= 0 else -1)
        if len(strengths) < 2 or strengths.std() == 0:
            return [(n, o.strength * direction(o), w) for n, o, w in outputs]
        z = (strengths - strengths.mean()) / (strengths.std() + 1e-9)
        # z를 0~1 범위로 클리핑
        normalized = np.clip((z + 2) / 4, 0.0, 1.0)
        return [
            (name, float(normalized[i]) * (1 if out.signal >= 0 else -1), weight)
            for i, (name, out, weight) in enumerate(outputs)
        ]

    def evaluate(
        self,
        df: pd.DataFrame,
        symbol: str = "",
        equity_curve: Optional[pd.Series] = None,
    ) -> "FusionResult":
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

        # 각 신호 계산
        raw_outputs: list[tuple[str, SignalOutput, float]] = []
        for sig, base_w in self._signals:
            try:
                out = sig.compute(df, symbol=symbol)
                adj_w = self._confidence_weight(sig.name(), base_w)
                raw_outputs.append((sig.name(), out, adj_w))
            except Exception as e:
                logger.warning("Signal %s failed for %s: %s", sig.name(), symbol, e)

        if not raw_outputs:
            return FusionResult(symbol=symbol, signal=0, score=0.0, strength=0.0)

        # 정규화 강도
        normalized = self._normalize_strengths(raw_outputs)

        total_weight = sum(w for _, _, w in normalized)
        if total_weight == 0:
            return FusionResult(symbol=symbol, signal=0, score=0.0, strength=0.0)

        bull_score = sum(ns * w for _, ns, w in normalized if ns > 0) / total_weight
        bear_score = sum(abs(ns) * w for _, ns, w in normalized if ns < 0) / total_weight
        weighted_score = (bull_score - bear_score)

        individual = {name: out for name, out, _ in raw_outputs}

        # ── 충돌 해소 ────────────────────────────────────────────────
        if bull_score > 0 and bear_score > 0:
            ratio = min(bull_score, bear_score) / max(bull_score, bear_score)
            if ratio > self.conflict_ratio:
                logger.debug("신호 충돌 %s (bull=%.3f bear=%.3f) → 중립", symbol, bull_score, bear_score)
                return FusionResult(symbol=symbol, signal=0,
                                    score=round(weighted_score, 4),
                                    strength=0.0, individual=individual,
                                    meta={"conflict": True})

        # ── 약신호 억제 ──────────────────────────────────────────────
        if abs(weighted_score) < self.min_score_threshold:
            return FusionResult(symbol=symbol, signal=0,
                                score=round(weighted_score, 4),
                                strength=round(abs(weighted_score), 4),
                                individual=individual,
                                meta={"suppressed": True})

        # ── 드로다운 매수 디스카운트 ─────────────────────────────────
        dd_scale = 1.0
        if equity_curve is not None and len(equity_curve) >= 2:
            peak = equity_curve.cummax().iloc[-1]
            curr = equity_curve.iloc[-1]
            current_dd = (curr - peak) / peak if peak > 0 else 0.0
            dd_ratio = min(abs(current_dd) / self.mdd_full_discount, 1.0)
            dd_scale = 1.0 - dd_ratio * (1.0 - self.dd_discount_floor)

        # ── 레짐 차단 ────────────────────────────────────────────────
        if regime_blocked and weighted_score > 0:
            return FusionResult(symbol=symbol, signal=0,
                                score=round(weighted_score, 4),
                                strength=round(abs(weighted_score) * dd_scale, 4),
                                individual=individual,
                                regime_blocked=True)

        # ── 최종 신호 결정 ───────────────────────────────────────────
        effective_score = weighted_score
        if weighted_score > 0:
            effective_score *= dd_scale

        if effective_score >= self.buy_threshold:
            final_signal = 1
        elif effective_score <= self.sell_threshold:
            final_signal = -1
        else:
            final_signal = 0

        return FusionResult(
            symbol=symbol,
            signal=final_signal,
            score=round(effective_score, 4),
            strength=round(abs(effective_score), 4),
            individual=individual,
            meta={"dd_scale": round(dd_scale, 4), "bull": round(bull_score, 4),
                  "bear": round(bear_score, 4)},
        )

    def scan(self, dfs: dict[str, pd.DataFrame],
             equity_curves: Optional[dict[str, pd.Series]] = None) -> list["FusionResult"]:
        results = []
        for symbol, df in dfs.items():
            eq = equity_curves.get(symbol) if equity_curves else None
            try:
                results.append(self.evaluate(df, symbol=symbol, equity_curve=eq))
            except Exception as e:
                logger.warning("RobustFusion scan failed for %s: %s", symbol, e)
        results.sort(key=lambda r: r.score, reverse=True)
        return results


def default_robust_fusion() -> RobustFusion:
    """
    KIS 한국 ETF 3종목 기본 Fusion.
    추세추종 40% + 모멘텀 40% + 변동성돌파 20%.
    """
    from backend.quant.signals.trend_following import TrendFollowingSignal
    from backend.quant.signals.momentum import MomentumSignal
    from backend.quant.signals.volatility_breakout import VolatilityBreakoutSignal

    def regime_ok(df):
        if len(df) < 200:
            return True
        close = df["Close"]
        sma200 = close.rolling(200).mean().iloc[-1]
        return float(close.iloc[-1]) > float(sma200)

    fusion = RobustFusion(
        buy_threshold=0.28,
        sell_threshold=-0.20,
        min_score_threshold=0.10,
        conflict_ratio=0.70,
    )
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
