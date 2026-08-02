"""PR #144 residual hardening: FastAPI lifespan + bounded sweeps.

Two items deferred from the liveness PR, covered here:

1. **Lifespan** — startup/shutdown ran on the deprecated ``@app.on_event``
   hooks. The migration must preserve the exact guarantees: tables created,
   the startup sweep scheduled off the boot path, the periodic sweep started
   and handed to ``app.state``, and the background thread signalled *and*
   joined on shutdown.
2. **Bounded sweeps** — a sweep could run past a shutdown signal (the join then
   expires while a row lock is still held), and a hung broker inquiry had no
   explicit deadline. The sweep now checks ``stop_event`` between orders, and a
   timed-out inquiry stays inconclusive (RESERVED) — never guessed into a
   terminal status — without stalling the rest of the cycle.
"""
import threading
from datetime import datetime, timedelta

import pytest
import requests
from sqlalchemy.orm import sessionmaker

from backend.database.testing import make_test_engine, StaticPool

from api.database import Base
from api import models  # noqa: F401 - register ORM models
from api.models import QuickTradeOrder, QT_RESERVED, QT_SUBMITTED, User, Credential
from api.services import quick_trade_recovery as rec
from api.services.quick_trade_recovery import recover_reserved_orders, run_sweep_once


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


def _row(odno, side, qty):
    return {"odno": odno, "sll_buy_dvsn_cd": "02" if side == "buy" else "01",
            "ft_ord_qty": str(int(qty))}


class FakeOrders:
    """Broker stub. ``exc_for`` raises for one symbol only, so a single bad
    order can be isolated from the healthy ones in the same sweep."""

    def __init__(self, rows=None, exc=None, exc_for=None):
        self.rows = rows if rows is not None else []
        self.exc = exc
        self.exc_for = exc_for
        self.calls = []

    def inquire_orders(self, symbol, market="us", excd="NASD"):
        self.calls.append(symbol)
        if self.exc is not None and (self.exc_for is None or self.exc_for == symbol):
            raise self.exc
        return self.rows


def _load_kis(orders):
    return lambda cred: (object(), orders, object())


def _seed(db, *, order_id, age_seconds=300, updated_offset=None, symbol="AAPL", qty=10.0):
    now = datetime.utcnow()
    o = QuickTradeOrder(
        id=order_id, user_id=1, credential_id=1,
        idempotency_key=f"key-{order_id}", request_hash=f"h-{order_id}",
        symbol=symbol, side="buy", market="us", exchange="NASD",
        order_type="limit", qty=qty, price=100.0, status=QT_RESERVED,
        created_at=now - timedelta(seconds=age_seconds),
        updated_at=now - timedelta(seconds=updated_offset if updated_offset is not None else age_seconds),
    )
    db.add(o)
    db.commit()
    return o


# ── 1. FastAPI lifespan ───────────────────────────────────────────────────────

def test_app_uses_lifespan_not_deprecated_on_event():
    """The deprecated hooks must be gone — not merely supplemented."""
    from api.main import app

    assert not app.router.on_startup, "startup still registered via @app.on_event"
    assert not app.router.on_shutdown, "shutdown still registered via @app.on_event"
    assert app.router.lifespan_context is not None


def test_lifespan_starts_and_stops_the_periodic_sweep(monkeypatch):
    """Startup must create tables, schedule the boot sweep, start the periodic
    sweep and publish the handle; shutdown must signal *and* join the thread."""
    from fastapi.testclient import TestClient

    import api.main as main_mod

    calls = {"tables": 0, "startup_sweep": 0}
    monkeypatch.setattr(main_mod, "create_tables", lambda: calls.__setitem__("tables", 1))

    stop_event = threading.Event()
    joined = threading.Event()

    class FakeThread:
        def __init__(self):
            self._alive = True

        def join(self, timeout=None):
            joined.set()
            self._alive = False

        def is_alive(self):
            return self._alive

    thread = FakeThread()
    monkeypatch.setattr(rec, "start_periodic_recovery", lambda *a, **kw: (thread, stop_event))
    monkeypatch.setattr(
        rec, "recover_on_startup", lambda *a, **kw: calls.__setitem__("startup_sweep", 1)
    )

    with TestClient(main_mod.app) as client:
        assert client.get("/health").status_code == 200
        assert calls["tables"] == 1
        assert main_mod.app.state.qt_recovery == (thread, stop_event)
        assert not stop_event.is_set()

    assert stop_event.is_set(), "shutdown must signal the sweep to stop"
    assert joined.is_set(), "shutdown must join the sweep thread"


