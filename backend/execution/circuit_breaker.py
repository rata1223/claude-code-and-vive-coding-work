"""
연속 주문 실패 차단기.

N회 연속 실패 → 트립 → cooldown 후 자동 복구.
IndicatorStrategy._execute_buy()/_execute_sell()에서 사용.
"""
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class ConsecutiveFailureBreaker:
    """N회 연속 주문 실패 시 일시 거래 차단."""

    def __init__(self, threshold: int = 3, cooldown_minutes: int = 30):
        self._threshold = threshold
        self._cooldown = cooldown_minutes * 60
        self._failures = 0
        self._tripped_at: Optional[float] = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold and self._tripped_at is None:
            self._tripped_at = time.monotonic()
            logger.error(
                "차단기 트립: %d회 연속 실패 — %.0f분 냉각 시작",
                self._failures, self._cooldown / 60,
            )

    def record_success(self) -> None:
        if self._failures > 0:
            logger.info("차단기 리셋: 연속 실패 카운터 초기화")
        self._failures = 0
        self._tripped_at = None

    def is_open(self) -> bool:
        """True = 차단기 작동 중 → 거래 차단."""
        if self._tripped_at is None:
            return False
        elapsed = time.monotonic() - self._tripped_at
        if elapsed >= self._cooldown:
            logger.info("차단기 복구: 냉각 완료 (%.0f분)", elapsed / 60)
            self._tripped_at = None
            self._failures = 0
            return False
        remaining = (self._cooldown - elapsed) / 60
        logger.warning("차단기 차단 중: 잔여 %.1f분", remaining)
        return True
