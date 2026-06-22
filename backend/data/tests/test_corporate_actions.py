"""
Tests for backend/data/corporate_actions.py (CorporateActionService)

Covers all 6 components (CorporateAction model, PriceAdjuster,
PositionAdjuster, CorporateActionDetector, AdjustmentAuditLog,
CorporateActionGate) and the orchestrator, including all 7 required
scenarios:
  stock split, reverse split, cash dividend, ticker change,
  multiple action chain, adjusted price consistency,
  position adjustment consistency.
"""
import json
from datetime import date

import pytest
from backend.database.testing import make_test_engine
from sqlalchemy.orm import sessionmaker

from backend.data.corporate_actions import (
    ActionStatus,
    ActionType,
    AdjustmentAuditLog,
    AdjustmentFactor,
    CorporateAction,
    CorporateActionDetector,
    CorporateActionGate,
    CorporateActionPendingError,
    CorporateActionService,
    PositionAdjuster,
    PositionSnapshot,
    PriceAdjuster,
)
from backend.database.models import AuditLog, Base


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

_EFF = date(2026, 6, 5)


def _bar(symbol="AAPL", ts=_EFF, open=100.0, high=105.0, low=99.0, close=100.0, volume=1000.0) -> dict:
    return {"symbol": symbol, "ts": ts, "open": open, "high": high,
            "low": low, "close": close, "volume": volume}


def _sqlite_factory():
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _count_audit(factory, event_type: str) -> int:
    sess = factory()
    try:
        return sess.query(AuditLog).filter(AuditLog.event_type == event_type).count()
    finally:
        sess.close()


# ─────────────────────────────────────────────────────────────────────────────
# TestCorporateActionModel
# ─────────────────────────────────────────────────────────────────────────────

class TestCorporateActionModel:
    def test_split_valid_with_ratio(self):
        action = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                   effective_date=_EFF, ratio=2.0)
        assert action.is_valid() is True

    def test_split_invalid_without_ratio(self):
        action = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL", effective_date=_EFF)
        assert action.is_valid() is False

    def test_split_invalid_ratio_of_one(self):
        action = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                   effective_date=_EFF, ratio=1.0)
        assert action.is_valid() is False

    def test_reverse_split_valid_with_ratio(self):
        action = CorporateAction(action_type=ActionType.REVERSE_SPLIT, symbol="AAPL",
                                   effective_date=_EFF, ratio=0.1)
        assert action.is_valid() is True

    def test_cash_dividend_valid_with_amount(self):
        action = CorporateAction(action_type=ActionType.CASH_DIVIDEND, symbol="AAPL",
                                   effective_date=_EFF, cash_amount=1.5)
        assert action.is_valid() is True

    def test_cash_dividend_invalid_without_amount(self):
        action = CorporateAction(action_type=ActionType.CASH_DIVIDEND, symbol="AAPL", effective_date=_EFF)
        assert action.is_valid() is False

    def test_cash_dividend_invalid_with_zero_amount(self):
        action = CorporateAction(action_type=ActionType.CASH_DIVIDEND, symbol="AAPL",
                                   effective_date=_EFF, cash_amount=0.0)
        assert action.is_valid() is False

    def test_ticker_change_valid_with_new_symbol(self):
        action = CorporateAction(action_type=ActionType.TICKER_CHANGE, symbol="OLD",
                                   effective_date=_EFF, new_symbol="NEW")
        assert action.is_valid() is True

    def test_ticker_change_invalid_without_new_symbol(self):
        action = CorporateAction(action_type=ActionType.TICKER_CHANGE, symbol="OLD", effective_date=_EFF)
        assert action.is_valid() is False

    def test_classified_keeps_valid_action_unchanged(self):
        action = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                   effective_date=_EFF, ratio=2.0, status=ActionStatus.CONFIRMED)
        classified = action.classified()
        assert classified is action

    def test_classified_downgrades_invalid_action_to_unknown(self):
        action = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                   effective_date=_EFF, status=ActionStatus.CONFIRMED)
        classified = action.classified()
        assert classified.status == ActionStatus.UNKNOWN
        assert classified.detail

    def test_corporate_action_pending_error_is_not_runtime_error(self):
        action = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                   effective_date=_EFF, ratio=2.0, status=ActionStatus.CONFIRMED)
        exc = CorporateActionPendingError(action)
        assert not isinstance(exc, RuntimeError)
        assert exc.action is action


