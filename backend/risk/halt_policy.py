"""
P0-07 S1 — Halt-vs-Exit policy (Policy B: ENTRY HALT + EXIT ALLOWED).

    A halt stops the creation of NEW risk. It must not remove the system's
    ability to reduce risk it already holds — EXCEPT when the halt exists
    because position state itself is untrusted.

Before S1, every halt was a single boolean and every gate blocked buys and
sells identically. An MDD breach therefore froze the book at maximum drawdown:
the control that fires to stop losses also disabled the stop-losses, leaving a
manual ``/api/admin/flatten`` as the only way out.

This module is the single decision authority for that question. It is pure and
stateless — callers supply the halt cause, the operation class, and (for exits)
a live position lookup. Nothing here talks to a broker, a database, or Redis.

Scope note: this is a policy layer only. It deliberately does NOT unify the
worker's in-process ``SAFE_MODE`` with the API's Redis halt flag — those remain
separate halt stores (out of scope for S1).
"""
import logging
import math
from enum import Enum
from typing import Callable, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)


class HaltCause(Enum):
    """Why trading is halted. The cause decides whether exits survive it."""

    #: Position/runtime state cannot be trusted (startup recovery incomplete,
    #: reconciliation failure). Quantities are unreliable, so even an exit
    #: could act on a position that is not what we think it is.
    UNTRUSTED_STATE = "untrusted_state"

    #: A risk limit fired (daily / weekly loss, MDD, kill switch). State is
    #: trustworthy; the exposure is the problem — reducing it must stay possible.
    RISK_BREACH = "risk_breach"

    #: Market data is critically stale. Exits are permitted but only at a
    #: validated live execution price (P0-07 G2 rules).
    DEGRADED_FEED = "degraded_feed"


class OperationClass(Enum):
    ENTRY = "entry"                  # creates exposure — or cannot be proven not to
    EXIT = "exit"                    # provably reduces an existing position
    EMERGENCY = "emergency"          # EmergencyFlatten — never gated by halt state
    NON_EXPOSURE = "non_exposure"    # cancel: removes a resting order


def is_allowed(cause: Optional[HaltCause], op: OperationClass) -> bool:
    """The decision matrix. ``cause=None`` means RUNNING (not halted).

    | state                 | ENTRY | EXIT  | EMERGENCY | NON_EXPOSURE |
    |-----------------------|-------|-------|-----------|--------------|
    | RUNNING               | ALLOW | ALLOW | ALLOW     | ALLOW        |
    | HALT(RISK_BREACH)     | BLOCK | ALLOW | ALLOW     | ALLOW        |
    | HALT(DEGRADED_FEED)   | BLOCK | ALLOW*| ALLOW*    | ALLOW        |
    | HALT(UNTRUSTED_STATE) | BLOCK | BLOCK | ALLOW     | ALLOW        |

    *EXIT/EMERGENCY under DEGRADED_FEED additionally require a valid live
    execution price — see :func:`is_valid_execution_price`. This function
    answers the state question only; the caller enforces the price rule.
    """
    # Emergency liquidation and cancels never create exposure, so no halt
    # state can justify blocking them (R6).
    if op in (OperationClass.EMERGENCY, OperationClass.NON_EXPOSURE):
        return True
    if cause is None:
        return True
    if op is OperationClass.ENTRY:
        return False
    # EXIT: allowed unless the halt says our position data is untrustworthy.
    # Listed positively so an unrecognised cause fails closed (R1/R3).
    return cause in (HaltCause.RISK_BREACH, HaltCause.DEGRADED_FEED)


def is_valid_execution_price(raw) -> bool:
    """True only for a real, finite, positive number.

    Same rule as the P0-07 G2 flatten guard: rejects ``None``, strings, ``bool``
    (an int subclass that would pass a bare ``> 0``), NaN, ±inf and values <= 0.
    The value is never rounded or clamped to make it pass.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return False
    try:
        value = float(raw)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(value) and value > 0


def prove_exit(
    get_positions: Callable[[], Iterable],
    symbol: str,
    qty,
) -> Tuple[bool, str]:
    """Prove that selling ``qty`` of ``symbol`` reduces an existing position.

    Returns ``(True, "")`` only when a live lookup shows a long position and
    ``0 < qty <= held_qty``. Anything else — no position, an over-close, a
    non-positive quantity, or a failed lookup — returns ``(False, reason)``.

    R2: proof comes from a live lookup, never from ``side == "sell"``, a cached
    position, or an inferred quantity. R3: a lookup failure is a BLOCK, not a
    pass. An over-close is rejected outright and never clamped to ``held_qty``
    — clamping would silently close a different quantity than requested.
    """
    if isinstance(qty, bool) or not isinstance(qty, (int, float)):
        return False, f"수량이 숫자가 아님: {qty!r}"
    try:
        qty_f = float(qty)
    except (OverflowError, ValueError):
        # An int too large to convert must still be a BLOCK, not an escaping
        # exception — the "any failure is a BLOCK" contract has no exceptions.
        return False, f"수량이 표현 범위를 초과: {qty!r}"
    if not math.isfinite(qty_f) or qty_f <= 0:
        return False, f"수량이 양수가 아님: {qty!r}"

    try:
        positions = get_positions()
    except Exception as e:  # noqa: BLE001 - any lookup failure is fail-closed
        return False, f"포지션 조회 실패: {e}"

    match = None
    for pos in positions or []:
        if getattr(pos, "symbol", None) == symbol:
            match = pos
            break

    if match is None:
        return False, f"{symbol} 보유 포지션 없음"

    # P0-07 S2: prove against what the broker will actually sell, not the raw
    # holding. Unsettled shares and quantity already committed to a resting
    # order are held but not sellable, and an exit proved against `held` would
    # over-ask. An unknown orderable figure is a BLOCK, never a fallback.
    #
    # No `pending_sell_qty` term here, deliberately. On this path a duplicate
    # sell is prevented one layer up by the per-symbol pending lock —
    # `PositionTracker.try_mark_pending` (backend/execution/position_tracker.py),
    # claimed at backend/strategy/indicator/strategy.py before the sell and
    # released on fill — so a second sell for the same symbol cannot start.
    # The positions this resolves against come from that same tracker, which
    # models no settlement or broker-side reservation and therefore reports no
    # independent pending figure to subtract. Giving this module a pending
    # source would make a deliberately pure, stateless policy stateful.
    from backend.risk.sellable_qty import sellable_from_position, validate_sell_qty

    sellable = sellable_from_position(match)
    ok, reason = validate_sell_qty(qty, sellable)
    if not ok:
        return False, reason
    return True, ""
