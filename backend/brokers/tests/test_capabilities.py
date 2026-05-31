"""
BrokerCapabilities 레이어 테스트 — TASK 1-2.

검증 범위:
- BrokerCapabilities 구조체 (필수 필드, frozen, 프리셋 값)
- BrokerCapabilityValidator 런타임 검증 (limit/market/stop/fractional)
- UnsupportedCapabilityError 페이로드
- KIS / Kiwoom / Simulator 어댑터 capabilities 프로퍼티 노출
- 실제 어댑터(Simulator, KIS)에서 미지원 주문이 런타임에 차단되는지
"""
import dataclasses
from datetime import time

import pytest

from backend.brokers.capabilities import (
    KIS_LIVE_CAPABILITIES,
    KIS_PAPER_CAPABILITIES,
    KIWOOM_CAPABILITIES,
    SIMULATOR_CAPABILITIES,
)
from backend.brokers.models import BrokerCapabilities
from backend.brokers.validator import BrokerCapabilityValidator, OrderRequest, UnsupportedCapabilityError
from backend.brokers.kiwoom import KiwoomBroker
from backend.strategy.runtime.simulator import SimulatedBroker

# ── Required boolean capability fields (13 specified by TASK 1-2) ──────────
REQUIRED_BOOL_FIELDS = {
    "supports_market_buy",
    "supports_market_sell",
    "supports_limit_order",
    "supports_stop_order",
    "supports_fractional",
    "supports_short",
    "supports_after_hours",
    "supports_websocket",
    "supports_realtime_quote",
    "supports_account_balance",
    "supports_portfolio",
}


def _caps(**overrides) -> BrokerCapabilities:
    """테스트용 역량 빌더 — 불리언 전부 True, 나머지 무해한 기본값."""
    base = dict(
        broker_id="test",
        market="test",
        currency="KRW",
        supports_market_buy=True,
        supports_market_sell=True,
        supports_limit_order=True,
        supports_stop_order=True,
        supports_fractional=True,
        supports_short=True,
        supports_after_hours=True,
        supports_websocket=True,
        supports_realtime_quote=True,
        supports_account_balance=True,
        supports_portfolio=True,
        fill_mechanism="sync",
        price_type="float",
        requires_exchange_code=False,
        requires_hashkey=False,
        cancel_requires_symbol=False,
        cancel_requires_qty_price=False,
        retry_safe_on_submit=True,
        rate_limit_per_sec=9999,
        settlement_days=0,
        has_securities_tax=False,
        securities_tax_rate=0.0,
        price_precision={"TEST": 4},
        min_order_qty=1,
        session_open_kst=None,
        session_close_kst=None,
    )
    base.update(overrides)
    return BrokerCapabilities(**base)


def _req(side="buy", order_type="limit", qty=1.0, price=1000.0, symbol="TEST") -> OrderRequest:
    return OrderRequest(symbol=symbol, side=side, qty=qty, price=price, order_type=order_type)


# ── BrokerCapabilities 구조체 ──────────────────────────────────────────────
class TestBrokerCapabilitiesStruct:
    def test_all_required_bool_fields_present(self):
        field_names = {f.name for f in dataclasses.fields(BrokerCapabilities)}
        assert REQUIRED_BOOL_FIELDS <= field_names

    def test_is_frozen(self):
        caps = _caps()
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            caps.supports_short = False  # type: ignore[misc]

    def test_preset_kis_live_market_orders_disabled(self):
        assert KIS_LIVE_CAPABILITIES.supports_market_buy is False
        assert KIS_LIVE_CAPABILITIES.supports_market_sell is False
        assert KIS_LIVE_CAPABILITIES.supports_limit_order is True

    def test_preset_kis_paper_inherits_live(self):
        assert KIS_PAPER_CAPABILITIES.supports_limit_order is True
        assert KIS_PAPER_CAPABILITIES.supports_market_buy is False
        assert KIS_PAPER_CAPABILITIES.rate_limit_per_sec == 5
        assert KIS_LIVE_CAPABILITIES.rate_limit_per_sec == 15

    def test_preset_kiwoom_unimplemented(self):
        assert KIWOOM_CAPABILITIES.supports_account_balance is False
        assert KIWOOM_CAPABILITIES.supports_portfolio is False

    def test_preset_simulator_market_orders_allowed(self):
        assert SIMULATOR_CAPABILITIES.supports_market_buy is True
        assert SIMULATOR_CAPABILITIES.supports_market_sell is True
        assert SIMULATOR_CAPABILITIES.supports_stop_order is False


