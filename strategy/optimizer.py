import logging
import yfinance as yf
from pypfopt import EfficientFrontier, risk_models, expected_returns

logger = logging.getLogger(__name__)

MAX_POSITION_PCT = 0.05


class PortfolioOptimizer:
    def compute_weights(self, symbols: list[str], total_capital: float) -> dict[str, float]:
        """
        샤프비율 최대화 기반 비중 계산.
        반환: {symbol: 투자금액(원)}
        """
        if not symbols:
            return {}

        try:
            prices = yf.download(symbols, period="1y", auto_adjust=True, progress=False)["Close"]
            if hasattr(prices, "columns"):
                prices = prices.dropna(axis=1, how="any")
            valid_symbols = list(prices.columns) if hasattr(prices, "columns") else symbols

            if len(valid_symbols) < 2:
                return self._equal_weight(valid_symbols or symbols, total_capital)

            mu = expected_returns.mean_historical_return(prices)
            S = risk_models.sample_cov(prices)

            ef = EfficientFrontier(mu, S, weight_bounds=(0, MAX_POSITION_PCT))
            ef.max_sharpe()
            weights = ef.clean_weights()

            result = {sym: round(w * total_capital) for sym, w in weights.items() if w > 0}
            logger.info("최적화 비중: %s", {k: f"{v:,.0f}원" for k, v in result.items()})
            return result

        except Exception as e:
            logger.warning("최적화 실패 (%s) → 균등 배분", e)
            return self._equal_weight(symbols, total_capital)

    def compute_atr_weights(self, symbols: list[str], total_capital: float,
                            signal_module) -> dict[str, float]:
        """
        ATR 기반 변동성 조정 포지션 사이징.
        signal_module: MultiTimeframeSignals 인스턴스
        """
        result = {}
        for symbol in symbols:
            try:
                df = signal_module._base.fetch_ohlcv(symbol)
                amount = signal_module.atr_position_size(df, total_capital)
                result[symbol] = round(amount)
            except Exception as e:
                logger.warning("ATR sizing failed for %s: %s", symbol, e)
                result[symbol] = round(total_capital * 0.02)

        logger.info("ATR 기반 비중: %s", {k: f"{v:,.0f}원" for k, v in result.items()})
        return result

    def _equal_weight(self, symbols: list[str], total_capital: float) -> dict[str, float]:
        w = min(MAX_POSITION_PCT, 1.0 / max(len(symbols), 1))
        amount = round(w * total_capital)
        return {s: amount for s in symbols}
