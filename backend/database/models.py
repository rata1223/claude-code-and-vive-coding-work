from datetime import datetime
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, Integer, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(4), nullable=False)  # buy/sell
    qty = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    market = Column(String(2), nullable=False)  # KR/US
    broker = Column(String(10), nullable=False, default="kis")
    strategy_run_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_orders_idempotency"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    broker_order_id = Column(String(50), nullable=True, index=True)
    idempotency_key = Column(String(100), nullable=True)
    symbol = Column(String(20), nullable=False, index=True)
    side = Column(String(4), nullable=False)
    qty = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    filled_qty = Column(Integer, nullable=False, default=0)
    avg_fill_price = Column(Float, nullable=True)
    status = Column(String(20), nullable=False, default="pending")
    market = Column(String(2), nullable=False)
    broker = Column(String(10), nullable=False, default="kis")
    strategy_run_id = Column(Integer, nullable=True, index=True)
    trade_date = Column(Date, nullable=True, index=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Fill(Base):
    __tablename__ = "fills"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, nullable=False, index=True)
    qty = Column(Integer, nullable=False)
    price = Column(Float, nullable=False)
    filled_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class StrategyRun(Base):
    __tablename__ = "strategy_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_type = Column(String(20), nullable=False)  # indicator/script/ai
    name = Column(String(100), nullable=False)
    config = Column(Text, nullable=True)  # JSON
    broker = Column(String(10), nullable=False, default="kis")
    is_active = Column(Boolean, default=True, nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    stopped_at = Column(DateTime, nullable=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    total_krw = Column(Float, nullable=False)
    cash_krw = Column(Float, nullable=False)
    cash_usd = Column(Float, nullable=False)
    snapped_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class Position(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    qty = Column(Integer, nullable=False)
    avg_price = Column(Float, nullable=False)
    market = Column(String(2), nullable=False)
    broker = Column(String(10), nullable=False, default="kis")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailyRiskState(Base):
    __tablename__ = "daily_risk_states"
    trade_date = Column(Date, primary_key=True)
    daily_pnl = Column(Float, nullable=False, default=0.0)
    weekly_pnl = Column(Float, nullable=False, default=0.0)
    peak_equity = Column(Float, nullable=False, default=0.0)
    kill_switch = Column(Boolean, nullable=False, default=False)
    kill_reason = Column(String(200), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Command(Base):
    __tablename__ = "commands"
    id = Column(Integer, primary_key=True, autoincrement=True)
    channel = Column(String(50), nullable=False, index=True)
    payload = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_at = Column(DateTime, nullable=True)


def init_db(db_url: str) -> Session:
    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(engine)
    return Session(engine)


def init_db_factory(db_url: str) -> sessionmaker:
    """Return a thread-safe sessionmaker. Each thread should call factory() to get its own Session."""
    engine = create_engine(db_url, pool_pre_ping=True, echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
