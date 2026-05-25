"""
포트폴리오 최적화 — PyPortfolioOpt 래퍼 + Risk Parity.
기존 strategy/optimizer.py의 PortfolioOptimizer를 확장.
"""
import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MAX_WEIGHT = 0.10   # 종목당 최대 10%
MIN_WEIGHT = 0.01


def max_sharpe_weights(prices: pd.DataFrame,
                       max_weight: float = MAX_WEIGHT) -> dict[str, float]:
    """샤프비율 최대화 포트폴리오 비중."""
    from pypfopt import EfficientFrontier, risk_models, expected_returns
    mu = expected_returns.mean_historical_return(prices)
    S = risk_models.sample_cov(prices)
    ef = EfficientFrontier(mu, S, weight_bounds=(MIN_WEIGHT, max_weight))
    ef.max_sharpe()
    return ef.clean_weights()


def min_volatility_weights(prices: pd.DataFrame,
                           max_weight: float = MAX_WEIGHT) -> dict[str, float]:
    """최소 변동성 포트폴리오."""
    from pypfopt import EfficientFrontier, risk_models, expected_returns
    mu = expected_returns.mean_historical_return(prices)
    S = risk_models.sample_cov(prices)
    ef = EfficientFrontier(mu, S, weight_bounds=(MIN_WEIGHT, max_weight))
    ef.min_volatility()
    return ef.clean_weights()


def risk_parity_weights(prices: pd.DataFrame) -> dict[str, float]:
    """
    Risk Parity: 각 자산이 포트폴리오 리스크에 동일 기여.
    PyPortfolioOpt CLA 또는 수동 계산.
    """
    try:
        from pypfopt import EfficientFrontier, risk_models
        from pypfopt.objective_functions import ex_ante_tracking_error
        S = risk_models.sample_cov(prices)
        n = len(prices.columns)
        # 단순 역변동성 (Risk Parity 근사)
        vols = np.sqrt(np.diag(S.values))
        inv_vols = 1.0 / (vols + 1e-8)
        weights = inv_vols / inv_vols.sum()
        return {sym: float(w) for sym, w in zip(prices.columns, weights)}
    except Exception as e:
        logger.warning("Risk parity failed: %s — equal weight fallback", e)
        n = len(prices.columns)
        return {sym: 1.0 / n for sym in prices.columns}


def equal_weight(symbols: list[str]) -> dict[str, float]:
    n = len(symbols)
    return {s: 1.0 / n for s in symbols} if n > 0 else {}


class PortfolioAllocator:
    """
    신호 스캔 결과(FusionResult 리스트)를 받아 자본 배분 계산.
    기존 PortfolioOptimizer + 새 퀀트 엔진 통합.
    """

    def __init__(self, total_capital: float, method: str = "max_sharpe"):
        self.total_capital = total_capital
        self.method = method  # max_sharpe | min_vol | risk_parity | equal

    def allocate(self, buy_signals: list[str],
                 price_history: Optional[dict[str, pd.DataFrame]] = None) -> dict[str, float]:
        """
        buy_signals: 매수 대상 종목 리스트
        price_history: {symbol: OHLCV df} — None이면 equal weight
        반환: {symbol: 투자금액(원)}
        """
        if not buy_signals:
            return {}

        if len(buy_signals) == 1 or price_history is None or self.method == "equal":
            w = equal_weight(buy_signals)
        else:
            try:
                prices = pd.DataFrame({
                    sym: price_history[sym]["Close"]
                    for sym in buy_signals if sym in price_history
                }).dropna()
                if len(prices.columns) < 2 or len(prices) < 60:
                    w = equal_weight(buy_signals)
                elif self.method == "max_sharpe":
                    w = max_sharpe_weights(prices)
                elif self.method == "min_vol":
                    w = min_volatility_weights(prices)
                elif self.method == "risk_parity":
                    w = risk_parity_weights(prices)
                else:
                    w = equal_weight(buy_signals)
            except Exception as e:
                logger.warning("Portfolio optimization failed (%s) → equal weight", e)
                w = equal_weight(buy_signals)

        return {sym: round(weight * self.total_capital)
                for sym, weight in w.items() if weight > 0}
