"""P1 — the KIS client must obey KIS's own limits, not its own instance's.

Two defects, one file, one theme: ``kis_adapter/client.py`` did not honour the
rules KIS actually enforces.

**Rate limiting was per-instance.** ``RateLimiter`` holds a single
``_last_call``, and ``KISClient.__init__`` built a fresh one per instance. But
``api/routers/quick_trade.py`` and ``api/routers/dashboard.py`` construct a new
``KISClient`` on *every request* — that is deliberate (P0-03 credential
isolation) and not changing. A fresh instance means ``_last_call = 0``, so N
concurrent requests waited zero times between them. KIS limits per **app key**
(paper 5/s, real 15/s) and answers ``EGW00201`` when exceeded.

The intent was already written down: ``backend/brokers/kis.py`` keeps a process
singleton whose comment says "rate-limit tracking must be shared across all
callers". The bot path honoured it; the API path bypassed it.

**GET retried decisions, not failures.** ``rt_cd != "0"`` is the broker saying
no. It was raised *inside* the ``try``, so the generic ``except`` retried it
three times, spending rate budget and 2s of latency to be told the same thing.
``POST`` already got this right and says so in a comment; only ``GET`` lacked
the discipline.

No network: ``requests`` and ``time.sleep`` are patched throughout.
"""
import pytest

from kis_adapter import client as client_mod
from kis_adapter.auth import KISCredentials


PAPER = KISCredentials(app_key="APPKEY-A", app_secret="s", account_no="1234567890AB", env="paper")
PAPER_SAME = KISCredentials(app_key="APPKEY-A", app_secret="s", account_no="1234567890AB", env="paper")
PAPER_OTHER = KISCredentials(app_key="APPKEY-B", app_secret="s", account_no="1234567890AB", env="paper")
REAL_SAME_KEY = KISCredentials(app_key="APPKEY-A", app_secret="s", account_no="1234567890AB", env="real")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Never actually wait — the tests assert on wiring, not wall clock."""
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


# ── the limiter is scoped to the app key, not the instance ───────────────────

def test_two_clients_for_one_app_key_share_a_limiter():
    """The defect: every request built its own limiter, so the limit never
    applied across concurrent requests."""
    a = client_mod.KISClient(PAPER)
    b = client_mod.KISClient(PAPER_SAME)

    assert a._limiter is b._limiter


def test_a_different_app_key_gets_its_own_limiter():
    """KIS meters per app key. Sharing one limiter across tenants would make
    every user wait for every other user."""
    a = client_mod.KISClient(PAPER)
    other = client_mod.KISClient(PAPER_OTHER)

    assert a._limiter is not other._limiter


def test_paper_and_real_are_metered_separately():
    """Same app key, different endpoint and different ceiling (5/s vs 15/s).
    One limiter for both would apply the wrong interval to one of them."""
    paper = client_mod.KISClient(PAPER)
    real = client_mod.KISClient(REAL_SAME_KEY)

    assert paper._limiter is not real._limiter
    assert paper._limiter.min_interval == pytest.approx(1.0 / 5)
    assert real._limiter.min_interval == pytest.approx(1.0 / 15)


def test_the_registry_never_holds_a_raw_app_key():
    """The key is a credential fingerprint, not the secret itself. Reuses
    ``KISAuth._fp`` — the same app-key+env hash the token cache already keys
    on — rather than inventing a second notion of credential identity."""
    client_mod.KISClient(PAPER)

    joined = " ".join(str(k) for k in client_mod._LIMITERS)
    assert "APPKEY-A" not in joined


# ── GET: retry failures, never decisions ─────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_get_does_not_retry_a_broker_rejection(monkeypatch):
    """``rt_cd != "0"`` is an answer. Asking again spends rate budget to hear
    it twice more — and for ``EGW00133`` (token issuance, 1/min) or
    ``EGW00201`` (rate exceeded) the retry is actively harmful."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _Resp({"rt_cd": "1", "msg1": "모의투자 주문이 불가합니다"})

    monkeypatch.setattr(client_mod.requests, "get", fake_get)
    c = client_mod.KISClient(PAPER)
    monkeypatch.setattr(c.auth, "get_headers", lambda tr_id: {})

    with pytest.raises(RuntimeError) as exc:
        c.get("/uapi/whatever", "TR", {})

    assert len(calls) == 1, f"a broker decision must not be retried (got {len(calls)} calls)"
    assert "모의투자 주문이 불가합니다" in str(exc.value)


