from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, Float,
    DateTime, ForeignKey, JSON
)
from sqlalchemy.orm import relationship
from api.database import Base


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