def test_lifespan_survives_a_failing_recovery_start(monkeypatch):
    """Recovery is best-effort: a launcher failure must not break startup, and
    shutdown must tolerate the missing handle."""
    from fastapi.testclient import TestClient

    import api.main as main_mod

    monkeypatch.setattr(main_mod, "create_tables", lambda: None)
    monkeypatch.setattr(
        rec, "start_periodic_recovery",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no thread for you")),
    )

    with TestClient(main_mod.app) as client:
        assert client.get("/health").status_code == 200
        assert main_mod.app.state.qt_recovery is None


# ── 2A. stop_event aborts the sweep between orders ────────────────────────────

def test_sweep_stops_between_orders_when_signalled(db, monkeypatch):
    """A shutdown signal mid-sweep must end the loop at the next row boundary."""
    for i in (1, 2, 3):
        _seed(db, order_id=i, updated_offset=100 * (4 - i), symbol=f"SYM{i}")

    stop = threading.Event()
    original = rec._classify

    def classify_then_signal(order, client):
        stop.set()  # simulate SIGTERM arriving while the first order is handled
        return original(order, client)

    monkeypatch.setattr(rec, "_classify", classify_then_signal)
    summary = recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(rows=[])), stop_event=stop)

    assert summary.seen == 1, "must not keep iterating after the stop signal"
    assert summary.aborted is True, "an interrupted sweep must not read as a clean full pass"
    # Untouched orders keep their state — an abort must never corrupt rows.
    assert db.get(QuickTradeOrder, 2).status == QT_RESERVED
    assert db.get(QuickTradeOrder, 3).status == QT_RESERVED


def test_sweep_not_marked_aborted_when_never_signalled(db):
    """The abort flag stays False on a normal full pass."""
    _seed(db, order_id=1)
    summary = recover_reserved_orders(
        db, load_kis=_load_kis(FakeOrders(rows=[_row("BRK-1", "buy", 10)])),
        stop_event=threading.Event(),
    )
    assert summary.aborted is False
    assert summary.submitted == 1


def test_absent_stop_event_keeps_previous_behaviour(db):
    """``stop_event`` is optional — existing callers must be unaffected."""
    _seed(db, order_id=1)
    summary = recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(rows=[_row("B", "buy", 10)])))
    assert summary.aborted is False
    assert db.get(QuickTradeOrder, 1).status == QT_SUBMITTED


def test_already_completed_work_is_kept_when_aborting(db, monkeypatch):
    """Rows resolved before the signal keep their terminal status."""
    _seed(db, order_id=1, updated_offset=900)
    _seed(db, order_id=2, updated_offset=300)

    stop = threading.Event()
    original = rec._classify

    def classify_and_signal_after_first(order, client):
        out = original(order, client)
        stop.set()  # first order resolves, then shutdown arrives
        return out

    monkeypatch.setattr(rec, "_classify", classify_and_signal_after_first)
    summary = recover_reserved_orders(
        db, load_kis=_load_kis(FakeOrders(rows=[_row("BRK-1", "buy", 10)])), stop_event=stop,
    )

    assert summary.submitted == 1
    assert summary.aborted is True
    assert db.get(QuickTradeOrder, 1).status == QT_SUBMITTED  # committed work survives
    assert db.get(QuickTradeOrder, 2).status == QT_RESERVED   # never reached


def test_abort_is_visible_in_the_summary_log(db, monkeypatch, caplog):
    """An interrupted cycle must be identifiable from logs alone."""
    _seed(db, order_id=1)
    _seed(db, order_id=2)
    stop = threading.Event()
    stop.set()

    with caplog.at_level("INFO"):
        recover_reserved_orders(db, load_kis=_load_kis(FakeOrders()), stop_event=stop)

    assert any("ABORTED" in r.getMessage() for r in caplog.records)


def test_stop_event_is_threaded_through_run_sweep_once(db, SessionLocal, monkeypatch):
    """``run_sweep_once`` must forward the signal, else the periodic loop's
    stop_event never reaches the row iteration."""
    _seed(db, order_id=1)
    seen = {}

    def spy(session, **kwargs):
        seen.update(kwargs)
        return rec.RecoverySummary()

    monkeypatch.setattr(rec, "recover_reserved_orders", spy)
    stop = threading.Event()
    run_sweep_once(session_factory=SessionLocal, load_kis=_load_kis(FakeOrders()),
                   label="test", stop_event=stop)
    assert seen.get("stop_event") is stop


