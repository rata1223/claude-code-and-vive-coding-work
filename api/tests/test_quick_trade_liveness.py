"""Quick Trade reconciliation **liveness** hardening.

The startup sweep alone leaves three runtime holes, covered here:

1. **Liveness** — an order that goes indeterminate while the process keeps
   running was never reconciled until the next restart. A background loop now
   re-sweeps on an interval, guarded so cycles never overlap.
2. **Escalation** — an order stuck RESERVED past the escalation threshold used to
   be retried silently forever. It is now surfaced once (structured log +
   ``Notification``) while *still* staying RESERVED — an unknown broker outcome
   must never be guessed into a terminal status.
3. **Truncation / starvation** — the sweep reads at most ``limit`` rows. Ordered
   by id, a block of permanently-inconclusive orders at the head starved every
   newer order behind it, and the hidden backlog never appeared in any log. The
   sweep now rotates least-recently-attempted first and reports the backlog.
"""
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from backend.database.testing import make_test_engine, StaticPool

from api.database import Base
from api import models  # noqa: F401 - register ORM models
from api.models import (
    Notification, QuickTradeOrder, QT_RESERVED, QT_SUBMITTED, QT_FAILED,
    User, Credential,
)
from api.services import quick_trade_recovery as rec
from api.services.quick_trade_recovery import (
    recover_reserved_orders,
    run_periodic_recovery,
    run_sweep_once,
    start_periodic_recovery,
    _REASON_AMBIGUOUS,
    _REASON_CLIENT_ERROR,
    _REASON_INQUIRY_ERROR,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def engine():
    eng = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)
    eng.dispose()


@pytest.fixture()
def SessionLocal(engine):
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)


@pytest.fixture()
def db(SessionLocal):
    s = SessionLocal()
    s.add_all([
        User(id=1, email="a@example.com", password_hash="x"),
        Credential(id=1, user_id=1, name="kis", exchange_id="kis", env="paper"),
    ])
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _row(odno, side, qty, market="us"):
    qty_field = "ft_ord_qty" if market == "us" else "ord_qty"
    return {"odno": odno, "sll_buy_dvsn_cd": "02" if side == "buy" else "01", qty_field: str(int(qty))}


class FakeOrders:
    def __init__(self, rows=None, exc=None):
        self.rows = rows if rows is not None else []
        self.exc = exc
        self.calls = []

    def inquire_orders(self, symbol, market="us", excd="NASD"):
        self.calls.append(symbol)
        if self.exc is not None:
            raise self.exc
        return self.rows


def _load_kis(orders):
    return lambda cred: (object(), orders, object())


def _boom_load_kis(cred):
    raise RuntimeError("credential decrypt failed")


def _seed(db, *, order_id, age_seconds=300, updated_offset=None, qty=10.0,
          side="buy", symbol="AAPL", user_id=1):
    """Insert a RESERVED order aged ``age_seconds``. ``updated_offset`` (seconds
    in the past) pins ``updated_at`` so sweep ordering is deterministic."""
    now = datetime.utcnow()
    o = QuickTradeOrder(
        id=order_id, user_id=user_id, credential_id=1,
        idempotency_key=f"key-{order_id}", request_hash=f"h-{order_id}",
        symbol=symbol, side=side, market="us", exchange="NASD",
        order_type="limit", qty=qty, price=100.0, status=QT_RESERVED,
        created_at=now - timedelta(seconds=age_seconds),
        updated_at=now - timedelta(seconds=updated_offset if updated_offset is not None else age_seconds),
    )
    db.add(o)
    db.commit()
    return o


# ── 1. Skip reasons are classified and counted apart ──────────────────────────

def test_inquiry_error_classified(db):
    _seed(db, order_id=1)
    summary = recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(exc=RuntimeError("timeout"))))
    assert summary.skipped == 1
    assert summary.skip_reasons == {_REASON_INQUIRY_ERROR: 1}
    assert db.get(QuickTradeOrder, 1).status == QT_RESERVED


def test_ambiguous_match_classified(db):
    _seed(db, order_id=1, qty=10.0, side="buy")
    two = FakeOrders(rows=[_row("A", "buy", 10), _row("B", "buy", 10)])
    summary = recover_reserved_orders(db, load_kis=_load_kis(two))
    assert summary.skip_reasons == {_REASON_AMBIGUOUS: 1}
    assert db.get(QuickTradeOrder, 1).status == QT_RESERVED


