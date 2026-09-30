"""KIS inquiries follow ``tr_cont`` to the last page (CLAUDE.md known issue 6).

Seven inquiries sent empty ``CTX_AREA_*`` keys and never read the continuation,
so past the first page a position list or order history came back truncated
without a word: holdings the risk checks never saw, an order the reconciler
called absent. No network: pages are scripted, ``requests`` is patched.
"""
import pytest

from kis_adapter import client as client_mod
from kis_adapter.auth import KISCredentials
from kis_adapter.pagination import MAX_PAGES, get_all_pages


class _Pages:
    """A client whose ``get_page`` plays back ``(body, tr_cont)`` pages."""

    def __init__(self, pages, env="paper"):
        self.pages = list(pages)
        self.calls = []                                  # (params, tr_cont)
        self.auth = type("A", (), {
            "env": env, "require_account": staticmethod(lambda: "1234567890AB"),
            "account_no": "1234567890AB"})()

    def get_page(self, path, tr_id, params, tr_cont=""):
        self.calls.append((dict(params), tr_cont))
        return self.pages.pop(0)

    def get(self, path, tr_id, params):
        raise AssertionError("a paginated inquiry used the single-page get()")


def _two_pages(list_key, ctx, first_rows, second_rows, summary=None):
    first = {list_key: first_rows, f"ctx_area_fk{ctx}": "FK-1   ",
             f"ctx_area_nk{ctx}": "NK-1   "}
    second = {list_key: second_rows, f"ctx_area_fk{ctx}": "", f"ctx_area_nk{ctx}": ""}
    if summary is not None:
        first["output2"] = summary
        second["output2"] = {"tot_evlu_amt": "999"}      # must not replace page 1
    return [(first, "M"), (second, "D")]


def _assert_continued(client, ctx):
    (p1, c1), (p2, c2) = client.calls
    assert c1 == "" and c2 == "N"
    assert p1[f"CTX_AREA_FK{ctx}"] == "" and p1[f"CTX_AREA_NK{ctx}"] == ""
    assert p2[f"CTX_AREA_FK{ctx}"] == "FK-1   " and p2[f"CTX_AREA_NK{ctx}"] == "NK-1   "


# ── the helper ─────────────────────────────────────────────────────────────

