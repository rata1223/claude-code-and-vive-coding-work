"""P0-05 — RiskManager pre-submit gate on the Quick Trade execution path.

The existing production ``strategy/risk.py::RiskManager`` is reused *as-is* as an
authoritative pre-submit gate, inserted between the durable idempotency
reservation (P0-04, committed) and the single broker call:

    reserve → COMMIT → **risk_gate()** → broker_submit() (only if ALLOWED)

Fail-closed: any uncertainty (RiskManager raises, risk state unavailable, unknown
decision) blocks the broker call and persists a terminal ``QT_BLOCKED`` audit row.
The gate lives *below* the duplicate/conflict/concurrency short-circuits, so all
P0-04 idempotency guarantees are preserved (a duplicate returns its existing row
without re-evaluating risk or calling the broker).

Concurrency (scenario 12) requires real Postgres (TEST_DATABASE_URL); it is
skipped on the default SQLite path.
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
    QT_RESERVED, QT_SUBMITTED, QT_BLOCKED,
    User, Credential,
)
from api.services.quick_trade_service import (
    reserve_and_submit,
    IdempotencyConflict,
    RiskDenied,
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


class RiskGate:
    """Injectable risk gate that records how many times it was evaluated.

    ``mode``: 'allow' → no-op; 'deny' → raise RiskDenied; 'raise' → raise a
    generic exception (fail-closed path); 'keyerror' → missing-context path.
    """

    def __init__(self, mode="allow", reason="halt"):
        self.calls = 0
        self.mode = mode
        self.reason = reason

    def __call__(self):
        self.calls += 1
        if self.mode == "deny":
            raise RiskDenied(self.reason)
        if self.mode == "raise":
            raise RuntimeError("redis unreachable")
        if self.mode == "keyerror":
            raise KeyError("risk_context")
        # allow → no-op


def _extract(result):
    return result.get("ODNO", "")


_REQ = {"symbol": "AAPL", "side": "buy", "qty": 10.0, "price": 100.0,
        "market": "us", "order_type": "limit"}


def _key_and_hash(req=_REQ, user_id=1, credential_id=1):
    h = request_fingerprint(user_id=user_id, credential_id=credential_id, **req)
    k = derive_idempotency_key(user_id=user_id, credential_id=credential_id, **req)
    return k, h


def _reserve(db, broker, risk_gate, *, req=_REQ, key=None, request_hash=None,
             user_id=1, credential_id=1):
    if key is None or request_hash is None:
        k, h = _key_and_hash(req, user_id, credential_id)
        key = key or k
        request_hash = request_hash or h
    return reserve_and_submit(
        db, user_id=user_id, credential_id=credential_id, request=req,
        idempotency_key=key, request_hash=request_hash,
        risk_gate=risk_gate,
        broker_submit=broker.submit, extract_order_id=_extract,
    )


# ── 1. risk allows → order proceeds to broker & SUBMITTED ─────────────────────

def test_risk_allows_order_submits(db):
    broker = FakeBroker(result={"ODNO": "BRK-1"})
    gate = RiskGate("allow")
    order = _reserve(db, broker, gate)
    assert order.status == QT_SUBMITTED
    assert order.broker_order_id == "BRK-1"
    assert gate.calls == 1
    assert broker.calls == 1


# ── 2. risk rejects (RiskDenied) → BLOCKED, broker never called ───────────────

def test_risk_denied_blocks(db):
    broker = FakeBroker()
    gate = RiskGate("deny", reason="daily loss limit")
    order = _reserve(db, broker, gate)
    assert order.status == QT_BLOCKED
    assert "risk-denied" in (order.error or "")
    assert "daily loss limit" in (order.error or "")
    assert broker.calls == 0  # scenario 7


# ── 3. kill switch active (halt flag) → BLOCKED ───────────────────────────────

def test_kill_switch_blocks(db):
    # A kill switch surfaces as RiskManager.is_trading_halted() == True, which the
    # gate translates into RiskDenied.
    broker = FakeBroker()
    gate = RiskGate("deny", reason="trading halted by RiskManager")
    order = _reserve(db, broker, gate)
    assert order.status == QT_BLOCKED
    assert broker.calls == 0


# ── 4. SafeMode active → BLOCKED ──────────────────────────────────────────────

def test_safemode_blocks(db):
    # SafeMode-active likewise surfaces via the halt flag → RiskDenied.
    broker = FakeBroker()
    gate = RiskGate("deny", reason="safe mode")
    order = _reserve(db, broker, gate)
    assert order.status == QT_BLOCKED
    assert broker.calls == 0


# ── 5. RiskManager throws (generic) → fail-closed BLOCKED ─────────────────────

def test_risk_error_fails_closed(db):
    broker = FakeBroker()
    gate = RiskGate("raise")  # e.g. Redis unreachable
    order = _reserve(db, broker, gate)
    assert order.status == QT_BLOCKED
    assert "risk-error" in (order.error or "")
    assert "fail-closed" in (order.error or "")
    assert broker.calls == 0


# ── 6. missing risk context (KeyError) → fail-closed BLOCKED ──────────────────

def test_missing_context_fails_closed(db):
    broker = FakeBroker()
    gate = RiskGate("keyerror")
    order = _reserve(db, broker, gate)
    assert order.status == QT_BLOCKED
    assert broker.calls == 0


# ── 7. broker NEVER called after any block (covered above; explicit assertion) ─

def test_broker_never_called_on_block(db):
    for mode in ("deny", "raise", "keyerror"):
        broker = FakeBroker()
        gate = RiskGate(mode)
        # each iteration uses a distinct key (distinct qty) to avoid dedupe
        req = {**_REQ, "qty": 10.0 + ("deny", "raise", "keyerror").index(mode)}
        k, h = _key_and_hash(req)
        order = _reserve(db, broker, gate, req=req, key=k, request_hash=h)
        assert order.status == QT_BLOCKED
        assert broker.calls == 0


# ── 8. broker called EXACTLY ONCE after approval ──────────────────────────────

def test_broker_called_once_on_allow(db):
    broker = FakeBroker()
    gate = RiskGate("allow")
    _reserve(db, broker, gate)
    assert broker.calls == 1
    assert gate.calls == 1


# ── 9. duplicate of a BLOCKED request → returns existing, no re-eval/broker ────

def test_duplicate_after_block_no_reeval_or_broker(db):
    broker = FakeBroker()
    gate = RiskGate("deny")
    key, h = _key_and_hash()
    first = _reserve(db, broker, gate, key=key, request_hash=h)
    assert first.status == QT_BLOCKED
    assert gate.calls == 1

    # A retry with the same key must return the existing BLOCKED row WITHOUT
    # re-reserving, re-evaluating risk, or touching the broker.
    second = _reserve(db, broker, gate, key=key, request_hash=h)
    assert second.id == first.id
    assert second.status == QT_BLOCKED
    assert gate.calls == 1  # NOT re-evaluated
    assert broker.calls == 0
    assert db.query(QuickTradeOrder).filter_by(idempotency_key=key).count() == 1


# ── 10. duplicate of an approved request → returns persisted SUBMITTED ─────────

def test_duplicate_after_allow_returns_existing(db):
    broker = FakeBroker(result={"ODNO": "BRK-D"})
    gate = RiskGate("allow")
    key, h = _key_and_hash()
    first = _reserve(db, broker, gate, key=key, request_hash=h)
    second = _reserve(db, broker, gate, key=key, request_hash=h)
    assert second.id == first.id
    assert second.status == QT_SUBMITTED
    assert gate.calls == 1  # risk evaluated only for the first
    assert broker.calls == 1


# ── 11. same key + different payload → IdempotencyConflict (no eval/broker) ────

def test_same_key_different_payload_conflicts(db):
    broker = FakeBroker()
    gate = RiskGate("allow")
    key, h1 = _key_and_hash(_REQ)
    _reserve(db, broker, gate, key=key, request_hash=h1)
    other = {**_REQ, "qty": 999.0}
    h2 = request_fingerprint(user_id=1, credential_id=1, **other)
    with pytest.raises(IdempotencyConflict):
        _reserve(db, broker, gate, req=other, key=key, request_hash=h2)
    assert gate.calls == 1  # conflict path never re-evaluates risk
    assert broker.calls == 1


# ── 12. concurrent duplicate → 1 reservation + 1 risk eval + 1 broker (PG) ─────

@pytest.mark.skipif(not _PG, reason="real concurrency requires Postgres (TEST_DATABASE_URL)")
def test_concurrent_duplicate_single_eval_and_broker(SessionLocal):
    seed = SessionLocal()
    seed.add_all([
        User(id=1, email="a@example.com", password_hash="x"),
        Credential(id=1, user_id=1, name="kis", exchange_id="kis", env="paper"),
    ])
    seed.commit()
    seed.close()

    key, h = _key_and_hash()
    lock = threading.Lock()
    counters = {"risk": 0, "broker": 0}
    start = threading.Barrier(6)

    def worker():
        s = SessionLocal()

        def risk_gate():
            with lock:
                counters["risk"] += 1

        def submit():
            with lock:
                counters["broker"] += 1
            return {"ODNO": "BRK-C"}

        try:
            start.wait()
            reserve_and_submit(
                s, user_id=1, credential_id=1, request=_REQ,
                idempotency_key=key, request_hash=h,
                risk_gate=risk_gate,
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
    # Exactly one winner reserved → exactly one risk eval → exactly one broker call.
    assert counters["risk"] == 1
    assert counters["broker"] == 1


# ── 13. tenant isolation: same key string, different users ────────────────────

def test_tenant_isolation(db):
    broker_a = FakeBroker(result={"ODNO": "A-1"})
    broker_b = FakeBroker(result={"ODNO": "B-1"})
    gate = RiskGate("allow")
    shared_key = "shared-key-risk"
    _, ha = _key_and_hash(_REQ, user_id=1, credential_id=1)
    order_a = _reserve(db, broker_a, gate, key=shared_key, request_hash=ha,
                       user_id=1, credential_id=1)
    order_b = _reserve(db, broker_b, gate, key=shared_key, request_hash=ha,
                       user_id=2, credential_id=2)
    assert order_a.id != order_b.id
    assert order_a.user_id == 1 and order_b.user_id == 2
    assert order_a.broker_order_id == "A-1"
    assert order_b.broker_order_id == "B-1"
    assert gate.calls == 2  # each tenant's fresh reservation is evaluated


# ── 14. block persists an auditable row (credential + params intact) ──────────

def test_blocked_row_is_auditable(db):
    broker = FakeBroker()
    gate = RiskGate("deny", reason="halt")
    order = _reserve(db, broker, gate)
    row = db.query(QuickTradeOrder).filter_by(user_id=1, status=QT_BLOCKED).one()
    assert row.id == order.id
    assert row.credential_id == 1
    assert row.symbol == "AAPL"
    assert row.side == "buy"
    assert row.broker_order_id is None  # broker never assigned an id


# ── 15. risk_gate is a required parameter (fail-closed by construction) ────────

def test_risk_gate_is_required(db):
    broker = FakeBroker()
    key, h = _key_and_hash()
    with pytest.raises(TypeError):
        reserve_and_submit(
            db, user_id=1, credential_id=1, request=_REQ,
            idempotency_key=key, request_hash=h,
            broker_submit=broker.submit, extract_order_id=_extract,
        )
    assert broker.calls == 0


# ── router-level: RiskManager patched to halt → Resp.err + BLOCKED, no broker ──

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
        json={"name": "P0-05", "exchange_id": "kis", "app_key": "k", "app_secret": "s"},
    )
    return res.json()["data"]["id"]


def _use_real_risk_gate():
    """Remove the conftest default-allow override so the real dependency runs."""
    from api.main import app
    from api.routers.quick_trade import get_risk_gate
    app.dependency_overrides.pop(get_risk_gate, None)


def test_http_halted_returns_error_and_blocks(
    client, auth_headers, db_session, http_fake, monkeypatch
):
    _use_real_risk_gate()
    # Patch the RiskManager the dependency constructs so is_trading_halted → True.
    class _Halted:
        def is_trading_halted(self):
            return True
    monkeypatch.setattr("api.routers.quick_trade.RiskManager", lambda: _Halted())

    cred_id = _seed_credential(client, auth_headers)
    payload = {"credential_id": cred_id, "symbol": "AAPL", "side": "buy",
               "qty": 5, "price": 150.0, "market": "us", "exchange": "NASD"}
    res = client.post("/api/quick-trade/place-order", headers=auth_headers, json=payload)
    assert res.status_code == 200
    assert res.json()["code"] == -1  # Resp.err
    assert db_session.query(QuickTradeOrder).filter_by(
        user_id=1, status=QT_BLOCKED).count() == 1
    assert http_fake.calls == 0  # broker never called


def test_http_risk_manager_raises_fails_closed(
    client, auth_headers, db_session, http_fake, monkeypatch
):
    _use_real_risk_gate()
    # RiskManager construction / check raising (e.g. Redis down) must fail closed.
    class _Boom:
        def is_trading_halted(self):
            raise RuntimeError("redis down")
    monkeypatch.setattr("api.routers.quick_trade.RiskManager", lambda: _Boom())

    cred_id = _seed_credential(client, auth_headers)
    payload = {"credential_id": cred_id, "symbol": "TSLA", "side": "buy",
               "qty": 5, "price": 150.0, "market": "us", "exchange": "NASD"}
    res = client.post("/api/quick-trade/place-order", headers=auth_headers, json=payload)
    assert res.status_code == 200
    assert res.json()["code"] == -1
    assert db_session.query(QuickTradeOrder).filter_by(
        user_id=1, status=QT_BLOCKED).count() == 1
    assert http_fake.calls == 0


def test_http_risk_allows_submits(
    client, auth_headers, db_session, http_fake, monkeypatch
):
    _use_real_risk_gate()
    class _Ok:
        def is_trading_halted(self):
            return False
    monkeypatch.setattr("api.routers.quick_trade.RiskManager", lambda: _Ok())

    cred_id = _seed_credential(client, auth_headers)
    payload = {"credential_id": cred_id, "symbol": "AAPL", "side": "buy",
               "qty": 5, "price": 150.0, "market": "us", "exchange": "NASD"}
    res = client.post("/api/quick-trade/place-order", headers=auth_headers, json=payload)
    assert res.status_code == 200
    assert res.json()["code"] == 1  # Resp.ok
    assert res.json()["data"]["status"] == "submitted"
    assert http_fake.calls == 1
