"""Quick Trade reserved-order reconciliation runtime (wires the P0-04
``reconcile_reserved`` into a runtime recovery sweep).

An order left in ``QT_RESERVED`` after an *indeterminate* broker submit
(network/timeout — the broker may or may not have received it) must eventually be
resolved: adopted as ``QT_SUBMITTED`` if the broker actually has it, or marked
``QT_FAILED`` if it conclusively does not. ``reconcile_reserved`` already encodes
that two-way decision; this sweep supplies the broker inquiry and — crucially —
only calls it on a *conclusive* inquiry. Any inconclusive result (broker query
raised, or ambiguous multi-match) leaves the order RESERVED (fail-safe): we never
guess SUBMITTED or FAILED.

Concurrency (scenario 9) needs real Postgres (``TEST_DATABASE_URL``) for
``SELECT … FOR UPDATE SKIP LOCKED``; it is skipped on the default SQLite path.
"""
import os
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from backend.database.testing import make_test_engine, StaticPool

from api.database import Base
from api import models  # noqa: F401 - register ORM models
from api.models import (
    QuickTradeOrder,
    QT_RESERVED, QT_SUBMITTED, QT_REJECTED, QT_FAILED, QT_BLOCKED,
    User, Credential,
)
from api.services.quick_trade_recovery import (
    recover_reserved_orders,
    RecoverySummary,
)

_PG = os.environ.get("TEST_DATABASE_URL")


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
        User(id=2, email="b@example.com", password_hash="x"),
        Credential(id=1, user_id=1, name="kis", exchange_id="kis", env="paper"),
        Credential(id=2, user_id=2, name="kis", exchange_id="kis", env="paper"),
    ])
    s.commit()
    try:
        yield s
    finally:
        s.close()


# A broker-order row in KIS inquiry schema (sll_buy_dvsn_cd 02=buy/01=sell;
# US qty=ft_ord_qty, KR qty=ord_qty; order number=odno).
def _row(odno, side, qty, market="us"):
    qty_field = "ft_ord_qty" if market == "us" else "ord_qty"
    return {"odno": odno, "sll_buy_dvsn_cd": "02" if side == "buy" else "01", qty_field: str(int(qty))}


class FakeOrders:
    """Stands in for kis_adapter.KISOrders. Records inquiry calls; returns canned
    rows or raises, per configuration."""

    def __init__(self, rows=None, exc=None, tag=None):
        self.rows = rows if rows is not None else []
        self.exc = exc
        self.tag = tag
        self.calls = []

    def inquire_orders(self, symbol, market="us", excd="NASD"):
        self.calls.append((symbol, market, excd))
        if self.exc is not None:
            raise self.exc
        return self.rows


def _load_kis_factory(orders_by_cred):
    """Return a load_kis(cred) -> (client, orders, portfolio) that hands back the
    FakeOrders registered for that credential id (so we can assert per-credential
    isolation)."""
    def load_kis(cred):
        orders = orders_by_cred[cred.id]
        return (object(), orders, object())
    return load_kis


def _seed_reserved(db, *, order_id, user_id=1, credential_id=1, symbol="AAPL",
                   side="buy", qty=10.0, price=100.0, market="us", exchange="NASD",
                   age_seconds=300, key=None):
    """Insert a RESERVED order whose created_at is `age_seconds` in the past."""
    o = QuickTradeOrder(
        id=order_id, user_id=user_id, credential_id=credential_id,
        idempotency_key=key or f"key-{order_id}", request_hash=f"h-{order_id}",
        symbol=symbol, side=side, market=market, exchange=exchange,
        order_type="limit", qty=qty, price=price, status=QT_RESERVED,
        created_at=datetime.utcnow() - timedelta(seconds=age_seconds),
    )
    db.add(o)
    db.commit()
    return o


# ── 1. RESERVED + one matching broker order → SUBMITTED (odno adopted) ─────────

