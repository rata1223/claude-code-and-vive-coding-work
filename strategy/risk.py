import os
import json
import logging
import redis
from pathlib import Path

logger = logging.getLogger(__name__)

DAILY_LOSS_KEY = "risk:daily_loss_pct"
TRADING_HALTED_KEY = "risk:trading_halted"
PEAK_EQUITY_KEY = "risk:peak_equity"

DAILY_LOSS_LIMIT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", 0.03))
MDD_LIMIT = float(os.environ.get("MDD_LIMIT_PCT", 0.15))

# Bound every Redis call so a slow/unreachable Redis fails fast instead of
# hanging a caller (e.g. the quick-trade pre-submit risk gate, which relies on
# a *prompt* exception to fail closed) or the trading bot. Finite by default.
_REDIS_TIMEOUT = float(os.environ.get("REDIS_SOCKET_TIMEOUT", "2"))

# Redis 재시작에도 peak equity 유지하는 파일 경로
_DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
_PEAK_FILE = _DATA_DIR / "peak_equity.json"


def _load_peak_from_file() -> float | None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        if _PEAK_FILE.exists():
            return float(json.loads(_PEAK_FILE.read_text())["peak"])
    except Exception as e:
        logger.warning("peak_equity 파일 로드 실패: %s", e)
    return None


def _save_peak_to_file(peak: float):
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _PEAK_FILE.write_text(json.dumps({"peak": peak}))
    except Exception as e:
        logger.warning("peak_equity 파일 저장 실패: %s", e)


class RiskManager:
    def __init__(self):
        self._redis = redis.from_url(
            os.environ.get("REDIS_URL", "redis://redis:6379"),
            socket_timeout=_REDIS_TIMEOUT,
            socket_connect_timeout=_REDIS_TIMEOUT,
        )
        # 부팅 시 파일 → Redis 복원
        peak = _load_peak_from_file()
        if peak and not self._redis.exists(PEAK_EQUITY_KEY):
            self._redis.set(PEAK_EQUITY_KEY, peak)
            logger.info("peak_equity 복원: %s", peak)

    def is_trading_halted(self) -> bool:
        return self._redis.exists(TRADING_HALTED_KEY) > 0

    def record_daily_loss(self, loss_pct: float) -> bool:
        """손실 누적 후 한도 초과 여부 반환. True면 당일 매매 중단."""
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
        peak_raw = self._redis.get(PEAK_EQUITY_KEY)

        if peak_raw is None:
            self._redis.set(PEAK_EQUITY_KEY, current_equity)
            _save_peak_to_file(current_equity)
            return False

        peak = float(peak_raw)
        if current_equity > peak:
            self._redis.set(PEAK_EQUITY_KEY, current_equity)
            _save_peak_to_file(current_equity)
            return False

        drawdown = (peak - current_equity) / peak
        logger.info("현재 MDD: %.2f%% (peak=%.0f, current=%.0f)", drawdown * 100, peak, current_equity)
        if drawdown >= MDD_LIMIT:
            logger.error("MDD 한도 초과 (%.2f%%) — 전량 현금화 필요", drawdown * 100)
            return True
        return False

    def is_stop_loss(self, entry_price: float, current_price: float) -> bool:
        stop_pct = float(os.environ.get("STOP_LOSS_PCT", 0.07))
        return current_price <= entry_price * (1 - stop_pct)

    def enforce_stop_losses(self, positions: list[dict], get_price_fn) -> list[dict]:
        """
        보유 포지션 전체에 stop-loss 적용.
        positions: [{"symbol": str, "entry_price": float, "qty": int, "market": "US"|"KR"}]
        get_price_fn: symbol → current_price
        반환: 손절해야 할 포지션 목록
        """
        to_sell = []
        for pos in positions:
            symbol = pos["symbol"]
            try:
                current = get_price_fn(symbol)
                if self.is_stop_loss(pos["entry_price"], current):
                    pct = (current - pos["entry_price"]) / pos["entry_price"] * 100
                    logger.warning("손절 대상: %s (%.2f%%)", symbol, pct)
                    to_sell.append({**pos, "current_price": current, "reason": f"손절 {pct:.1f}%"})
            except Exception as e:
                logger.warning("손절 체크 실패 %s: %s", symbol, e)
        return to_sell