# ─────────────────────────────────────────────────────────────────────────────
# TestPriceAdjuster
# ─────────────────────────────────────────────────────────────────────────────

class TestPriceAdjuster:
    def test_factor_for_split(self):
        action = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                   effective_date=_EFF, ratio=2.0)
        factor = PriceAdjuster.factor_for(action)
        assert factor.price_factor == 0.5
        assert factor.qty_factor == 2.0

    def test_factor_for_reverse_split(self):
        action = CorporateAction(action_type=ActionType.REVERSE_SPLIT, symbol="AAPL",
                                   effective_date=_EFF, ratio=0.1)
        factor = PriceAdjuster.factor_for(action)
        assert factor.price_factor == 10.0
        assert factor.qty_factor == 0.1

    def test_factor_for_cash_dividend(self):
        action = CorporateAction(action_type=ActionType.CASH_DIVIDEND, symbol="AAPL",
                                   effective_date=_EFF, cash_amount=1.5)
        factor = PriceAdjuster.factor_for(action)
        assert factor.price_factor == 1.0
        assert factor.qty_factor == 1.0
        assert factor.cash_per_share == 1.5

    def test_factor_for_ticker_change(self):
        action = CorporateAction(action_type=ActionType.TICKER_CHANGE, symbol="OLD",
                                   effective_date=_EFF, new_symbol="NEW")
        factor = PriceAdjuster.factor_for(action)
        assert factor.price_factor == 1.0
        assert factor.qty_factor == 1.0
        assert factor.new_symbol == "NEW"

    def test_adjust_bar_scales_ohlc_and_volume(self):
        bar = _bar(open=100.0, high=110.0, low=90.0, close=100.0, volume=1000.0)
        factor = AdjustmentFactor(price_factor=0.5, qty_factor=2.0)
        adjusted = PriceAdjuster.adjust_bar(bar, factor)
        assert adjusted["open"] == 50.0
        assert adjusted["high"] == 55.0
        assert adjusted["low"] == 45.0
        assert adjusted["close"] == 50.0
        assert adjusted["volume"] == 2000.0

    def test_adjust_bar_does_not_mutate_input(self):
        bar = _bar(close=100.0, volume=1000.0)
        factor = AdjustmentFactor(price_factor=0.5, qty_factor=2.0)
        adjusted = PriceAdjuster.adjust_bar(bar, factor)
        assert bar["close"] == 100.0
        assert bar["volume"] == 1000.0
        assert adjusted is not bar

    def test_adjust_bar_ticker_change_remaps_symbol(self):
        bar = _bar(symbol="OLD")
        factor = AdjustmentFactor(new_symbol="NEW")
        adjusted = PriceAdjuster.adjust_bar(bar, factor)
        assert adjusted["symbol"] == "NEW"
        assert bar["symbol"] == "OLD"

    def test_adjust_bars_applies_to_list(self):
        bars = [_bar(close=100.0), _bar(close=110.0)]
        factor = AdjustmentFactor(price_factor=0.5)
        adjusted = PriceAdjuster.adjust_bars(bars, factor)
        assert [b["close"] for b in adjusted] == [50.0, 55.0]

    def test_combine_multiplies_price_and_qty_factors(self):
        f1 = AdjustmentFactor(price_factor=0.5, qty_factor=2.0)
        f2 = AdjustmentFactor(price_factor=0.5, qty_factor=2.0)
        combined = PriceAdjuster.combine([f1, f2])
        assert combined.price_factor == 0.25
        assert combined.qty_factor == 4.0

    def test_adjusted_price_consistency_sequential_vs_combined(self):
        bars = [_bar(open=100.0, high=110.0, low=90.0, close=100.0, volume=1000.0)]
        f1 = AdjustmentFactor(price_factor=0.5, qty_factor=2.0)
        f2 = AdjustmentFactor(price_factor=0.25, qty_factor=4.0)

        sequential = PriceAdjuster.adjust_bars(PriceAdjuster.adjust_bars(bars, f1), f2)
        combined = PriceAdjuster.adjust_bars(bars, PriceAdjuster.combine([f1, f2]))

        assert sequential[0]["close"] == pytest.approx(combined[0]["close"])
        assert sequential[0]["volume"] == pytest.approx(combined[0]["volume"])


# ─────────────────────────────────────────────────────────────────────────────
# TestPositionAdjuster
# ─────────────────────────────────────────────────────────────────────────────

