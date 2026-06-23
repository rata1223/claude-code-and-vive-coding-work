"""
Regression tests (TASK P2-01C) for ``backend/data/corporate_actions.py``.

Scenario-focused regressions over the shipped ``CorporateActionService`` that
pin down the two safety invariants for every corporate-action type:

  * **portfolio value preserved** — a split / reverse-split / ticker-change never
    creates or destroys book value (``qty * avg_price`` is invariant); a cash
    dividend leaves the position's book value untouched and delivers the economic
    value as a separate ``cash_delta`` (so position + cash is conserved).
  * **average cost preserved** — the *total* cost basis (``qty * avg_price``) is
    always conserved; per-share average cost is rebased multiplicatively for
    splits/reverse-splits and left exactly unchanged for dividends and ticker
    changes (no phantom P&L).

These complement the unit tests in ``test_corporate_actions.py`` (same module)
but are organised as end-to-end regressions across single positions *and* a
multi-symbol portfolio. The existing suite must remain green alongside these.
"""
from datetime import date

import pytest

from backend.data.corporate_actions import (
    ActionType,
    CorporateAction,
    CorporateActionService,
    PositionSnapshot,
)

_EFF = date(2026, 6, 5)

# Tight tolerance for floating-point value/cost comparisons.
_REL = 1e-9
_ABS = 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pos(symbol: str, qty: float, avg: float) -> PositionSnapshot:
    return PositionSnapshot(symbol=symbol, qty=qty, avg_price=avg)


def _book_value(p: PositionSnapshot) -> float:
    """Cost-basis book value of a position (qty * average cost)."""
    return p.qty * p.avg_price


def _action(action_type: ActionType, symbol: str, **kw) -> CorporateAction:
    return CorporateAction(action_type=action_type, symbol=symbol,
                           effective_date=_EFF, **kw)


def _assert_value_preserved(before: PositionSnapshot, after: PositionSnapshot) -> None:
    """Book value (== total cost basis) unchanged by the adjustment."""
    assert after.qty * after.avg_price == pytest.approx(before.qty * before.avg_price,
                                                        rel=_REL, abs=_ABS)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Stock split
# ─────────────────────────────────────────────────────────────────────────────

class TestStockSplitRegression:
    @pytest.mark.parametrize("ratio", [2.0, 3.0, 4.0, 1.5, 10.0])
    def test_split_preserves_value_and_cost_basis(self, ratio):
        svc = CorporateActionService()
        before = _pos("AAPL", 120, 600.0)
        result = svc.apply(_action(ActionType.SPLIT, "AAPL", ratio=ratio), position=before)
        after = result.position_result.position

        # quantity scales up, per-share average cost scales inversely
        assert after.qty == pytest.approx(before.qty * ratio)
        assert after.avg_price == pytest.approx(before.avg_price / ratio)
        # portfolio value + total cost basis preserved
        assert result.position_result.value_preserved is True
        _assert_value_preserved(before, after)

    def test_split_records_value_preserving_history(self):
        svc = CorporateActionService()
        before = _pos("AAPL", 100, 100.0)
        svc.apply(_action(ActionType.SPLIT, "AAPL", ratio=2.0), position=before)
        rec = svc.history_for("AAPL")[0]
        assert rec.value_preserved is True
        assert rec.cash_delta == 0.0
        _assert_value_preserved(rec.position_before, rec.position_after)

    def test_split_also_adjusts_price_bars_consistently(self):
        svc = CorporateActionService()
        bar = {"symbol": "AAPL", "ts": _EFF, "open": 200.0, "high": 210.0,
               "low": 190.0, "close": 200.0, "volume": 1000.0}
        result = svc.apply(_action(ActionType.SPLIT, "AAPL", ratio=2.0),
                           bars=[bar], position=_pos("AAPL", 100, 200.0))
        adj = result.adjusted_bars[0]
        assert adj["close"] == 100.0 and adj["volume"] == 2000.0
        assert bar["close"] == 200.0  # raw bar untouched


# ─────────────────────────────────────────────────────────────────────────────
# 2. Reverse split
# ─────────────────────────────────────────────────────────────────────────────

