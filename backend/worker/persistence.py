"""
체결·포지션 DB 영속 저장 헬퍼.
WorkerSession에서 on_fill 콜백으로 사용.
"""
import logging
from datetime import datetime

from backend.brokers.models import Order as BOrder
from backend.database.models import Fill as DBFill
from backend.database.models import Order as DBOrder
from backend.database.models import Position as DBPosition
from backend.execution.position_tracker import Fill

logger = logging.getLogger(__name__)


class DBPersistence:
    def __init__(self, db_session, broker: str = "kis", strategy_run_id: int | None = None):
        self._db = db_session
        self._broker = broker
        self._run_id = strategy_run_id

    def persist_order(self, order: BOrder):
        try:
            existing = self._db.query(DBOrder).filter(
                DBOrder.broker_order_id == order.id
            ).first()
            if existing:
                existing.status = order.status.value
                existing.updated_at = datetime.utcnow()
            else:
                row = DBOrder(
                    broker_order_id=order.id,
                    symbol=order.symbol,
                    side=order.side,
                    qty=order.qty,
                    price=order.price,
                    status=order.status.value,
                    market="US" if (len(order.symbol) < 6 or not order.symbol.isdigit()) else "KR",
                    broker=self._broker,
                    strategy_run_id=self._run_id,
                )
                self._db.add(row)
            self._db.commit()
        except Exception as e:
            logger.warning("주문 DB 저장 실패: %s", e)
            self._db.rollback()

    def persist_fill(self, fill: Fill):
        try:
            db_order = self._db.query(DBOrder).filter(
                DBOrder.broker_order_id == fill.order_id
            ).first()
            order_pk = db_order.id if db_order else None
            row = DBFill(
                order_id=order_pk or 0,
                qty=fill.qty,
                price=fill.price,
            )
            self._db.add(row)
            self._db.commit()
        except Exception as e:
            logger.warning("체결 DB 저장 실패: %s", e)
            self._db.rollback()

    def sync_positions(self, positions):
        """포지션 목록을 DB와 동기화 (upsert)."""
        try:
            symbols = {p.symbol for p in positions}
            for pos in positions:
                existing = self._db.query(DBPosition).filter(
                    DBPosition.symbol == pos.symbol,
                    DBPosition.broker == self._broker,
                ).first()
                if existing:
                    existing.qty = pos.qty
                    existing.avg_price = pos.avg_price
                    existing.updated_at = datetime.utcnow()
                else:
                    self._db.add(DBPosition(
                        symbol=pos.symbol,
                        qty=pos.qty,
                        avg_price=pos.avg_price,
                        market=pos.market,
                        broker=self._broker,
                    ))
            # 청산된 포지션 삭제
            self._db.query(DBPosition).filter(
                DBPosition.broker == self._broker,
                ~DBPosition.symbol.in_(symbols),
            ).delete(synchronize_session=False)
            self._db.commit()
        except Exception as e:
            logger.warning("포지션 DB 동기화 실패: %s", e)
            self._db.rollback()