def test_periodic_loop_passes_its_stop_event_to_the_sweep(monkeypatch):
    """The loop's own signal must reach the sweep — otherwise the between-order
    check can never fire in production."""
    seen = {}

    def spy(**kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(rec, "run_sweep_once", spy)
    stop = threading.Event()
    rec.run_periodic_recovery(stop, interval_seconds=1, max_cycles=1)
    assert seen.get("stop_event") is stop


# ── 2B. Broker HTTP timeout: inconclusive, isolated, observable ───────────────

def test_inquiry_timeout_leaves_order_reserved(db):
    """A timeout is inconclusive — never FAILED, never SUBMITTED."""
    _seed(db, order_id=1)
    summary = recover_reserved_orders(
        db, load_kis=_load_kis(FakeOrders(exc=requests.exceptions.Timeout("read timed out"))),
    )

    assert db.get(QuickTradeOrder, 1).status == QT_RESERVED
    assert summary.skipped == 1
    assert summary.skip_reasons == {rec._REASON_INQUIRY_TIMEOUT: 1}


def test_inquiry_timeout_does_not_stall_the_rest_of_the_sweep(db):
    """One hung broker call must not block the other orders in the cycle."""
    _seed(db, order_id=1, updated_offset=900, symbol="SLOW")
    _seed(db, order_id=2, updated_offset=300, symbol="FAST")

    broker = FakeOrders(
        rows=[_row("BRK-2", "buy", 10)],
        exc=requests.exceptions.ConnectTimeout("connect timed out"),
        exc_for="SLOW",
    )
    summary = recover_reserved_orders(db, load_kis=_load_kis(broker))

    assert summary.skip_reasons == {rec._REASON_INQUIRY_TIMEOUT: 1}
    assert summary.submitted == 1
    assert db.get(QuickTradeOrder, 1).status == QT_RESERVED
    assert db.get(QuickTradeOrder, 2).status == QT_SUBMITTED


def test_timeout_is_logged_with_order_context(db, caplog):
    """Structured context, so a timeout is diagnosable from logs alone."""
    _seed(db, order_id=42, symbol="TSLA")
    with caplog.at_level("WARNING"):
        recover_reserved_orders(
            db, load_kis=_load_kis(FakeOrders(exc=requests.exceptions.Timeout("boom"))),
        )
    line = next(r.getMessage() for r in caplog.records if "timed out" in r.getMessage())
    assert "42" in line and "TSLA" in line


def test_non_timeout_inquiry_error_still_classified_separately(db):
    """No regression: a plain inquiry error keeps its own reason."""
    _seed(db, order_id=1)
    summary = recover_reserved_orders(db, load_kis=_load_kis(FakeOrders(exc=RuntimeError("auth"))))
    assert summary.skip_reasons == {rec._REASON_INQUIRY_ERROR: 1}


def test_kis_client_http_timeout_is_configurable(monkeypatch):
    """The deadline comes from the existing KISClient config, not a new client."""
    from kis_adapter import client as kis_client_mod

    assert kis_client_mod.HTTP_TIMEOUT_SECONDS > 0
    monkeypatch.delenv(kis_client_mod.HTTP_TIMEOUT_ENV, raising=False)
    assert kis_client_mod._http_timeout() == kis_client_mod.HTTP_TIMEOUT_SECONDS

    monkeypatch.setenv(kis_client_mod.HTTP_TIMEOUT_ENV, "3")
    assert kis_client_mod._http_timeout() == 3.0

    for bad in ("garbage", "0", "-5"):
        monkeypatch.setenv(kis_client_mod.HTTP_TIMEOUT_ENV, bad)
        assert kis_client_mod._http_timeout() == kis_client_mod.HTTP_TIMEOUT_SECONDS, bad


def test_every_kis_http_call_sends_a_timeout(monkeypatch):
    """No request may leave without a deadline."""
    from kis_adapter import client as kis_client_mod

    sent = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"rt_cd": "0"}

    def fake_get(url, **kw):
        sent["get"] = kw.get("timeout")
        return FakeResp()

    def fake_post(url, **kw):
        sent["post"] = kw.get("timeout")
        return FakeResp()

    monkeypatch.setattr(kis_client_mod.requests, "get", fake_get)
    monkeypatch.setattr(kis_client_mod.requests, "post", fake_post)
    monkeypatch.setenv(kis_client_mod.HTTP_TIMEOUT_ENV, "7")

    client = kis_client_mod.KISClient.__new__(kis_client_mod.KISClient)
    client._limiter = kis_client_mod.RateLimiter(1000)
    client.auth = type("A", (), {
        "get_headers": lambda self, tr: {},
        "get_hashkey": lambda self, body: "hash",
        "base_url": "https://x",
    })()

    client.get("/p", "TR")
    client.post("/p", "TR", {})
    assert sent == {"get": 7.0, "post": 7.0}
