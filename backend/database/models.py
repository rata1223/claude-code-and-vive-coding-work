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
    __table_args__ = (
        UniqueConstraint("symbol", "broker", name="uq_position_symbol_broker"),
    )
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


class ReconciliationLog(Base):
    """Immutable record of each reconciliation run."""
    __tablename__ = "reconciliation_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger = Column(String(20), nullable=False, index=True)  # startup/periodic/manual
    broker = Column(String(10), nullable=True, index=True)    # "kis" / "kiwoom"
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    gaps_found = Column(Integer, nullable=False, default=0)
    repairs_made = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    detail = Column(Text, nullable=True)  # JSON summary


class AuditLog(Base):
    """Append-only audit trail — every order action, risk event, and operator intervention."""
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(50), nullable=False, index=True)
    symbol = Column(String(20), nullable=True, index=True)
    order_id = Column(String(50), nullable=True, index=True)
    actor = Column(String(50), nullable=True)  # worker/api/scheduler/operator
    detail = Column(Text, nullable=True)  # JSON
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class CorporateAction(Base):
    """Persisted corporate-action record (P2-02C runtime integration).

    Survives restart so the trading gate is fail-closed across reboots. The
    (broker, symbol, effective_date, action_type) unique key makes recording
    idempotent — the periodic reconciler cannot insert the same split twice.

    NOTE: this is the *runtime persistence* row. The pure in-memory model lives
    in ``backend.data.corporate_actions.CorporateAction`` (a frozen dataclass);
    the names intentionally match the domain concept across the two layers.
    """
    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint("broker", "symbol", "effective_date", "action_type",
                         name="uq_corporate_action"),
    )
    id = Column(Integer, primary_key=True, autoincrement=True)
    broker = Column(String(10), nullable=False, default="kis", index=True)
    symbol = Column(String(20), nullable=False, index=True)
    action_type = Column(String(20), nullable=False)  # split/reverse_split/cash_dividend/ticker_change/unknown
    effective_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, default="pending", index=True)  # pending/confirmed/applied/unknown/dismissed
    ratio = Column(Float, nullable=True)
    cash_amount = Column(Float, nullable=True)
    new_symbol = Column(String(20), nullable=True)
    source = Column(String(40), nullable=True)  # reconcile_signature/price_jump_heuristic/external/manual
    detail = Column(Text, nullable=True)
    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    applied_at = Column(DateTime, nullable=True)


class CorporateActionHistory(Base):
    """Append-only adjustment history — one immutable row per applied corporate
    action, recording the broker-resolved before/after position basis so the
    qty/avg change and value preservation can be audited (P2-02C)."""
    __tablename__ = "corporate_action_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    corporate_action_id = Column(Integer, nullable=True, index=True)
    broker = Column(String(10), nullable=False, default="kis")
    symbol = Column(String(20), nullable=False, index=True)  # original (pre-ticker-change) symbol
    action_type = Column(String(20), nullable=False)
    qty_before = Column(Float, nullable=True)
    avg_before = Column(Float, nullable=True)
    qty_after = Column(Float, nullable=True)
    avg_after = Column(Float, nullable=True)
    cash_delta = Column(Float, nullable=False, default=0.0)
    value_preserved = Column(Boolean, nullable=False, default=True)
    applied_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    actor = Column(String(50), nullable=True)


def init_db(db_url: str) -> Session:
    engine = create_engine(db_url, echo=False)
    Base.metadata.create_all(engine)
    return Session(engine)


def init_db_factory(db_url: str) -> sessionmaker:
    """Return a thread-safe sessionmaker. Each thread should call factory() to get its own Session."""
    engine = create_engine(db_url, pool_pre_ping=True, echo=False)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