class TestReverseSplitRegression:
    @pytest.mark.parametrize("ratio", [0.1, 0.2, 0.5])
    def test_reverse_split_preserves_value_and_cost_basis(self, ratio):
        svc = CorporateActionService()
        before = _pos("PENNY", 1000, 0.5)
        result = svc.apply(_action(ActionType.REVERSE_SPLIT, "PENNY", ratio=ratio),
                           position=before)
        after = result.position_result.position

        assert after.qty == pytest.approx(before.qty * ratio)        # fewer shares
        assert after.avg_price == pytest.approx(before.avg_price / ratio)  # higher avg
        assert result.position_result.value_preserved is True
        _assert_value_preserved(before, after)

    def test_reverse_split_history_preserves_value(self):
        svc = CorporateActionService()
        before = _pos("PENNY", 500, 2.0)
        svc.apply(_action(ActionType.REVERSE_SPLIT, "PENNY", ratio=0.1), position=before)
        rec = svc.history_for("PENNY")[0]
        assert rec.value_preserved is True
        _assert_value_preserved(rec.position_before, rec.position_after)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Dividend
# ─────────────────────────────────────────────────────────────────────────────

class TestDividendRegression:
    @pytest.mark.parametrize("name", ["DIVIDEND", "CASH_DIVIDEND"])
    def test_dividend_leaves_position_untouched_and_pays_cash(self, name):
        # Both the P2-01B alias (DIVIDEND) and the original (CASH_DIVIDEND) name.
        svc = CorporateActionService()
        before = _pos("KO", 200, 55.0)
        result = svc.apply(_action(getattr(ActionType, name), "KO", cash_amount=1.5),
                           position=before)
        after = result.position_result.position

        # position quantity + average cost unchanged (no phantom P&L)
        assert after.qty == before.qty
        assert after.avg_price == before.avg_price
        # economic value delivered as cash = qty * per-share dividend
        assert result.position_result.cash_delta == pytest.approx(200 * 1.5)
        # book value (cost basis) preserved; value moved to cash, not the position
        assert result.position_result.value_preserved is True
        _assert_value_preserved(before, after)

    def test_dividend_total_wealth_is_position_plus_cash(self):
        svc = CorporateActionService()
        before = _pos("KO", 100, 50.0)
        result = svc.apply(_action(ActionType.DIVIDEND, "KO", cash_amount=2.0),
                           position=before)
        after = result.position_result.position
        wealth_before = _book_value(before) + 0.0
        wealth_after = _book_value(after) + result.position_result.cash_delta
        assert wealth_after == pytest.approx(wealth_before + 100 * 2.0)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Ticker change
# ─────────────────────────────────────────────────────────────────────────────

class TestTickerChangeRegression:
    def test_ticker_change_remaps_symbol_preserving_value_and_cost(self):
        svc = CorporateActionService()
        before = _pos("FB", 80, 175.0)
        result = svc.apply(_action(ActionType.TICKER_CHANGE, "FB", new_symbol="META"),
                           position=before)
        after = result.position_result.position

        assert after.symbol == "META"
        assert after.qty == before.qty              # quantity unchanged
        assert after.avg_price == before.avg_price  # average cost unchanged
        assert result.position_result.value_preserved is True
        _assert_value_preserved(before, after)

    def test_ticker_change_history_keyed_on_old_symbol(self):
        svc = CorporateActionService()
        svc.apply(_action(ActionType.TICKER_CHANGE, "FB", new_symbol="META"),
                  position=_pos("FB", 10, 100.0))
        assert len(svc.history_for("FB")) == 1   # keyed on original symbol
        assert svc.history_for("META") == []


# ─────────────────────────────────────────────────────────────────────────────
# 5. Multiple actions (chain on one symbol)
# ─────────────────────────────────────────────────────────────────────────────

class TestMultipleActionsRegression:
    def test_split_then_dividend_then_ticker_chain(self):
        svc = CorporateActionService()
        before = _pos("OLD", 100, 100.0)   # book value 10,000
        actions = [
            _action(ActionType.SPLIT, "OLD", ratio=2.0),       # -> 200 @ 50
            _action(ActionType.DIVIDEND, "OLD", cash_amount=1.0),  # cash 200, pos unchanged
            _action(ActionType.TICKER_CHANGE, "OLD", new_symbol="NEW"),
        ]
        results = svc.apply_chain(actions, position=before)
        final = results[-1].position_result.position

        assert final.symbol == "NEW"
        assert final.qty == pytest.approx(200)
        assert final.avg_price == pytest.approx(50.0)
        # dividend cash computed off the post-split quantity (200), not the original 100
        assert results[1].position_result.cash_delta == pytest.approx(200.0)
        # book value preserved end-to-end through the whole chain
        _assert_value_preserved(before, final)
        # one history entry per applied action, all keyed on the original symbol
        assert len(svc.history_for("OLD")) == 3

    def test_chain_order_independence_for_split_and_dividend_value(self):
        # Whatever the order, end book value is preserved and total dividend cash
        # equals (shares held at the dividend) * per-share amount.
        before = _pos("X", 100, 10.0)

        svc_a = CorporateActionService()
        res_a = svc_a.apply_chain(
            [_action(ActionType.SPLIT, "X", ratio=2.0),
             _action(ActionType.DIVIDEND, "X", cash_amount=1.0)], position=before)
        cash_a = sum(r.position_result.cash_delta for r in res_a)

        svc_b = CorporateActionService()
        res_b = svc_b.apply_chain(
            [_action(ActionType.DIVIDEND, "X", cash_amount=1.0),
             _action(ActionType.SPLIT, "X", ratio=2.0)], position=before)
        cash_b = sum(r.position_result.cash_delta for r in res_b)

        # dividend-before-split pays on 100 shares; split-before-dividend pays on 200
        assert cash_b == pytest.approx(100.0)
        assert cash_a == pytest.approx(200.0)
        # but book value is preserved either way
        _assert_value_preserved(before, res_a[-1].position_result.position)
        _assert_value_preserved(before, res_b[-1].position_result.position)


