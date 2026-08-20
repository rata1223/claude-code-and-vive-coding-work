"""P0 — request-scoped KR order cancel.

``KISOrders`` already had ``cancel_us``; the KR side (``TTTC0803U`` /
``VTTC0803U``) existed **only** inline inside ``KISBroker.cancel_order``, which
is reached through the ``get_kis_broker()`` process singleton built from
``os.environ``. QuickTrade is multi-tenant — every handler builds clients from
the caller's stored credential via ``_load_kis(cred)`` and never touches the
environment (P0-03) — so cancelling through the singleton would pull an order on
*the process's* account rather than the caller's.

This adds the missing KR method to the request-scoped adapter, mirroring the
body already proven in the broker. It deliberately returns the **raw response
dict**, not a bool: the broker's version swallows every failure into ``False``,
and a caller that cannot see ``rt_cd``/``msg1`` cannot tell "cancelled" from
"the broker refused" — which on a cancel is the difference between an order
being gone and still resting in the market.
"""
import pytest

from kis_adapter.orders import KISOrders


class _FakeAuth:
    def __init__(self, env):
        self.env = env

    def require_account(self):
        return "1234567801"


class _FakeClient:
    """Records the outbound call instead of making it. No network, no broker."""

    def __init__(self, env="paper", response=None):
        self.auth = _FakeAuth(env)
        self.posts = []
        self._response = response if response is not None else {"rt_cd": "0"}

    def post(self, path, tr_id, body):
        self.posts.append({"path": path, "tr_id": tr_id, "body": body})
        return self._response


def _orders(env="paper", response=None):
    return KISOrders(client=_FakeClient(env=env, response=response))


def test_cancel_kr_posts_to_the_domestic_rvsecncl_path():
    orders = _orders()

    orders.cancel_kr("ODNO-1", "069500", 7, 9000.0)

    (call,) = orders._client.posts
    assert call["path"] == "/uapi/domestic-stock/v1/trading/order-rvsecncl"


def test_cancel_kr_uses_the_paper_tr_in_paper_env():
    orders = _orders(env="paper")

    orders.cancel_kr("ODNO-1", "069500", 7, 9000.0)

    assert orders._client.posts[0]["tr_id"] == "VTTC0803U"


def test_cancel_kr_uses_the_real_tr_in_real_env():
    """A paper TR sent against a real account is a silently failing cancel."""
    orders = _orders(env="real")

    orders.cancel_kr("ODNO-1", "069500", 7, 9000.0)

    assert orders._client.posts[0]["tr_id"] == "TTTC0803U"


def test_cancel_kr_marks_the_request_as_a_cancel_of_the_whole_order():
    orders = _orders()

    orders.cancel_kr("ODNO-42", "069500", 7, 9000.0)

    body = orders._client.posts[0]["body"]
    assert body["ORGN_ODNO"] == "ODNO-42"
    assert body["RVSE_CNCL_DVSN_CD"] == "02"   # 02 = 취소 (not 정정)
    assert body["QTY_ALL_ORD_YN"] == "Y"       # cancel the full resting quantity


def test_cancel_kr_splits_the_account_number():
    orders = _orders()

    orders.cancel_kr("ODNO-1", "069500", 7, 9000.0)

    body = orders._client.posts[0]["body"]
    assert body["CANO"] == "12345678"
    assert body["ACNT_PRDT_CD"] == "01"


def test_cancel_kr_returns_the_raw_response_not_a_bool():
    """The caller must be able to read rt_cd/msg1 and refuse to report success
    on a broker refusal."""
    refusal = {"rt_cd": "1", "msg1": "취소할 수 있는 수량이 없습니다"}
    orders = _orders(response=refusal)

    result = orders.cancel_kr("ODNO-1", "069500", 7, 9000.0)

    assert result == refusal


def test_cancel_kr_does_not_swallow_transport_errors():
    """A raised transport error must reach the caller. Returning False here is
    what makes the broker version unable to distinguish failure modes."""
    class _Boom(_FakeClient):
        def post(self, path, tr_id, body):
            raise RuntimeError("connection reset")

    orders = KISOrders(client=_Boom())

    with pytest.raises(RuntimeError):
        orders.cancel_kr("ODNO-1", "069500", 7, 9000.0)


def test_cancel_kr_reads_no_process_environment(monkeypatch):
    """Tenancy guard: the account must come from the injected credential."""
    monkeypatch.setenv("KIS_ACCOUNT_NO", "9999999999")
    orders = _orders()

    orders.cancel_kr("ODNO-1", "069500", 7, 9000.0)

    body = orders._client.posts[0]["body"]
    assert body["CANO"] == "12345678", "account must come from the injected client"
