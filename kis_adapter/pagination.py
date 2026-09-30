"""Follow KIS's paginated inquiries to the last page.

Balance and order inquiries return a limited number of rows per call. Every
caller used to send empty ``CTX_AREA_*`` keys and never read the continuation,
so a long position list or order history silently came back as its first page:
holdings the risk checks never saw, an order the reconciler reported as absent.

``get_all_pages`` asks again while the response header ``tr_cont`` says more
follows, passing back the body's ``ctx_area_fk*``/``ctx_area_nk*``, and
concatenates the row list. Everything else in the body (the ``output2``
summary) comes from the first page. It never returns a partial list: a
continuation without a key, or more pages than ``max_pages``, raises.
"""
from __future__ import annotations

from typing import Any

#: Response ``tr_cont`` values meaning "another page follows".
MORE = frozenset({"F", "M"})

MAX_PAGES = 20


def _rows(value: Any) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, dict):
        return [value]
    return list(value)


def get_all_pages(client, path: str, tr_id: str, params: dict, *, ctx: str,
                  list_key: str | tuple[str, ...], max_pages: int = MAX_PAGES) -> dict:
    """The first page's body with the row list holding every page's rows.

    ``ctx`` is the key width the endpoint uses: ``"100"`` (``CTX_AREA_FK100``/
    ``NK100``) or ``"200"``. ``list_key`` may name several candidates when an
    endpoint's row key is not pinned down (``("output1", "output")``): the one
    the first page carries is merged, and a continuation with none of them
    raises — otherwise its later pages would again be dropped in silence.
    """
    fk_req, nk_req = f"CTX_AREA_FK{ctx}", f"CTX_AREA_NK{ctx}"
    fk_resp, nk_resp = fk_req.lower(), nk_req.lower()
    keys = (list_key,) if isinstance(list_key, str) else tuple(list_key)

    data, cont = client.get_page(path, tr_id, params, "")
    key = next((k for k in keys if data.get(k) not in (None, "")), None)
    if key is None:
        if cont in MORE:
            raise RuntimeError(
                f"KIS {tr_id}: 다음 페이지가 있다는데 행 목록({', '.join(keys)})이 없습니다")
        key = keys[0]
    merged = dict(data)
    rows = _rows(data.get(key))
    pages = 1
    while cont in MORE:
        fk, nk = data.get(fk_resp) or "", data.get(nk_resp) or ""
        if not (fk.strip() or nk.strip()):
            raise RuntimeError(
                f"KIS {tr_id}: 다음 페이지가 있다는데 연속 키가 없습니다 — 일부만 반환하지 않음")
        if pages >= max_pages:
            raise RuntimeError(
                f"KIS {tr_id}: {max_pages}페이지를 넘었습니다 — 일부만 반환하지 않음")
        data, cont = client.get_page(path, tr_id, {**params, fk_req: fk, nk_req: nk}, "N")
        rows.extend(_rows(data.get(key)))
        pages += 1
    merged[key] = rows
    return merged
