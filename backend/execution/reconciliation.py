"""
ReconciliationEngine — fill lifecycle and portfolio reconciliation.

Extends PositionReconciler (reconciler.py) with:
  - FillReconciler: idempotent Fill-row sync for partial fills, cancels, rejects
  - ReconciliationEngine: unified orchestrator (positions + orders + fills)
  - PortfolioSnapshot: broker-scoped position aggregate

PositionReconciler already handles position gaps and order status sync.
This module fills the remaining gap: persisting Fill rows for every state where
filled_qty > 0 (partial_filled, filled, canceled-with-partial, rejected-with-partial).

Idempotency via incremental accounting:
    existing_filled = SUM(Fill.qty) for order
    increment       = order.filled_qty - existing_filled
    insert Fill(qty=increment) only when increment > 0
Running reconcile N times produces the same Fill rows as running it once.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from backend.brokers.base import BrokerAdapter
from backend.execution.reconciler import PositionReconciler, ReconciliationResult, _session

logger = logging.getLogger(__name__)


class FillReconciler:
    """
    Idempotent fill-record sync for orders with filled_qty > 0.

    reconciler.py creates a Fill row only for FILLED orders.  This class
    covers the remaining cases:
      - PARTIAL_FILLED: Fill row written so in-progress fills are visible
      - CANCELED / REJECTED with partial fill: partial Fill row recorded before
        the order reaches its terminal state
      - FILLED (missed by reconciler.py): Fill row inserted if not already present

    The incremental accounting approach guarantees no duplicate rows on replay.
    """

    def __init__(self, db_factory: Callable, broker_name: str = "kis"):
        self._factory = db_factory
        self._broker_name = broker_name

    def sync_fills_all_open_orders(
        self, result: ReconciliationResult, dry_run: bool = False
    ) -> None:
        """
        Scan all orders with filled_qty > 0 for this broker and insert incremental
        Fill rows where SUM(Fill.qty) < order.filled_qty.

        Must be called AFTER PositionReconciler.reconcile() so that order.filled_qty
        reflects the latest broker state.
        """
        from backend.database.models import Order as DBOrder

        with _session(self._factory) as db:
            rows = db.query(DBOrder).filter(
                DBOrder.broker == self._broker_name,
                DBOrder.filled_qty > 0,
                DBOrder.status.in_(["partial_filled", "filled", "canceled", "rejected"]),
            ).all()
            order_data = [
                {
                    "id": r.id,
                    "symbol": r.symbol,
                    "filled_qty": r.filled_qty,
                    "avg_fill_price": r.avg_fill_price,
                }
                for r in rows
            ]

        for od in order_data:
            existing = self._existing_filled_qty(od["id"])
            increment = od["filled_qty"] - existing
            if increment <= 0:
                continue
            if not dry_run:
                self._insert_fill(od["id"], increment, od["avg_fill_price"] or 0.0)
            result.repaired(
                "sync_fill",
                od["symbol"],
                f"Fill 추가: order_id={od['id']} qty={increment}",
            )

    def _existing_filled_qty(self, order_id: int) -> int:
        """Sum of all Fill.qty rows already persisted for this order_id."""
        from backend.database.models import Fill as DBFill
        from sqlalchemy import func

        with _session(self._factory) as db:
            total = (
                db.query(func.sum(DBFill.qty))
                .filter(DBFill.order_id == order_id)
                .scalar()
            )
            return int(total or 0)

    def _insert_fill(self, order_id: int, qty: int, price: float) -> None:
        from backend.database.models import Fill as DBFill

        with _session(self._factory) as db:
            db.add(DBFill(order_id=order_id, qty=qty, price=price))
            db.commit()


@dataclass
class PortfolioSnapshot:
    """Broker-scoped position aggregate taken from DB after reconciliation."""

    positions: list[dict]  # [{symbol, qty, avg_price, market, broker}, ...]
    broker: str
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def total_qty_for(self, symbol: str) -> int:
        return sum(p["qty"] for p in self.positions if p["symbol"] == symbol)

    def symbols(self) -> list[str]:
        return [p["symbol"] for p in self.positions]


class ReconciliationEngine:
    """
    Unified reconciliation orchestrator.

    Stage 1 — PositionReconciler.reconcile(): position gaps + order status sync.
    Stage 2 — FillReconciler.sync_fills_all_open_orders(): incremental Fill rows.

    Both stages share the same ReconciliationResult so the caller sees all gaps
    and repairs in one place.

    Usage:
        engine = ReconciliationEngine(broker, db_factory, broker_name="kis")
        result = engine.reconcile("startup")
        snapshot = engine.get_portfolio_snapshot()
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        db_factory: Callable,
        redis_client=None,
        poller=None,
        broker_name: str = "kis",
    ):
        self._pos_reconciler = PositionReconciler(
            broker, db_factory, redis_client, poller, broker_name
        )
        self._fill_reconciler = FillReconciler(db_factory, broker_name)
        self._factory = db_factory
        self._broker_name = broker_name

    def reconcile(
        self, trigger: str = "periodic", dry_run: bool = False
    ) -> ReconciliationResult:
        """
        Full reconciliation pass (positions + order status + fills).

        dry_run=True detects gaps without mutating DB (same semantics as
        PositionReconciler.reconcile(dry_run=True)).
        """
        result = self._pos_reconciler.reconcile(trigger, dry_run)
        self._fill_reconciler.sync_fills_all_open_orders(result, dry_run)
        return result

    def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        """
        Return current DB positions for this broker as an immutable snapshot.

        Call after reconcile() so positions reflect broker ground truth.
        """
        from backend.database.models import Position as DBPosition

        with _session(self._factory) as db:
            rows = (
                db.query(DBPosition)
                .filter(DBPosition.broker == self._broker_name)
                .all()
            )
            positions = [
                {
                    "symbol": r.symbol,
                    "qty": r.qty,
                    "avg_price": r.avg_price,
                    "market": r.market,
                    "broker": r.broker,
                }
                for r in rows
            ]
        return PortfolioSnapshot(positions=positions, broker=self._broker_name)
