import logging
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)


def build_scheduler(trading_engine) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone="Asia/Seoul")

    # 한국 시장 — 평일 09:05 KST
    scheduler.add_job(
        trading_engine.run_kr_session,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=5, timezone="Asia/Seoul"),
        id="kr_session",
        name="한국주식 매매",
    )

    # 미국 시장 — 평일 22:35 KST (미국 동부 09:35 기준)
    scheduler.add_job(
        trading_engine.run_us_session,
        CronTrigger(day_of_week="mon-fri", hour=22, minute=35, timezone="Asia/Seoul"),
        id="us_session",
        name="미국주식 매매",
    )

    # 월 1회 리밸런싱 — 매월 첫째 평일 08:00 KST
    scheduler.add_job(
        trading_engine.run_rebalance,
        CronTrigger(day="1", hour=8, minute=0, timezone="Asia/Seoul"),
        id="rebalance",
        name="월간 리밸런싱",
    )

    # 일일 손실 카운터 리셋 — 매일 00:01 KST
    scheduler.add_job(
        trading_engine.reset_daily_risk,
        CronTrigger(hour=0, minute=1, timezone="Asia/Seoul"),
        id="risk_reset",
        name="리스크 카운터 리셋",
    )

    # 일일 결산 알림 — 매일 23:50 KST
    scheduler.add_job(
        trading_engine.send_daily_summary,
        CronTrigger(hour=23, minute=50, timezone="Asia/Seoul"),
        id="daily_summary",
        name="일일 결산",
    )

    return scheduler
