"""Quick Trade order persistence — crash-safe reserve-before-submit (P0-04).

CRITICAL INVARIANT (reserve-before-submit): the idempotency reservation is
durably COMMITted to the DB *before* any broker call is made. A duplicate,
retry, or crash therefore can never cause a second broker submission for the
same key.

    1. BEGIN
    2. INSERT quick_trade_orders (status=RESERVED)
    3. COMMIT                         ← reservation is now durable
    4. [AFTER COMMIT] broker_submit() ← the ONLY broker call site
    5. UPDATE row with broker order id + terminal state

Application-level guarantee: **1 durable DB reservation per idempotency key**
(enforced by the ``(user_id, idempotency_key)`` unique constraint). This is NOT
"exactly-once broker submission" — KIS offers no broker-side idempotency, so a
network/timeout after the broker received an order leaves a deterministic
``RESERVED`` state resolved by :func:`reconcile_reserved`, never a blind retry.

Scope: Quick Trade only. No coupling to ``backend/execution`` (OrderStateMachine,
OrderFillPoller, PositionTracker, IdempotencyStore) — those are single-account
by design (P0-02). The fingerprint scheme below mirrors the *pattern* of
``backend/execution/idempotency.py`` without importing it.
"""
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.models import (
    QuickTradeOrder, qt_transition,
    QT_RESERVED, QT_SUBMITTED, QT_REJECTED, QT_FAILED,
)

logger = logging.getLogger(__name__)

# Double-click window for the *derived* idempotency key: identical params from
# the same tenant within this window collapse to one key. An explicit
# ``Idempotency-Key`` header bypasses the window entirely.
IDEMPOTENCY_BUCKET_SECONDS = 10


class IdempotencyConflict(Exception):
    """An idempotency key was reused with different request parameters."""

    def __init__(self, existing: QuickTradeOrder):
        self.existing = existing
        super().__init__("idempotency key reused with different request parameters")


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def request_fingerprint(
    *, user_id: int, credential_id: int, symbol: str, side: str,
    qty: float, price: float, market: str = "us", exchange: str = "NASD",
    order_type: str = "limit",
) -> str:
    """Stable SHA256 over the order parameters only (no time component).

    Used as ``request_hash`` to detect an idempotency key reused with different
    params. ``qty``/``price`` are quantised to integer 'cents' to avoid float
    drift, matching ``backend/execution/idempotency.py``'s approach. ``exchange``
    is part of the order identity — the same symbol on NASD vs NYSE is a
    distinct order.
    """
    return hashlib.sha256(_canonical({
        "v": 1,
        "u": user_id,
        "c": credential_id,
        "sym": symbol,
        "side": side.lower(),
        "qty_c": round(qty * 100),
        "price_c": round(price * 100),
        "mkt": market.lower(),
        "exch": exchange.upper(),
        "ot": order_type.lower(),
    }).encode()).hexdigest()


def derive_idempotency_key(
    *, user_id: int, credential_id: int, symbol: str, side: str,
    qty: float, price: float, market: str = "us", exchange: str = "NASD",
    order_type: str = "limit",
    bucket_seconds: int = IDEMPOTENCY_BUCKET_SECONDS,
    _now: Optional[datetime] = None,
) -> str:
    """Server-derived key = request fingerprint + a coarse time bucket.

    Gives best-effort double-click protection with no frontend change; an
    explicit ``Idempotency-Key`` header should be preferred by callers that
    have one.
    """
    now = _now or datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    bucket = epoch - (epoch % max(bucket_seconds, 1))
    fp = request_fingerprint(
        user_id=user_id, credential_id=credential_id, symbol=symbol, side=side,
        qty=qty, price=price, market=market, exchange=exchange, order_type=order_type,
    )
    return hashlib.sha256(f"{fp}:{bucket}".encode()).hexdigest()


def reserve_and_submit(
    db: Session,
    *,
    user_id: int,
    credential_id: int,
    request: dict,
    idempotency_key: str,
    request_hash: str,
    broker_submit: Callable[[], dict],
    extract_order_id: Callable[[dict], str],
) -> QuickTradeOrder:
    """Reserve durably, then submit to the broker exactly once for a fresh key.

    ``broker_submit`` is invoked at most once, and only strictly after the
    reservation row is committed. A duplicate key returns the existing row
    without calling the broker; a key reused with different params raises
    :class:`IdempotencyConflict`.
    """
    order = QuickTradeOrder(
        user_id=user_id,
        credential_id=credential_id,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        symbol=request["symbol"],
        side=request["side"],
        market=request.get("market", "us"),
        exchange=request.get("exchange", "NASD"),
        order_type=request.get("order_type", "limit"),
        qty=request["qty"],
        price=request["price"],
        status=QT_RESERVED,
    )
    db.add(order)
    try:
        db.commit()  # reservation durable BEFORE any broker call
    except IntegrityError:
        # A reservation for this (user, key) already exists (a concurrent winner
        # or a prior request) → return its state, never call the broker. If the
        # integrity error is something else (e.g. an FK violation), there is no
        # such row: surface the real error rather than masking it.
        db.rollback()
        existing = (
            db.query(QuickTradeOrder)
            .filter_by(user_id=user_id, idempotency_key=idempotency_key)
            .one_or_none()
        )
        if existing is None:
            raise
        if existing.request_hash != request_hash:
            raise IdempotencyConflict(existing) from None
        return existing
    db.refresh(order)

    # [AFTER COMMIT] the single broker submission for this reservation.
    try:
        result = broker_submit()
    except RuntimeError as e:
        # Broker explicitly rejected (rt_cd != "0") — terminal.
        qt_transition(order, QT_REJECTED)
        order.error = str(e)
        db.commit()
        return order
    except Exception as e:
        # Network/timeout — the broker may or may not have received the order.
        # Keep RESERVED (recoverable); reconcile_reserved resolves it. Never
        # blindly retry the broker here.
        order.error = str(e)
        db.commit()  # status stays RESERVED
        logger.warning("quick-trade broker submit indeterminate (order %s): %s", order.id, e)
        return order

    order.broker_order_id = extract_order_id(result)
    qt_transition(order, QT_SUBMITTED)
    db.commit()
    return order


def reconcile_reserved(
    db: Session,
    order: QuickTradeOrder,
    broker_lookup: Callable[[str], Optional[Tuple[str, str]]],
) -> QuickTradeOrder:
    """Deterministically resolve a ``RESERVED`` order after an indeterminate submit.

    ``broker_lookup(symbol)`` returns ``(broker_order_id, status)`` if the broker
    has a matching order, else ``None``. Found → adopt it and mark ``SUBMITTED``;
    not found → the broker never got it, mark ``FAILED``. Idempotent: a
    non-RESERVED order is returned unchanged. The broker query is injected, so
    this stays Quick-Trade-scoped (no OrderFillPoller coupling).
    """
    if order.status != QT_RESERVED:
        return order
    found = broker_lookup(order.symbol)
    if found:
        order.broker_order_id, _broker_status = found
        qt_transition(order, QT_SUBMITTED)
    else:
        qt_transition(order, QT_FAILED)
        order.error = order.error or "reconcile: broker has no matching order"
    db.commit()
    return order