def test_match_adopts_submitted(db):
    _seed_reserved(db, order_id=1, symbol="AAPL", side="buy", qty=10.0)
    fake = FakeOrders(rows=[_row("BRK-US-1", "buy", 10)])
    summary = recover_reserved_orders(db, load_kis=_load_kis_factory({1: fake}))
    row = db.get(QuickTradeOrder, 1)
    assert row.status == QT_SUBMITTED
    assert row.broker_order_id == "BRK-US-1"
    assert summary.submitted == 1 and summary.failed == 0 and summary.skipped == 0
    assert len(fake.calls) == 1  # broker queried once


# ── 2. RESERVED + broker conclusively absent → FAILED ─────────────────────────

def test_absent_marks_failed(db):
    _seed_reserved(db, order_id=1, symbol="AAPL", side="buy", qty=10.0)
    fake = FakeOrders(rows=[_row("OTHER", "sell", 3)])  # no buy/10 match
    summary = recover_reserved_orders(db, load_kis=_load_kis_factory({1: fake}))
    row = db.get(QuickTradeOrder, 1)
    assert row.status == QT_FAILED
    assert row.error
    assert summary.failed == 1


# ── 3. RESERVED + broker inquiry raises → SKIP, stays RESERVED (fail-safe) ─────

def test_inquiry_error_skips_leaves_reserved(db):
    _seed_reserved(db, order_id=1, symbol="AAPL", side="buy", qty=10.0)
    fake = FakeOrders(exc=TimeoutError("broker unreachable"))
    summary = recover_reserved_orders(db, load_kis=_load_kis_factory({1: fake}))
    row = db.get(QuickTradeOrder, 1)
    assert row.status == QT_RESERVED  # never guessed FAILED on an error
    assert summary.skipped == 1 and summary.failed == 0 and summary.submitted == 0


# ── 4. RESERVED + 2 ambiguous candidates → SKIP, stays RESERVED ───────────────

def test_ambiguous_multi_match_skips(db):
    _seed_reserved(db, order_id=1, symbol="AAPL", side="buy", qty=10.0)
    fake = FakeOrders(rows=[_row("BRK-A", "buy", 10), _row("BRK-B", "buy", 10)])
    summary = recover_reserved_orders(db, load_kis=_load_kis_factory({1: fake}))
    row = db.get(QuickTradeOrder, 1)
    assert row.status == QT_RESERVED  # never guessed which one
    assert summary.skipped == 1


# ── 5. Grace window: a RESERVED order newer than grace is untouched ────────────

def test_grace_window_skips_recent_reservation(db):
    _seed_reserved(db, order_id=1, age_seconds=5)  # very recent (in-flight)
    fake = FakeOrders(rows=[_row("BRK-US-1", "buy", 10)])
    summary = recover_reserved_orders(
        db, load_kis=_load_kis_factory({1: fake}), grace_seconds=60
    )
    row = db.get(QuickTradeOrder, 1)
    assert row.status == QT_RESERVED
    assert summary.seen == 0            # not even selected
    assert len(fake.calls) == 0          # broker never queried for an in-flight order


# ── 6. Non-RESERVED orders are ignored by the sweep ───────────────────────────

def test_non_reserved_ignored(db):
    for oid, st in [(1, QT_SUBMITTED), (2, QT_REJECTED), (3, QT_FAILED), (4, QT_BLOCKED)]:
        o = QuickTradeOrder(
            id=oid, user_id=1, credential_id=1, idempotency_key=f"k{oid}",
            request_hash=f"h{oid}", symbol="AAPL", side="buy", market="us",
            exchange="NASD", order_type="limit", qty=10.0, price=100.0, status=st,
            created_at=datetime.utcnow() - timedelta(seconds=300),
        )
        db.add(o)
    db.commit()
    fake = FakeOrders(rows=[_row("BRK", "buy", 10)])
    summary = recover_reserved_orders(db, load_kis=_load_kis_factory({1: fake}))
    assert summary.seen == 0
    assert len(fake.calls) == 0
    # statuses unchanged
    assert db.get(QuickTradeOrder, 1).status == QT_SUBMITTED
    assert db.get(QuickTradeOrder, 4).status == QT_BLOCKED


