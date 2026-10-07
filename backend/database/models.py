from datetime import date, datetime, timedelta, timezone
from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, Integer, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

_KST = timezone(timedelta(hours=9))


def trading_day() -> date:
    """This platform's trading day, in Asia/Seoul (UTC+9).

    The one way to produce a ``DailyRiskState.trade_date`` key. It lives beside
    the model because the primary key's meaning is the table's contract, not any
    one caller's choice, and every caller already imports ``DailyRiskState``
    from here.

    Why it is needed: the rest of the platform already runs on KST — the
    scheduler is ``BackgroundScheduler(timezone="Asia/Seoul")`` and
    ``LossTracker`` rolls its day over on the Seoul date — but the containers
    run on UTC (no ``TZ`` in ``docker-compose.yml``), so the writers that keyed
    rows with ``date.today()`` were a day behind for the nine hours of
    **KST 00:00–09:00**. Readers and writers then disagreed about which row was
    "today", which lost a live halt across a worker restart (issue #160).

    Callers import this *inside the function* that needs it, matching how
    ``DailyRiskState`` is already imported, so that patching this one name
    covers every site.
    """
    return datetime.now(_KST).date()


def trading_days_in_play() -> tuple[date, date]:
    """The trading days a live halt can be sitting on — ``(today, yesterday)``.

    The US session runs 22:30–05:00 KST, so it **straddles Seoul midnight**: a
    halt that fires before midnight is on yesterday's row, one that fires after
    it is on today's. Anything asking "is trading halted right now" has to read
    both, or it misses half the session.

    Including yesterday unconditionally does not over-block. A halt that was
    cleared has ``kill_switch`` false and does not match; only an *uncleared*
    one does, and that is exactly what should still be blocking.

    Before issue #160 the row key was the UTC date, whose boundary falls at
    09:00 KST — outside every session — so one row was enough and nothing here
    was needed. Moving the key to KST put the boundary inside the US session,
    which is what makes this the shared definition rather than one caller's
    special case.
    """
    today = trading_day()
    return today, today - timedelta(days=1)


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


def lock_risk_row(sess: Session, day: date) -> tuple["DailyRiskState", bool]:
    """The ``DailyRiskState`` row for ``day``, created if missing and locked.

    **Every write to this table goes through here** (issue #164). Four writers
    in two processes read-modify-write the row — the worker's loss tracker, the
    operator reset API, the API-side ``WorkerWatchdog`` (one per gunicorn
    worker), and the worker's shutdown checkpoint — and with no lock the last
    commit silently erased whatever the others had decided. The worst case
    failed open: a reset read the row, a new halt committed, and the reset's
    ``False`` wiped it.

    ``SELECT … FOR UPDATE`` makes each writer wait for the one ahead of it and
    then decide against the row *as committed*, so every existing
    set-only-if-not-set check means what it says. Creation is
    ``INSERT … ON CONFLICT DO NOTHING``: two writers opening a new trading day
    no longer race to a duplicate-key error — the loser waits, finds the
    winner's row, and locks that.

    Returns ``(row, created)``. ``created`` is True only for the session whose
    insert made the row; it then holds nobody's decision yet.

    The lock is held until the caller commits or rolls back — keep that short
    and do no I/O inside it. SQLite ignores ``FOR UPDATE``; tests there cover
    the logic, and the Postgres suite covers the locking.
    """
    created = _insert_risk_row_if_missing(sess, day)
    row = sess.get(DailyRiskState, day, with_for_update=True, populate_existing=True)
    return row, created


def lock_risk_rows(sess: Session, days) -> list["DailyRiskState"]:
    """Lock the *existing* rows among ``days``, always in date order.

    For writers that act on several trading days at once (the US session
    straddles Seoul midnight). A fixed order is what keeps two such writers
    from deadlocking on each other; a missing day is skipped, not created.
    """
    rows = []
    for day in sorted(set(days)):
        row = sess.get(DailyRiskState, day, with_for_update=True, populate_existing=True)
        if row is not None:
            rows.append(row)
    return rows


def _insert_risk_row_if_missing(sess: Session, day: date) -> bool:
    dialect = sess.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise NotImplementedError(f"lock_risk_row: unsupported dialect {dialect!r}")
    result = sess.execute(
        insert(DailyRiskState).values(trade_date=day).on_conflict_do_nothing(
            index_elements=[DailyRiskState.trade_date]))
    return result.rowcount == 1


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


class RunUptime(Base):
    """When a strategy run was actually running — one row per unbroken stretch.

    The 4-week paper gate counts this, not calendar days
    (``promotion_guard.uptime_by_run``): a run that sat six hours without a
    worker running it needs six more hours. The Redis heartbeat expires after
    90 s and keeps no history, and the worker being up is not enough either — a
    run whose restore failed stays active for the next boot while nothing runs
    it. So the run's own session (``WorkerSession``) records it: a row once
    ``strategy.start()`` has succeeded, ``last_beat_at`` moved every minute,
    ``ended_at`` when the session ends cleanly. A crash leaves ``ended_at`` empty
    and the row ends at the last beat. Beats that could not be written for longer
    than the grace start a new row, so the gap is not counted.

    A new table, not new columns on an existing one: ``create_all`` creates
    missing tables but never adds columns (#194).
    """
    __tablename__ = "run_uptime"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, nullable=False, index=True)
    boot_at = Column(DateTime, nullable=False)
    last_beat_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=True)


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
