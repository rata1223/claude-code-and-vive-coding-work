import os
import logging
import asyncio
from telegram import Bot

logger = logging.getLogger(__name__)

_bot: Bot = None


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        token = os.environ.get("TELEGRAM_TOKEN")
        if not token or token.startswith("여기에"):
            logger.warning("텔레그램 토큰 미설정 — 알림 비활성화")
            return None
        _bot = Bot(token=token)
    return _bot


def send_alert(message: str):
    bot = _get_bot()
    if bot is None:
        logger.info("[알림 미전송] %s", message)
        return

    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id or chat_id.startswith("여기에"):
        logger.warning("텔레그램 CHAT_ID 미설정")
        return

    try:
        asyncio.run(bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML"))
    except Exception as e:
        logger.error("텔레그램 전송 실패: %s", e)


def alert_buy(symbol: str, qty: int, price: float, market: str = "US"):
    send_alert(f"✅ <b>매수 완료</b>\n종목: {symbol} ({market})\n수량: {qty}주\n가격: {price:,.2f}")


def alert_sell(symbol: str, qty: int, price: float, reason: str = "", market: str = "US"):
    send_alert(f"🔴 <b>매도 완료</b>\n종목: {symbol} ({market})\n수량: {qty}주\n가격: {price:,.2f}\n사유: {reason}")


def alert_error(message: str):
    send_alert(f"⚠️ <b>오류 발생</b>\n{message}")


def alert_emergency(message: str):
    send_alert(f"🚨 <b>긴급 알림</b>\n{message}")


def alert_daily_summary(summary: dict):
    """A value of ``None`` means it could not be read — shown as ``—``, never
    as 0 (an unread balance is not an empty account, #149/#192)."""
    kill_line = ""
    if summary.get("kill_switch"):
        kill_line = f"\n🚨 킬스위치: {summary.get('kill_reason', '활성')}"
    elif "kill_switch" in summary and summary["kill_switch"] is None:
        kill_line = "\n⚠️ 킬스위치 상태 조회 실패 — 직접 확인할 것"
    day_line = ""
    if summary.get("risk_day"):
        day_line = f"리스크 데이: {summary['risk_day']} (07:00~07:00 KST)\n"

    def _num(key, fmt, unit):
        value = summary.get(key, 0)
        return "—" if value is None else f"{value:{fmt}}{unit}"

    msg = (
        f"📊 <b>일일 결산</b>\n"
        f"{day_line}"
        f"총 자산: {_num('total_equity', ',.0f', '원')}\n"
        f"일 수익률: {_num('daily_pnl_pct', '.2f', '%')}\n"
        f"포지션 수: {_num('position_count', 'd', '개')}"
        f"{kill_line}"
    )
    send_alert(msg)