class TestPositionAdjuster:
    def test_split_doubles_qty_halves_avg_price(self):
        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=100.0)
        factor = PriceAdjuster.factor_for(
            CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL", effective_date=_EFF, ratio=2.0))
        result = PositionAdjuster.adjust(position, factor)
        assert result.position.qty == 200
        assert result.position.avg_price == 50.0
        assert result.value_preserved is True

    def test_reverse_split_shrinks_qty_grows_avg_price(self):
        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=10.0)
        factor = PriceAdjuster.factor_for(
            CorporateAction(action_type=ActionType.REVERSE_SPLIT, symbol="AAPL", effective_date=_EFF, ratio=0.1))
        result = PositionAdjuster.adjust(position, factor)
        assert result.position.qty == pytest.approx(10.0)
        assert result.position.avg_price == pytest.approx(100.0)
        assert result.value_preserved is True

    def test_cash_dividend_leaves_position_unchanged_and_computes_cash_delta(self):
        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=150.0)
        factor = PriceAdjuster.factor_for(
            CorporateAction(action_type=ActionType.CASH_DIVIDEND, symbol="AAPL",
                            effective_date=_EFF, cash_amount=1.0))
        result = PositionAdjuster.adjust(position, factor)
        assert result.position.qty == 100
        assert result.position.avg_price == 150.0
        assert result.cash_delta == 100.0
        assert result.value_preserved is True

    def test_ticker_change_remaps_symbol_leaves_qty_and_price(self):
        position = PositionSnapshot(symbol="OLD", qty=100, avg_price=50.0)
        factor = PriceAdjuster.factor_for(
            CorporateAction(action_type=ActionType.TICKER_CHANGE, symbol="OLD",
                            effective_date=_EFF, new_symbol="NEW"))
        result = PositionAdjuster.adjust(position, factor)
        assert result.position.symbol == "NEW"
        assert result.position.qty == 100
        assert result.position.avg_price == 50.0
        assert result.value_preserved is True

    def test_from_obj_duck_types_position_like_object(self):
        class FakePosition:
            symbol = "AAPL"
            qty = 10
            avg_price = 200.0

        snapshot = PositionSnapshot.from_obj(FakePosition())
        assert snapshot == PositionSnapshot(symbol="AAPL", qty=10, avg_price=200.0)


# ─────────────────────────────────────────────────────────────────────────────
# TestCorporateActionDetector
# ─────────────────────────────────────────────────────────────────────────────

class TestCorporateActionDetector:
    def test_fifty_percent_drop_detected_as_split(self):
        detector = CorporateActionDetector()
        action = detector.detect_from_price_jump("AAPL", prev_close=100.0, curr_close=50.0, effective_date=_EFF)
        assert action is not None
        assert action.action_type == ActionType.SPLIT
        assert action.ratio == 2.0
        assert action.status == ActionStatus.PENDING

    def test_tenx_rise_detected_as_reverse_split(self):
        detector = CorporateActionDetector()
        action = detector.detect_from_price_jump("AAPL", prev_close=10.0, curr_close=100.0, effective_date=_EFF)
        assert action is not None
        assert action.action_type == ActionType.REVERSE_SPLIT
        assert action.ratio == pytest.approx(0.1)
        assert action.status == ActionStatus.PENDING

    def test_normal_daily_move_not_detected(self):
        detector = CorporateActionDetector()
        action = detector.detect_from_price_jump("AAPL", prev_close=100.0, curr_close=97.0, effective_date=_EFF)
        assert action is None

    def test_three_for_two_split_within_tolerance(self):
        detector = CorporateActionDetector()
        # 3-for-2 split: price drops to 2/3 of previous
        action = detector.detect_from_price_jump("AAPL", prev_close=100.0, curr_close=66.7, effective_date=_EFF)
        assert action is not None
        assert action.action_type == ActionType.SPLIT
        assert action.ratio == pytest.approx(1.5)

    def test_none_when_prices_missing_or_nonpositive(self):
        detector = CorporateActionDetector()
        assert detector.detect_from_price_jump("AAPL", None, 50.0, _EFF) is None
        assert detector.detect_from_price_jump("AAPL", 100.0, None, _EFF) is None
        assert detector.detect_from_price_jump("AAPL", 0.0, 50.0, _EFF) is None
        assert detector.detect_from_price_jump("AAPL", 100.0, -1.0, _EFF) is None