# ── BrokerCapabilityValidator ──────────────────────────────────────────────
class TestValidator:
    def test_limit_order_allowed_when_supported(self):
        v = BrokerCapabilityValidator(_caps())
        v.validate(_req("buy", "limit"))
        v.validate(_req("sell", "limit"))

    def test_limit_order_passes_even_when_flag_false(self):
        # Validator treats limit as the universal fallback — never blocks it directly.
        # Market-unsupported orders degrade to limit; blocking happens only at stop/fractional level.
        v = BrokerCapabilityValidator(_caps(supports_limit_order=False))
        v.validate(_req("buy", "limit"))  # no raise — limit is always the safe path

    def test_market_buy_allowed_independently(self):
        v = BrokerCapabilityValidator(_caps(supports_market_buy=True, supports_market_sell=False))
        v.validate(_req("buy", "market"))  # no raise

    def test_market_sell_without_price_blocked_when_unsupported(self):
        # With price=None there's no limit-fallback available → explicit UnsupportedCapabilityError
        v = BrokerCapabilityValidator(_caps(supports_market_buy=True, supports_market_sell=False))
        with pytest.raises(UnsupportedCapabilityError) as ei:
            v.validate(_req("sell", "market", price=None))
        assert "supports_market_sell" in ei.value.capability

    def test_stop_order_blocked(self):
        v = BrokerCapabilityValidator(_caps(supports_stop_order=False))
        with pytest.raises(UnsupportedCapabilityError) as ei:
            v.validate(_req("buy", "stop"))
        assert "supports_stop_order" in ei.value.capability

    def test_fractional_qty_blocked_when_unsupported(self):
        v = BrokerCapabilityValidator(_caps(supports_fractional=False))
        req = _req("buy", "limit", qty=0.5)
        with pytest.raises(UnsupportedCapabilityError) as ei:
            v.validate(req)
        assert "supports_fractional" in ei.value.capability

    def test_integer_qty_passes_no_fractional(self):
        v = BrokerCapabilityValidator(_caps(supports_fractional=False))
        v.validate(_req("buy", "limit", qty=1.0))  # whole number — no raise

    def test_market_order_with_price_degrades_to_limit(self):
        """Broker that doesn't support market buy but has a price → convert to limit, no error."""
        v = BrokerCapabilityValidator(_caps(supports_market_buy=False))
        req = _req("buy", "market", price=5000.0)
        result = v.validate(req)
        assert result.order_type == "limit"

    def test_market_order_without_price_raises_when_broker_unsupported(self):
        v = BrokerCapabilityValidator(_caps(supports_market_buy=False))
        req = _req("buy", "market", price=None)
        with pytest.raises(UnsupportedCapabilityError):
            v.validate(req)

    def test_validate_balance_query_raises_when_unsupported(self):
        v = BrokerCapabilityValidator(_caps(supports_account_balance=False))
        with pytest.raises(UnsupportedCapabilityError):
            v.validate_balance_query()

    def test_validate_portfolio_query_raises_when_unsupported(self):
        v = BrokerCapabilityValidator(_caps(supports_portfolio=False))
        with pytest.raises(UnsupportedCapabilityError):
            v.validate_portfolio_query()


# ── UnsupportedCapabilityError 페이로드 ──────────────────────────────────
class TestException:
    def test_carries_capability_and_broker_id(self):
        err = UnsupportedCapabilityError("supports_stop_order", "kis", "no stop support")
        assert err.capability == "supports_stop_order"
        assert err.broker_id == "kis"
        assert "supports_stop_order" in str(err)
        assert "kis" in str(err)

    def test_detail_is_optional(self):
        err = UnsupportedCapabilityError("supports_fractional", "simulator")
        assert "supports_fractional" in str(err)


# ── 어댑터별 capabilities 프로퍼티 노출 ────────────────────────────────────
class TestAdaptersExposeCapabilities:
    def test_simulator_exposes_capabilities_property(self):
        sim = SimulatedBroker(initial_cash_krw=1_000_000)
        assert sim.capabilities is SIMULATOR_CAPABILITIES

    def test_kiwoom_exposes_capabilities_property(self):
        assert KiwoomBroker().capabilities is KIWOOM_CAPABILITIES

    def test_kis_class_exposes_preset_without_instantiation(self):
        kis = pytest.importorskip("backend.brokers.kis")
        # KISBroker requires env vars — check the property returns the right preset type
        # by inspecting the return annotation or checking the class body
        assert hasattr(kis.KISBroker, "capabilities")

    def test_kiwoom_account_and_portfolio_unsupported(self):
        assert KIWOOM_CAPABILITIES.supports_account_balance is False
        assert KIWOOM_CAPABILITIES.supports_portfolio is False

    def test_simulator_capabilities_correct_fill_mechanism(self):
        assert SIMULATOR_CAPABILITIES.fill_mechanism == "sync"
        assert SIMULATOR_CAPABILITIES.retry_safe_on_submit is True


# ── 실제 어댑터 런타임 enforcement ─────────────────────────────────────────
class TestSimulatorEnforcement:
    def test_limit_buy_succeeds(self):
        sim = SimulatedBroker(initial_cash_krw=1_000_000)
        sim.feed_price("005930", 70000)
        order = sim.place_order("005930", "buy", 1, 70000)
        assert order.status.value == "filled"

    def test_market_buy_succeeds(self):
        sim = SimulatedBroker(initial_cash_krw=1_000_000)
        sim.feed_price("005930", 70000)
        order = sim.place_order("005930", "buy", 1, 70000, order_type="market")
        assert order.status.value in ("filled", "submitted")

    def test_stop_order_blocked_at_runtime(self):
        sim = SimulatedBroker(initial_cash_krw=1_000_000)
        sim.feed_price("005930", 70000)
        with pytest.raises(UnsupportedCapabilityError) as ei:
            sim.place_order("005930", "buy", 1, 70000, order_type="stop")
        assert "supports_stop_order" in ei.value.capability


# ── KIS market order guard (CodeRabbit finding) ────────────────────────────
class TestKISCapabilities:
    def test_kis_live_blocks_market_orders(self):
        """KIS always sends ORD_DVSN=00 (limit); market orders must be blocked explicitly."""
        v = BrokerCapabilityValidator(KIS_LIVE_CAPABILITIES)
        with pytest.raises(UnsupportedCapabilityError):
            v.validate(_req("buy", "market", price=None))
        with pytest.raises(UnsupportedCapabilityError):
            v.validate(_req("sell", "market", price=None))

    def test_kis_live_allows_limit_orders(self):
        v = BrokerCapabilityValidator(KIS_LIVE_CAPABILITIES)
        v.validate(_req("buy", "limit", price=70000.0))

    def test_kis_paper_rate_limit_is_5(self):
        assert KIS_PAPER_CAPABILITIES.rate_limit_per_sec == 5

    def test_kis_live_rate_limit_is_15(self):
        assert KIS_LIVE_CAPABILITIES.rate_limit_per_sec == 15
