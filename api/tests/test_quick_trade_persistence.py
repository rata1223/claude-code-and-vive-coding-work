"""P0-04 — quick_trade_orders reserve-before-submit persistence & crash-safe idempotency.

Covers the 12 required scenarios and crash boundaries A–E. The CRITICAL INVARIANT:
the idempotency reservation is durably COMMITted to the DB before any broker call,
so a duplicate/retry/crash never double-submits. Application guarantee is
"1 durable DB reservation per idempotency key" — not exactly-once broker submission.

Concurrency (scenario 9) requires real Postgres (TEST_DATABASE_URL); it is skipped
on the default SQLite path. All other scenarios run everywhere.
"""
import os
import threading

import pytest
from sqlalchemy.orm import sessionmaker

from backend.database.testing import make_test_engine, StaticPool

from api.database import Base
from api import models  # noqa: F401 - register ORM models
from api.models import (
    QuickTradeOrder,
    QT_RESERVED, QT_SUBMITTED, QT_REJECTED, QT_FAILED,
    User, Credential,
)
from api.services.quick_trade_service import (
    reserve_and_submit,
    reconcile_reserved,
    IdempotencyConflict,
    request_fingerprint,
    derive_idempotency_key,
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
    # A user + credential to satisfy FKs / ownership.
    u = User(id=1, email="a@example.com", password_hash="x")
    u2 = User(id=2, email="b@example.com", password_hash="x")
    c = Credential(id=1, user_id=1, name="kis", exchange_id="kis", env="paper")
    c2 = Credential(id=2, user_id=2, name="kis", exchange_id="kis", env="paper")
    s.add_all([u, u2, c, c2])
    s.commit()
    try:
        yield s
    finally:
        s.close()


class FakeBroker:
    def __init__(self, result=None, exc=None):
        self.calls = 0
        self.result = result if result is not None else {"ODNO": "BRK-1"}
        self.exc = exc

    def submit(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


def _extract(result):
    return result.get("ODNO", "")


_REQ = {"symbol": "AAPL", "side": "buy", "qty": 10.0, "price": 100.0,
        "market": "us", "order_type": "limit"}


def _key_and_hash(req=_REQ, user_id=1, credential_id=1):
    h = request_fingerprint(user_id=user_id, credential_id=credential_id, **req)
    k = derive_idempotency_key(user_id=user_id, credential_id=credential_id, **req)
    return k, h


def _reserve(db, broker, *, req=_REQ, key=None, request_hash=None, user_id=1, credential_id=1):
    if key is None or request_hash is None:
        k, h = _key_and_hash(req, user_id, credential_id)
        key = key or k
        request_hash = request_hash or h
    return reserve_and_submit(
        db, user_id=user_id, credential_id=credential_id, request=req,
        idempotency_key=key, request_hash=request_hash,
        risk_gate=lambda: None,  # P0-05: allow (these tests target P0-04 persistence)
        broker_submit=broker.submit, extract_order_id=_extract,
    )


# ── 1. first reservation succeeds ─────────────────────────────────────────────

def test_first_request_reserves_and_submits(db):
    broker = FakeBroker(result={"ODNO": "BRK-1"})
    order = _reserve(db, broker)
    assert order.status == QT_SUBMITTED
    assert order.broker_order_id == "BRK-1"
    assert broker.calls == 1
    assert db.query(QuickTradeOrder).count() == 1


# ── 2 & 3. duplicate key → existing state, broker not re-called ────────────────

def test_duplicate_key_returns_existing_without_recalling_broker(db):
    broker = FakeBroker()
    first = _reserve(db, broker)
    second = _reserve(db, broker)  # same derived key + hash
    assert second.id == first.id
    assert second.status == QT_SUBMITTED
    assert broker.calls == 1  # scenario 3: broker called at most once
    assert db.query(QuickTradeOrder).count() == 1


# ── 4. same key + different params → conflict/reject ──────────────────────────

def test_same_key_different_params_is_rejected(db):
    broker = FakeBroker()
    key, h1 = _key_and_hash(_REQ)
    _reserve(db, broker, key=key, request_hash=h1)
    other = {**_REQ, "qty": 999.0}
    h2 = request_fingerprint(user_id=1, credential_id=1, **other)
    assert h2 != h1
    with pytest.raises(IdempotencyConflict):
        _reserve(db, broker, req=other, key=key, request_hash=h2)
    assert broker.calls == 1  # only the first reservation reached the broker


# ── 5. broker call happens strictly AFTER the reservation is committed ─────────

def test_broker_submit_only_after_commit(db, SessionLocal):
    seen = {}

    def broker_submit():
        # A fresh session must already see the RESERVED row → it was committed
        # before this broker call fired.
        s2 = SessionLocal()
        try:
            row = s2.query(QuickTradeOrder).filter_by(idempotency_key=key).one_or_none()
            seen["visible"] = row is not None
            seen["status_at_call"] = row.status if row else None
        finally:
            s2.close()
        return {"ODNO": "BRK-9"}

    key, h = _key_and_hash()
    order = reserve_and_submit(
        db, user_id=1, credential_id=1, request=_REQ,
        idempotency_key=key, request_hash=h,
        risk_gate=lambda: None,
        broker_submit=broker_submit, extract_order_id=_extract,
    )
    assert seen["visible"] is True
    assert seen["status_at_call"] == QT_RESERVED
    assert order.status == QT_SUBMITTED


# ── 6. broker rejection → REJECTED persisted ──────────────────────────────────

def test_broker_rejection_persists_rejected(db):
    broker = FakeBroker(exc=RuntimeError("KIS API error: rejected"))
    order = _reserve(db, broker)
    assert order.status == QT_REJECTED
    assert "rejected" in (order.error or "")
    assert broker.calls == 1


# ── 7. broker timeout → recoverable RESERVED state retained ───────────────────

def test_broker_timeout_keeps_reserved(db):
    broker = FakeBroker(exc=TimeoutError("read timed out"))
    order = _reserve(db, broker)
    assert order.status == QT_RESERVED  # recoverable, never blindly retried
    assert order.error


# ── 8. reservation survives a "restart" (fresh session/engine view) ───────────

def test_reservation_durable_across_new_session(db, SessionLocal):
    broker = FakeBroker(result={"ODNO": "BRK-8"})
    key, h = _key_and_hash()
    _reserve(db, broker, key=key, request_hash=h)
    s2 = SessionLocal()
    try:
        row = s2.query(QuickTradeOrder).filter_by(idempotency_key=key).one()
        assert row.status == QT_SUBMITTED
        assert row.broker_order_id == "BRK-8"
    finally:
        s2.close()


# ── 9. concurrent same key → exactly one reservation + one broker call (PG) ────

@pytest.mark.skipif(not _PG, reason="real concurrency requires Postgres (TEST_DATABASE_URL)")
def test_concurrent_same_key_single_reservation(SessionLocal):
    # Seed the FK parents (this test uses SessionLocal directly, not the `db` fixture).
    seed = SessionLocal()
    seed.add_all([
        User(id=1, email="a@example.com", password_hash="x"),
        Credential(id=1, user_id=1, name="kis", exchange_id="kis", env="paper"),
    ])
    seed.commit()
    seed.close()

    key, h = _key_and_hash()
    lock = threading.Lock()
    calls = {"n": 0}
    start = threading.Barrier(6)

    def worker():
        s = SessionLocal()
        broker_calls = []

        def submit():
            with lock:
                calls["n"] += 1
            return {"ODNO": "BRK-C"}

        try:
            start.wait()
            reserve_and_submit(
                s, user_id=1, credential_id=1, request=_REQ,
                idempotency_key=key, request_hash=h,
                risk_gate=lambda: None,
                broker_submit=submit, extract_order_id=_extract,
            )
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = SessionLocal()
    try:
        assert check.query(QuickTradeOrder).filter_by(idempotency_key=key).count() == 1
    finally:
        check.close()
    assert calls["n"] == 1


# ── 10. DB unique constraint prevents a duplicate reservation (sequential) ─────

def test_unique_constraint_dedupes_sequential(db):
    broker = FakeBroker()
    key, h = _key_and_hash()
    _reserve(db, broker, key=key, request_hash=h)
    _reserve(db, broker, key=key, request_hash=h)
    assert db.query(QuickTradeOrder).filter_by(user_id=1, idempotency_key=key).count() == 1


# ── 11. multi-tenant isolation: same key string, different users ──────────────

def test_multi_tenant_same_key_isolated(db):
    broker_a = FakeBroker(result={"ODNO": "A-1"})
    broker_b = FakeBroker(result={"ODNO": "B-1"})
    # Both tenants happen to use the same key string.
    shared_key = "shared-key-xyz"
    _, ha = _key_and_hash(_REQ, user_id=1, credential_id=1)
    order_a = _reserve(db, broker_a, key=shared_key, request_hash=ha, user_id=1, credential_id=1)
    order_b = _reserve(db, broker_b, key=shared_key, request_hash=ha, user_id=2, credential_id=2)

    assert order_a.id != order_b.id
    assert order_a.user_id == 1 and order_b.user_id == 2
    assert order_a.broker_order_id == "A-1"
    assert order_b.broker_order_id == "B-1"
    # Each tenant's key resolves to its OWN row, never the other's.
    row_b = db.query(QuickTradeOrder).filter_by(user_id=2, idempotency_key=shared_key).one()
    assert row_b.id == order_b.id
    assert broker_a.calls == 1 and broker_b.calls == 1


# ── Boundary A: crash before reservation commit → no broker call, no row ───────

def test_boundary_a_crash_before_commit_no_broker_call(db, monkeypatch):
    broker = FakeBroker()
    real_commit = db.commit
    state = {"first": True}

    def flaky_commit():
        if state["first"]:
            state["first"] = False
            raise RuntimeError("simulated crash before reserve commit")
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)
    with pytest.raises(RuntimeError):
        _reserve(db, broker)
    assert broker.calls == 0  # broker is only reached after a successful commit


# ── Boundary D: deterministic reconciliation of a RESERVED order ──────────────

def test_reconcile_reserved_adopts_broker_order_when_found(db):
    broker = FakeBroker(exc=TimeoutError("timeout"))
    order = _reserve(db, broker)  # ends RESERVED
    assert order.status == QT_RESERVED
    reconcile_reserved(db, order, broker_lookup=lambda sym: ("BRK-R", "submitted"))
    assert order.status == QT_SUBMITTED
    assert order.broker_order_id == "BRK-R"


def test_reconcile_reserved_fails_when_broker_has_no_order(db):
    broker = FakeBroker(exc=TimeoutError("timeout"))
    order = _reserve(db, broker)
    reconcile_reserved(db, order, broker_lookup=lambda sym: None)
    assert order.status == QT_FAILED


# ── 12. HTTP backward-compat: response unchanged + a row is persisted ──────────

class _HttpFakeOrders:
    def __init__(self):
        self.calls = 0

    def buy_us(self, symbol, excd, qty, price):
        self.calls += 1
        return {"output": {"ODNO": "ORDER-HTTP-1"}}


@pytest.fixture()
def http_fake(monkeypatch):
    fake = _HttpFakeOrders()
    monkeypatch.setattr(
        "api.routers.quick_trade._load_kis", lambda cred: (None, fake, object())
    )
    return fake


def _seed_credential(client, auth_headers) -> int:
    res = client.post(
        "/api/credentials/create",
        headers=auth_headers,
        json={"name": "P0-04", "exchange_id": "kis", "app_key": "k", "app_secret": "s"},
    )
    return res.json()["data"]["id"]


def test_http_place_order_backward_compat_and_row_written(
    client, auth_headers, db_session, http_fake
):
    cred_id = _seed_credential(client, auth_headers)
    payload = {"credential_id": cred_id, "symbol": "AAPL", "side": "buy",
               "qty": 5, "price": 150.0, "market": "us", "exchange": "NASD"}

    res = client.post("/api/quick-trade/place-order", headers=auth_headers, json=payload)
    assert res.status_code == 200
    data = res.json()["data"]
    # Backward-compatible response shape/values.
    assert data["order_id"] == "ORDER-HTTP-1"
    assert data["status"] == "submitted"
    assert data["symbol"] == "AAPL"
    assert data["side"] == "buy"
    assert data["qty"] == 5

    # A durable row now exists for this order.
    row = db_session.query(QuickTradeOrder).filter_by(user_id=1, symbol="AAPL").one()
    assert row.status == QT_SUBMITTED
    assert row.broker_order_id == "ORDER-HTTP-1"
    assert http_fake.calls == 1


def test_http_double_submit_same_params_dedupes_at_router(
    client, auth_headers, db_session, http_fake
):
    cred_id = _seed_credential(client, auth_headers)
    payload = {"credential_id": cred_id, "symbol": "AAPL", "side": "buy",
               "qty": 5, "price": 150.0, "market": "us", "exchange": "NASD"}

    r1 = client.post("/api/quick-trade/place-order", headers=auth_headers, json=payload)
    r2 = client.post("/api/quick-trade/place-order", headers=auth_headers, json=payload)
    assert r1.status_code == r2.status_code == 200
    # Identical params within the double-click window → one broker call, one row.
    assert http_fake.calls == 1
    assert db_session.query(QuickTradeOrder).filter_by(user_id=1, symbol="AAPL").count() == 1


class _HttpRejectingOrders:
    def buy_us(self, symbol, excd, qty, price):
        raise RuntimeError("KIS API error: rejected")


def test_http_broker_rejection_returns_error_envelope(client, auth_headers, db_session, monkeypatch):
    monkeypatch.setattr(
        "api.routers.quick_trade._load_kis",
        lambda cred: (None, _HttpRejectingOrders(), object()),
    )
    cred_id = _seed_credential(client, auth_headers)
    payload = {"credential_id": cred_id, "symbol": "AAPL", "side": "buy",
               "qty": 5, "price": 150.0, "market": "us", "exchange": "NASD"}
    res = client.post("/api/quick-trade/place-order", headers=auth_headers, json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["code"] == -1  # Resp.err — clients branching on code detect failure
    # The reservation is still persisted as REJECTED (auditable), just not "ok".
    assert db_session.query(QuickTradeOrder).filter_by(user_id=1, status=QT_REJECTED).count() == 1


# ── exchange is part of order identity ────────────────────────────────────────

def test_exchange_distinguishes_idempotency(db):
    broker = FakeBroker()
    nasd = {**_REQ, "exchange": "NASD"}
    nyse = {**_REQ, "exchange": "NYSE"}
    k1, h1 = _key_and_hash(nasd)
    k2, h2 = _key_and_hash(nyse)
    assert k1 != k2 and h1 != h2  # same symbol/side/qty/price, different exchange
    _reserve(db, broker, req=nasd, key=k1, request_hash=h1)
    _reserve(db, broker, req=nyse, key=k2, request_hash=h2)
    assert db.query(QuickTradeOrder).count() == 2
    assert broker.calls == 2
    assert {o.exchange for o in db.query(QuickTradeOrder).all()} == {"NASD", "NYSE"}


# ── credential delete cascades quick-trade orders (no FK error) ───────────────

def test_deleting_credential_cascades_quick_trade_orders(db):
    _reserve(db, FakeBroker())
    assert db.query(QuickTradeOrder).count() == 1
    cred = db.get(Credential, 1)
    db.delete(cred)
    db.commit()  # must not raise an FK violation
    assert db.query(QuickTradeOrder).filter_by(credential_id=1).count() == 0


# ── illegal state transitions are rejected ────────────────────────────────────

def test_transition_guard_rejects_illegal_transition(db):
    from api.models import qt_transition
    order = _reserve(db, FakeBroker())  # ends SUBMITTED (terminal)
    assert order.status == QT_SUBMITTED
    with pytest.raises(ValueError):
        qt_transition(order, QT_RESERVED)  # cannot re-open a terminal order
