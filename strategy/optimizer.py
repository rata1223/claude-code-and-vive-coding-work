import logging
import numpy as np
import pandas as pd
import yfinance as yf
from pypfopt import EfficientFrontier, risk_models, expected_returns

logger = logging.getLogger(__name__)

MAX_POSITION_PCT = 0.05  # 종목당 최대 5%


class PortfolioOptimizer:
    def compute_weights(self, symbols: list[str], total_capital: float) -> dict[str, float]:
        if not symbols:
            return {}

        try:
            prices = yf.download(symbols, period="1y", auto_adjust=True)["Close"]
            prices = prices.dropna(axis=1, how="any")
            if prices.empty or len(prices.columns) < 2:
                return self._equal_weight(list(prices.columns), total_capital)

            mu = expected_returns.mean_historical_return(prices)
            S = risk_models.sample_cov(prices)

            ef = EfficientFrontier(mu, S, weight_bounds=(0, MAX_POSITION_PCT))
            ef.max_sharpe()
            weights = ef.clean_weights()

            result = {}
            for sym, w in weights.items():
                if w > 0:
                    result[sym] = round(w * total_capital, 0)

            logger.info("Optimized weights: %s", result)
            return result

        except Exception as e:
            logger.warning("Optimization failed (%s), falling back to equal weight", e)
            return self._equal_weight(symbols, total_capital)

    def _equal_weight(self, symbols: list[str], total_capital: float) -> dict[str, float]:
        w = min(MAX_POSITION_PCT, 1.0 / max(len(symbols), 1))
        amount = round(w * total_capital, 0)
        return {s: amount for s in symbols}