# ─────────────────────────────────────────────────────────────────────────────
# 6. Portfolio consistency (multi-symbol)
# ─────────────────────────────────────────────────────────────────────────────

class TestPortfolioConsistencyRegression:
    """A small multi-position portfolio. Corporate actions touch individual
    holdings; the portfolio's total book value must only change by dividend cash
    received, and untouched holdings must be left exactly as they were."""

    def _portfolio(self):
        return {
            "AAPL": _pos("AAPL", 100, 100.0),   # 10,000
            "MSFT": _pos("MSFT", 50, 200.0),    # 10,000
            "KO": _pos("KO", 200, 50.0),        # 10,000
        }

    def test_split_on_one_holding_preserves_total_book_value(self):
        svc = CorporateActionService()
        port = self._portfolio()
        total_before = sum(_book_value(p) for p in port.values())

        res = svc.apply(_action(ActionType.SPLIT, "AAPL", ratio=4.0),
                        position=port["AAPL"])
        port["AAPL"] = res.position_result.position

        # touched holding rebased, untouched holdings identical
        assert port["AAPL"].qty == 400 and port["AAPL"].avg_price == 25.0
        assert port["MSFT"] == _pos("MSFT", 50, 200.0)
        assert port["KO"] == _pos("KO", 200, 50.0)
        # no split-only action changes total portfolio book value
        assert sum(_book_value(p) for p in port.values()) == pytest.approx(total_before)

    def test_mixed_actions_conserve_position_plus_cash(self):
        svc = CorporateActionService()
        port = self._portfolio()
        cash = 0.0
        book_before = sum(_book_value(p) for p in port.values())

        # split AAPL (value-preserving), dividend on KO (pays cash), rename MSFT
        r1 = svc.apply(_action(ActionType.SPLIT, "AAPL", ratio=2.0), position=port["AAPL"])
        port["AAPL"] = r1.position_result.position

        r2 = svc.apply(_action(ActionType.DIVIDEND, "KO", cash_amount=1.5), position=port["KO"])
        port["KO"] = r2.position_result.position
        cash += r2.position_result.cash_delta

        r3 = svc.apply(_action(ActionType.TICKER_CHANGE, "MSFT", new_symbol="MSFT2"),
                       position=port["MSFT"])
        port.pop("MSFT")
        port[r3.position_result.position.symbol] = r3.position_result.position

        book_after = sum(_book_value(p) for p in port.values())
        # book value of positions is unchanged by split/ticker/dividend...
        assert book_after == pytest.approx(book_before)
        # ...and total wealth grew by exactly the dividend cash received
        assert cash == pytest.approx(200 * 1.5)
        assert (book_after + cash) == pytest.approx(book_before + 200 * 1.5)
        # symbol remap applied; old key gone, new key present, value intact
        assert "MSFT" not in port and port["MSFT2"].qty == 50 and port["MSFT2"].avg_price == 200.0

    def test_history_spans_all_touched_symbols(self):
        svc = CorporateActionService()
        port = self._portfolio()
        svc.apply(_action(ActionType.SPLIT, "AAPL", ratio=2.0), position=port["AAPL"])
        svc.apply(_action(ActionType.DIVIDEND, "KO", cash_amount=1.0), position=port["KO"])
        assert len(svc.history_for()) == 2
        assert len(svc.history_for("AAPL")) == 1
        assert len(svc.history_for("KO")) == 1
        # every recorded adjustment preserved position book value
        assert all(r.value_preserved for r in svc.history_for())
