"""
TASK 1-3C — Market routing validation tests.

Covers:
- KIS rejects KR symbols when KIS_MARKET_ENFORCEMENT=true
- KIS accepts US symbols (always)
- KIS allows KR symbols when enforcement is off (warns only)
- Kiwoom rejects US symbols when enforcement is on
- Kiwoom passes KR symbols (then raises NotImplementedError as expected)
- MarketRouter routes KR symbols to kr_broker
- MarketRouter routes US symbols to us_broker
- MarketRouter.get_positions() merges both (silences NotImplementedError from stub)
- MarketRouter.get_balance() sums both (silences NotImplementedError from stub)
"""
import importlib
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from backend.brokers.capabilities import KIS_LIVE_CAPABILITIES, KIWOOM_CAPABILITIES
from backend.brokers.models import Balance, BrokerCapabilities, Order, OrderStatus, Position
from backend.brokers.router import MarketRouter
from backend.brokers.validator import BrokerCapabilityValidator, OrderRequest, UnsupportedCapabilityError


# ── Helpers ──────────────────────────────────────────────────────────────────

KR_SYMBOL = "005930"   # Samsung — 6-digit KR domestic
US_SYMBOL = "AAPL"     # Apple — US ticker

_FILLED_ORDER = Order(id="x", symbol="", side="buy", qty=1, price=1.0, status=OrderStatus.FILLED)
_BALANCE = Balance(cash_krw=1_000_000.0, cash_usd=0.0, total_eval_krw=1_000_000.0)
_KR_POS = [Position(symbol=KR_SYMBOL, qty=10, avg_price=70000.0, market="KR")]
_US_POS = [Position(symbol=US_SYMBOL, qty=5, avg_price=150.0, market="US")]


def _make_broker(positions=None, balance=None):
    """Return a MagicMock BrokerAdapter with configurable return values."""
    b = MagicMock()
    b.get_positions.return_value = positions or []
    b.get_balance.return_value = balance or _BALANCE
    b.place_order.return_value = _FILLED_ORDER
    b.cancel_order.return_value = True
    b.get_order_status.return_value = None
    b.get_price.return_value = 70000.0
    return b


def _validator(caps: BrokerCapabilities) -> BrokerCapabilityValidator:
    return BrokerCapabilityValidator(caps)


def _req(symbol: str, market: str, side: str = "buy") -> OrderRequest:
    return OrderRequest(symbol=symbol, side=side, qty=1.0, price=1000.0,
                        order_type="limit", market=market)


# ── Validator market routing (unit) ──────────────────────────────────────────

class TestValidatorMarketRouting:

    def test_us_symbol_accepted_by_kis(self):
        v = _validator(KIS_LIVE_CAPABILITIES)
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", True):
            v._check_market_routing(_req(US_SYMBOL, "US"))  # no raise

    def test_kr_symbol_rejected_by_kis_when_enforcement_on(self):
        v = _validator(KIS_LIVE_CAPABILITIES)
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", True):
            with pytest.raises(UnsupportedCapabilityError) as ei:
                v._check_market_routing(_req(KR_SYMBOL, "KR"))
        assert "market_routing" in ei.value.capability
        assert "US" in str(ei.value)
        assert "KR" in str(ei.value)

    def test_kr_symbol_warns_not_raises_when_enforcement_off(self, caplog):
        import logging
        v = _validator(KIS_LIVE_CAPABILITIES)
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", False):
            with caplog.at_level(logging.WARNING):
                v._check_market_routing(_req(KR_SYMBOL, "KR"))  # must NOT raise
        assert any("market routing mismatch" in r.message for r in caplog.records)

    def test_us_symbol_rejected_by_kiwoom_when_enforcement_on(self):
        v = _validator(KIWOOM_CAPABILITIES)
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", True):
            with pytest.raises(UnsupportedCapabilityError) as ei:
                v._check_market_routing(_req(US_SYMBOL, "US"))
        assert "market_routing" in ei.value.capability

    def test_kr_symbol_accepted_by_kiwoom(self):
        v = _validator(KIWOOM_CAPABILITIES)
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", True):
            v._check_market_routing(_req(KR_SYMBOL, "KR"))  # no raise

    def test_market_none_always_skips_check(self):
        v = _validator(KIS_LIVE_CAPABILITIES)
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", True):
            req = OrderRequest(symbol=KR_SYMBOL, side="buy", qty=1.0, price=1.0,
                               order_type="limit", market=None)
            v._check_market_routing(req)  # no raise — backwards compat

    def test_simulation_market_skips_routing_check(self):
        from backend.brokers.capabilities import SIMULATOR_CAPABILITIES
        v = _validator(SIMULATOR_CAPABILITIES)
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", True):
            v._check_market_routing(_req(KR_SYMBOL, "KR"))  # no raise
            v._check_market_routing(_req(US_SYMBOL, "US"))  # no raise


# ── KiwoomBroker routing guard ───────────────────────────────────────────────

