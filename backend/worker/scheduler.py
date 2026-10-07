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

_DB_FACTORY = None


def _get_db_factory():
    global _DB_FACTORY
    if _DB_FACTORY is None:
        from backend.database.models import init_db_factory
        db_url = os.environ.get("DB_URL", "postgresql://quantdinger:quantdinger@postgres:5432/quantdinger")
        _DB_FACTORY = init_db_factory(db_url)
    return _DB_FACTORY


def _get_db():
    return _get_db_factory()()


def _closing_day_risk(day):
    """``(daily_pnl_pct, kill_switch, kill_reason)`` for the summary; Nones if unread.

    The halt is read on its own, not behind the peak check — a halted row with
    no peak read as "no halt" — and from every risk day a halt can be on, like
    ``/api/status`` and the tracker's restore (#216): the day's own halt first,
    else the newest.
    """
    from backend.database.models import DailyRiskState, risk_days_in_play
    db = None
    try:
        db = _get_db()
        row = db.get(DailyRiskState, day)
        pnl_pct = 0.0
        if row and row.peak_equity > 0:
            pnl_pct = row.daily_pnl / row.peak_equity * 100
        halted = [r for r in (db.get(DailyRiskState, k) for k in risk_days_in_play(db, day))
                  if r is not None and r.kill_switch]
        if not halted:
            return pnl_pct, False, ""
        shown = next((r for r in halted if r.trade_date == day), halted[0])
        return pnl_pct, True, shown.kill_reason or ""
    except Exception as e:
        logger.warning("일일 결산 리스크 상태 조회 실패: %s", e)
        return None, None, None
    finally:
        if db is not None:
            db.close()


def _save_equity_snapshot():
    """자산 스냅샷을 DB에 저장 + Telegram 일일 결산 발송.

    Runs at 06:50 KST: after the US close (05:00, winter 06:00) and before the
    07:00 risk-day boundary, so ``trading_day()`` is still the risk day that
    just closed — the Korean session plus the US session after it — and the
    summary covers all of it. At 23:50 it reported the Korean session and only
    the first ~80 minutes of the US one (#166, CLAUDE.md known issue 15).

    The day is fixed when the job starts — a slow broker call must not carry
    the summary past 07:00 onto the new, empty day — and the summary is sent
    even if the broker or the snapshot fails: the halt line is the part an
    operator most needs in the morning, and it needs only the database.
    """
    from backend.database.models import EquitySnapshot, trading_day
    day = trading_day()
    daily_pnl_pct, kill_switch, kill_reason = _closing_day_risk(day)

    total_equity = None
    position_count = None
    broker = None
    try:
        from backend.brokers.kis import get_kis_broker
        broker = get_kis_broker()
        bal = broker.get_balance()
        total_equity = bal.total_eval_krw
        db = _get_db()
        try:
            db.add(EquitySnapshot(
                total_krw=bal.total_eval_krw,
                cash_krw=bal.cash_krw,
                cash_usd=bal.cash_usd,
            ))
            db.commit()
        finally:
            db.close()
        logger.info("자산 스냅샷 저장: %.0f원", bal.total_eval_krw)
    except Exception as e:
        logger.warning("자산 스냅샷 실패: %s", e)
    if broker is not None:
        try:
            position_count = len(broker.get_positions())
        except Exception as e:
            logger.warning("일일 결산 포지션 조회 실패: %s", e)

    try:
        from bot.notifier import alert_daily_summary
        alert_daily_summary({
            "total_equity": total_equity,
            "daily_pnl_pct": daily_pnl_pct,
            "position_count": position_count,
            "kill_switch": kill_switch,
            "kill_reason": kill_reason,
            "risk_day": day.isoformat(),
        })
    except Exception as e:
        logger.warning("Telegram 일일 결산 알림 실패: %s", e)


