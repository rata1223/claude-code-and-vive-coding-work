"""
운영 스케줄러 — APScheduler 기반.
bot/scheduler.py의 기존 스케줄을 backend 계층으로 통합.
"""
import logging
import os
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def _get_db():
    from backend.database.models import init_db
    db_url = os.environ.get("DB_URL", "postgresql://quantdinger:quantdinger@postgres:5432/quantdinger")
    return init_db(db_url)


def _save_equity_snapshot():
    """자산 스냅샷을 DB에 저장 (일 1회 결산용)."""
    try:
        from backend.brokers.kis import KISBroker
        from backend.database.models import EquitySnapshot
        broker = KISBroker()
        bal = broker.get_balance()
        db = _get_db()
        snap = EquitySnapshot(
            total_krw=bal.total_eval_krw,
            cash_krw=bal.cash_krw,
            cash_usd=bal.cash_usd,
        )
        db.add(snap)
        db.commit()
        logger.info("자산 스냅샷 저장: %.0f원", bal.total_eval_krw)
    except Exception as e:
        logger.warning("자산 스냅샷 실패: %s", e)


def _reset_daily_risk():
    """일일 리스크 카운터 Redis 초기화."""
    try:
        import redis as _redis
        r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        r.delete("risk:daily_loss_pct", "risk:trading_halted")
        logger.info("일일 리스크 카운터 리셋")
    except Exception as e:
        logger.warning("리스크 리셋 실패: %s", e)


def _trigger_kr_session():
    """한국 세션 신호 — Redis를 통해 Worker에 전달."""
    try:
        import json
        import redis as _redis
        r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        r.publish("session:kr_open", json.dumps({"ts": datetime.utcnow().isoformat()}))
        logger.info("한국 세션 신호 발행")
    except Exception as e:
        logger.warning("한국 세션 신호 실패: %s", e)


def _trigger_us_session():
    """미국 세션 신호 — Redis를 통해 Worker에 전달."""
    try:
        import json
        import redis as _redis
        r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        r.publish("session:us_open", json.dumps({"ts": datetime.utcnow().isoformat()}))
        logger.info("미국 세션 신호 발행")
    except Exception as e:
        logger.warning("미국 세션 신호 실패: %s", e)


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    # 한국 시장 09:05 KST
    scheduler.add_job(
        _trigger_kr_session,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=5, timezone="Asia/Seoul"),
        id="kr_session", name="한국주식 매매",
    )

    # 미국 시장 22:35 KST
    scheduler.add_job(
        _trigger_us_session,
        CronTrigger(day_of_week="mon-fri", hour=22, minute=35, timezone="Asia/Seoul"),
        id="us_session", name="미국주식 매매",
    )

    # 일일 리스크 카운터 리셋 00:01 KST
    scheduler.add_job(
        _reset_daily_risk,
        CronTrigger(hour=0, minute=1, timezone="Asia/Seoul"),
        id="risk_reset", name="리스크 카운터 리셋",
    )

    # 자산 스냅샷 23:50 KST
    scheduler.add_job(
        _save_equity_snapshot,
        CronTrigger(hour=23, minute=50, timezone="Asia/Seoul"),
        id="equity_snapshot", name="자산 스냅샷",
    )

    return scheduler
