"""An order that may already have been accepted must never be re-sent.

``KISClient.post()`` is the order-submission path: ``buy_us``/``sell_us``/
``buy_kr``/``sell_kr`` and the two cancel calls all go through it. It retried
up to ``MAX_RETRIES`` on **any** exception — transport failures and HTTP error
statuses alike. If KIS accepted an order and only the response was lost, the
retry sends it again: a duplicate order, on a real account.

What makes this more than a general "retries are risky" worry is that the layer
above already solved it and this retry defeats the solution:

* ``api/models.py`` — ``QT_RESERVED`` is a *durable idempotency reservation,
  pre/awaiting broker*, committed **before** the broker is called, with no
  transition edge out of it because "the broker was never told".
* ``api/services/quick_trade_service.py`` — ``reserve_and_submit`` invokes the
  broker **at most once per reservation**, and on any non-``RuntimeError``
  exception keeps the row ``RESERVED``: *"the broker may or may not have
  received the order... Never blindly retry the broker here."*
* ``kis_adapter/orders.py`` — ``inquire_orders()`` exists to resolve exactly
  that state by asking the broker what actually landed.

So "submitted, outcome unknown" is modelled precisely, recovered deliberately,
and resolved by inquiry. ``post()`` retrying *underneath* that means one
reservation can put three orders on the wire before the service ever sees the
failure. The fix is not a new mechanism — it is to stop overriding the one
that exists.

A broker *decision* (``rt_cd != "0"``) is unaffected: it is an answer, already
terminal, and stays terminal.

No network, no KIS, no orders — ``requests`` and ``time.sleep`` are patched.
"""
import pytest
import requests

from kis_adapter import client as client_mod
from kis_adapter.auth import KISCredentials


PAPER = KISCredentials(app_key="APPKEY-A", app_secret="s",
                       account_no="1234567890AB", env="paper")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Never actually wait — these assert on wiring, not wall clock."""
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_a, **_k: None)


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Each test starts from an empty limiter registry."""
    reg = getattr(client_mod, "_LIMITERS", None)
    if reg is not None:
        reg.clear()
    yield
    if reg is not None:
        reg.clear()


class _Resp:
    """A ``requests`` response double that fails the way the real one does."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for url", response=self)

    def json(self):
        return self._payload


def _client(monkeypatch):
    """A paper client with token issuance and hashkey stubbed out."""
    c = client_mod.KISClient(PAPER)
    monkeypatch.setattr(c.auth, "get_headers", lambda tr_id: {})
    monkeypatch.setattr(c.auth, "get_hashkey", lambda body: "HASH")
    return c


def _posts_recording(monkeypatch, behaviour):
    """Patch ``requests.post`` with ``behaviour(call_number)``, recording calls."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return behaviour(len(calls))

    monkeypatch.setattr(client_mod.requests, "post", fake_post)
    return calls


ORDER_BODY = {"PDNO": "AAPL", "ORD_QTY": "1"}


# ── an indeterminate outcome must not become a second order ──────────────────

def test_post_does_not_resend_after_a_timeout(monkeypatch):
    """A read timeout is the dangerous case: the request was written, and the
    answer was lost. The order may be live. Sending it again risks doubling a
    real position, so the exception goes up and the reservation stays open."""
    def times_out(_n):
        raise requests.Timeout("read timed out")

    calls = _posts_recording(monkeypatch, times_out)
    c = _client(monkeypatch)

    with pytest.raises(requests.Timeout):
        c.post("/uapi/order", "TR", ORDER_BODY)

    assert len(calls) == 1, (
        f"an order must be sent once, never re-sent (got {len(calls)} sends)")


def test_post_does_not_resend_on_a_server_error(monkeypatch):
    """A 503 from the gateway says nothing about whether KIS booked the order.
    ``get`` retries this — deliberately, because a read can be repeated. An
    order cannot."""
    calls = _posts_recording(monkeypatch, lambda _n: _Resp({}, status_code=503))
    c = _client(monkeypatch)

    with pytest.raises(requests.HTTPError):
        c.post("/uapi/order", "TR", ORDER_BODY)

    assert len(calls) == 1


def test_post_does_not_resend_on_a_connection_error(monkeypatch):
    """Even a refused connection — where the order almost certainly never
    landed — is sent once. "Almost certainly" is not a basis for re-sending an
    order, and the reservation makes one attempt recoverable anyway."""
    def refused(_n):
        raise requests.ConnectionError("connection refused")

    calls = _posts_recording(monkeypatch, refused)
    c = _client(monkeypatch)

    with pytest.raises(requests.ConnectionError):
        c.post("/uapi/order", "TR", ORDER_BODY)

    assert len(calls) == 1


