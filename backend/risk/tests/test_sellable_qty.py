"""
P0-07 S2 — Sellable quantity hardening: the core rule (T1–T3, T8–T11).

Every sell path treated the *held* quantity as if it were immediately sellable
(`hldg_qty` for KR, `ovrs_cblc_qty` for US). Held is not sellable: shares can be
unsettled, or already committed to a resting sell order. Asking the broker for
more than it will actually let us sell gets the order rejected — and in an
emergency that rejection is indistinguishable from "we liquidated".

    sellable = max(0, min(broker_orderable, held) - locally_pending_sells)

Per-path enforcement (T4–T7, T12) lives with each path's own suite.
"""
import pytest

from backend.brokers.models import Position
from backend.risk.sellable_qty import (
    SellableResult,
    UNKNOWN,
    pending_sell_qty_from_rows,
    resolve_sellable,
    sellable_from_position,
    validate_sell_qty,
)


def _pos(symbol="SPY", qty=10, sellable=None, avg=100.0):
    return Position(symbol=symbol, qty=qty, avg_price=avg, market="US",
                    sellable_qty=sellable)


# ── T1: a valid sellable quantity passes through unchanged ───────────────────

def test_t1_valid_sellable_passes_unchanged():
    res = resolve_sellable(held_qty=10, broker_sellable=10)
    assert res.known and res.qty == 10
    assert validate_sell_qty(10, res) == (True, "")


def test_t1_partial_sell_within_sellable_passes():
    res = resolve_sellable(held_qty=10, broker_sellable=10)
    assert validate_sell_qty(3, res)[0] is True


# ── T2: requested > sellable is blocked, never clamped ───────────────────────

def test_t2_over_ask_is_blocked_not_clamped():
    res = resolve_sellable(held_qty=10, broker_sellable=4)
    assert res.qty == 4

    ok, reason = validate_sell_qty(10, res)
    assert ok is False
    assert "10" in reason and "4" in reason      # both numbers reported
    assert res.qty == 4                          # nothing mutated


def test_t2_held_is_not_the_ceiling_sellable_is():
    """10 held but only 4 orderable → even 5 must be refused."""
    res = resolve_sellable(held_qty=10, broker_sellable=4)
    assert validate_sell_qty(5, res)[0] is False


@pytest.mark.parametrize("requested", [0, -1, -10])
def test_t2_non_positive_request_is_blocked(requested):
    res = resolve_sellable(held_qty=10, broker_sellable=10)
    assert validate_sell_qty(requested, res)[0] is False


@pytest.mark.parametrize("requested", [None, "5", float("nan"), float("inf"), True, 2.5])
def test_t2_untrustworthy_request_is_blocked(requested):
    res = resolve_sellable(held_qty=10, broker_sellable=10)
    assert validate_sell_qty(requested, res)[0] is False


# ── T3: unknown / untrusted inputs fail closed ───────────────────────────────

def test_t3_unknown_broker_sellable_fails_closed():
    res = resolve_sellable(held_qty=10, broker_sellable=UNKNOWN)
    assert res.known is False and res.qty is None
    assert validate_sell_qty(1, res)[0] is False


def test_t3_unknown_never_falls_back_to_held():
    """Reverting to `held` when the broker won't say is exactly the defect."""
    res = resolve_sellable(held_qty=999, broker_sellable=UNKNOWN)
    assert res.qty is None and res.qty != 999


@pytest.mark.parametrize("bad", [None, "10", float("nan"), float("inf"),
                                 float("-inf"), True, -1, 2.5, 10 ** 400])
def test_t3_untrusted_held_fails_closed(bad):
    assert resolve_sellable(held_qty=bad, broker_sellable=10).known is False


@pytest.mark.parametrize("bad", ["4", float("nan"), float("inf"), True, -1, 2.5])
def test_t3_untrusted_broker_value_fails_closed(bad):
    assert resolve_sellable(held_qty=10, broker_sellable=bad).known is False


@pytest.mark.parametrize("bad", ["1", float("nan"), float("inf"), True, -1])
def test_t3_untrusted_pending_value_fails_closed(bad):
    assert resolve_sellable(held_qty=10, broker_sellable=10,
                            pending_sell_qty=bad).known is False


# ── Core arithmetic ──────────────────────────────────────────────────────────

def test_sellable_is_capped_by_held():
    """A broker reporting more orderable than held is inconsistent — trust the
    smaller number rather than over-asking."""
    assert resolve_sellable(held_qty=5, broker_sellable=50).qty == 5


def test_sellable_floors_at_zero_never_negative():
    res = resolve_sellable(held_qty=10, broker_sellable=10, pending_sell_qty=25)
    assert res.qty == 0
    assert validate_sell_qty(1, res)[0] is False


