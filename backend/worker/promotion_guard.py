"""
실전 매매 전환 체크리스트.

KIS_ENV=real + ENABLE_LIVE_TRADING=true 로 전환하기 전에
모든 항목이 통과해야 한다. StartupRecovery._step_enable_trading()에서 호출.
"""
import logging
import os
from datetime import datetime, timedelta
from typing import Callable

logger = logging.getLogger(__name__)


class LivePromotionGuard:
    """
    실전 전환 전 필수 체크리스트.

    미달 항목이 있으면 (False, [failed_items]) 반환.
    SAFE_MODE를 enable하지 않고 Worker를 SafeMode로 유지.
    """

    def __init__(self, db_factory: Callable, redis_client=None):
        self._factory = db_factory
        self._redis = redis_client

    def check(self) -> tuple[bool, list[str]]:
        checks: list[tuple[str, Callable[[], bool]]] = [
            ("KIS_ENV=real", self._check_kis_env),
            ("ENABLE_LIVE_TRADING=true", self._check_live_flag),
            ("Telegram 설정됨", self._check_telegram),
            ("DB 연결 가능", self._check_db),
            ("Redis 연결 가능", self._check_redis),
            ("4주 모의투자 완료", self._check_paper_run),
        ]
        failed = []
        for name, fn in checks:
            try:
                if not fn():
                    failed.append(name)
                    logger.warning("실전 전환 체크 실패: %s", name)
            except Exception as e:
                failed.append(f"{name} (오류: {e})")
                logger.warning("실전 전환 체크 예외 %s: %s", name, e)

        if failed:
            logger.error("실전 전환 불가 — 미달 항목: %s", failed)
        else:
            logger.info("실전 전환 체크리스트 전항목 통과")
        return len(failed) == 0, failed

    def _check_kis_env(self) -> bool:
        return os.environ.get("KIS_ENV") == "real"

    def _check_live_flag(self) -> bool:
        return os.environ.get("ENABLE_LIVE_TRADING", "false").lower() == "true"

    def _check_telegram(self) -> bool:
        token = os.environ.get("TELEGRAM_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        return (bool(token) and not token.startswith("여기에") and
                bool(chat_id) and not chat_id.startswith("여기에"))

    def _check_db(self) -> bool:
        try:
            from sqlalchemy import text
            db = self._factory()
            db.execute(text("SELECT 1"))
            db.close()
            return True
        except Exception:
            return False

    def _check_redis(self) -> bool:
        if self._redis is None:
            return False
        try:
            self._redis.ping()
            return True
        except Exception:
            return False

    def _check_paper_run(self) -> bool:
        """최소 28일(4주) 동안 모의투자 전략이 실행됐는지 확인."""
        try:
            from backend.database.models import StrategyRun
            db = self._factory()
            cutoff = datetime.utcnow() - timedelta(days=28)
            count = (db.query(StrategyRun)
                     .filter(StrategyRun.started_at <= cutoff)
                     .count())
            db.close()
            if count == 0:
                logger.warning("4주 모의투자 미완료: strategy_runs에 28일 이상 된 실행 없음")
                return False
            return True
        except Exception as e:
            logger.warning("모의투자 기간 확인 실패: %s", e)
            return False