def test_post_does_not_resend_on_a_malformed_body(monkeypatch):
    """A 200 with an unparseable body is the worst case of all — KIS answered,
    so the order probably *did* land, and we cannot read the order number."""
    class _Garbage(_Resp):
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    calls = _posts_recording(monkeypatch, lambda _n: _Garbage({}))
    c = _client(monkeypatch)

    with pytest.raises(ValueError):
        c.post("/uapi/order", "TR", ORDER_BODY)

    assert len(calls) == 1


# ── behaviour that must not change ───────────────────────────────────────────

def test_a_broker_rejection_is_unchanged(monkeypatch):
    """``rt_cd != "0"`` was already terminal and stays terminal — the service
    maps this ``RuntimeError`` to ``QT_REJECTED``."""
    calls = _posts_recording(
        monkeypatch,
        lambda _n: _Resp({"rt_cd": "1", "msg1": "주문가능금액이 부족합니다"}))
    c = _client(monkeypatch)

    with pytest.raises(RuntimeError) as exc:
        c.post("/uapi/order", "TR", ORDER_BODY)

    assert len(calls) == 1
    assert "주문가능금액이 부족합니다" in str(exc.value)


def test_the_market_closed_branch_is_unchanged(monkeypatch):
    """A closed-market rejection still raises ``MarketClosedError`` rather than
    a bare ``RuntimeError``; callers branch on it."""
    from backend.data.calendar import MarketClosedError

    calls = _posts_recording(
        monkeypatch,
        lambda _n: _Resp({"rt_cd": "-90", "msg1": "거래가능시간이 아닙니다"}))
    c = _client(monkeypatch)

    with pytest.raises(MarketClosedError):
        c.post("/uapi/order", "TR", ORDER_BODY)

    assert len(calls) == 1


def test_a_successful_order_is_unchanged(monkeypatch):
    """The happy path must be untouched — one send, payload returned."""
    calls = _posts_recording(
        monkeypatch,
        lambda _n: _Resp({"rt_cd": "0", "output": {"ODNO": "0000123456"}}))
    c = _client(monkeypatch)

    data = c.post("/uapi/order", "TR", ORDER_BODY)

    assert len(calls) == 1
    assert data["output"]["ODNO"] == "0000123456"


def test_the_rate_limiter_still_paces_an_order(monkeypatch):
    """Removing the retry loop must not remove the limiter wait with it."""
    waited = []
    calls = _posts_recording(monkeypatch, lambda _n: _Resp({"rt_cd": "0"}))
    c = _client(monkeypatch)
    monkeypatch.setattr(c._limiter, "wait", lambda: waited.append(True))

    c.post("/uapi/order", "TR", ORDER_BODY)

    assert waited == [True]
    assert len(calls) == 1


def test_the_hashkey_is_still_sent(monkeypatch):
    """KIS requires a hashkey header on order POSTs."""
    seen = {}

    def capture(url, **kwargs):
        seen.update(kwargs.get("headers") or {})
        return _Resp({"rt_cd": "0"})

    monkeypatch.setattr(client_mod.requests, "post", capture)
    c = _client(monkeypatch)

    c.post("/uapi/order", "TR", ORDER_BODY)

    assert seen.get("hashkey") == "HASH"


# ── a cancel IS replayable, and must keep its retry ──────────────────────────
#
# Review finding: dropping the retry for *everything* that goes through
# ``post()`` would have made a pre-existing gap easier to hit.
# ``Reconciler._mark_order_lost`` sets ``status = CANCELED`` whether or not the
# cancel raised, and ``OrderFillPoller._handle_timeout_locked`` pops the entry
# from polling *before* it cancels. So a cancel lost to one transient blip
# leaves a live resting order at KIS recorded as dead and no longer watched.
#
# A cancel is keyed to an existing ``ORGN_ODNO`` with ``QTY_ALL_ORD_YN=Y``, so
# re-sending cancels the same order again — the second attempt draws an
# ``rt_cd`` error, never a second effect. That is what makes it safe to retry
# when a new order is not.

def test_a_cancel_is_retried_on_a_transient_failure(monkeypatch):
    def flaky(n):
        if n < 3:
            raise requests.ConnectionError("boom")
        return _Resp({"rt_cd": "0", "output": {"ODNO": "0000123456"}})

    calls = _posts_recording(monkeypatch, flaky)
    c = _client(monkeypatch)

    data = c.post("/uapi/cancel", "TR", ORDER_BODY, idempotent=True)

    assert len(calls) == 3
    assert data["rt_cd"] == "0"