def test_client_error_classified(db):
    """A credential that cannot build a broker client is inconclusive, not failed."""
    _seed(db, order_id=1)
    summary = recover_reserved_orders(db, load_kis=_boom_load_kis)
    assert summary.skip_reasons == {_REASON_CLIENT_ERROR: 1}
    assert db.get(QuickTradeOrder, 1).status == QT_RESERVED


def test_mixed_reasons_counted_separately(db):
    """Each order gets its own client, so reasons can differ within one sweep."""
    _seed(db, order_id=1, symbol="AAA")
    _seed(db, order_id=2, symbol="BBB")
    per_symbol = {"AAA": FakeOrders(exc=RuntimeError("x")),
                  "BBB": FakeOrders(rows=[_row("A", "buy", 10), _row("B", "buy", 10)])}
    calls = {"n": 0}

    def load_kis(cred):
        # order 1 (AAA) is swept first — both share credential 1
        symbol = "AAA" if calls["n"] == 0 else "BBB"
        calls["n"] += 1
        return (object(), per_symbol[symbol], object())

    summary = recover_reserved_orders(db, load_kis=load_kis)
    assert summary.skipped == 2
    assert summary.skip_reasons == {_REASON_INQUIRY_ERROR: 1, _REASON_AMBIGUOUS: 1}


# ── 2. Conclusive outcomes still resolve (no regression from P0-04) ───────────

def test_match_and_absent_unaffected(db):
    _seed(db, order_id=1, symbol="AAPL", qty=10.0)
    summary = recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(rows=[_row("BRK-1", "buy", 10)])))
    assert summary.submitted == 1 and summary.skipped == 0
    assert db.get(QuickTradeOrder, 1).status == QT_SUBMITTED

    _seed(db, order_id=2, symbol="MSFT", qty=5.0)
    summary = recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(rows=[])))
    assert summary.failed == 1
    assert db.get(QuickTradeOrder, 2).status == QT_FAILED


# ── 3. Escalation of long-stuck RESERVED orders ──────────────────────────────

def test_escalates_after_threshold_and_notifies(db):
    _seed(db, order_id=1, age_seconds=7200)  # 2h old
    summary = recover_reserved_orders(
        db, load_kis=_load_kis(FakeOrders(exc=RuntimeError("timeout"))), escalate_seconds=3600,
    )
    assert summary.escalated == 1

    notes = db.query(Notification).filter(Notification.user_id == 1).all()
    assert len(notes) == 1
    assert "AAPL" in notes[0].message

    row = db.get(QuickTradeOrder, 1)
    # Escalation surfaces the order; it must NOT invent a terminal status.
    assert row.status == QT_RESERVED
    assert "escalated=1" in row.error
    assert _REASON_INQUIRY_ERROR in row.error


def test_young_order_not_escalated(db):
    _seed(db, order_id=1, age_seconds=300)  # 5m old
    summary = recover_reserved_orders(
        db, load_kis=_load_kis(FakeOrders(exc=RuntimeError("timeout"))), escalate_seconds=3600,
    )
    assert summary.escalated == 0
    assert db.query(Notification).count() == 0
    assert "escalated=1" not in (db.get(QuickTradeOrder, 1).error or "")


def test_escalation_is_once_not_per_cycle(db):
    """Repeated sweeps must not spam the user with a notification per cycle."""
    _seed(db, order_id=1, age_seconds=7200)
    load = _load_kis(FakeOrders(exc=RuntimeError("timeout")))
    for _ in range(3):
        recover_reserved_orders(db, load_kis=load, escalate_seconds=3600)
    assert db.query(Notification).count() == 1


def test_diagnostic_stamped_on_skip(db):
    _seed(db, order_id=1, age_seconds=600)
    recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(exc=RuntimeError("timeout"))))
    err = db.get(QuickTradeOrder, 1).error
    assert err.startswith("qt-recovery:")
    assert "skip(inquiry_error)" in err and "age=10m" in err


# ── 4. Truncation / starvation (the 200-row window) ──────────────────────────

def test_truncation_reported_with_full_backlog(db):
    for i in range(1, 6):
        _seed(db, order_id=i)
    summary = recover_reserved_orders(
        db, load_kis=_load_kis(FakeOrders(exc=RuntimeError("x"))), limit=2,
    )
    assert summary.seen == 2
    assert summary.eligible == 5      # the hidden backlog is visible, not silently dropped
    assert summary.truncated is True