class TestKiwoomRoutingGuard:

    def _get_broker(self):
        from backend.brokers.kiwoom import KiwoomBroker
        return KiwoomBroker()

    def test_kr_symbol_reaches_not_implemented(self):
        b = self._get_broker()
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", True):
            with pytest.raises(NotImplementedError):
                b.place_order(KR_SYMBOL, "buy", 1, 70000.0)

    def test_us_symbol_blocked_before_not_implemented_when_enforcement_on(self):
        b = self._get_broker()
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", True):
            with pytest.raises(UnsupportedCapabilityError) as ei:
                b.place_order(US_SYMBOL, "buy", 1, 150.0)
        assert "market_routing" in ei.value.capability

    def test_us_symbol_reaches_not_implemented_when_enforcement_off(self):
        b = self._get_broker()
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", False):
            with pytest.raises(NotImplementedError):
                b.place_order(US_SYMBOL, "buy", 1, 150.0)

    def test_get_price_kr_reaches_not_implemented(self):
        b = self._get_broker()
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", True):
            with pytest.raises(NotImplementedError):
                b.get_price(KR_SYMBOL)

    def test_get_price_us_blocked_when_enforcement_on(self):
        b = self._get_broker()
        with patch("backend.brokers.validator._MARKET_ENFORCEMENT", True):
            with pytest.raises(UnsupportedCapabilityError):
                b.get_price(US_SYMBOL)


# ── MarketRouter ─────────────────────────────────────────────────────────────

class TestMarketRouter:

    def _router(self, kr_positions=None, us_positions=None):
        kr = _make_broker(positions=kr_positions or _KR_POS)
        us = _make_broker(positions=us_positions or _US_POS)
        return MarketRouter(kr_broker=kr, us_broker=us), kr, us

    def test_kr_symbol_routed_to_kr_broker(self):
        router, kr, us = self._router()
        router.place_order(KR_SYMBOL, "buy", 10, 70000.0)
        kr.place_order.assert_called_once()
        us.place_order.assert_not_called()

    def test_us_symbol_routed_to_us_broker(self):
        router, kr, us = self._router()
        router.place_order(US_SYMBOL, "buy", 5, 150.0)
        us.place_order.assert_called_once()
        kr.place_order.assert_not_called()

    def test_get_price_kr_routed_to_kr(self):
        router, kr, us = self._router()
        router.get_price(KR_SYMBOL)
        kr.get_price.assert_called_once_with(KR_SYMBOL)
        us.get_price.assert_not_called()

    def test_get_price_us_routed_to_us(self):
        router, kr, us = self._router()
        router.get_price(US_SYMBOL)
        us.get_price.assert_called_once_with(US_SYMBOL)
        kr.get_price.assert_not_called()

    def test_cancel_order_kr_routed(self):
        router, kr, us = self._router()
        router.cancel_order("ORD001", symbol=KR_SYMBOL)
        kr.cancel_order.assert_called_once()
        us.cancel_order.assert_not_called()

    def test_cancel_order_us_routed(self):
        router, kr, us = self._router()
        router.cancel_order("ORD002", symbol=US_SYMBOL)
        us.cancel_order.assert_called_once()
        kr.cancel_order.assert_not_called()

    def test_get_positions_merges_both(self):
        router, _, _ = self._router()
        positions = router.get_positions()
        symbols = {p.symbol for p in positions}
        assert KR_SYMBOL in symbols
        assert US_SYMBOL in symbols

    def test_get_positions_silences_not_implemented_from_kr(self):
        kr = _make_broker()
        kr.get_positions.side_effect = NotImplementedError("stub")
        us = _make_broker(positions=_US_POS)
        router = MarketRouter(kr_broker=kr, us_broker=us)
        positions = router.get_positions()
        assert any(p.symbol == US_SYMBOL for p in positions)

    def test_get_balance_sums_both(self):
        kr = _make_broker(balance=Balance(cash_krw=500_000.0, cash_usd=0.0, total_eval_krw=500_000.0))
        us = _make_broker(balance=Balance(cash_krw=0.0, cash_usd=300.0, total_eval_krw=405_000.0))
        router = MarketRouter(kr_broker=kr, us_broker=us)
        bal = router.get_balance()
        assert bal.cash_krw == pytest.approx(500_000.0)
        assert bal.cash_usd == pytest.approx(300.0)
        assert bal.total_eval_krw == pytest.approx(905_000.0)

    def test_get_balance_silences_not_implemented_from_kr(self):
        kr = _make_broker()
        kr.get_balance.side_effect = NotImplementedError("stub")
        us = _make_broker(balance=Balance(cash_krw=0.0, cash_usd=100.0, total_eval_krw=135_000.0))
        router = MarketRouter(kr_broker=kr, us_broker=us)
        bal = router.get_balance()
        assert bal.total_eval_krw == pytest.approx(135_000.0)

    def test_is_live_reflects_underlying_brokers(self):
        kr = _make_broker()
        kr.is_live = False
        us = _make_broker()
        us.is_live = True
        router = MarketRouter(kr_broker=kr, us_broker=us)
        assert router.is_live is True

    def test_capabilities_raises(self):
        router, _, _ = self._router()
        with pytest.raises(NotImplementedError):
            _ = router.capabilities


# ── Capability preset market values ──────────────────────────────────────────

class TestCapabilityMarketValues:

    def test_kis_market_is_US(self):
        from backend.brokers.capabilities import KIS_LIVE_CAPABILITIES, KIS_PAPER_CAPABILITIES
        assert KIS_LIVE_CAPABILITIES.market == "US"
        assert KIS_PAPER_CAPABILITIES.market == "US"

    def test_kiwoom_market_is_KR(self):
        assert KIWOOM_CAPABILITIES.market == "KR"

    def test_simulator_market_is_simulation(self):
        from backend.brokers.capabilities import SIMULATOR_CAPABILITIES
        assert SIMULATOR_CAPABILITIES.market == "simulation"