# ── 7. Tenant/credential isolation: each order queried with ITS credential ────

def test_per_credential_isolation(db):
    _seed_reserved(db, order_id=1, user_id=1, credential_id=1, symbol="AAPL", side="buy", qty=10.0)
    _seed_reserved(db, order_id=2, user_id=2, credential_id=2, symbol="TSLA", side="sell", qty=4.0)
    fake1 = FakeOrders(rows=[_row("A-1", "buy", 10)], tag="cred1")
    fake2 = FakeOrders(rows=[_row("B-1", "sell", 4)], tag="cred2")
    recover_reserved_orders(db, load_kis=_load_kis_factory({1: fake1, 2: fake2}))
    # Each order resolved against its own credential's broker view.
    assert db.get(QuickTradeOrder, 1).broker_order_id == "A-1"
    assert db.get(QuickTradeOrder, 2).broker_order_id == "B-1"
    assert fake1.calls == [("AAPL", "us", "NASD")]
    assert fake2.calls == [("TSLA", "us", "NASD")]


# ── 8. Idempotent: running the sweep twice makes no extra transitions ──────────

def test_idempotent_second_sweep_noop(db):
    _seed_reserved(db, order_id=1, symbol="AAPL", side="buy", qty=10.0)
    fake = FakeOrders(rows=[_row("BRK-US-1", "buy", 10)])
    load = _load_kis_factory({1: fake})
    recover_reserved_orders(db, load_kis=load)
    assert db.get(QuickTradeOrder, 1).status == QT_SUBMITTED
    calls_after_first = len(fake.calls)
    summary2 = recover_reserved_orders(db, load_kis=load)
    assert summary2.seen == 0             # already SUBMITTED, not re-selected
    assert len(fake.calls) == calls_after_first  # broker not re-queried
    assert db.get(QuickTradeOrder, 1).status == QT_SUBMITTED


# ── 9. Concurrency: two sweeps, each order reconciled exactly once (Postgres) ──

@pytest.mark.skipif(not _PG, reason="FOR UPDATE SKIP LOCKED requires Postgres (TEST_DATABASE_URL)")
def test_concurrent_sweeps_reconcile_once(SessionLocal):
    seed = SessionLocal()
    seed.add_all([
        User(id=1, email="a@example.com", password_hash="x"),
        Credential(id=1, user_id=1, name="kis", exchange_id="kis", env="paper"),
    ])
    seed.commit()
    for oid in range(1, 9):
        seed.add(QuickTradeOrder(
            id=oid, user_id=1, credential_id=1, idempotency_key=f"k{oid}",
            request_hash=f"h{oid}", symbol="AAPL", side="buy", market="us",
            exchange="NASD", order_type="limit", qty=10.0, price=100.0,
            status=QT_RESERVED, created_at=datetime.utcnow() - timedelta(seconds=300),
        ))
    seed.commit()
    seed.close()

    lock = threading.Lock()
    total_calls = {"n": 0}
    start = threading.Barrier(4)

    def load_kis(cred):
        def inquire_orders(symbol, market="us", excd="NASD"):
            with lock:
                total_calls["n"] += 1
            return [_row("BRK-C", "buy", 10)]
        return (object(), type("O", (), {"inquire_orders": staticmethod(inquire_orders)})(), object())

    def worker():
        s = SessionLocal()
        try:
            start.wait()
            recover_reserved_orders(s, load_kis=load_kis)
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = SessionLocal()
    try:
        submitted = check.query(QuickTradeOrder).filter_by(status=QT_SUBMITTED).count()
        reserved = check.query(QuickTradeOrder).filter_by(status=QT_RESERVED).count()
        assert submitted == 8 and reserved == 0
    finally:
        check.close()
    # Each of the 8 orders queried the broker exactly once — no double-processing.
    assert total_calls["n"] == 8