def test_not_truncated_when_window_covers_backlog(db):
    _seed(db, order_id=1)
    summary = recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(exc=RuntimeError("x"))), limit=10)
    assert summary.eligible == 1 and summary.truncated is False


def test_skipped_orders_rotate_and_do_not_starve_newer_ones(db):
    """The starvation regression: with a limit smaller than the backlog, a
    permanently-inconclusive order must not occupy the window forever."""
    _seed(db, order_id=1, updated_offset=900)   # least recently attempted
    _seed(db, order_id=2, updated_offset=600)
    _seed(db, order_id=3, updated_offset=300)   # most recently attempted
    load = _load_kis(FakeOrders(exc=RuntimeError("x")))

    seen_first = [o.id for o in _sweep_and_capture(db, load, limit=1)]
    seen_second = [o.id for o in _sweep_and_capture(db, load, limit=1)]
    seen_third = [o.id for o in _sweep_and_capture(db, load, limit=1)]

    # Each cycle picks a different order — the head never squats the window.
    assert seen_first == [1] and seen_second == [2] and seen_third == [3]


def _sweep_and_capture(db, load_kis, *, limit):
    """Run one sweep and return the orders it actually touched (by comparing the
    diagnostic timestamps the sweep stamps)."""
    before = {o.id: o.updated_at for o in db.query(QuickTradeOrder).all()}
    recover_reserved_orders(db, load_kis=load_kis, limit=limit)
    db.expire_all()
    touched = []
    for o in db.query(QuickTradeOrder).order_by(QuickTradeOrder.id).all():
        if o.updated_at != before[o.id]:
            touched.append(o)
    return touched


# ── 5. Periodic loop + concurrency guard ─────────────────────────────────────

def test_periodic_loop_runs_bounded_cycles(SessionLocal):
    stop = threading.Event()
    calls = {"n": 0}

    def counting_factory():
        calls["n"] += 1
        return SessionLocal()

    cycles = run_periodic_recovery(
        stop, session_factory=counting_factory, load_kis=_load_kis(FakeOrders()),
        interval_seconds=1, max_cycles=3,
    )
    assert cycles == 3 and calls["n"] == 3


def test_periodic_loop_stops_immediately_when_signalled(SessionLocal):
    stop = threading.Event()
    stop.set()
    cycles = run_periodic_recovery(
        stop, session_factory=SessionLocal, load_kis=_load_kis(FakeOrders()), interval_seconds=999,
    )
    assert cycles == 0  # never enters the loop, and never waits out the interval


def test_overlapping_sweep_is_skipped_not_queued(SessionLocal):
    """A slow cycle outrunning the interval must be dropped, not stacked."""
    assert rec._SWEEP_LOCK.acquire(blocking=False)
    try:
        result = run_sweep_once(session_factory=SessionLocal, load_kis=_load_kis(FakeOrders()))
        assert result is None  # skipped because a sweep is already in progress
    finally:
        rec._SWEEP_LOCK.release()

    # Lock released → the next cycle runs normally.
    assert run_sweep_once(session_factory=SessionLocal, load_kis=_load_kis(FakeOrders())) is not None


def test_sweep_errors_are_swallowed(SessionLocal):
    def broken_factory():
        raise RuntimeError("db down")

    assert run_sweep_once(session_factory=broken_factory, load_kis=_load_kis(FakeOrders())) is None
    # The guard must be released even on failure, or every later cycle would skip.
    assert rec._SWEEP_LOCK.acquire(blocking=False)
    rec._SWEEP_LOCK.release()


def test_periodic_disabled_by_env(monkeypatch, SessionLocal):
    monkeypatch.setenv("QT_RECOVERY_PERIODIC", "false")
    assert start_periodic_recovery(session_factory=SessionLocal) is None


def test_start_periodic_returns_stoppable_thread(monkeypatch, SessionLocal):
    monkeypatch.setenv("QT_RECOVERY_PERIODIC", "true")
    handle = start_periodic_recovery(
        session_factory=SessionLocal, load_kis=_load_kis(FakeOrders()), interval_seconds=60,
    )
    assert handle is not None
    thread, stop = handle
    try:
        assert thread.daemon is True  # never blocks interpreter exit
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()  # the interruptible wait makes shutdown immediate


# ── 6. Diagnostics never survive a conclusive transition ──────────────────────