# ─────────────────────────────────────────────────────────────────────────────
# TestCorporateActionGate
# ─────────────────────────────────────────────────────────────────────────────

class TestCorporateActionGate:
    def _action(self, status, action_type=ActionType.SPLIT, ratio=2.0):
        return CorporateAction(action_type=action_type, symbol="AAPL",
                                 effective_date=_EFF, status=status, ratio=ratio)

    def test_confirmed_always_blocks(self):
        gate = CorporateActionGate(block_on_unconfirmed=False)
        action = self._action(ActionStatus.CONFIRMED)
        assert gate.is_blocking(action) is True

    def test_pending_blocks_by_default(self):
        gate = CorporateActionGate()
        action = self._action(ActionStatus.PENDING)
        assert gate.is_blocking(action) is True

    def test_unknown_blocks_by_default(self):
        gate = CorporateActionGate()
        action = self._action(ActionStatus.UNKNOWN)
        assert gate.is_blocking(action) is True

    def test_pending_does_not_block_when_configured(self):
        gate = CorporateActionGate(block_on_unconfirmed=False)
        action = self._action(ActionStatus.PENDING)
        assert gate.is_blocking(action) is False

    def test_assert_tradeable_raises_on_first_blocking_action(self):
        gate = CorporateActionGate()
        action = self._action(ActionStatus.PENDING)
        with pytest.raises(CorporateActionPendingError) as exc_info:
            gate.assert_tradeable([action])
        assert exc_info.value.action is action

    def test_assert_tradeable_noop_for_empty_or_non_blocking(self):
        gate = CorporateActionGate(block_on_unconfirmed=False)
        gate.assert_tradeable([])
        gate.assert_tradeable([self._action(ActionStatus.PENDING), self._action(ActionStatus.UNKNOWN)])


# ─────────────────────────────────────────────────────────────────────────────
# TestAdjustmentAuditLog
# ─────────────────────────────────────────────────────────────────────────────

class TestAdjustmentAuditLog:
    def _action(self, status=ActionStatus.PENDING, action_type=ActionType.SPLIT, ratio=2.0):
        return CorporateAction(action_type=action_type, symbol="AAPL",
                                 effective_date=_EFF, status=status, ratio=ratio)

    def test_record_detected_persists(self):
        factory = _sqlite_factory()
        log = AdjustmentAuditLog(db_factory=factory)
        log.record_detected(self._action())
        assert _count_audit(factory, AdjustmentAuditLog.EVENT_DETECTED) == 1

    def test_record_registered_persists(self):
        factory = _sqlite_factory()
        log = AdjustmentAuditLog(db_factory=factory)
        log.record_registered(self._action())
        assert _count_audit(factory, AdjustmentAuditLog.EVENT_REGISTERED) == 1

    def test_record_applied_persists_with_position_extra(self):
        factory = _sqlite_factory()
        log = AdjustmentAuditLog(db_factory=factory)
        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=100.0)
        action = self._action()
        factor = PriceAdjuster.factor_for(action)
        result = PositionAdjuster.adjust(position, factor)
        log.record_applied(action, result)

        sess = factory()
        try:
            row = sess.query(AuditLog).filter(AuditLog.event_type == AdjustmentAuditLog.EVENT_APPLIED).one()
        finally:
            sess.close()
        body = json.loads(row.detail)
        assert body["cash_delta"] == 0.0
        assert body["value_before"] == pytest.approx(body["value_after"])

    def test_record_blocked_persists(self):
        factory = _sqlite_factory()
        log = AdjustmentAuditLog(db_factory=factory)
        log.record_blocked(self._action(status=ActionStatus.CONFIRMED))
        assert _count_audit(factory, AdjustmentAuditLog.EVENT_BLOCKED) == 1

    def test_no_db_factory_does_not_raise(self):
        log = AdjustmentAuditLog(db_factory=None)
        log.record_detected(self._action())
        log.record_applied(self._action())


# ─────────────────────────────────────────────────────────────────────────────
# TestCorporateActionService — required scenarios + other coverage
# ─────────────────────────────────────────────────────────────────────────────

