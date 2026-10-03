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

#: 실전 전환 전에 모의투자로 채워야 하는 연속 실행 기간.
PAPER_RUN_MIN = timedelta(days=28)


def paper_run_qualifies(run, now: datetime) -> bool:
    """이 실행이 28일 동안 중지되지 않았는가.

    워커 재시작은 실행을 멈추지 않는다(``is_active`` 유지, 기동 시 복원) — 그래서
    워커가 내려가 있던 시간은 여기서 구분하지 못한다. 실행의 환경(모의/실전)과
    실제 매매 여부도 보지 않는다(``strategy_runs``에 그 기록이 없다).

    - 아직 활성(``is_active``)이고 워커가 종료를 기록하지 않았으면(``stopped_at``
      없음) 시작부터 지금까지.
    - 워커가 종료를 기록했으면(``stopped_at``) 시작부터 종료까지.
    - 중지 요청은 됐는데(``is_active=False``) 종료 기록이 없는 행은 언제 멈췄는지
      알 수 없으므로 불통과(fail-closed).
    """
    started = getattr(run, "started_at", None)
    if started is None:
        return False
    stopped = getattr(run, "stopped_at", None)
    if stopped is not None:
        return stopped - started >= PAPER_RUN_MIN
    if getattr(run, "is_active", False):
        return now - started >= PAPER_RUN_MIN
    return False


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
        """최소 28일(4주) 동안 중지되지 않은 전략 실행이 있는지 확인.

        예전에는 ``started_at <= now-28d`` 인 행이 **있기만** 하면 통과했다 — 28일
        전에 시작해 1분 뒤 중지한 실행도. 이제 그 시작 시각부터 28일을 실제로
        채운 실행만 센다(:func:`paper_run_qualifies`).
        """
        try:
            from backend.database.models import StrategyRun
            db = self._factory()
            try:
                now = datetime.utcnow()
                cutoff = now - PAPER_RUN_MIN
                candidates = (db.query(StrategyRun)
                              .filter(StrategyRun.started_at <= cutoff)
                              .all())
                qualified = [r.id for r in candidates if paper_run_qualifies(r, now)]
            finally:
                db.close()
            if not qualified:
                logger.warning("4주 모의투자 미완료: 28일 동안 중지되지 않은 실행 없음 "
                               "(28일 전에 시작한 행 %d개 — 중지됐거나 종료 확인 전)",
                               len(candidates))
                return False
            logger.info("4주 모의투자 확인: run_id=%s", qualified)
            return True
        except Exception as e:
            logger.warning("모의투자 기간 확인 실패: %s", e)
            return False