def _reset_daily_risk():
    """일일 리스크 카운터 Redis + DB 초기화."""
    try:
        import redis as _redis
        r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        r.delete("risk:daily_loss_pct", "risk:trading_halted")
        logger.info("일일 리스크 카운터 리셋 (Redis)")
    except Exception as e:
        logger.warning("리스크 Redis 리셋 실패: %s", e)

    # `daily_pnl` is deliberately left alone here — it is not this job's to reset.
    #
    # `LossTracker.record_pnl()` already rolls the day over itself, on the risk
    # day (`trading_day()`, 07:00 KST since #166) — the same boundary as the row
    # key — and persists it, so the tracker owns that counter. This job runs at
    # 07:01, just after the risk day turns; zeroing anything here would race the
    # tracker's own rollover.
    #
    # It used to look harmless because it keyed by the UTC date and so touched
    # the *closed* day, missing both the live row and the live
    # `risk:daily_pnl:<KST>` key. Once issue #160 aligned the keys, the same
    # code wiped the live row *and* the live Redis key — and those two stores
    # are exactly what `PersistentLossTracker._restore_state()` reads, so a
    # restart after 06:01 came back with `daily_pnl = 0.0` and handed the
    # Korean session a fresh 3% loss budget on top of the overnight loss.
    #
    # Two writers with different day boundaries is the shape of issue #158; the
    # replacement here is the tracker's own rollover, which is already in place.

    # Re-arm SAFE_MODE for the new day — skip if a kill switch is still live
    try:
        from backend.database.models import DailyRiskState, risk_days_in_play
        from backend.worker.recovery import SAFE_MODE
        kill_active = False
        halted_row = None
        #: Only a lookup that actually completed can license re-enabling trading.
        checked = False
        db_check = None
        try:
            # Every risk day a halt can be on (`risk_days_in_play`): the day
            # that just began, the one that just ended, and any older day still
            # halted — newest first, so the carry below starts from the latest.
            # Reading one row once resumed trading over a live halt (with the old
            # Seoul-midnight key, a halt fired before midnight sat on the other
            # row), and reading two let a halt age out after two quiet days.
            #
            # Yesterday must also be KST-based: on the UTC date it landed a
            # further day back, normally an empty row, so even the pre-midnight
            # halt read as "no halt" (issue #160).
            db_check = _get_db()
            for key in risk_days_in_play(db_check):
                row = db_check.get(DailyRiskState, key)
                if row and row.kill_switch:
                    kill_active = True
                    halted_row = (key, row.kill_reason, row.peak_equity)
                    break
            checked = True
        except Exception as e:
            # Fail closed. Swallowing this left `kill_active` False and fell
            # through to SAFE_MODE.enable(), so a database outage re-opened
            # trading without anyone having checked the kill switch.
            logger.warning("킬스위치 조회 실패 — SAFE_MODE 재활성화 보류: %s", e)
        finally:
            if db_check is not None:
                db_check.close()
        if kill_active:
            logger.warning("킬스위치 활성 — SAFE_MODE 재활성화 차단. 수동 해제 필요.")
            _carry_halt_forward(*halted_row)
        elif checked and not SAFE_MODE.can_trade:
            SAFE_MODE.enable()
            logger.info("일일 리셋 후 SAFE_MODE 재활성화")
    except Exception as e:
        logger.warning("SAFE_MODE 재활성화 실패: %s", e)


def _carry_halt_forward(from_day, reason, peak_equity) -> None:
    """Put a live halt on the new risk day's row.

    A halt lasts until someone clears it, and readers find it on any still-
    halted row (``risk_days_in_play``). Keeping it on the newest row as well
    means today's row says what is in force — the tracker adopts it at its
    first write — rather than leaving that to the tracker's next fill or
    shutdown, which a halted worker over a weekend may never have.

    Today's row may already exist unhalted — a tracker write in the minute
    since 07:00 that had not yet adopted an outside halt creates it with its
    own ``False``. That ``False`` is nobody's clear: an operator's release
    (``api/routers/risk.py``) clears *every* halted in-play row in one
    transaction, so while the earlier row is still halted the halt is in force
    and today's row should say so. Only the flag is set; the equity columns of
    an existing row are left alone.

    The earlier row is re-read under its lock, in date order like every
    multi-day writer: a release that committed after this job's first read is
    seen here, and the halt is not put back over it (#158, #164).
    """
    from backend.database.models import lock_risk_row, lock_risk_rows, trading_day
    today = trading_day()
    if from_day >= today:
        # Already on today's row, or on a row dated ahead of it (the old
        # Seoul-midnight key on deploy day). Every reader sees that row as it
        # is; carrying from it would lock a later date before an earlier one,
        # against the order every multi-day writer keeps (#164).
        return
    db = None
    try:
        db = _get_db()
        source = lock_risk_rows(db, [from_day])
        if not source or not source[0].kill_switch:
            db.rollback()
            logger.info("킬스위치 이월 생략 — %s 행이 그 사이 해제됨", from_day)
            return
        row, is_new = lock_risk_row(db, today)
        carried = not row.kill_switch
        if carried:
            row.kill_switch = True
            row.kill_reason = source[0].kill_reason or reason
            if is_new:
                # The peak goes along: a new row with peak 0 has no MDD
                # baseline for `/api/metrics` and the 06:50 summary's PnL %.
                row.peak_equity = source[0].peak_equity or peak_equity or 0.0
        db.commit()
        if carried:
            logger.warning("킬스위치를 새 리스크 데이(%s)로 이월: %s", today, row.kill_reason)
    except Exception as e:
        logger.warning("킬스위치 이월 실패: %s", e)
        if db is not None:
            db.rollback()
    finally:
        if db is not None:
            db.close()


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
    """한국 세션 신호 — Redis Pub/Sub + DB fallback. 오늘이 KRX 휴장이면 생략."""
    try:
        from datetime import timezone as _tz
        from backend.data.calendar import get_calendar_service, Market as _Market
        _svc = get_calendar_service()
        _now = datetime.now(_tz.utc)
        if not _svc.is_trading_day(_Market.KRX, _svc.trade_date(_Market.KRX, _now)):
            logger.info("오늘 KRX 휴장 — 한국 세션 신호 생략")
            return
    except Exception as _e:
        logger.error("Calendar gate 오류 — 세션 신호 스킵 (fail-closed): %s", _e)
        return

    _publish_session_signal("session:kr_open")