def test_a_cancel_is_retried_on_a_server_error(monkeypatch):
    calls = _posts_recording(
        monkeypatch,
        lambda n: _Resp({}, status_code=503) if n == 1
        else _Resp({"rt_cd": "0"}))
    c = _client(monkeypatch)

    c.post("/uapi/cancel", "TR", ORDER_BODY, idempotent=True)

    assert len(calls) == 2


def test_a_cancel_gives_up_after_max_retries(monkeypatch):
    def dead(_n):
        raise requests.ConnectionError("boom")

    calls = _posts_recording(monkeypatch, dead)
    c = _client(monkeypatch)

    with pytest.raises(requests.ConnectionError):
        c.post("/uapi/cancel", "TR", ORDER_BODY, idempotent=True)

    assert len(calls) == client_mod.KISClient.MAX_RETRIES


def test_a_cancel_does_not_retry_a_broker_rejection(monkeypatch):
    """Replayable does not mean "retry anything" — an ``rt_cd`` answer (for a
    cancel, typically "already filled/canceled") is still final."""
    calls = _posts_recording(
        monkeypatch,
        lambda _n: _Resp({"rt_cd": "1", "msg1": "정정취소할 수량이 없습니다"}))
    c = _client(monkeypatch)

    with pytest.raises(RuntimeError):
        c.post("/uapi/cancel", "TR", ORDER_BODY, idempotent=True)

    assert len(calls) == 1


def test_replaying_is_opt_in_so_a_new_call_site_cannot_get_it_by_accident():
    """The default must be the unsafe-to-replay case. If the flag ever flips to
    default-True, every future order call site silently becomes re-sendable."""
    import inspect

    sig = inspect.signature(client_mod.KISClient.post)
    param = sig.parameters["idempotent"]

    assert param.default is False
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "keyword-only, so it can never be set by positional accident")


def test_the_cancel_call_sites_opt_in(monkeypatch):
    """The wiring, not just the mechanism: the two KIS cancel methods must
    actually pass the flag, or the retry above protects nothing."""
    from kis_adapter.orders import KISOrders

    seen = []

    class _SpyClient:
        auth = type("A", (), {"env": "paper", "require_account": lambda self: "1234567890AB"})()
        MAX_RETRIES = 3

        def post(self, path, tr_id, body, *, idempotent=False):
            seen.append((path, idempotent))
            return {"rt_cd": "0"}

    orders = KISOrders(client=_SpyClient())
    orders.cancel_us("0001", "AAPL", "NASD", 1, 100.0)
    orders.cancel_kr("0002", "069500", 1, 10000)

    assert [flag for _p, flag in seen] == [True, True], (
        "both cancels must opt into replay")


def test_an_order_call_site_does_not_opt_in(monkeypatch):
    """The other half: buy/sell must NOT be marked replayable."""
    from kis_adapter.orders import KISOrders

    seen = []

    class _SpyClient:
        auth = type("A", (), {"env": "paper", "require_account": lambda self: "1234567890AB"})()
        MAX_RETRIES = 3

        def post(self, path, tr_id, body, *, idempotent=False):
            seen.append(idempotent)
            return {"rt_cd": "0"}

    orders = KISOrders(client=_SpyClient())
    orders.buy_us("AAPL", "NASD", 1, 100.0)
    orders.sell_us("AAPL", "NASD", 1, 100.0)
    orders.buy_kr("069500", 1, 10000)
    orders.sell_kr("069500", 1, 10000)

    assert seen == [False, False, False, False], (
        "a new order must never be marked replayable")


# ── the service layer treats a single failed send as recoverable ─────────────

def test_an_indeterminate_send_leaves_the_row_reserved():
    """The end of the chain: one send, it fails, and the order row stays
    ``RESERVED`` so ``inquire_orders()`` can resolve what actually landed.

    This is the behaviour the retry was quietly overriding — it is asserted
    here to pin that the client change and the service contract line up.
    """
    from api.models import QT_RESERVED
    from api.services import quick_trade_service as svc

    sends = []

    def broker_submit():
        sends.append(1)
        raise requests.Timeout("read timed out")

    captured = {}

    class _FakeDb:
        def add(self, obj):
            captured["order"] = obj

        def commit(self):
            pass

        def refresh(self, obj):
            pass

    order = svc.reserve_and_submit(
        _FakeDb(),
        user_id=1,
        credential_id=1,
        request={"symbol": "AAPL", "side": "buy", "qty": 1, "price": 100.0},
        idempotency_key="k-1",
        request_hash="h-1",
        risk_gate=lambda: None,
        broker_submit=broker_submit,
        extract_order_id=lambda r: "",
    )

    assert sends == [1], "the broker is called exactly once per reservation"
    assert order.status == QT_RESERVED, (
        "an indeterminate send must stay recoverable, not become failed")
