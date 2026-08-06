"""
P0-07 S2 — Sellable quantity.

    Held is not sellable.

Every sell path used to treat the held quantity as if it could be sold right
now: ``hldg_qty`` for KR, ``ovrs_cblc_qty`` for US. Shares can be unsettled, or
already committed to a resting sell order. Asking the broker for more than it
will actually let us sell gets the order rejected — and in an emergency that
rejection is indistinguishable from "we liquidated".

The rule:

    sellable = max(0, min(broker_orderable, held) - locally_pending_sells)

with an UNKNOWN ``broker_orderable`` failing closed rather than silently
reverting to ``held``. No quantity is ever derived from price, notional, cost
basis, or any other heuristic — the only inputs are counts.

This module is pure and stateless, like ``backend/risk/halt_policy``. It talks
to no broker, database, or cache; callers supply the numbers.
"""
import logging
import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The broker did not state how much of this position is orderable. Callers
#: must fail closed — never substitute the held quantity.
UNKNOWN = None

#: Why a result is not ``known``. Both are a BLOCK on every ordinary sell path;
#: they are distinguished because EmergencyFlatten treats them differently and
#: because they are different forensic events.
#:   ``UNREPORTED`` — the broker returned no orderable figure at all (field
#:                    absent/empty; e.g. a KIS response-shape change).
#:   ``UNTRUSTED``  — a figure was returned but could not be read as an exact,
#:                    finite, non-negative share count.
CAUSE_UNREPORTED = "unreported"
CAUSE_UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class SellableResult:
    """How much of a position may be sold right now.

    ``qty is None`` means the answer is unknown, which is always a BLOCK.
    ``reason`` explains an unknown, or records how a known figure was derived.
    ``cause`` is the machine-readable counterpart of ``reason`` for an unknown
    (``CAUSE_UNREPORTED`` / ``CAUSE_UNTRUSTED``); it is ``None`` when known.
    """

    qty: Optional[int]
    reason: str = ""
    cause: Optional[str] = None

    @property
    def known(self) -> bool:
        return self.qty is not None


