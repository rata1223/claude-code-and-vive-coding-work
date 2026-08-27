"""P0 — ``QT_CANCELED``: the one lifecycle state a cancel path needs.

``QT_SUBMITTED`` was terminal (``QT_VALID_TRANSITIONS[QT_SUBMITTED] = set()``),
which is correct while nothing can act on a submitted order. A cancel path can,
so exactly one edge opens: ``QT_SUBMITTED -> QT_CANCELED``. Nothing else about
the lifecycle changes — no fill tracking, no poller, no partial-fill state.

The second half of this file guards the consequence that is easy to miss: a
cancelled sell must **release** the quantity it was reserving. ``_RELEASED_SELL_
STATUSES`` is written as the *released* list precisely so an unrecognised status
keeps reserving (fail-closed), which means a new status is reserving-by-default
until it is added. Leaving ``canceled`` out would make every cancelled sell hold
its quantity forever — the permanent-breakage failure mode P0-07 S2 documents.
"""
import pytest

from api.models import (
    QT_BLOCKED,
    QT_CANCELED,
    QT_FAILED,
    QT_REJECTED,
    QT_RESERVED,
    QT_SUBMITTED,
    QT_VALID_TRANSITIONS,
    qt_transition,
)
from backend.risk.sellable_qty import pending_sell_qty_from_rows


class _Order:
    """Minimal stand-in — ``qt_transition`` only reads/writes ``status``."""

    def __init__(self, status):
        self.status = status


# ── the single new edge ───────────────────────────────────────────────────────

def test_submitted_can_be_canceled():
    order = _Order(QT_SUBMITTED)

    qt_transition(order, QT_CANCELED)

    assert order.status == QT_CANCELED


def test_canceled_is_terminal():
    assert QT_VALID_TRANSITIONS[QT_CANCELED] == set()
    for target in (QT_SUBMITTED, QT_RESERVED, QT_FAILED, QT_REJECTED, QT_BLOCKED):
        with pytest.raises(ValueError):
            qt_transition(_Order(QT_CANCELED), target)


def test_a_reserved_order_cannot_be_canceled():
    """Reserved means the broker was never told. There is nothing to cancel —
    that row is the reconciler's business, not the cancel endpoint's."""
    with pytest.raises(ValueError):
        qt_transition(_Order(QT_RESERVED), QT_CANCELED)


@pytest.mark.parametrize("terminal", [QT_REJECTED, QT_FAILED, QT_BLOCKED])
def test_other_terminal_states_cannot_be_canceled(terminal):
    with pytest.raises(ValueError):
        qt_transition(_Order(terminal), QT_CANCELED)


def test_submitted_gains_exactly_one_edge_and_no_others():
    """Guards the blast radius: opening QT_SUBMITTED must not re-open it to
    anything else, or a stray mutation could resurrect a finished order."""
    assert QT_VALID_TRANSITIONS[QT_SUBMITTED] == {QT_CANCELED}


# ── the consequence: a cancelled sell stops reserving ────────────────────────

def test_a_canceled_sell_releases_its_reserved_quantity():
    rows = [("AAPL", "sell", 5, QT_CANCELED)]

    assert pending_sell_qty_from_rows(rows, "AAPL") == 0


def test_a_canceled_sell_does_not_mask_a_still_open_one():
    rows = [
        ("AAPL", "sell", 5, QT_CANCELED),   # released
        ("AAPL", "sell", 3, QT_RESERVED),   # still holding
    ]

    assert pending_sell_qty_from_rows(rows, "AAPL") == 3


def test_an_unrecognised_status_still_reserves():
    """The fail-closed default that makes adding ``canceled`` necessary in the
    first place — pinned here so the released-list semantics cannot be inverted
    into an open-list without this failing."""
    rows = [("AAPL", "sell", 5, "some_future_state")]

    assert pending_sell_qty_from_rows(rows, "AAPL") == 5
