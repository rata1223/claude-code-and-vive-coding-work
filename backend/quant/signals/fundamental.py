"""
펀더멘털 필터 — yfinance.info 기반 (무료 공개 데이터).
신호 강도 승수를 0.3~1.5 범위로 반환.
"""
import logging

logger = logging.getLogger(__name__)

# 승수 범위 상수
WEIGHT_MIN = 0.3
WEIGHT_MAX = 1.5
WEIGHT_DEFAULT = 1.0


def fundamental_weight(symbol: str, *, timeout: int = 10) -> float:
    """
    종목 펀더멘털 품질 승수 반환 (0.3 ~ 1.5).

    평가 항목:
    - ROE >= 15%  → +0.2
    - 부채비율 < 0.5  → +0.2
    - 영업이익률 > 10%  → +0.1
    - PER 5~25 구간  → +0.2 (고평가·적자 제외)

    취득 실패 시 1.0(중립) 반환.
    """
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        score = 1.0

        roe = info.get("returnOnEquity")
        if roe is not None and roe >= 0.15:
            score += 0.2

        debt_ratio = info.get("debtToEquity")
        if debt_ratio is not None and 0 < debt_ratio < 50:  # yf: %단위
            score += 0.2

        op_margin = info.get("operatingMargins")
        if op_margin is not None and op_margin > 0.10:
            score += 0.1

        per = info.get("trailingPE")
        if per is not None and 5 < per < 25:
            score += 0.2

        result = float(max(WEIGHT_MIN, min(WEIGHT_MAX, score)))
        logger.debug("펀더멘털 승수 %s: %.2f (raw score=%.2f)", symbol, result, score)
        return result

    except Exception as e:
        logger.warning("펀더멘털 데이터 취득 실패 %s: %s — 중립 반환", symbol, e)
        return WEIGHT_DEFAULT


class FundamentalFilter:
    """
    캐시 내장 펀더멘털 필터.

    사용 예:
        ff = FundamentalFilter(cache_ttl_hours=24)
        weight = ff.get_weight("AAPL")
        adjusted_score = raw_score * weight
    """

    def __init__(self, cache_ttl_hours: float = 24.0):
        self._cache: dict[str, tuple[float, float]] = {}  # {symbol: (weight, timestamp)}
        self._ttl_seconds = cache_ttl_hours * 3600

    def get_weight(self, symbol: str) -> float:
        """캐시된 승수 반환. 만료 시 재취득."""
        import time
        now = time.time()
        if symbol in self._cache:
            weight, ts = self._cache[symbol]
            if now - ts < self._ttl_seconds:
                return weight

        weight = fundamental_weight(symbol)
        self._cache[symbol] = (weight, now)
        return weight

    def clear_cache(self) -> None:
        self._cache.clear()

    def is_fundamentally_sound(self, symbol: str, min_weight: float = 0.8) -> bool:
        """승수가 min_weight 이상인지 여부 (진입 필터용)."""
        return self.get_weight(symbol) >= min_weight
