"""P0 — the order contract must not grow a field the backend ignores.

Audit correction, recorded here because it changed this work unit's scope: the
audit claimed a "market" order reaches the broker as limit-at-0. It does not.
``place_order`` already rejects ``price <= 0``, ``qty <= 0`` and fractional
quantities before any reservation or broker call — P0-08 added exactly that
(``api/tests/test_p0_08_runtime_validation.py``). Nothing bad reaches KIS.

The real remaining defect is narrower and purely presentational: the UI offers a
market/limit toggle whose "market" branch skips price entry, so selecting it
produces a request the backend can only refuse. The user is shown a failure they
cannot act on for an option that never could have worked. The fix is therefore
removing the toggle (WU-2/WU-8), **not** adding backend validation.

Duplicating P0-08's assertions with Pydantic field validators was tried and
reverted: it moved rejection from a readable ``Resp.err`` envelope to a FastAPI
422, whose ``detail`` shape the frontend interceptor discards in favour of a
generic message — strictly worse UX for the same rule.

What is left to pin is the contract boundary itself.
"""
from api.schemas import PlaceOrderRequest


def test_order_type_is_not_part_of_the_contract():
    """The handler always submits ``ORD_DVSN "00"`` (limit).

    Accepting an ``order_type`` it then ignores is what let the UI believe
    market orders existed. If a future change adds the field, it must add a
    real implementation with it — this failing is the reminder.
    """
    assert "order_type" not in PlaceOrderRequest.model_fields


def test_the_crypto_era_fields_are_not_part_of_the_contract():
    """``leverage``/``source``/``market_type`` are sent by the UI today and
    dropped by ``extra='ignore'``. They must never become real fields on an
    equities order."""
    for field in ("leverage", "source", "market_type", "position_side"):
        assert field not in PlaceOrderRequest.model_fields


def test_extra_client_fields_are_dropped_not_rejected():
    """The UI still sends them mid-migration; 422-ing every order would be a
    worse outage than the mislabelling this phase is fixing."""
    body = PlaceOrderRequest(
        credential_id=1, symbol="AAPL", side="buy", qty=3, price=175.5,
        source="manual", leverage=1, order_type="limit", market_type="spot",
    )

    assert body.qty == 3
    assert body.price == 175.5