class TestCorporateActionService:
    # --- stock split ---
    def test_stock_split_detect_and_apply(self):
        svc = CorporateActionService()
        prev_bar = _bar(close=100.0)
        curr_bar = _bar(close=50.0)
        action = svc.detect_from_bars("AAPL", prev_bar, curr_bar)
        assert action is not None
        assert action.action_type == ActionType.SPLIT
        assert action.ratio == 2.0
        assert svc.pending_for("AAPL") == [action]

        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=100.0)
        result = svc.apply(action, bars=[curr_bar], position=position)

        assert result.adjusted_bars[0]["close"] == 25.0
        assert result.adjusted_bars[0]["volume"] == curr_bar["volume"] * 2
        assert result.position_result.position.qty == 200
        assert result.position_result.position.avg_price == 50.0
        assert result.position_result.value_preserved is True
        assert svc.pending_for("AAPL") == []

    # --- reverse split ---
    def test_reverse_split_detect_and_apply(self):
        svc = CorporateActionService()
        prev_bar = _bar(close=10.0)
        curr_bar = _bar(close=100.0)
        action = svc.detect_from_bars("AAPL", prev_bar, curr_bar)
        assert action is not None
        assert action.action_type == ActionType.REVERSE_SPLIT
        assert action.ratio == pytest.approx(0.1)

        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=10.0)
        result = svc.apply(action, bars=[curr_bar], position=position)

        assert result.adjusted_bars[0]["close"] == pytest.approx(1000.0)
        assert result.adjusted_bars[0]["volume"] == pytest.approx(curr_bar["volume"] * 0.1)
        assert result.position_result.position.qty == pytest.approx(10.0)
        assert result.position_result.position.avg_price == pytest.approx(100.0)
        assert result.position_result.value_preserved is True

    # --- cash dividend ---
    def test_cash_dividend_register_and_apply(self):
        svc = CorporateActionService()
        action = CorporateAction(action_type=ActionType.CASH_DIVIDEND, symbol="AAPL",
                                   effective_date=_EFF, cash_amount=1.0)
        registered = svc.register_action(action)
        assert registered.status == ActionStatus.UNKNOWN or registered.is_valid()
        assert registered.is_valid() is True  # cash_amount provided -> stays as given status

        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=150.0)
        result = svc.apply(registered, position=position)

        assert result.position_result.cash_delta == 100.0
        assert result.position_result.position.qty == 100
        assert result.position_result.position.avg_price == 150.0
        assert svc.pending_for("AAPL") == []

    # --- ticker change ---
    def test_ticker_change_register_and_apply(self):
        svc = CorporateActionService()
        action = CorporateAction(action_type=ActionType.TICKER_CHANGE, symbol="OLD",
                                   effective_date=_EFF, new_symbol="NEWSYM")
        registered = svc.register_action(action)

        bar = _bar(symbol="OLD", close=50.0, volume=500.0)
        position = PositionSnapshot(symbol="OLD", qty=10, avg_price=50.0)
        result = svc.apply(registered, bars=[bar], position=position)

        assert result.adjusted_bars[0]["symbol"] == "NEWSYM"
        assert result.adjusted_bars[0]["close"] == 50.0
        assert result.position_result.position.symbol == "NEWSYM"
        assert result.position_result.position.qty == 10
        assert result.position_result.position.avg_price == 50.0

    # --- multiple action chain ---
    def test_multiple_action_chain(self):
        factory = _sqlite_factory()
        svc = CorporateActionService(db_factory=factory)

        split = svc.register_action(
            CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL", effective_date=_EFF, ratio=2.0))
        dividend = svc.register_action(
            CorporateAction(action_type=ActionType.CASH_DIVIDEND, symbol="AAPL",
                            effective_date=_EFF, cash_amount=1.0))
        ticker_change = svc.register_action(
            CorporateAction(action_type=ActionType.TICKER_CHANGE, symbol="AAPL",
                            effective_date=_EFF, new_symbol="NEWSYM"))

        pending = svc.pending_for("AAPL")
        assert pending == [split, dividend, ticker_change]

        bar = _bar(symbol="AAPL", close=100.0, volume=1000.0)
        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=100.0)
        results = svc.apply_chain(pending, bars=[bar], position=position)

        # split halves price, doubles volume
        assert results[0].adjusted_bars[0]["close"] == 50.0
        assert results[0].adjusted_bars[0]["volume"] == 2000.0
        # dividend: cash_delta computed off the post-split qty (200)
        assert results[1].position_result.cash_delta == 200.0
        # ticker change: symbol remapped on final bars/position
        final_bars = results[-1].adjusted_bars
        final_position = results[-1].position_result.position
        assert final_bars[0]["symbol"] == "NEWSYM"
        assert final_bars[0]["close"] == 50.0
        assert final_position.symbol == "NEWSYM"
        assert final_position.qty == 200
        assert final_position.avg_price == 50.0

        assert svc.pending_for("AAPL") == []
        assert _count_audit(factory, AdjustmentAuditLog.EVENT_APPLIED) == 3

    # --- adjusted price consistency (service-level) ---
    def test_adjusted_price_consistency_detect_then_manual_adjust_match(self):
        svc = CorporateActionService()
        prev_bar = _bar(close=200.0)
        curr_bar = _bar(close=100.0, volume=500.0)
        action = svc.detect_from_bars("AAPL", prev_bar, curr_bar)

        result = svc.apply(action, bars=[curr_bar])
        manual_factor = PriceAdjuster.factor_for(action)
        manual_bar = PriceAdjuster.adjust_bar(curr_bar, manual_factor)

        assert result.adjusted_bars[0] == manual_bar

    # --- position adjustment consistency (service-level) ---
    def test_position_adjustment_consistency_for_split(self):
        svc = CorporateActionService()
        action = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                   effective_date=_EFF, ratio=4.0)
        position = PositionSnapshot(symbol="AAPL", qty=40, avg_price=400.0)
        result = svc.apply(action, position=position)

        assert result.position_result.value_before == pytest.approx(16000.0)
        assert result.position_result.value_after == pytest.approx(16000.0)
        assert result.position_result.value_preserved is True

    # --- trading gate integration ---
    def test_assert_tradeable_blocks_pending_action_by_default(self):
        svc = CorporateActionService()
        prev_bar = _bar(close=100.0)
        curr_bar = _bar(close=50.0)
        svc.detect_from_bars("AAPL", prev_bar, curr_bar)

        with pytest.raises(CorporateActionPendingError):
            svc.assert_tradeable("AAPL")

    def test_assert_tradeable_allows_when_gate_configured(self):
        svc = CorporateActionService(gate=CorporateActionGate(block_on_unconfirmed=False))
        prev_bar = _bar(close=100.0)
        curr_bar = _bar(close=50.0)
        svc.detect_from_bars("AAPL", prev_bar, curr_bar)

        svc.assert_tradeable("AAPL")  # should not raise

    def test_assert_tradeable_passes_after_apply(self):
        svc = CorporateActionService()
        prev_bar = _bar(close=100.0)
        curr_bar = _bar(close=50.0)
        action = svc.detect_from_bars("AAPL", prev_bar, curr_bar)
        svc.apply(action, bars=[curr_bar])

        svc.assert_tradeable("AAPL")  # should not raise

    # --- reset ---
    def test_reset_single_symbol(self):
        svc = CorporateActionService()
        svc.detect_from_bars("AAPL", _bar(close=100.0), _bar(close=50.0))
        svc.detect_from_bars("MSFT", _bar(close=100.0), _bar(close=50.0))
        svc.reset("AAPL")
        assert svc.pending_for("AAPL") == []
        assert len(svc.pending_for("MSFT")) == 1

    def test_reset_all(self):
        svc = CorporateActionService()
        svc.detect_from_bars("AAPL", _bar(close=100.0), _bar(close=50.0))
        svc.detect_from_bars("MSFT", _bar(close=100.0), _bar(close=50.0))
        svc.reset()
        assert svc.pending_for("AAPL") == []
        assert svc.pending_for("MSFT") == []

    # --- multi-symbol independence ---
    def test_multi_symbol_independence(self):
        svc = CorporateActionService()
        svc.detect_from_bars("AAPL", _bar(close=100.0), _bar(close=50.0))
        assert len(svc.pending_for("AAPL")) == 1
        assert svc.pending_for("MSFT") == []

    # --- AuditLog persistence integration ---
    def test_audit_log_persistence_lifecycle(self):
        factory = _sqlite_factory()
        svc = CorporateActionService(db_factory=factory)

        action = svc.detect_from_bars("AAPL", _bar(close=100.0), _bar(close=50.0))
        assert _count_audit(factory, AdjustmentAuditLog.EVENT_DETECTED) == 1

        registered = svc.register_action(
            CorporateAction(action_type=ActionType.TICKER_CHANGE, symbol="MSFT",
                            effective_date=_EFF, new_symbol="NEWMSFT"))
        assert _count_audit(factory, AdjustmentAuditLog.EVENT_REGISTERED) == 1

        with pytest.raises(CorporateActionPendingError):
            svc.assert_tradeable("AAPL")
        assert _count_audit(factory, AdjustmentAuditLog.EVENT_BLOCKED) == 1

        svc.apply(action)
        svc.apply(registered)
        assert _count_audit(factory, AdjustmentAuditLog.EVENT_APPLIED) == 2

    def test_no_db_factory_does_not_raise(self):
        svc = CorporateActionService()
        action = svc.detect_from_bars("AAPL", _bar(close=100.0), _bar(close=50.0))
        svc.apply(action)
        assert svc.pending_for("AAPL") == []


