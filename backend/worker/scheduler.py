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
    from backend.database.models import init_db_factory
    db_url = os.environ.get("DB_URL", "postgresql://quantdinger:quantdinger@postgres:5432/quantdinger")
    factory = init_db_factory(db_url)
    return factory()


def _save_equity_snapshot():
    """자산 스냅샷을 DB에 저장 + Telegram 일일 결산 발송."""
    db = None
    try:
        from backend.brokers.kis import get_kis_broker
        from backend.database.models import EquitySnapshot, DailyRiskState
        from datetime import date
        broker = get_kis_broker()
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

        # Collect daily risk state for Telegram summary
        risk_row = db.get(DailyRiskState, date.today())
        daily_pnl_pct = 0.0
        kill_switch = False
        kill_reason = ""
        if risk_row and risk_row.peak_equity > 0:
            daily_pnl_pct = risk_row.daily_pnl / risk_row.peak_equity * 100
            kill_switch = risk_row.kill_switch
            kill_reason = risk_row.kill_reason or ""

        try:
            from bot.notifier import alert_daily_summary
            alert_daily_summary({
                "total_equity": bal.total_eval_krw,
                "daily_pnl_pct": daily_pnl_pct,
                "position_count": len(broker.get_positions()),
                "kill_switch": kill_switch,
                "kill_reason": kill_reason,
            })
        except Exception as e:
            logger.warning("Telegram 일일 결산 알림 실패: %s", e)

    except Exception as e:
        logger.warning("자산 스냅샷 실패: %s", e)
    finally:
        if db is not None:
            db.close()


def _reset_daily_risk():
    """일일 리스크 카운터 Redis + DB 초기화."""
    try:
        import redis as _redis
        r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        r.delete("risk:daily_loss_pct", "risk:trading_halted")
        # Also purge today's PnL key so PersistentLossTracker starts fresh
        from datetime import date
        r.delete(f"risk:daily_pnl:{date.today().isoformat()}")
        logger.info("일일 리스크 카운터 리셋 (Redis)")
    except Exception as e:
        logger.warning("리스크 Redis 리셋 실패: %s", e)

    db = None
    try:
        from datetime import date
        from backend.database.models import DailyRiskState
        db = _get_db()
        row = db.get(DailyRiskState, date.today())
        if row:
            row.daily_pnl = 0.0
            # kill_switch intentionally NOT cleared — requires manual operator reset
            db.commit()
        logger.info("일일 리스크 카운터 리셋 (DB)")
    except Exception as e:
        logger.warning("리스크 DB 리셋 실패: %s", e)
    finally:
        if db is not None:
            db.close()

    # Re-arm SAFE_MODE for the new day — skip if kill-switch fired yesterday
    try:
        from datetime import date, timedelta
        from backend.database.models import DailyRiskState
        from backend.worker.recovery import SAFE_MODE
        kill_active = False
        db_check = None
        try:
            yesterday = date.today() - timedelta(days=1)
            db_check = _get_db()
            prev_row = db_check.get(DailyRiskState, yesterday)
            if prev_row and prev_row.kill_switch:
                kill_active = True
        except Exception:
            pass
        finally:
            if db_check is not None:
                db_check.close()
        if kill_active:
            logger.warning("어제 킬스위치 활성 — SAFE_MODE 재활성화 차단. 수동 해제 필요.")
        elif not SAFE_MODE.can_trade:
            SAFE_MODE.enable()
            logger.info("일일 리셋 후 SAFE_MODE 재활성화")
    except Exception as e:
        logger.warning("SAFE_MODE 재활성화 실패: %s", e)


def _publish_session_signal(channel: str) -> None:
    """Redis Pub/Sub 세션 신호 발행 + DB fallback.

    DB에 먼저 기록해 Redis 장애 중에도 Worker의 DB 폴링이 처리할 수 있도록 한다.
    """
    import json
    import redis as _redis
    payload = json.dumps({"ts": datetime.utcnow().isoformat()})
    db = None
    try:
        from backend.database.models import Command
        db = _get_db()
        db.add(Command(channel=channel, payload=payload))
        db.commit()
    except Exception as e:
        logger.warning("세션 신호 DB 기록 실패 [%s]: %s", channel, e)
    finally:
        if db is not None:
            db.close()
    try:
        r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        r.publish(channel, payload)
        logger.info("세션 신호 발행: %s", channel)
    except Exception as e:
        logger.warning("Redis 세션 신호 실패 (DB 폴링으로 처리): %s", e)


def _trigger_kr_session():
    """한국 세션 신호 — Redis Pub/Sub + DB fallback."""
    _publish_session_signal("session:kr_open")


def _trigger_us_session():
    """미국 세션 신호 — Redis Pub/Sub + DB fallback."""
    _publish_session_signal("session:us_open")


def _periodic_reconcile():
    """30분 주기 포지션·주문 조정 — 장중 브로커 desync 감지."""
    db_url = os.environ.get("DB_URL", "postgresql://quantdinger:quantdinger@postgres:5432/quantdinger")
    try:
        from backend.execution.reconciler import PositionReconciler
        from backend.brokers.kis import get_kis_broker
        from backend.database.models import init_db_factory
        import redis as _redis
        r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        result = PositionReconciler(
            broker=get_kis_broker(),
            db_factory=init_db_factory(db_url),
            redis_client=r,
        ).reconcile("periodic")
        logger.info("주기 조정 완료: 갭=%d 수정=%d", len(result.gaps), len(result.repairs))
    except Exception as e:
        logger.warning("주기 조정 실패: %s", e)


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

    # 30분 주기 포지션·주문 조정 — 한국 장중 09:05~15:30, 미국 장중 22:35~06:00 KST
    scheduler.add_job(
        _periodic_reconcile,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15,22-23",
            minute="*/30",
            timezone="Asia/Seoul",
        ),
        id="periodic_reconcile",
        name="주기 포지션 조정",
        max_instances=1,
        coalesce=True,
    )

    return scheduler
