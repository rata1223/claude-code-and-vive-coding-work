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

Liveness: besides the startup sweep, a background thread re-runs the sweep every
``QT_RECOVERY_INTERVAL_SECONDS`` so an order that goes indeterminate *while the
process keeps running* is resolved without waiting for a restart. An order that
stays RESERVED past ``QT_RECOVERY_ESCALATE_SECONDS`` is escalated (structured log
+ a user-visible ``Notification`` row) instead of being silently retried forever.

Concurrency: each order is claimed with ``SELECT … FOR UPDATE SKIP LOCKED`` so
concurrent API workers never reconcile the same row (a no-op degrade on SQLite),
and an in-process non-blocking lock keeps the periodic sweep from overlapping
either itself or the startup sweep.

Fairness: eligible orders are swept least-recently-attempted first, and every
skip stamps a diagnostic on the row (which advances ``updated_at``). Without
that, a block of permanently-inconclusive orders at the head of an id-ordered
``LIMIT`` window would starve every newer order behind it forever.
"""
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import requests  # exception types only — the HTTP call itself lives in kis_adapter
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.models import Notification, QuickTradeOrder, QT_RESERVED, QT_SUBMITTED
from api.services.quick_trade_service import reconcile_reserved
from backend.brokers.semantic_mapper import KIS_DOMESTIC_MAPPER, KIS_OVERSEAS_MAPPER

logger = logging.getLogger(__name__)

STARTUP_ENV = "QT_RECOVERY_ON_STARTUP"
GRACE_ENV = "QT_RECOVERY_GRACE_SECONDS"
PERIODIC_ENV = "QT_RECOVERY_PERIODIC"
INTERVAL_ENV = "QT_RECOVERY_INTERVAL_SECONDS"
ESCALATE_ENV = "QT_RECOVERY_ESCALATE_SECONDS"


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
# How often the background sweep re-runs, and how long an order may stay RESERVED
# before it stops being "retry quietly" and becomes an operator-visible incident.
DEFAULT_INTERVAL_SECONDS = _int_env(INTERVAL_ENV, 300)
DEFAULT_ESCALATE_SECONDS = _int_env(ESCALATE_ENV, 3600)

# Diagnostics are stamped into the existing ``error`` column — deliberately no new
# columns, because ``quick_trade_orders`` is provisioned by ``create_all``, which
# never ALTERs an existing table (a new column would silently not exist in prod).
_DIAG_PREFIX = "qt-recovery:"
_ESCALATED_MARK = "escalated=1"


@dataclass
class RecoverySummary:
    seen: int = 0
    submitted: int = 0
    failed: int = 0
    skipped: int = 0
    #: RESERVED orders past the grace window at snapshot time — including the ones
    #: this cycle's ``limit`` could not reach. ``truncated`` says the window hid
    #: ``eligible - seen`` of them, which the log line would otherwise never show.
    eligible: int = 0
    truncated: bool = False
    escalated: int = 0
    #: True when a shutdown signal ended the cycle early. Without it an aborted
    #: sweep is indistinguishable from a clean full pass in logs and callers.
    aborted: bool = False
    #: skip reason → count, so "skipped=7" is never an undiagnosable number.
    skip_reasons: Dict[str, int] = field(default_factory=dict)


# Classification outcomes for a RESERVED order against the broker's view.
_MATCH = "match"
_ABSENT = "absent"
_SKIP = "skip"

# Why an order was left RESERVED. Each is *inconclusive* — never a guess — but
# they have different operator meanings, so they are counted and stamped apart.
_REASON_INQUIRY_ERROR = "inquiry_error"     # broker inquiry raised (network/auth/rate limit)
_REASON_AMBIGUOUS = "ambiguous_match"       # 2+ broker orders match side+qty — cannot disambiguate
_REASON_CLIENT_ERROR = "client_error"       # could not build a broker client from the credential
_REASON_RECONCILE_ERROR = "reconcile_error"  # the resolving write itself failed (integrity/connection)
_REASON_INQUIRY_TIMEOUT = "inquiry_timeout"  # the inquiry hit its HTTP deadline — outcome unknown


def _classify(order: QuickTradeOrder, orders_client) -> Tuple[str, Optional[str], Optional[str]]:
    """Return ``(outcome, broker_order_id, skip_reason)``.

    ``match`` (with an odno) → the broker has exactly one order matching this
    reservation's side + qty. ``absent`` → the inquiry succeeded and no candidate
    matched (the broker never got it). ``skip`` (with a reason) → the inquiry
    raised, or two-plus candidates matched (ambiguous): leave RESERVED, never guess.
    """
    mapper = KIS_DOMESTIC_MAPPER if order.market.lower() == "kr" else KIS_OVERSEAS_MAPPER
    try:
        rows = orders_client.inquire_orders(order.symbol, market=order.market, excd=order.exchange)
    except requests.exceptions.Timeout as e:
        # The deadline (KIS_HTTP_TIMEOUT_SECONDS, enforced in kis_adapter) fired.
        # Bounding the call is what keeps one hung broker from stalling the cycle;
        # the outcome is unknown, so the order stays RESERVED like any other skip.
        logger.warning(
            "QT recovery: broker inquiry timed out for order %s (%s %s x%s market=%s) "
            "after the HTTP deadline (%s) — inconclusive, left RESERVED",
            order.id, order.side, order.symbol, order.qty, order.market, e,
        )
        return _SKIP, None, _REASON_INQUIRY_TIMEOUT
    except Exception as e:  # noqa: BLE001 - any broker/inquiry error is inconclusive → skip (fail-safe)
        logger.warning("QT recovery: broker inquiry failed for order %s (%s) — skip", order.id, e)
        return _SKIP, None, _REASON_INQUIRY_ERROR

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
        return _MATCH, candidates[0], None
    if len(candidates) == 0:
        return _ABSENT, None, None
    logger.warning(
        "QT recovery: %d ambiguous broker matches for order %s (%s %s x%s) — skip",
        len(candidates), order.id, order.side, order.symbol, want_qty,
    )
    return _SKIP, None, _REASON_AMBIGUOUS


def _claim_reserved_ids(db: Session, cutoff: datetime, limit: int) -> Tuple[List[int], int]:
    """Snapshot ``(ids, eligible_total)`` for RESERVED orders older than ``cutoff``.

    Ordered least-recently-attempted first (``updated_at``, then ``id``): each skip
    stamps the row, so a permanently-inconclusive order rotates to the back of the
    queue instead of squatting at the head of the ``limit`` window and starving
    every newer order behind it. ``eligible_total`` is returned so a truncated
    window is reported rather than silently hiding the backlog.

    No lock is held across the sweep; each order is re-selected with a row lock
    when processed.
    """
    base = db.query(QuickTradeOrder).filter(
        QuickTradeOrder.status == QT_RESERVED, QuickTradeOrder.created_at <= cutoff
    )
    eligible_total = base.count()
    rows = (
        base.with_entities(QuickTradeOrder.id)
        # COALESCE: ``updated_at`` is nullable, and PostgreSQL sorts NULLs LAST on
        # ASC — a row written outside the ORM would sit permanently behind the
        # window, which is the exact starvation this ordering exists to prevent.
        .order_by(
            func.coalesce(QuickTradeOrder.updated_at, QuickTradeOrder.created_at).asc(),
            QuickTradeOrder.id.asc(),
        )
        .limit(limit)
        .all()
    )
    db.commit()  # end the read transaction
    return [r[0] for r in rows], eligible_total


def _age_seconds(order: QuickTradeOrder, now: datetime) -> float:
    created = order.created_at or now
    return max(0.0, (now - created).total_seconds())


def _already_escalated(order: QuickTradeOrder) -> bool:
    return _ESCALATED_MARK in (order.error or "")


def _stamp_skip(order: QuickTradeOrder, reason: str, now: datetime, escalated: bool) -> None:
    """Record why this cycle left the order RESERVED, on the existing ``error``
    column. Also advances ``updated_at`` (ORM ``onupdate``), which is what makes
    the fairness rotation in :func:`_claim_reserved_ids` work."""
    age_min = int(_age_seconds(order, now) // 60)
    mark = f" {_ESCALATED_MARK}" if escalated or _already_escalated(order) else ""
    order.error = (
        f"{_DIAG_PREFIX} skip({reason}) age={age_min}m "
        f"checked={now.replace(microsecond=0).isoformat()}{mark}"
    )


def _stamp_and_commit(
    db: Session, order: QuickTradeOrder, reason: str, now: datetime, escalated: bool = False
) -> None:
    """Stamp the diagnostic and persist it, releasing the row lock. Status is
    unchanged. A stamp failure must never abort the sweep — but note the row then
    keeps its old ``updated_at`` and will be retried at the head of the next cycle.
    """
    _stamp_skip(order, reason, now, escalated)
    try:
        db.commit()
    except Exception as e:  # noqa: BLE001 - a stamp failure must not abort the sweep
        logger.warning("QT recovery: could not stamp diagnostic on order %s (%s)", order.id, e)
        db.rollback()


def _clear_diagnostic(order: QuickTradeOrder) -> None:
    """Drop a recovery stamp before a conclusive transition.

    ``error`` is the user-facing failure field, and :func:`reconcile_reserved`
    neither clears it when adopting a broker order (SUBMITTED) nor overwrites it
    on the FAILED path (``order.error or ...``). Left in place, a stale stamp
    would make a good order read as failed, and would mask the real failure
    reason on a genuinely failed one.
    """
    if (order.error or "").startswith(_DIAG_PREFIX):
        order.error = None


def _escalate(db: Session, order: QuickTradeOrder, reason: str, age_seconds: float) -> bool:
    """Raise a stuck RESERVED order to operator/user visibility exactly once.

    Structured ERROR log + a ``Notification`` row for the order's owner. Best
    effort: a notification failure must never abort the sweep or change the
    order's status. Returns True if this call escalated it (first time).
    """
    if _already_escalated(order):
        return False

    age_min = int(age_seconds // 60)
    logger.error(
        "QT recovery ESCALATION: order %s stuck RESERVED for %dm (reason=%s user=%s "
        "%s %s x%s @%s) — broker outcome still unknown, manual reconciliation required",
        order.id, age_min, reason, order.user_id,
        order.side, order.symbol, order.qty, order.price,
    )
    try:
        db.add(Notification(
            user_id=order.user_id,
            title="퀵트레이드 주문 상태 미확정",
            message=(
                f"{order.symbol} {order.side} {order.qty}주 주문이 {age_min}분째 "
                f"체결 여부를 확인하지 못했습니다 (사유: {reason}). "
                "증권사 앱에서 주문 내역을 직접 확인해 주세요."
            ),
        ))
    except Exception as e:  # noqa: BLE001 - notification is best-effort, never fatal
        logger.warning("QT recovery: escalation notification failed for order %s (%s)", order.id, e)
        return False
    return True


def recover_reserved_orders(
    db: Session,
    *,
    load_kis: Callable,
    now: Optional[datetime] = None,
    grace_seconds: int = DEFAULT_GRACE_SECONDS,
    limit: int = DEFAULT_LIMIT,
    escalate_seconds: int = DEFAULT_ESCALATE_SECONDS,
    stop_event: Optional[threading.Event] = None,
) -> RecoverySummary:
    """Reconcile RESERVED orders older than ``grace_seconds``.

    ``load_kis(cred) -> (client, orders, portfolio)`` builds a request-scoped KIS
    client from an order's credential (same factory the router uses). Only the
    ``orders`` handle (its ``inquire_orders``) is used — read-only.

    Orders still inconclusive after ``escalate_seconds`` are escalated once
    (structured log + ``Notification``); they stay RESERVED either way, because an
    unknown broker outcome must never be guessed into a terminal status.
    """
    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=grace_seconds)
    summary = RecoverySummary()

    oids, summary.eligible = _claim_reserved_ids(db, cutoff, limit)
    summary.truncated = summary.eligible > len(oids)

    for oid in oids:
        # Shutdown asked us to stop: end at this row boundary. Everything already
        # committed stands; the rest stay RESERVED and are picked up next cycle.
        # Checking here (not mid-order) is what keeps the join from expiring while
        # a row lock is still held.
        if stop_event is not None and stop_event.is_set():
            summary.aborted = True
            logger.info(
                "QT recovery sweep interrupted by shutdown after %d of %d claimed "
                "orders — remaining deferred to the next sweep",
                summary.seen, len(oids),
            )
            break

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
            outcome, odno, reason = _classify(order, orders_client)
        except Exception as e:  # noqa: BLE001 - can't build broker client → inconclusive, skip
            logger.warning("QT recovery: cannot build broker client for order %s (%s) — skip", order.id, e)
            outcome, odno, reason = _SKIP, None, _REASON_CLIENT_ERROR

        if outcome in (_MATCH, _ABSENT):
            _clear_diagnostic(order)
            lookup = (
                (lambda _s, _odno=odno: (_odno, QT_SUBMITTED)) if outcome == _MATCH
                else (lambda _s: None)
            )
            try:
                reconcile_reserved(db, order, broker_lookup=lookup)
            except Exception as e:  # noqa: BLE001 - one bad row must not abort the whole sweep
                # Without this, the exception escapes the sweep: every remaining
                # order is deferred, and the failing row keeps its old
                # ``updated_at``, so the fairness ordering hands it back first
                # next cycle — a poison pill that blocks recovery indefinitely.
                logger.error(
                    "QT recovery: reconcile failed for order %s (%s) — left RESERVED", oid, e
                )
                db.rollback()
                summary.skipped += 1
                summary.skip_reasons[_REASON_RECONCILE_ERROR] = (
                    summary.skip_reasons.get(_REASON_RECONCILE_ERROR, 0) + 1
                )
                _stamp_and_commit(db, order, _REASON_RECONCILE_ERROR, now)  # rotate it out of the head
                continue
            if outcome == _MATCH:
                summary.submitted += 1
            else:
                summary.failed += 1
        else:  # _SKIP — inconclusive, leave RESERVED (never guess a terminal status)
            age = _age_seconds(order, now)
            escalated = False
            if age >= escalate_seconds:
                escalated = _escalate(db, order, reason, age)
                if escalated:
                    summary.escalated += 1
            summary.skipped += 1
            summary.skip_reasons[reason] = summary.skip_reasons.get(reason, 0) + 1
            _stamp_and_commit(db, order, reason, now, escalated)

    logger.info(
        "QT recovery sweep%s: seen=%d submitted=%d failed=%d skipped=%d "
        "escalated=%d eligible=%d reasons=%s",
        " (ABORTED — shutdown)" if summary.aborted else "",
        summary.seen, summary.submitted, summary.failed, summary.skipped,
        summary.escalated, summary.eligible, summary.skip_reasons or "{}",
    )
    if summary.truncated:
        # The per-cycle window hid part of the backlog. Without this line the
        # sweep looks healthy at exactly `limit` orders while the queue grows.
        logger.warning(
            "QT recovery sweep truncated: %d of %d eligible orders were outside this "
            "cycle's window (limit=%d) — backlog growing, raise the per-cycle limit or "
            "lower %s, and investigate the skip reasons above",
            summary.eligible - len(oids), summary.eligible, limit, INTERVAL_ENV,
        )
    return summary


_FALSEY = ("0", "false", "no", "off")

# In-process guard: the periodic sweep must never overlap itself (a slow broker
# cycle outrunning the interval) or the startup sweep. Row-level SKIP LOCKED
# already protects *across* processes; this protects within one.
_SWEEP_LOCK = threading.Lock()


def _env_enabled(name: str, explicit: Optional[bool], default: str = "true") -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get(name, default).strip().lower() not in _FALSEY


def _startup_enabled(explicit: Optional[bool]) -> bool:
    return _env_enabled(STARTUP_ENV, explicit)


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

    return run_sweep_once(session_factory=session_factory, load_kis=load_kis, label="startup")


def run_sweep_once(
    *,
    session_factory=None,
    load_kis: Optional[Callable] = None,
    label: str = "periodic",
    **sweep_kwargs,
) -> Optional[RecoverySummary]:
    """Run one sweep on its own DB session, swallowing every error.

    Skips immediately if another sweep is already running in this process (the
    lock is acquired non-blocking, so a slow cycle is dropped rather than queued
    — the next tick will pick the work up).
    """
    if not _SWEEP_LOCK.acquire(blocking=False):
        logger.info("QT recovery (%s): another sweep is in progress — skipping this cycle", label)
        return None

    db = None
    try:
        if session_factory is None:
            from api.database import SessionLocal
            session_factory = SessionLocal
        if load_kis is None:
            # Lazy import avoids an import cycle (router imports the service layer).
            from api.routers.quick_trade import _load_kis as load_kis  # noqa: N806
        db = session_factory()
        return recover_reserved_orders(db, load_kis=load_kis, **sweep_kwargs)
    except Exception as e:  # noqa: BLE001 - recovery must never crash startup or the loop
        logger.error("QT recovery (%s) failed (swallowed): %s", label, e)
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception as e:  # noqa: BLE001 - close failure must not mask the result
                logger.debug("QT recovery (%s): session close failed (%s)", label, e)
        _SWEEP_LOCK.release()


def run_periodic_recovery(
    stop_event: threading.Event,
    *,
    session_factory=None,
    load_kis: Optional[Callable] = None,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    max_cycles: Optional[int] = None,
    **sweep_kwargs,
) -> int:
    """Sweep loop: run, wait ``interval_seconds``, repeat until ``stop_event``.

    Returns the number of cycles run. ``max_cycles`` bounds the loop for tests.
    The wait is interruptible, so shutdown is immediate rather than up to one
    interval late.
    """
    interval = max(1, int(interval_seconds))
    cycles = 0
    while not stop_event.is_set():
        run_sweep_once(
            session_factory=session_factory, load_kis=load_kis,
            label="periodic", stop_event=stop_event, **sweep_kwargs,
        )
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        stop_event.wait(interval)
    return cycles


def start_periodic_recovery(
    *,
    session_factory=None,
    load_kis: Optional[Callable] = None,
    enabled: Optional[bool] = None,
    interval_seconds: Optional[int] = None,
) -> Optional[Tuple[threading.Thread, threading.Event]]:
    """Env-gated launcher for the background sweep. Returns ``(thread, stop_event)``
    so the caller can stop it on shutdown, or ``None`` when disabled."""
    if not _env_enabled(PERIODIC_ENV, enabled):
        logger.info("QT periodic recovery disabled (%s)", PERIODIC_ENV)
        return None

    interval = DEFAULT_INTERVAL_SECONDS if interval_seconds is None else interval_seconds
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_periodic_recovery,
        args=(stop_event,),
        kwargs={
            "session_factory": session_factory,
            "load_kis": load_kis,
            "interval_seconds": interval,
        },
        name="qt-recovery-sweep",
        daemon=True,  # never block interpreter exit
    )
    thread.start()
    logger.info("QT periodic recovery started (every %ds)", interval)
    return thread, stop_event
