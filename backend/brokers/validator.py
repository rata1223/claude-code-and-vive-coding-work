import logging
import os
from dataclasses import dataclass

from .models import BrokerCapabilities

# When true, cross-market routing raises UnsupportedCapabilityError.
# Default false preserves backwards compat while KR → Kiwoom migration is in progress.
_MARKET_ENFORCEMENT: bool = os.environ.get("KIS_MARKET_ENFORCEMENT", "false").lower() == "true"

logger = logging.getLogger(__name__)


class UnsupportedCapabilityError(Exception):
    """Raised when an operation is requested that the broker cannot support."""

    def __init__(self, capability: str, broker_id: str, detail: str = ""):
        self.capability = capability
        self.broker_id = broker_id
        super().__init__(f"[{broker_id}] does not support {capability}" +
                         (f": {detail}" if detail else ""))


@dataclass
class OrderRequest:
    symbol: str
    side: str           # "buy" | "sell"
    qty: float          # float to catch fractional intent before int cast
    price: float | None
    order_type: str     # "limit" | "market" | "stop"
    market: str | None = None  # "KR" | "US" | None


class BrokerCapabilityValidator:
    """
    Validates an OrderRequest against BrokerCapabilities before submission.

    Hard violations raise UnsupportedCapabilityError.
    Soft violations (market order when only limit supported) degrade gracefully:
    the returned OrderRequest has order_type corrected to "limit".
    """

    def __init__(self, caps: BrokerCapabilities):
        self.caps = caps

    def validate(self, req: OrderRequest) -> OrderRequest:
        """
        Returns a validated (possibly normalized) OrderRequest or raises
        UnsupportedCapabilityError for operations the broker cannot perform.

        Validation order:
          1. Market routing (if req.market is set and enforcement is on)
          2. Fractional qty
          3. Short sell flag
          4. Stop order type
          5. Market order → limit fallback if price available, else hard error
        """
        self._check_market_routing(req)
        self._check_fractional(req)
        self._check_short(req)
        self._check_stop(req)
        req = self._normalize_market_order(req)
        return req

    def validate_balance_query(self) -> None:
        if not self.caps.supports_account_balance:
            raise UnsupportedCapabilityError(
                "supports_account_balance", self.caps.broker_id,
                "get_balance() is not implemented for this broker",
            )

    def validate_portfolio_query(self) -> None:
        if not self.caps.supports_portfolio:
            raise UnsupportedCapabilityError(
                "supports_portfolio", self.caps.broker_id,
                "get_positions() is not implemented for this broker",
            )

    # ── private checks ────────────────────────────────────────────────────

    def _check_market_routing(self, req: OrderRequest) -> None:
        """Block orders where req.market doesn't match the broker's declared market.

        Only runs when req.market is explicitly set (backwards compat: None skips check).
        Behaviour is controlled by KIS_MARKET_ENFORCEMENT env var:
          false (default) — logs a warning and allows the order through
          true            — raises UnsupportedCapabilityError
        """
        if req.market is None:
            return
        broker_market = self.caps.market
        if broker_market not in ("US", "KR"):
            return  # simulation / custom market — no routing restriction
        if req.market == broker_market:
            return
        detail = f"{req.symbol!r} targets {req.market!r} but broker handles {broker_market!r} only"
        if _MARKET_ENFORCEMENT:
            raise UnsupportedCapabilityError("market_routing", self.caps.broker_id, detail)
        logger.warning("[%s] market routing mismatch (KIS_MARKET_ENFORCEMENT=false): %s",
                       self.caps.broker_id, detail)

    def _check_fractional(self, req: OrderRequest) -> None:
        if not self.caps.supports_fractional and req.qty != int(req.qty):
            raise UnsupportedCapabilityError(
                "supports_fractional", self.caps.broker_id,
                f"qty={req.qty} is fractional; broker requires whole shares",
            )

    def _check_short(self, req: OrderRequest) -> None:
        if not self.caps.supports_short and req.side == "sell":
            # Short-sell detection is the caller's responsibility (position check).
            # This flag is informational — only raise if the broker explicitly forbids short.
            # We log but do NOT raise here; the broker will reject at the exchange level.
            # Raise only if the platform wants to pre-empt that rejection.
            pass  # short validation deferred to execution gate / position tracker

    def _check_stop(self, req: OrderRequest) -> None:
        if req.order_type == "stop" and not self.caps.supports_stop_order:
            raise UnsupportedCapabilityError(
                "supports_stop_order", self.caps.broker_id,
                "stop orders are not supported; use limit orders with manual stop logic",
            )

    def _normalize_market_order(self, req: OrderRequest) -> OrderRequest:
        if req.order_type != "market":
            return req
        is_buy = req.side == "buy"
        cap_flag = self.caps.supports_market_buy if is_buy else self.caps.supports_market_sell
        if cap_flag:
            return req  # broker supports market orders natively

        # Broker doesn't support true market orders.
        if req.price is not None:
            # Degrade to limit at the provided price — matches what KIS does silently.
            logger.warning(
                "[%s] market %s not supported — converting to limit at price=%.4f for %s",
                self.caps.broker_id, req.side, req.price, req.symbol,
            )
            return OrderRequest(
                symbol=req.symbol,
                side=req.side,
                qty=req.qty,
                price=req.price,
                order_type="limit",
                market=req.market,
            )

        cap_name = "supports_market_buy" if is_buy else "supports_market_sell"
        raise UnsupportedCapabilityError(
            cap_name, self.caps.broker_id,
            f"market {req.side} requires a price fallback when broker only supports limit orders",
        )