def _as_count(value) -> Optional[int]:
    """Coerce a share count, or ``None`` if it is not a trustworthy one.

    Rejects ``bool`` (an int subclass that would pass a bare ``>= 0``), strings,
    ``None``, NaN, ±inf, values too large to convert, negatives, and any float
    carrying a fraction — a fractional share count means we misread the field,
    not that a fractional sell is intended.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        as_float = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(as_float) or as_float < 0:
        return None
    if as_float != int(as_float):
        return None
    return int(as_float)


def resolve_sellable(
    held_qty,
    broker_sellable,
    pending_sell_qty=0,
    broker_nets_pending: bool = False,
) -> SellableResult:
    """Resolve the sellable quantity for one position.

    ``broker_sellable`` is the broker's own orderable figure (KIS
    ``ord_psbl_qty``). ``UNKNOWN``/``None`` means the broker did not report one,
    which fails closed.

    ``pending_sell_qty`` is quantity we have already asked to sell but which the
    broker may not have reflected yet. Set ``broker_nets_pending=True`` when the
    broker's figure already excludes resting orders, so it is not subtracted
    twice (double-subtracting would refuse legitimate closes).
    """
    held = _as_count(held_qty)
    if held is None:
        return SellableResult(None, f"보유 수량을 신뢰할 수 없음: {held_qty!r}",
                              CAUSE_UNTRUSTED)

    if broker_sellable is UNKNOWN:
        return SellableResult(
            None,
            "브로커가 주문가능수량을 보고하지 않음 — 보유수량으로 대체하지 않고 차단",
            CAUSE_UNREPORTED,
        )

    orderable = _as_count(broker_sellable)
    if orderable is None:
        return SellableResult(None, f"주문가능수량을 신뢰할 수 없음: {broker_sellable!r}",
                              CAUSE_UNTRUSTED)

    pending = _as_count(pending_sell_qty)
    if pending is None:
        return SellableResult(None, f"대기 매도 수량을 신뢰할 수 없음: {pending_sell_qty!r}",
                              CAUSE_UNTRUSTED)

    # A broker reporting more orderable than held is inconsistent; trust the
    # smaller number rather than over-asking.
    base = min(orderable, held)
    if broker_nets_pending:
        pending = 0
    sellable = max(0, base - pending)
    return SellableResult(
        sellable,
        f"min(주문가능 {orderable}, 보유 {held}) - 대기매도 {pending}",
    )


def validate_sell_qty(requested_qty, sellable: SellableResult) -> Tuple[bool, str]:
    """Check a requested sell quantity against the resolved sellable quantity.

    Returns ``(True, "")`` only for a positive whole quantity within the
    sellable figure. An over-ask is rejected outright and **never clamped** —
    clamping would silently sell a different quantity than was requested, which
    is the behaviour the P0-07C close-position contract already refuses.
    """
    if not sellable.known:
        return False, f"매도가능수량 미확정 — 차단: {sellable.reason}"

    requested = _as_count(requested_qty)
    if requested is None:
        return False, f"요청 수량을 신뢰할 수 없음: {requested_qty!r}"
    if requested <= 0:
        return False, f"요청 수량이 양수가 아님: {requested_qty!r}"
    if requested > sellable.qty:
        return False, (
            f"매도가능수량 초과: 요청 {requested} > 가능 {sellable.qty} "
            f"({sellable.reason})"
        )
    return True, ""


def sellable_from_position(position, pending_sell_qty=0,
                           broker_nets_pending: bool = False) -> SellableResult:
    """Resolve the sellable quantity straight from a :class:`Position`.

    Adapters that can report an orderable figure set ``Position.sellable_qty``
    (KIS parses ``ord_psbl_qty``). Adapters where held is sellable by
    construction — the simulator and the in-memory tracker, which model no
    settlement or reservation — set it equal to ``qty``. A ``None`` therefore
    means "the broker did not say", which fails closed.
    """
    return resolve_sellable(
        held_qty=getattr(position, "qty", None),
        broker_sellable=getattr(position, "sellable_qty", UNKNOWN),
        pending_sell_qty=pending_sell_qty,
        broker_nets_pending=broker_nets_pending,
    )


#: Quick Trade order statuses that have *released* their quantity. Mirrors
#: ``QT_REJECTED``/``QT_FAILED``/``QT_BLOCKED`` in ``api/models.py``; this module
#: stays import-free by design, so the values are repeated rather than imported.
#:
#: The set is written as the terminal states, not the open ones, so that the
#: default is fail-closed: any status this module does not recognise — a
#: renamed constant, or a non-terminal state added later such as a
#: partially-filled one — is assumed to still hold quantity. Listing the open
#: states instead would make both of those silently report 0 pending, which
#: permits an over-ask.
_RELEASED_SELL_STATUSES = frozenset({"rejected", "failed", "blocked"})


def pending_sell_qty_from_rows(rows: Iterable[Sequence], symbol: str) -> int:
    """Sum quantity locked by our own open SELL orders for ``symbol``.

    ``rows`` are ``(symbol, side, qty, status)`` tuples — typically
    ``quick_trade_orders`` records. Only non-terminal sells for this symbol
    count; ``rejected``/``failed``/``blocked`` released their quantity. An
    unrecognised status counts as still holding quantity (see
    ``_RELEASED_SELL_STATUSES``).

    Raises :class:`ValueError` on a row whose quantity cannot be read: an
    unreadable pending row means we cannot bound our own outstanding exposure,
    and guessing zero there would silently permit an over-ask.
    """
    total = 0
    wanted = (symbol or "").upper()
    for row in rows or []:
        row_symbol, side, qty, status = row[0], row[1], row[2], row[3]
        if (row_symbol or "").upper() != wanted:
            continue
        if (side or "").lower() != "sell":
            continue
        if (status or "").lower() in _RELEASED_SELL_STATUSES:
            continue
        counted = _as_count(qty)
        if counted is None:
            raise ValueError(
                f"대기 매도 주문의 수량을 읽을 수 없음 ({row_symbol}): {qty!r}"
            )
        total += counted
    return total