def test_zero_position_is_known_and_sells_nothing():
    res = resolve_sellable(held_qty=0, broker_sellable=0)
    assert res.known is True and res.qty == 0
    assert validate_sell_qty(1, res)[0] is False


# ── T9: pending sell orders reserve sellable quantity ────────────────────────

def test_t9_pending_sells_reduce_sellable():
    res = resolve_sellable(held_qty=10, broker_sellable=10, pending_sell_qty=4)
    assert res.qty == 6
    assert validate_sell_qty(6, res)[0] is True
    assert validate_sell_qty(7, res)[0] is False


def test_t9_only_open_sells_for_this_symbol_reserve_quantity():
    from api.models import (QT_BLOCKED, QT_FAILED, QT_REJECTED, QT_RESERVED,
                            QT_SUBMITTED)

    rows = [
        ("SPY", "sell", 3, QT_RESERVED),    # counts
        ("SPY", "sell", 2, QT_SUBMITTED),   # counts
        ("SPY", "sell", 5, QT_REJECTED),    # terminal — released
        ("SPY", "sell", 5, QT_FAILED),      # terminal — released
        ("SPY", "sell", 5, QT_BLOCKED),     # never reached the broker
        ("SPY", "buy", 9, QT_RESERVED),     # wrong side
        ("QQQ", "sell", 7, QT_RESERVED),    # wrong symbol
    ]
    assert pending_sell_qty_from_rows(rows, "SPY") == 5


def test_t9_symbol_match_is_case_insensitive():
    from api.models import QT_RESERVED
    assert pending_sell_qty_from_rows([("spy", "SELL", 3, QT_RESERVED)], "SPY") == 3


def test_t9_no_rows_reserves_nothing():
    assert pending_sell_qty_from_rows([], "SPY") == 0
    assert pending_sell_qty_from_rows(None, "SPY") == 0


def test_t9_unreadable_pending_qty_raises_rather_than_guessing_zero():
    """We cannot bound our own outstanding exposure — guessing 0 would permit
    an over-ask."""
    from api.models import QT_RESERVED
    with pytest.raises(ValueError):
        pending_sell_qty_from_rows([("SPY", "sell", None, QT_RESERVED)], "SPY")


# ── T8: partial fills leave the remainder sellable ───────────────────────────

def test_t8_settled_partial_fill_lowers_both_held_and_orderable():
    # 4 of 10 sold and settled away → broker now reports 6 held / 6 orderable
    assert resolve_sellable(held_qty=6, broker_sellable=6).qty == 6


def test_t8_resting_partial_sell_reserves_the_unfilled_remainder():
    # 10 held, 4 resting unfilled → 6 still sellable
    assert resolve_sellable(held_qty=10, broker_sellable=10, pending_sell_qty=4).qty == 6


def test_t8_broker_already_netting_the_resting_order_is_not_double_counted():
    """If the broker's orderable figure already excludes the resting order, the
    local pending figure must not subtract it a second time."""
    res = resolve_sellable(held_qty=10, broker_sellable=6, pending_sell_qty=4,
                           broker_nets_pending=True)
    assert res.qty == 6


# ── Position helper (feeds T4–T7) ────────────────────────────────────────────

def test_position_helper_prefers_sellable_over_held():
    assert sellable_from_position(_pos(qty=10, sellable=4)).qty == 4


def test_position_without_reported_sellable_fails_closed():
    res = sellable_from_position(_pos(qty=10, sellable=None))
    assert res.known is False


def test_position_helper_subtracts_pending():
    assert sellable_from_position(_pos(qty=10, sellable=10), pending_sell_qty=3).qty == 7


# ── T11: resolution is pure — no side effects, no submissions ────────────────

def test_t11_resolution_is_pure_and_repeatable():
    a = resolve_sellable(held_qty=10, broker_sellable=7, pending_sell_qty=2)
    b = resolve_sellable(held_qty=10, broker_sellable=7, pending_sell_qty=2)
    assert a == b and a.qty == 5


def test_t11_result_is_immutable():
    res = resolve_sellable(held_qty=10, broker_sellable=10)
    assert isinstance(res, SellableResult)
    with pytest.raises(Exception):
        res.qty = 999          # type: ignore[misc]


# ── T10: reproducible from durable state across a restart ────────────────────

def test_t10_recovery_recomputes_the_same_sellable_from_persisted_rows():
    """After a restart the only inputs are the live broker figure and the
    persisted pending rows — same inputs, same answer."""
    from api.models import QT_RESERVED

    rows = [("SPY", "sell", 4, QT_RESERVED)]        # survives the restart
    before = resolve_sellable(held_qty=10, broker_sellable=10,
                              pending_sell_qty=pending_sell_qty_from_rows(rows, "SPY"))
    after = resolve_sellable(held_qty=10, broker_sellable=10,
                             pending_sell_qty=pending_sell_qty_from_rows(rows, "SPY"))

    assert before == after and after.qty == 6