# ── 10. kis_adapter KISOrders.inquire_orders: correct TR + output parsing ──────

class _RecordingClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, path, tr_id, params):
        self.calls.append((path, tr_id, params))
        return self.response


def _make_kis_orders(env, client):
    from kis_adapter import KISOrders
    return KISOrders(client=_FakeClientWrap(client, env))


class _FakeClientWrap:
    """Wraps a recording client with the auth attributes KISOrders needs."""
    def __init__(self, recording, env):
        self._rec = recording
        self.auth = type("A", (), {
            "env": env,
            "require_account": staticmethod(lambda: "1234567890AB"),
        })()

    def get(self, path, tr_id, params):
        return self._rec.get(path, tr_id, params)


def test_inquire_orders_us_tr_and_parsing_paper():
    rec = _RecordingClient({"output": [_row("US-1", "buy", 10)]})
    orders = _make_kis_orders("paper", rec)
    out = orders.inquire_orders("AAPL", market="us", excd="NASD")
    assert out == [_row("US-1", "buy", 10)]
    path, tr_id, params = rec.calls[0]
    assert tr_id == "VTTS3035R"                   # US paper inquiry TR
    assert "overseas-stock" in path and "inquire" in path
    assert params.get("PDNO") == "AAPL" and params.get("OVRS_EXCG_CD") == "NASD"
    # Inquiry must be date-bounded: an empty range collapses to "today only",
    # so a RESERVED order reserved on a prior day would look absent during the
    # startup recovery sweep → a false QT_FAILED. See kis_adapter/dates.py.
    assert params.get("ORD_STRT_DT") and params.get("ORD_END_DT")
    assert len(params["ORD_STRT_DT"]) == 8 and len(params["ORD_END_DT"]) == 8
    assert params["ORD_STRT_DT"] <= params["ORD_END_DT"]


def test_inquire_orders_kr_tr_real():
    rec = _RecordingClient({"output1": [_row("KR-1", "buy", 5, market="kr")]})
    orders = _make_kis_orders("real", rec)
    out = orders.inquire_orders("005930", market="kr")
    assert out == [_row("KR-1", "buy", 5, market="kr")]
    _, tr_id, params = rec.calls[0]
    assert tr_id == "TTTC8036R"                    # KR real inquiry TR
    # Same date-bounding requirement on the domestic inquiry (see US test above).
    assert params.get("INQR_STRT_DT") and params.get("INQR_END_DT")
    assert len(params["INQR_STRT_DT"]) == 8 and len(params["INQR_END_DT"]) == 8
    assert params["INQR_STRT_DT"] <= params["INQR_END_DT"]


# ── 11. Startup wiring: env-gated, non-blocking, never crashes on failure ──────

def test_recover_on_startup_disabled_is_noop(monkeypatch):
    from api.services import quick_trade_recovery as rec
    monkeypatch.setenv("QT_RECOVERY_ON_STARTUP", "false")
    called = {"n": 0}

    def boom_factory():
        called["n"] += 1
        raise AssertionError("must not run when disabled")

    # Should return without opening a session / running the sweep.
    result = rec.recover_on_startup(session_factory=boom_factory, load_kis=lambda c: None)
    assert result is None
    assert called["n"] == 0


def test_recover_on_startup_swallows_errors(monkeypatch):
    from api.services import quick_trade_recovery as rec
    monkeypatch.setenv("QT_RECOVERY_ON_STARTUP", "true")

    class _BoomSession:
        def query(self, *a, **k):
            raise RuntimeError("db down")

        def close(self):
            pass

    # A failing sweep must NOT propagate (startup must never crash).
    result = rec.recover_on_startup(session_factory=lambda: _BoomSession(), load_kis=lambda c: None)
    assert result is None  # swallowed