class TestGetAllPages:
    def test_a_single_page_is_one_call(self):
        c = _Pages([({"output": [1, 2]}, "D")])
        assert get_all_pages(c, "/p", "TR", {}, ctx="200", list_key="output")["output"] == [1, 2]
        assert len(c.calls) == 1

    def test_rows_from_every_page_and_the_first_pages_summary(self):
        c = _Pages(_two_pages("output1", "100", [1], [2, 3], summary={"tot_evlu_amt": "5"}))
        out = get_all_pages(c, "/p", "TR", {"CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
                            ctx="100", list_key="output1")
        assert out["output1"] == [1, 2, 3]
        assert out["output2"] == {"tot_evlu_amt": "5"}
        _assert_continued(c, "100")

    def test_f_also_means_more(self):
        c = _Pages([({"output": [1], "ctx_area_nk200": "K"}, "F"), ({"output": [2]}, "E")])
        assert get_all_pages(c, "/p", "TR", {}, ctx="200", list_key="output")["output"] == [1, 2]

    def test_more_without_a_key_raises_rather_than_truncating(self):
        c = _Pages([({"output": [1], "ctx_area_fk200": "  ", "ctx_area_nk200": ""}, "M")])
        with pytest.raises(RuntimeError, match="연속 키"):
            get_all_pages(c, "/p", "TR", {}, ctx="200", list_key="output")

    def test_too_many_pages_raises_rather_than_truncating(self):
        page = ({"output": [1], "ctx_area_nk200": "K"}, "M")
        c = _Pages([page] * (MAX_PAGES + 1))
        with pytest.raises(RuntimeError, match="페이지를 넘었"):
            get_all_pages(c, "/p", "TR", {}, ctx="200", list_key="output")
        assert len(c.calls) == MAX_PAGES

    def test_a_later_page_without_the_row_key_raises(self):
        """CodeRabbit: a missing key used to count as no rows on that page."""
        c = _Pages([({"output": [1], "ctx_area_nk200": "K"}, "M"), ({"rt_cd": "0"}, "D")])
        with pytest.raises(RuntimeError, match="다음 페이지에 행 목록"):
            get_all_pages(c, "/p", "TR", {}, ctx="200", list_key="output")

    def test_a_later_page_with_an_empty_row_list_is_fine(self):
        c = _Pages([({"output": [1], "ctx_area_nk200": "K"}, "M"), ({"output": []}, "D")])
        assert get_all_pages(c, "/p", "TR", {}, ctx="200", list_key="output")["output"] == [1]

    def test_a_single_object_row_is_a_list_of_one(self):
        c = _Pages([({"output": {"odno": "1"}}, "D")])
        assert get_all_pages(c, "/p", "TR", {}, ctx="200",
                             list_key="output")["output"] == [{"odno": "1"}]

    def test_missing_rows_are_an_empty_list(self):
        c = _Pages([({"rt_cd": "0"}, "D")])
        assert get_all_pages(c, "/p", "TR", {}, ctx="200", list_key="output")["output"] == []


class TestCandidateRowKeys:
    """Code review: KR order inquiries accept rows under ``output1`` or
    ``output``; later pages must follow whichever key page 1 used."""

    def test_the_key_page_one_uses_is_merged(self):
        c = _Pages(_two_pages("output", "100", [1], [2]))
        out = get_all_pages(c, "/p", "TR", {}, ctx="100", list_key=("output1", "output"))
        assert out["output"] == [1, 2]

    def test_a_continuation_with_no_known_row_key_raises(self):
        c = _Pages([({"rows": [1], "ctx_area_nk100": "K"}, "M")])
        with pytest.raises(RuntimeError, match="행 목록"):
            get_all_pages(c, "/p", "TR", {}, ctx="100", list_key=("output1", "output"))

    def test_kr_order_inquiry_under_output(self):
        from kis_adapter.orders import KISOrders
        c = _Pages(_two_pages("output", "100", [{"odno": "1"}], [{"odno": "2"}]))
        assert KISOrders(client=c).inquire_orders("005930", market="kr") == [
            {"odno": "1"}, {"odno": "2"}]

    def test_broker_kr_order_on_page_two_under_output(self):
        c = _Pages(_two_pages("output", "100", [_order_row("A")], [_order_row("B")]))
        order = _broker(c)._get_kr_order_status("B")
        assert order is not None and order.id == "B"


# ── the client sends and reads tr_cont ─────────────────────────────────────

class _Resp:
    status_code = 200

    def __init__(self, payload, tr_cont):
        self._payload = payload
        self.headers = {"tr_cont": tr_cont}

    def json(self):
        return self._payload


def test_get_page_sends_and_returns_tr_cont(monkeypatch):
    c = client_mod.KISClient(KISCredentials(app_key="K-PAGE", app_secret="s",
                                            account_no="1234567890AB", env="paper"))
    monkeypatch.setattr(c.auth, "get_headers", lambda tr_id: {"tr_id": tr_id})
    sent = []

    def fake_get(url, headers=None, params=None, timeout=None):
        sent.append(dict(headers))
        return _Resp({"rt_cd": "0"}, "M ")

    monkeypatch.setattr(client_mod.requests, "get", fake_get)
    assert c.get_page("/p", "TR", {}, "") == ({"rt_cd": "0"}, "M")
    assert "tr_cont" not in sent[0]                     # first page: no header
    c.get_page("/p", "TR", {}, "N")
    assert sent[1]["tr_cont"] == "N"


# ── every inquiry follows the pages ────────────────────────────────────────

def test_us_balance(monkeypatch):
    from kis_adapter.portfolio import KISPortfolio
    c = _Pages(_two_pages("output1", "200", [{"s": "A"}], [{"s": "B"}], summary={"x": 1}))
    out = KISPortfolio(c).get_us_balance()
    assert out["positions"] == [{"s": "A"}, {"s": "B"}] and out["summary"] == {"x": 1}
    _assert_continued(c, "200")


def test_kr_balance():
    from kis_adapter.portfolio import KISPortfolio
    c = _Pages(_two_pages("output1", "100", [{"s": "A"}], [{"s": "B"}], summary=[{"x": 1}]))
    out = KISPortfolio(c).get_kr_balance()
    assert out["positions"] == [{"s": "A"}, {"s": "B"}] and out["summary"] == [{"x": 1}]
    _assert_continued(c, "100")


def test_us_pending_orders():
    from kis_adapter.market_data import KISMarketData
    c = _Pages(_two_pages("output", "200", [{"odno": "1"}], [{"odno": "2"}]))
    assert KISMarketData(c).get_pending_us("1234567890AB") == [{"odno": "1"}, {"odno": "2"}]
    _assert_continued(c, "200")


def test_kr_order_inquiry():
    from kis_adapter.orders import KISOrders
    c = _Pages(_two_pages("output1", "100", [{"odno": "1"}], [{"odno": "2"}]))
    assert KISOrders(client=c).inquire_orders("005930", market="kr") == [{"odno": "1"}, {"odno": "2"}]
    _assert_continued(c, "100")


def test_us_order_inquiry():
    from kis_adapter.orders import KISOrders
    c = _Pages(_two_pages("output", "200", [{"odno": "1"}], [{"odno": "2"}]))
    assert KISOrders(client=c).inquire_orders("AAPL", market="us", excd="NASD") == [
        {"odno": "1"}, {"odno": "2"}]
    _assert_continued(c, "200")


def _broker(client):
    from backend.brokers.kis import KISBroker
    b = KISBroker.__new__(KISBroker)
    b._paper = True
    b._account = "123456789012"
    b._client = client
    return b


def _order_row(odno):
    return {"odno": odno, "pdno": "005930", "tot_ccld_qty": "1", "ord_qty": "1",
            "avg_prvs": "100", "ord_unpr": "100", "sll_buy_dvsn_cd": "02",
            "ft_ccld_qty": "1", "ft_ord_qty": "1", "ft_ccld_unpr3": "100",
            "ft_ord_unpr3": "100"}


def test_broker_finds_a_kr_order_on_the_second_page():
    """#173's neighbour: the order is on page 2 — it used to read as absent."""
    c = _Pages(_two_pages("output1", "100", [_order_row("A")], [_order_row("B")]))
    order = _broker(c)._get_kr_order_status("B")
    assert order is not None and order.id == "B"
    _assert_continued(c, "100")


def test_broker_finds_a_us_order_on_the_second_page():
    c = _Pages(_two_pages("output", "200", [_order_row("A")], [_order_row("B")]))
    order = _broker(c)._get_us_order_status("B", "AAPL")
    assert order is not None and order.id == "B"
    _assert_continued(c, "200")


# ── an order found on an early page is not lost to a later page's failure ──

class _FailsAfter(_Pages):
    """Plays back its pages, then raises on the next request."""

    def get_page(self, path, tr_id, params, tr_cont=""):
        if not self.pages:
            self.calls.append((dict(params), tr_cont))
            raise ConnectionError("page request failed")
        return super().get_page(path, tr_id, params, tr_cont)


class TestFindRow:
    """CodeRabbit architecture review: the broker status lookups turn any
    exception into None, which the reconciler reads as "absent at the broker".
    Reading every page before matching let a page-2 failure hide an order
    already on page 1 — one the single-page code found."""

    def test_stops_at_the_page_with_the_row(self):
        from kis_adapter.pagination import find_row
        c = _Pages(_two_pages("output", "200", [{"odno": "A"}], [{"odno": "B"}]))
        assert find_row(c, "/p", "TR", {}, ctx="200", list_key="output",
                        match=lambda r: r["odno"] == "A") == {"odno": "A"}
        assert len(c.calls) == 1

    def test_none_only_after_every_page(self):
        from kis_adapter.pagination import find_row
        c = _Pages(_two_pages("output", "200", [{"odno": "A"}], [{"odno": "B"}]))
        assert find_row(c, "/p", "TR", {}, ctx="200", list_key="output",
                        match=lambda r: r["odno"] == "Z") is None
        assert len(c.calls) == 2

    def test_a_failure_before_the_row_still_raises(self):
        from kis_adapter.pagination import find_row
        c = _FailsAfter([({"output": [{"odno": "A"}], "ctx_area_nk200": "K"}, "M")])
        with pytest.raises(ConnectionError):
            find_row(c, "/p", "TR", {}, ctx="200", list_key="output",
                     match=lambda r: r["odno"] == "B")

    def test_broker_kr_finds_a_page_one_order_when_page_two_fails(self):
        c = _FailsAfter([({"output1": [_order_row("A")], "ctx_area_nk100": "K"}, "M")])
        order = _broker(c)._get_kr_order_status("A")
        assert order is not None and order.id == "A"

    def test_broker_us_finds_a_page_one_order_when_page_two_fails(self):
        c = _FailsAfter([({"output": [_order_row("A")], "ctx_area_nk200": "K"}, "M")])
        order = _broker(c)._get_us_order_status("A", "AAPL")
        assert order is not None and order.id == "A"