# ─────────────────────────────────────────────────────────────────────────────
# TASK P2-01B additions: DIVIDEND alias, UNKNOWN type, fail-closed apply,
# adjustment history
# ─────────────────────────────────────────────────────────────────────────────

from backend.data.corporate_actions import (  # noqa: E402
    AdjustmentRecord,
    SUPPORTED_ACTION_TYPES,
    UnsupportedCorporateActionError,
)


class TestActionTypeP2_01B:
    def test_dividend_is_alias_of_cash_dividend(self):
        # Task P2-01B uses the name DIVIDEND; the shipped enum member is
        # CASH_DIVIDEND. They must be the same member (same wire value).
        assert ActionType.DIVIDEND is ActionType.CASH_DIVIDEND
        assert ActionType.DIVIDEND.value == "cash_dividend"
        assert ActionType("cash_dividend") is ActionType.CASH_DIVIDEND

    def test_unknown_action_type_exists_and_is_unsupported(self):
        assert ActionType.UNKNOWN.value == "unknown"
        assert ActionType.UNKNOWN not in SUPPORTED_ACTION_TYPES

    def test_unknown_action_is_never_valid(self):
        action = CorporateAction(action_type=ActionType.UNKNOWN, symbol="AAPL",
                                   effective_date=_EFF, ratio=2.0, cash_amount=1.0,
                                   new_symbol="NEW")
        # Even with every field populated, an UNKNOWN type is not valid.
        assert action.is_valid() is False

    def test_supported_action_types_are_the_four_real_ones(self):
        assert SUPPORTED_ACTION_TYPES == frozenset({
            ActionType.SPLIT, ActionType.REVERSE_SPLIT,
            ActionType.CASH_DIVIDEND, ActionType.TICKER_CHANGE,
        })

    def test_dividend_alias_constructs_valid_action(self):
        action = CorporateAction(action_type=ActionType.DIVIDEND, symbol="AAPL",
                                   effective_date=_EFF, cash_amount=2.0)
        assert action.is_valid() is True
        assert action.action_type == ActionType.CASH_DIVIDEND


