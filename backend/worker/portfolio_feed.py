"""Positions and equity for the operator live feed (kis-ws).

kis-ws relays ``position:update`` and ``equity:update`` from Redis to the
operator screen; nothing published them, so the screen could show orders and
alerts but not what the account holds or is worth. This reads both from the
broker and publishes them.

It is a view, never a decision input: every failure is logged and swallowed,
and the two reads are independent — a failed balance read still publishes the
positions, and the other way round. Overlapping triggers (the scheduler, a
burst of fills) coalesce: a publish already in progress makes the next one a
no-op rather than queueing more broker reads.
"""
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_busy = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _equity_payload(bal) -> dict:
    return {
        "total_eval_krw": bal.total_eval_krw,
        "cash_krw": bal.cash_krw,
        "cash_usd": bal.cash_usd,
        "equity_verified": bool(getattr(bal, "equity_verified", True)),
        "at": _now(),
    }


def _positions_payload(positions) -> dict:
    return {
        "positions": [
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_price": p.avg_price,
                "current_price": getattr(p, "current_price", None) or None,
                "market": p.market,
            }
            for p in positions
        ],
        "at": _now(),
    }


def publish_portfolio(broker=None) -> bool:
    """Publish the account's equity and positions. ``True`` if anything was
    published; ``False`` when another publish was already running or both
    reads failed. Never raises."""
    if not _busy.acquire(blocking=False):
        logger.debug("포트폴리오 발행 진행 중 — 이번 요청은 건너뛴다")
        return False
    try:
        try:
            from backend.websocket.server import publish_equity_update, publish_position_update
            if broker is None:
                from backend.brokers.kis import get_kis_broker
                broker = get_kis_broker()
        except Exception as e:
            logger.warning("포트폴리오 발행 준비 실패: %s", e)
            return False

        published = False
        try:
            publish_equity_update(_equity_payload(broker.get_balance()))
            published = True
        except Exception as e:
            logger.warning("자산 발행 실패: %s", e)
        try:
            publish_position_update(_positions_payload(broker.get_positions()))
            published = True
        except Exception as e:
            logger.warning("포지션 발행 실패: %s", e)
        return published
    finally:
        _busy.release()
