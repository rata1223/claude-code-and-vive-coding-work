import os
import logging
import redis

logger = logging.getLogger(__name__)

DAILY_LOSS_KEY = "risk:daily_loss_pct"
TRADING_HALTED_KEY = "risk:trading_halted"
PEAK_EQUITY_KEY = "risk:peak_equity"

DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", 0.03))
MDD_LIMIT = float(os.environ.get("MDD_LIMIT_PCT", 0.15))


class RiskManager:
    def __init__(self):
        self._redis = redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))

    def is_trading_halted(self) -> bool:
        return self._redis.exists(TRADING_HALTED_KEY) > 0

    def record_daily_loss(self, loss_pct: float) -> bool:
        """손실 기록 후 한도 초과 여부 반환. True면 당일 매매 중단."""
        current = float(self._redis.get(DAILY_LOSS_KEY) or 0)
        new_total = current + abs(loss_pct)
        self._redis.set(DAILY_LOSS_KEY, new_total, ex=86400)

        if new_total >= DAILY_LOSS_LIMIT:
            self._redis.set(TRADING_HALTED_KEY, "1", ex=86400)
            logger.warning("일일 손실 한도 초과 (%.2f%%) — 당일 매매 중단", new_total * 100)
            return True
        return False

    def reset_daily_counters(self):
        self._redis.delete(DAILY_LOSS_KEY, TRADING_HALTED_KEY)
        logger.info("일일 손실 카운터 리셋")

    def check_mdd(self, current_equity: float) -> bool:
        """MDD 한도 초과 시 True 반환 (전량 현금화 필요)."""
        peak = self._redis.get(PEAK_EQUITY_KEY)
        if peak is None:
            self._redis.set(PEAK_EQUITY_KEY, current_equity)
            return False

        peak = float(peak)
        if current_equity > peak:
            self._redis.set(PEAK_EQUITY_KEY, current_equity)
            return False

        drawdown = (peak - current_equity) / peak
        if drawdown >= MDD_LIMIT:
            logger.error("MDD 한도 초과 (%.2f%%) — 전량 현금화 필요", drawdown * 100)
            return True
        return False

    def is_stop_loss(self, entry_price: float, current_price: float) -> bool:
        stop_pct = float(os.environ.get("STOP_LOSS_PCT", 0.07))
        return current_price <= entry_price * (1 - stop_pct)