class TestFailClosedApply:
    def test_apply_unknown_type_raises_and_stays_pending(self):
        factory = _sqlite_factory()
        svc = CorporateActionService(db_factory=factory)
        action = CorporateAction(action_type=ActionType.UNKNOWN, symbol="AAPL",
                                   effective_date=_EFF)
        svc.register_action(action)  # lands in pending (registered)

        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=100.0)
        with pytest.raises(UnsupportedCorporateActionError) as exc_info:
            svc.apply(action, position=position)

        # fail-closed: still pending (symbol stays blocked), nothing applied.
        # (register_action stored the classified() copy, so compare by type/symbol,
        # not object identity.)
        still_pending = svc.pending_for("AAPL")
        assert len(still_pending) == 1
        assert still_pending[0].action_type == ActionType.UNKNOWN
        assert svc.history_for("AAPL") == []
        assert _count_audit(factory, AdjustmentAuditLog.EVENT_BLOCKED) == 1
        assert _count_audit(factory, AdjustmentAuditLog.EVENT_APPLIED) == 0
        assert exc_info.value.action.action_type == ActionType.UNKNOWN

    def test_apply_invalid_split_without_ratio_fails_closed(self):
        svc = CorporateActionService()
        action = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                   effective_date=_EFF)  # missing ratio -> invalid
        with pytest.raises(UnsupportedCorporateActionError):
            svc.apply(action, position=PositionSnapshot("AAPL", 10, 100.0))
        assert svc.history_for("AAPL") == []

    def test_unsupported_error_is_not_runtime_error(self):
        action = CorporateAction(action_type=ActionType.UNKNOWN, symbol="AAPL",
                                   effective_date=_EFF)
        exc = UnsupportedCorporateActionError(action)
        assert not isinstance(exc, RuntimeError)
        assert exc.action is action

    def test_apply_chain_fails_closed_midway(self):
        svc = CorporateActionService()
        good = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                 effective_date=_EFF, ratio=2.0)
        bad = CorporateAction(action_type=ActionType.UNKNOWN, symbol="AAPL",
                                effective_date=_EFF)
        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=100.0)
        with pytest.raises(UnsupportedCorporateActionError):
            svc.apply_chain([good, bad], position=position)
        # the good split was applied and recorded before the bad one halted the chain
        assert len(svc.history_for("AAPL")) == 1