def _trigger_us_session():
    """미국 세션 신호 — Redis Pub/Sub + DB fallback. 오늘이 NYSE 휴장이면 생략."""
    try:
        from datetime import timezone as _tz
        from backend.data.calendar import get_calendar_service, Market as _Market
        _svc = get_calendar_service()
        _now = datetime.now(_tz.utc)
        if not _svc.is_trading_day(_Market.NYSE, _svc.trade_date(_Market.NYSE, _now)):
            logger.info("오늘 NYSE 휴장 — 미국 세션 신호 생략")
            return
    except Exception as _e:
        logger.error("Calendar gate 오류 — 세션 신호 스킵 (fail-closed): %s", _e)
        return

    _publish_session_signal("session:us_open")


def _periodic_reconcile():
    """30분 주기 포지션·주문 조정 — 장중 브로커 desync 감지."""
    try:
        from backend.execution.reconciler import PositionReconciler
        from backend.brokers.kis import get_kis_broker
        import redis as _redis
        r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379"))
        result = PositionReconciler(
            broker=get_kis_broker(),
            # The module's one factory — init_db_factory() here built a new
            # engine (and ran create_all) every 30 minutes and never disposed it.
            db_factory=_get_db_factory(),
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

    # 미국 시장 09:30 Eastern — APScheduler가 DST 자동 처리
    # 서머타임: 22:30 KST / 겨울: 23:30 KST
    scheduler.add_job(
        _trigger_us_session,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone="America/New_York"),
        id="us_session", name="미국주식 매매",
    )

    # 일일 리스크 리셋 07:01 KST — 리스크 데이(07:00 경계, #166)가 바뀐 직후,
    # 한국 개장(09:00) 전. 미국 세션은 05:00(겨울 06:00)에 끝난다.
    scheduler.add_job(
        _reset_daily_risk,
        CronTrigger(hour=7, minute=1, timezone="Asia/Seoul"),
        id="risk_reset", name="리스크 카운터 리셋",
    )

    # 자산 스냅샷 + 일일 결산 06:50 KST — 미국 마감(05:00, 겨울 06:00) 뒤, 리스크 데이가
    # 바뀌는 07:00 전. 방금 끝난 리스크 데이(한국 세션 + 그날 밤 미국 세션) 전체를 보고한다.
    # 늦게 시작해도(재기동·스레드 지연) 06:59까지는 실행한다 — 날짜는 시작할 때 정하므로 07:00
    # 전에 시작하면 끝난 리스크 데이를 보고한다. 그 뒤로 밀린 실행은 버린다(새 날을 보고하게 된다).
    scheduler.add_job(
        _save_equity_snapshot,
        CronTrigger(hour=6, minute=50, timezone="Asia/Seoul"),
        id="equity_snapshot", name="자산 스냅샷",
        misfire_grace_time=9 * 60, coalesce=True,
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

    # 운영 화면 계좌 카드(포지션·자산) 10분 주기 — 한국 장중, 서울 시각 기준 미국 장중.
    # 체결 직후에도 워커가 따로 발행한다.
    from backend.worker.portfolio_feed import publish_portfolio
    scheduler.add_job(
        publish_portfolio,
        CronTrigger(
            day_of_week="mon-sat",
            hour="0-6,9-15,22-23",
            minute="*/10",
            timezone="Asia/Seoul",
        ),
        id="portfolio_feed",
        name="운영 화면 계좌 발행",
        max_instances=1,
        coalesce=True,
    )

    return scheduler
