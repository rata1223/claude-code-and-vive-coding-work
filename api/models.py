from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, Float,
    DateTime, ForeignKey, JSON, UniqueConstraint
)
from sqlalchemy.orm import relationship
from api.database import Base

# Quick Trade order lifecycle states (P0-04). Kept as plain strings — a
# stored submission outcome, never a fill/lifecycle status (see P0-02 §7).
QT_RESERVED = "reserved"    # durable idempotency reservation, pre/awaiting broker
QT_SUBMITTED = "submitted"  # broker acknowledged the submission
QT_REJECTED = "rejected"    # broker explicitly rejected (terminal)
QT_FAILED = "failed"        # reconciliation determined the broker never got it
QT_BLOCKED = "blocked"      # risk gate denied/errored before submit — broker NEVER called (P0-05)

# Legal transitions out of each state (terminal states → empty set).
# QT_BLOCKED is distinct from QT_REJECTED: BLOCKED means the pre-submit RiskManager
# gate stopped the order (broker never contacted); REJECTED means the broker itself
# rejected it. Keeping them separate gives a clean audit trail.
QT_VALID_TRANSITIONS = {
    QT_RESERVED: {QT_SUBMITTED, QT_REJECTED, QT_FAILED, QT_BLOCKED},
    QT_SUBMITTED: set(),
    QT_REJECTED: set(),
    QT_FAILED: set(),
    QT_BLOCKED: set(),
}


def qt_transition(order, new_status: str) -> None:
    """Move a QuickTradeOrder to ``new_status``, enforcing QT_VALID_TRANSITIONS.

    Raises ``ValueError`` on an illegal transition (e.g. re-opening a terminal
    order), so a stray mutation can never silently corrupt an order's lifecycle.
    """
    if new_status not in QT_VALID_TRANSITIONS.get(order.status, set()):
        raise ValueError(
            f"illegal QuickTradeOrder transition {order.status!r} -> {new_status!r}"
        )
    order.status = new_status


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    nickname = Column(String(100), nullable=True)
    avatar = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    credentials = relationship("Credential", back_populates="user", cascade="all, delete-orphan")
    strategies = relationship("Strategy", back_populates="user", cascade="all, delete-orphan")
    watchlist = relationship("WatchlistItem", back_populates="user", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")


class Credential(Base):
    __tablename__ = "credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(100), nullable=False)
    exchange_id = Column(String(50), nullable=False)  # 'kis', 'kiwoom'
    # Encrypted fields
    app_key_enc = Column(Text, nullable=True)
    app_secret_enc = Column(Text, nullable=True)
    account_no_enc = Column(Text, nullable=True)
    hts_id_enc = Column(Text, nullable=True)
    api_key_enc = Column(Text, nullable=True)
    env = Column(String(20), default="paper", nullable=False)  # paper / real
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="credentials")
    # Deleting a credential removes its quick-trade order records, mirroring the
    # Strategy→Trade cascade — otherwise the required FK would fail the delete.
    quick_trade_orders = relationship(
        "QuickTradeOrder", back_populates="credential", cascade="all, delete-orphan"
    )


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(200), nullable=False)
    type = Column(String(50), nullable=False, default="indicator")  # indicator / script
    status = Column(String(20), nullable=False, default="stopped")  # running / stopped / error
    symbol = Column(String(50), nullable=True)
    timeframe = Column(String(20), nullable=True, default="1h")
    market_type = Column(String(20), nullable=True, default="spot")  # spot / futures
    direction = Column(String(20), nullable=True, default="long")    # long / short / both
    initial_capital = Column(Float, nullable=True, default=10000.0)
    config = Column(JSON, nullable=True, default=dict)
    script_code = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="strategies")
    trades = relationship("Trade", back_populates="strategy", cascade="all, delete-orphan")
    logs = relationship("StrategyLog", back_populates="strategy", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="strategy", cascade="all, delete-orphan")


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False)
    symbol = Column(String(50), nullable=False)
    side = Column(String(10), nullable=False)  # buy / sell
    qty = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    filled_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    pnl = Column(Float, nullable=True, default=0.0)
    fee = Column(Float, nullable=True, default=0.0)

    strategy = relationship("Strategy", back_populates="trades")


class StrategyLog(Base):
    __tablename__ = "strategy_logs"

    id = Column(Integer, primary_key=True, index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False)
    message = Column(Text, nullable=False)
    level = Column(String(20), nullable=False, default="INFO")  # INFO / WARN / ERROR
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    strategy = relationship("Strategy", back_populates="logs")


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    market = Column(String(20), nullable=False)   # KRX, NASD, NYSE, etc.
    symbol = Column(String(50), nullable=False)
    name = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="watchlist")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=True)
    title = Column(String(300), nullable=False)
    message = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="notifications")
    strategy = relationship("Strategy", back_populates="notifications")


class QuickTradeOrder(Base):
    """Dedicated persistence for manual Quick Trade orders (P0-04).

    Separate from the Execution Layer's ``backend/database/models.py`` tables
    (which are single-account, no user/credential columns — see P0-02). Owned
    exclusively by the ``api/`` app and provisioned by ``create_all``; the
    repo's Alembic config targets the *backend* Base, so this table is not an
    Alembic migration.

    The ``(user_id, idempotency_key)`` unique constraint is the durable
    reserve-before-submit guarantee: one committed reservation per key per
    tenant, enforced by the DB. The application guarantee is "1 durable DB
    reservation per idempotency key", NOT exactly-once broker submission.
    """

    __tablename__ = "quick_trade_orders"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_qto_user_idempotency"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    credential_id = Column(Integer, ForeignKey("credentials.id"), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    symbol = Column(String(50), nullable=False)
    side = Column(String(10), nullable=False)              # buy / sell
    market = Column(String(10), nullable=False, default="us")
    exchange = Column(String(10), nullable=False, default="NASD")  # NASD / NYSE / KRX
    order_type = Column(String(20), nullable=False, default="limit")
    qty = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    status = Column(String(20), nullable=False, default=QT_RESERVED)
    broker_order_id = Column(String(64), nullable=True)    # broker ODNO after submit
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    credential = relationship("Credential", back_populates="quick_trade_orders")
