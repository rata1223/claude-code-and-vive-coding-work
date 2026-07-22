"""Quick Trade reserved-order reconciliation runtime.

Wires the P0-04 :func:`reconcile_reserved` into a runtime **recovery** sweep. An
order left ``QT_RESERVED`` after an *indeterminate* broker submit (network/timeout
— the broker may or may not have received it) is otherwise never resolved at
runtime; a timed-out order stays RESERVED forever. This sweep queries the broker
per order and lets ``reconcile_reserved`` finalize it:

    * conclusive **match**   → ``QT_SUBMITTED`` (adopt the broker order id)
    * conclusive **absent**  → ``QT_FAILED``    (the broker never got it)
    * **inconclusive**       → SKIP, leave RESERVED (fail-safe — never guessed)

"Inconclusive" = the broker inquiry raised, or returned two-or-more candidates
that both match the reservation (side + qty). In those cases we must NOT call
``reconcile_reserved`` (its None→FAILED contract would falsely fail a real fill).

Invariants preserved: no broker *submission* happens here (read-only inquiry +
terminal-status write); ``reserve_and_submit``/``reconcile_reserved`` are
unchanged; each order's broker client is built from *that order's* credential
(request-scoped, P0-03 — never ``os.environ``); uncertainty never takes an
irreversible wrong action.

Concurrency: each order is claimed with ``SELECT … FOR UPDATE SKIP LOCKED`` so
concurrent API workers never reconcile the same row (a no-op degrade on SQLite).
"""
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

from sqlalchemy.orm import Session

from api.models import QuickTradeOrder, QT_RESERVED, QT_SUBMITTED
from api.services.quick_trade_service import reconcile_reserved
from backend.brokers.semantic_mapper import KIS_DOMESTIC_MAPPER, KIS_OVERSEAS_MAPPER

logger = logging.getLogger(__name__)

STARTUP_ENV = "QT_RECOVERY_ON_STARTUP"
GRACE_ENV = "QT_RECOVERY_GRACE_SECONDS"


def _int_env(name: str, default: int) -> int:
    """Parse a non-negative int env var, falling back to ``default`` on a
    malformed or negative value (never crash import on bad config)."""
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("invalid %s, using default %s", name, default)
        return default
    if value < 0:
        logger.warning("%s must be >= 0, using default %s", name, default)
        return default
    return value


# Only reconcile orders that have been RESERVED for at least this long, so an order
# still in flight in another request (reserved-committed, broker call not yet
# returned) is never touched by the sweep.
DEFAULT_GRACE_SECONDS = _int_env(GRACE_ENV, 60)
DEFAULT_LIMIT = 200


@dataclass
class RecoverySummary:
    seen: int = 0
    submitted: int = 0
    failed: int = 0
    skipped: int = 0


# Classification outcomes for a RESERVED order against the broker's view.
_MATCH = "match"
_ABSENT = "absent"
_SKIP = "skip"


def _classify(order: QuickTradeOrder, orders_client) -> Tuple[str, Optional[str]]:
    """Return ``(outcome, broker_order_id)``.

    ``match`` (with an odno) → the broker has exactly one order matching this
    reservation's side + qty. ``absent`` → the inquiry succeeded and no candidate
    matched (the broker never got it). ``skip`` → the inquiry raised, or two-plus
    candidates matched (ambiguous): leave RESERVED, never guess.
    """
    mapper = KIS_DOMESTIC_MAPPER if order.market.lower() == "kr" else KIS_OVERSEAS_MAPPER
    try:
        rows = orders_client.inquire_orders(order.symbol, market=order.market, excd=order.exchange)
    except Exception as e:  # noqa: BLE001 - any broker/inquiry error is inconclusive → skip (fail-safe)
        logger.warning("QT recovery: broker inquiry failed for order %s (%s) — skip", order.id, e)
        return _SKIP, None

    want_qty = int(round(order.qty))
    want_side = order.side.lower()
    candidates = []
    for row in rows or []:
        try:
            if mapper.extract_side(row) == want_side and mapper.extract_order_qty(row) == want_qty:
                odno = row.get("odno")
                if odno:
                    candidates.append(odno)
        except Exception:  # noqa: BLE001 - a malformed row is not a match, not a failure
            continue

    if len(candidates) == 1:
        return _MATCH, candidates[0]
    if len(candidates) == 0:
        return _ABSENT, None
    logger.warning(
        "QT recovery: %d ambiguous broker matches for order %s (%s %s x%s) — skip",
        len(candidates), order.id, order.side, order.symbol, want_qty,
    )
    return _SKIP, None


