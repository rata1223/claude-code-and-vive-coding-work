"""Shared date helpers for KIS order inquiries.

The KIS single-symbol order-inquiry TRs (overseas ``TTTS3035R``/``VTTS3035R``
and domestic ``TTTC8036R``/``VTTC8036R``) take ``ORD_STRT_DT``/``ORD_END_DT``
(overseas) or ``INQR_STRT_DT``/``INQR_END_DT`` (domestic) as ``YYYYMMDD`` date
bounds. Sending an empty range collapses the lookup to "today only" (or is
rejected), which for reconciliation can hide a real order and produce a false
"absent" verdict — and, on a live-money account, a false ``QT_FAILED``.

Both the Quick Trade reconciliation sweep (``kis_adapter/orders.py``) and the
Execution-Layer poller (``backend/brokers/kis.py``) use this single helper so
their inquiry windows never drift apart.
"""
from datetime import datetime, timedelta, timezone

# KIS trading servers operate in KST (UTC+9). A 30-day lookback covers a
# reserved order across weekends, holidays, or a multi-day outage before the
# startup recovery sweep runs.
_KST = timezone(timedelta(hours=9))
INQUIRE_LOOKBACK_DAYS = 30


def inquiry_date_range(lookback_days: int = INQUIRE_LOOKBACK_DAYS) -> tuple[str, str]:
    """Return ``(start, end)`` as ``YYYYMMDD`` (KST) for a KIS order inquiry.

    ``end`` is today (KST); ``start`` is ``lookback_days`` earlier. A negative
    ``lookback_days`` is clamped to a same-day range so a bad caller can never
    invert the window.
    """
    today = datetime.now(_KST).date()
    start = today - timedelta(days=max(lookback_days, 0))
    return start.strftime("%Y%m%d"), today.strftime("%Y%m%d")
