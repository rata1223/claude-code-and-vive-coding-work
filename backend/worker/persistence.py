"""
체결·포지션 DB 영속 저장 헬퍼.
WorkerSession에서 on_fill 콜백으로 사용.
"""
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Callable

from backend.brokers.models import Order as BOrder
from backend.database.models import Fill as DBFill
from backend.database.models import Order as DBOrder
from backend.database.models import Position as DBPosition
from backend.execution.position_tracker import Fill

logger = logging.getLogger(__name__)


@contextmanager
def _session(factory):
    sess = factory()
    try:
        yield sess
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


class DBPersistence:
    """Each public method creates and closes its own DB session via the factory."""

    def __init__(self, db_factory: Callable, broker: str = "kis",
                 strategy_run_id: int | None = None):
        self._factory = db_factory
        self._broker = broker
        self._run_id = strategy_run_id

    def persist_order(self, order: BOrder):
        try:
            with _session(self._factory) as db:
                existing = db.query(DBOrder).filter(
                    DBOrder.broker_order_id == order.id
                ).first()
                if existing:
                    existing.status = order.status.value
                    existing.updated_at = datetime.utcnow()
                else:
                    db.add(DBOrder(
                        broker_order_id=order.id,
                        symbol=order.symbol,
                        side=order.side,
                        qty=order.qty,
                        price=order.price,
                        status=order.status.value,
                        market="US" if (len(order.symbol) < 6 or not order.symbol.isdigit()) else "KR",
                        broker=self._broker,
                        strategy_run_id=self._run_id,
                    ))
                db.commit()
        except Exception as e:
            logger.warning("주문 DB 저장 실패: %s", e)

    def persist_fill(self, fill: Fill):
        try:
            with _session(self._factory) as db:
                db_order = db.query(DBOrder).filter(
                    DBOrder.broker_order_id == fill.order_id
                ).first()
                if db_order is None:
                    logger.warning("체결 DB 저장 스킵: 미등록 order_id=%s", fill.order_id)
                    return
                db.add(DBFill(order_id=db_order.id, qty=fill.qty, price=fill.price))
                db.commit()
        except Exception as e:
            logger.warning("체결 DB 저장 실패: %s", e)

    def sync_positions(self, positions):
        """포지션 목록을 DB와 동기화 (upsert). 빈 리스트일 때는 삭제하지 않는다."""
        if not positions:
            logger.debug("sync_positions: 빈 포지션 리스트 — 삭제 스킵")
            return
        try:
            with _session(self._factory) as db:
                symbols = {p.symbol for p in positions}
                for pos in positions:
                    existing = db.query(DBPosition).filter(
                        DBPosition.symbol == pos.symbol,
                        DBPosition.broker == self._broker,
                    ).first()
                    if existing:
                        existing.qty = pos.qty
                        existing.avg_price = pos.avg_price
                        existing.updated_at = datetime.utcnow()
                    else:
                        db.add(DBPosition(
                            symbol=pos.symbol,
                            qty=pos.qty,
                            avg_price=pos.avg_price,
                            market=pos.market,
                            broker=self._broker,
                        ))
                # 청산된 포지션 삭제
                db.query(DBPosition).filter(
                    DBPosition.broker == self._broker,
                    ~DBPosition.symbol.in_(symbols),
                ).delete(synchronize_session=False)
                db.commit()
        except Exception as e:
            logger.warning("포지션 DB 동기화 실패: %s", e)