def _claim_reserved_ids(db: Session, cutoff: datetime, limit: int) -> List[int]:
    """Snapshot the ids of RESERVED orders older than ``cutoff`` (no lock held
    across the sweep; each order is re-selected with a row lock when processed)."""
    rows = (
        db.query(QuickTradeOrder.id)
        .filter(QuickTradeOrder.status == QT_RESERVED, QuickTradeOrder.created_at <= cutoff)
        .order_by(QuickTradeOrder.id)
        .limit(limit)
        .all()
    )
    db.commit()  # end the read transaction
    return [r[0] for r in rows]


def recover_reserved_orders(
    db: Session,
    *,
    load_kis: Callable,
    now: Optional[datetime] = None,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
    limit: int = DEFAULT_LIMIT,
) -> RecoverySummary:
    """Reconcile RESERVED orders older than ``grace_seconds``.

    ``load_kis(cred) -> (client, orders, portfolio)`` builds a request-scoped KIS
    client from an order's credential (same factory the router uses). Only the
    ``orders`` handle (its ``inquire_orders``) is used — read-only.
    """
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=grace_seconds)
    summary = RecoverySummary()

    for oid in _claim_reserved_ids(db, cutoff, limit):
        # Claim this row so a concurrent worker skips it (no-op degrade on SQLite).
        order = (
            db.query(QuickTradeOrder)
            .filter(QuickTradeOrder.id == oid, QuickTradeOrder.status == QT_RESERVED)
            .with_for_update(skip_locked=True)
            .first()
        )
        if order is None:
            # Already taken by another worker, or no longer RESERVED. Release any
            # transaction state and move on.
            db.rollback()
            continue

        summary.seen += 1
        cred = order.credential
        try:
            _client, orders_client, _portfolio = load_kis(cred)
        except Exception as e:  # noqa: BLE001 - can't build broker client → inconclusive, skip
            logger.warning("QT recovery: cannot build broker client for order %s (%s) — skip", order.id, e)
            db.commit()  # release the row lock; status unchanged
            summary.skipped += 1
            continue

        outcome, odno = _classify(order, orders_client)
        if outcome == _MATCH:
            reconcile_reserved(db, order, broker_lookup=lambda _s, _odno=odno: (_odno, QT_SUBMITTED))
            summary.submitted += 1
        elif outcome == _ABSENT:
            reconcile_reserved(db, order, broker_lookup=lambda _s: None)
            summary.failed += 1
        else:  # _SKIP — inconclusive, leave RESERVED
            db.commit()  # release the row lock; status unchanged
            summary.skipped += 1

    logger.info(
        "QT recovery sweep: seen=%d submitted=%d failed=%d skipped=%d",
        summary.seen, summary.submitted, summary.failed, summary.skipped,
    )
    return summary


def _startup_enabled(explicit: Optional[bool]) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get(STARTUP_ENV, "true").strip().lower() not in ("0", "false", "no", "off")


def recover_on_startup(
    *,
    session_factory=None,
    load_kis: Optional[Callable] = None,
    enabled: Optional[bool] = None,
) -> Optional[RecoverySummary]:
    """Env-gated startup entry point. Opens its own DB session, runs the sweep,
    and **swallows every error** — a recovery failure must never crash or block
    app startup. Returns the summary on success, else ``None``."""
    if not _startup_enabled(enabled):
        logger.info("QT recovery on startup disabled (%s)", STARTUP_ENV)
        return None

    if session_factory is None:
        from api.database import SessionLocal
        session_factory = SessionLocal
    if load_kis is None:
        # Lazy import avoids an import cycle (router imports the service layer).
        from api.routers.quick_trade import _load_kis as load_kis  # noqa: N806

    db = None
    try:
        db = session_factory()
        return recover_reserved_orders(db, load_kis=load_kis)
    except Exception as e:  # noqa: BLE001 - never let recovery crash startup
        logger.error("QT recovery on startup failed (swallowed): %s", e)
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
