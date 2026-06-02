"""
MarketRouter — compatibility adapter for multi-broker symbol routing.

Routes BrokerAdapter calls to the market-appropriate broker:
  KR symbols (6-digit or in KR_ETF list) → kr_broker  (Kiwoom, when implemented)
  US symbols (everything else)            → us_broker  (KIS)

Provides a single BrokerAdapter interface so callers (runner, strategy, reconciler)
require no changes when Kiwoom is eventually implemented.

Usage:
    router = MarketRouter(kr_broker=KiwoomBroker(), us_broker=get_kis_broker())

NotImplementedError from stub brokers is silently swallowed in get_balance() and
get_positions() so that a single-broker deployment (KIS-only) continues to work.
"""
import logging
from typing import Optional

from .base import BrokerAdapter
from .models import Balance, BrokerCapabilities, Order, Position

logger = logging.getLogger(__name__)


def _is_kr_symbol(symbol: str) -> bool:
    from backend.quant.data.universe import KR_ETF
    return symbol in KR_ETF or (len(symbol) == 6 and symbol.isdigit())


class MarketRouter(BrokerAdapter):
    """Routes broker calls to the market-appropriate underlying adapter."""

    def __init__(self, kr_broker: BrokerAdapter, us_broker: BrokerAdapter):
        self._kr = kr_broker
        self._us = us_broker

    # ── identity ─────────────────────────────────────────────────────────────

    @property
    def is_live(self) -> bool:  # type: ignore[override]
        return self._us.is_live or self._kr.is_live

    @property
    def capabilities(self) -> BrokerCapabilities:
        raise NotImplementedError(
            "MarketRouter has no single capability set. "
            "Use .kr_broker.capabilities or .us_broker.capabilities."
        )

    @property
    def kr_broker(self) -> BrokerAdapter:
        return self._kr

    @property
    def us_broker(self) -> BrokerAdapter:
        return self._us

    # ── routing ──────────────────────────────────────────────────────────────

    def _route(self, symbol: str) -> BrokerAdapter:
        return self._kr if _is_kr_symbol(symbol) else self._us

    # ── BrokerAdapter implementation ─────────────────────────────────────────

    def place_order(self, symbol: str, side: str, qty: int, price: float,
                    order_type: str = "limit") -> Order:
        return self._route(symbol).place_order(symbol, side, qty, price, order_type)

    def cancel_order(self, order_id: str, symbol: str = "",
                     qty: int = 0, price: float = 0.0) -> bool:
        return self._route(symbol).cancel_order(order_id, symbol, qty, price)

    def get_order_status(self, order_id: str, symbol: str = "") -> Optional[Order]:
        return self._route(symbol).get_order_status(order_id, symbol)

    def get_price(self, symbol: str) -> float:
        return self._route(symbol).get_price(symbol)

    def get_positions(self) -> list[Position]:
        """Returns combined positions from both brokers; swallows NotImplementedError from stubs."""
        positions: list[Position] = []
        for broker, label in ((self._kr, "kr"), (self._us, "us")):
            try:
                positions.extend(broker.get_positions())
            except NotImplementedError:
                pass
            except Exception as exc:
                logger.warning("MarketRouter.get_positions [%s] 실패: %s", label, exc)
        return positions

    def get_balance(self) -> Balance:
        """Returns summed balance across both brokers; swallows NotImplementedError from stubs."""
        cash_krw = cash_usd = total_eval_krw = 0.0
        for broker, label in ((self._kr, "kr"), (self._us, "us")):
            try:
                b = broker.get_balance()
                cash_krw += b.cash_krw
                cash_usd += b.cash_usd
                total_eval_krw += b.total_eval_krw
            except NotImplementedError:
                pass
            except Exception as exc:
                logger.warning("MarketRouter.get_balance [%s] 실패: %s", label, exc)
        return Balance(cash_krw=cash_krw, cash_usd=cash_usd, total_eval_krw=total_eval_krw)