def test_get_still_retries_a_network_failure(monkeypatch):
    """The guard must not remove the retry that exists for real transport
    failures."""
    calls = []

    def flaky_get(url, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            raise ConnectionError("boom")
        return _Resp({"rt_cd": "0", "output": {"ok": True}})

    monkeypatch.setattr(client_mod.requests, "get", flaky_get)
    c = client_mod.KISClient(PAPER)
    monkeypatch.setattr(c.auth, "get_headers", lambda tr_id: {})

    data = c.get("/uapi/whatever", "TR", {})

    assert len(calls) == 3
    assert data["output"] == {"ok": True}


def test_get_gives_up_after_max_retries_on_network_failure(monkeypatch):
    calls = []

    def dead_get(url, **kwargs):
        calls.append(url)
        raise ConnectionError("boom")

    monkeypatch.setattr(client_mod.requests, "get", dead_get)
    c = client_mod.KISClient(PAPER)
    monkeypatch.setattr(c.auth, "get_headers", lambda tr_id: {})

    with pytest.raises(ConnectionError):
        c.get("/uapi/whatever", "TR", {})

    assert len(calls) == client_mod.KISClient.MAX_RETRIES


def test_a_successful_get_returns_the_payload(monkeypatch):
    monkeypatch.setattr(client_mod.requests, "get",
                        lambda url, **kw: _Resp({"rt_cd": "0", "output": [1, 2]}))
    c = client_mod.KISClient(PAPER)
    monkeypatch.setattr(c.auth, "get_headers", lambda tr_id: {})

    assert c.get("/uapi/whatever", "TR", {})["output"] == [1, 2]


# ── POST keeps the behaviour it already had ──────────────────────────────────

def test_post_still_does_not_retry_a_rejection(monkeypatch):
    """POST was already correct; this pins it so the shared refactor cannot
    regress the side that got it right."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _Resp({"rt_cd": "1", "msg1": "주문가능금액이 부족합니다"})

    monkeypatch.setattr(client_mod.requests, "post", fake_post)
    c = client_mod.KISClient(PAPER)
    monkeypatch.setattr(c.auth, "get_headers", lambda tr_id: {})
    monkeypatch.setattr(c.auth, "get_hashkey", lambda body: "HASH")

    with pytest.raises(RuntimeError):
        c.post("/uapi/order", "TR", {"PDNO": "AAPL"})

    assert len(calls) == 1


# ── review findings: a rate-exceeded read IS transient ───────────────────────

def test_get_retries_a_rate_exceeded_rejection(monkeypatch):
    """``EGW00201`` means "you are going too fast". Waiting is the remedy, so
    this is the one ``rt_cd`` that must still be retried.

    The limiter above is per **process**, and the same app key is driven by the
    ``api`` container, ``kis-worker`` and the bot at once — so EGW00201 stays
    reachable no matter how well one process paces itself. Treating it as a
    final answer turns a throttle into a failed balance lookup, and
    ``_live_position_row`` rejects the user's close order on the back of it.
    """
    calls = []

    def throttled_then_ok(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return _Resp({"rt_cd": "1", "msg_cd": "EGW00201",
                          "msg1": "초당 거래건수를 초과하였습니다."})
        return _Resp({"rt_cd": "0", "output": {"ok": True}})

    monkeypatch.setattr(client_mod.requests, "get", throttled_then_ok)
    c = client_mod.KISClient(PAPER)
    monkeypatch.setattr(c.auth, "get_headers", lambda tr_id: {})

    data = c.get("/uapi/whatever", "TR", {})

    assert len(calls) == 2, "a throttle must be waited out, not surfaced"
    assert data["output"] == {"ok": True}


def test_get_gives_up_on_a_persistent_throttle(monkeypatch):
    """Retrying is bounded by MAX_RETRIES like any other transient failure."""
    calls = []

    def always_throttled(url, **kwargs):
        calls.append(url)
        return _Resp({"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수 초과"})

    monkeypatch.setattr(client_mod.requests, "get", always_throttled)
    c = client_mod.KISClient(PAPER)
    monkeypatch.setattr(c.auth, "get_headers", lambda tr_id: {})

    with pytest.raises(RuntimeError):
        c.get("/uapi/whatever", "TR", {})

    assert len(calls) == client_mod.KISClient.MAX_RETRIES


def test_a_business_rejection_is_still_not_retried(monkeypatch):
    """The distinction has to be the error code, not "any rt_cd". An
    insufficient-funds answer does not become true by asking again."""
    calls = []

    monkeypatch.setattr(client_mod.requests, "get",
                        lambda url, **kw: (calls.append(url),
                                           _Resp({"rt_cd": "1", "msg_cd": "40580000",
                                                  "msg1": "조회할 자료가 없습니다"}))[1])
    c = client_mod.KISClient(PAPER)
    monkeypatch.setattr(c.auth, "get_headers", lambda tr_id: {})

    with pytest.raises(RuntimeError):
        c.get("/uapi/whatever", "TR", {})

    assert len(calls) == 1


# ── review findings: the shared limiter must be well-behaved ─────────────────

def test_the_limiter_uses_a_monotonic_clock():
    """``_last_call`` now lives for the process's lifetime rather than one
    request. With a wall clock, a backward NTP step of N seconds becomes an
    N-second sleep that stalls every call on that credential."""
    import time as _time

    limiter = client_mod.RateLimiter(5)
    limiter.wait()

    # The two clocks are ~1.7e9 apart, so proximity identifies which was used.
    # Asserting only "later than monotonic()" would pass on a wall clock too.
    assert abs(limiter._last_call - _time.monotonic()) < 1.0, (
        "_last_call must come from time.monotonic(), not time.time()"
    )
    assert abs(limiter._last_call - _time.time()) > 1.0, (
        "_last_call still tracks the wall clock"
    )


def test_the_limiter_does_not_sleep_holding_its_lock(monkeypatch):
    """Sleeping under the mutex serialises every waiter behind the sleeper
    instead of merely pacing them. These handlers are sync FastAPI endpoints
    running on a bounded threadpool — including ``GET /health``, so a credential
    being throttled must not be able to fail the container's healthcheck."""
    held = []

    limiter = client_mod.RateLimiter(5)
    limiter.wait()  # prime _last_call so the next call must wait

    def checking_sleep(_seconds):
        held.append(limiter._lock.acquire(blocking=False))
        if held[-1]:
            limiter._lock.release()

    monkeypatch.setattr(client_mod.time, "sleep", checking_sleep)
    limiter.wait()

    assert held and all(held), "the lock must be released before sleeping"