class TestAdjustmentHistory:
    def test_split_apply_records_history_with_before_after(self):
        svc = CorporateActionService()
        action = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                   effective_date=_EFF, ratio=2.0)
        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=100.0)
        svc.apply(action, position=position)

        history = svc.history_for("AAPL")
        assert len(history) == 1
        rec = history[0]
        assert isinstance(rec, AdjustmentRecord)
        assert rec.position_before == position
        assert rec.position_after.qty == 200
        assert rec.position_after.avg_price == 50.0
        assert rec.cash_delta == 0.0
        assert rec.value_preserved is True

    def test_dividend_history_carries_cash_delta_and_preserves_value(self):
        svc = CorporateActionService()
        action = CorporateAction(action_type=ActionType.DIVIDEND, symbol="AAPL",
                                   effective_date=_EFF, cash_amount=1.0)
        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=150.0)
        svc.apply(action, position=position)

        rec = svc.history_for("AAPL")[0]
        assert rec.cash_delta == 100.0
        assert rec.position_after.qty == 100
        assert rec.position_after.avg_price == 150.0
        assert rec.value_preserved is True

    def test_history_without_position_is_recorded(self):
        svc = CorporateActionService()
        action = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                   effective_date=_EFF, ratio=2.0)
        svc.apply(action, bars=[_bar(close=100.0)])
        rec = svc.history_for("AAPL")[0]
        assert rec.position_before is None
        assert rec.position_after is None
        assert rec.value_preserved is True  # vacuously preserved when no position

    def test_history_for_filters_by_symbol_and_global_returns_all(self):
        svc = CorporateActionService()
        svc.apply(CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                    effective_date=_EFF, ratio=2.0))
        svc.apply(CorporateAction(action_type=ActionType.SPLIT, symbol="MSFT",
                                    effective_date=_EFF, ratio=3.0))
        assert len(svc.history_for("AAPL")) == 1
        assert len(svc.history_for("MSFT")) == 1
        assert len(svc.history_for()) == 2

    def test_ticker_change_history_keyed_on_original_symbol(self):
        svc = CorporateActionService()
        action = CorporateAction(action_type=ActionType.TICKER_CHANGE, symbol="OLD",
                                   effective_date=_EFF, new_symbol="NEW")
        position = PositionSnapshot(symbol="OLD", qty=10, avg_price=50.0)
        svc.apply(action, position=position)
        # history is keyed on the pre-change symbol
        assert len(svc.history_for("OLD")) == 1
        assert svc.history_for("NEW") == []
        assert svc.history_for("OLD")[0].position_after.symbol == "NEW"

    def test_chain_records_one_history_entry_per_applied_action(self):
        svc = CorporateActionService()
        split = CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                  effective_date=_EFF, ratio=2.0)
        dividend = CorporateAction(action_type=ActionType.DIVIDEND, symbol="AAPL",
                                     effective_date=_EFF, cash_amount=1.0)
        position = PositionSnapshot(symbol="AAPL", qty=100, avg_price=100.0)
        svc.apply_chain([split, dividend], position=position)
        assert len(svc.history_for("AAPL")) == 2

    def test_reset_keeps_history_clear_history_empties_it(self):
        svc = CorporateActionService()
        svc.apply(CorporateAction(action_type=ActionType.SPLIT, symbol="AAPL",
                                    effective_date=_EFF, ratio=2.0))
        svc.reset()
        assert len(svc.history_for("AAPL")) == 1  # reset does not touch history
        svc.clear_history()
        assert svc.history_for("AAPL") == []