def test_stamp_cleared_when_order_later_resolves_to_submitted(db):
    """A skip stamp must not outlive the RESERVED state.

    ``error`` is the user-facing failure field, so a stamp left on a SUBMITTED
    order makes a good order read as failed.
    """
    _seed(db, order_id=1)
    recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(exc=RuntimeError("timeout"))))
    assert (db.get(QuickTradeOrder, 1).error or "").startswith(rec._DIAG_PREFIX)

    recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(rows=[_row("BRK-9", "buy", 10)])))
    row = db.get(QuickTradeOrder, 1)
    assert row.status == QT_SUBMITTED
    assert row.broker_order_id == "BRK-9"
    assert not row.error  # a submitted order must not carry recovery diagnostics


def test_stamp_does_not_mask_the_real_failure_reason(db):
    """``reconcile_reserved`` writes its reason with ``error or ...``, so a stale
    stamp would suppress the genuine 'broker has no matching order' message."""
    _seed(db, order_id=1)
    recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(exc=RuntimeError("timeout"))))
    recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(rows=[])))  # absent → FAILED

    row = db.get(QuickTradeOrder, 1)
    assert row.status == QT_FAILED
    assert not (row.error or "").startswith(rec._DIAG_PREFIX)
    assert "no matching order" in (row.error or "")


# ── 7. One failing row cannot starve the sweep (poison pill) ──────────────────

def test_reconcile_failure_is_isolated_per_order(db, monkeypatch):
    """A raise inside ``reconcile_reserved`` must not abort the remaining rows."""
    _seed(db, order_id=1, updated_offset=900)   # swept first
    _seed(db, order_id=2, updated_offset=300)

    real = rec.reconcile_reserved

    def flaky(session, order, **kw):
        if order.id == 1:
            raise RuntimeError("integrity error")
        return real(session, order, **kw)

    monkeypatch.setattr(rec, "reconcile_reserved", flaky)
    summary = recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(rows=[_row("BRK-1", "buy", 10)])))

    assert summary.submitted == 1                          # order 2 still resolved
    assert summary.skip_reasons == {rec._REASON_RECONCILE_ERROR: 1}
    assert db.get(QuickTradeOrder, 1).status == QT_RESERVED  # never guessed terminal
    assert db.get(QuickTradeOrder, 2).status == QT_SUBMITTED


def test_failing_row_rotates_and_cannot_wedge_later_cycles(db, monkeypatch):
    """The poison-pill regression: the failing row must be stamped so the
    least-recently-attempted ordering moves it behind the healthy ones."""
    _seed(db, order_id=1, updated_offset=900)
    _seed(db, order_id=2, updated_offset=300)

    real = rec.reconcile_reserved
    monkeypatch.setattr(
        rec, "reconcile_reserved",
        lambda s, o, **kw: (_ for _ in ()).throw(RuntimeError("boom")) if o.id == 1 else real(s, o, **kw),
    )
    recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(rows=[_row("BRK-1", "buy", 10)])))

    stamped = db.get(QuickTradeOrder, 1)
    assert (stamped.error or "").startswith(rec._DIAG_PREFIX)
    assert rec._REASON_RECONCILE_ERROR in stamped.error

    # With limit=1 the next cycle must reach a *different* order, not re-hit #1.
    _seed(db, order_id=3, updated_offset=600)
    monkeypatch.setattr(rec, "reconcile_reserved", real)
    summary = recover_reserved_orders(
        db, load_kis=_load_kis(FakeOrders(rows=[_row("BRK-3", "buy", 10)])), limit=1,
    )
    assert summary.seen == 1
    assert db.get(QuickTradeOrder, 3).status == QT_SUBMITTED


# ── 8. Truncation reporting ───────────────────────────────────────────────────

def test_truncation_counts_the_window_not_the_claimed_rows(db, caplog):
    """The deferred count is ``eligible - window``; ``seen`` excludes rows another
    worker claimed, so using it would overstate the backlog."""
    for i in (1, 2, 3):
        _seed(db, order_id=i, updated_offset=100 * i)

    with caplog.at_level("WARNING"):
        summary = recover_reserved_orders(
            db, load_kis=_load_kis(FakeOrders(exc=RuntimeError("timeout"))), limit=1,
        )

    assert summary.truncated is True
    assert summary.eligible == 3
    warning = [r.getMessage() for r in caplog.records if "truncated" in r.getMessage()]
    assert warning and "2 of 3" in warning[0]
    assert "raise the per-cycle limit" in warning[0]  # not "raise the interval"
