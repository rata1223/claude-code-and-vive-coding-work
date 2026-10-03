"""Issue #164 — ``DailyRiskState`` writers serialise on a row lock.

Four writers in two processes read-modify-write the same row. Without a lock
the last commit silently erased what the others had decided; the worst case
failed open (a reset read the row, a new halt committed, the reset's ``False``
wiped it). Every writer now goes through ``lock_risk_row(s)``.

SQLite ignores ``FOR UPDATE``, so only a real Postgres can show the overlap.
Each test holds the lock in one session, starts a real writer in a thread,
checks the writer is **blocked**, then releases and checks the final row.

The reset endpoint lives in ``api/routers`` and needs FastAPI, which the
Postgres CI jobs do not install; its own test is skipped there. The reset's
locking pattern (``lock_risk_rows`` → act on the rows as locked) is covered
without it.
"""
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.database.models import (
    Base, DailyRiskState, lock_risk_row, lock_risk_rows, trading_day,
)
from backend.database.testing import make_test_engine

_BLOCK_SEC = 0.5   # long enough that an unblocked writer has certainly finished


@pytest.fixture()
def factory():
    engine = make_test_engine()
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _seed(factory, day, *, kill_switch=False, reason=None):
    with factory() as s:
        s.add(DailyRiskState(trade_date=day, kill_switch=kill_switch,
                             kill_reason=reason, peak_equity=1_000_000.0))
        s.commit()


def _row(factory, day):
    with factory() as s:
        return s.get(DailyRiskState, day)


class _Writer(threading.Thread):
    """Run ``fn`` in a thread and keep its result or exception."""

    def __init__(self, fn):
        super().__init__(daemon=True)
        self._fn = fn
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self._fn()
        except BaseException as e:  # noqa: BLE001 - reported by the test
            self.error = e


def _assert_blocked_then_run(writer, holder):
    """``writer`` must wait on ``holder``'s lock, then finish once it commits."""
    writer.start()
    writer.join(_BLOCK_SEC)
    assert writer.is_alive(), "writer did not wait for the row lock"
    holder.commit()
    writer.join(10)
    assert not writer.is_alive(), "writer still blocked after the lock was released"
    assert writer.error is None, writer.error


@pytest.fixture()
def watchdog(factory, monkeypatch):
    """The real ``WorkerWatchdog._alert_dead_worker`` against the test schema."""
    import backend.database.models as models
    monkeypatch.setattr(models, "init_db_factory", lambda _url: factory)
    monkeypatch.setattr("bot.notifier.alert_emergency", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr("backend.websocket.server.publish_alert",
                        lambda *_a, **_k: None, raising=False)
    from backend.worker.heartbeat import WorkerWatchdog
    return WorkerWatchdog(redis_client=None)


@pytest.fixture()
def safe_mode():
    """Isolate the process-wide ``SAFE_MODE`` the tracker closes on adoption."""
    from backend.worker.recovery import SAFE_MODE
    saved = dict(SAFE_MODE.__dict__)
    SAFE_MODE.enable()
    yield SAFE_MODE
    SAFE_MODE.__dict__.clear()
    SAFE_MODE.__dict__.update(saved)


# ── C: watchdog ───────────────────────────────────────────────────────────────

def test_watchdog_waits_and_keeps_the_halt_committed_ahead_of_it(factory, watchdog):
    """First reason wins for real: the watchdog sees the committed halt."""
    day = trading_day()
    _seed(factory, day)

    holder = factory()
    row, _ = lock_risk_row(holder, day)
    row.kill_switch = True
    row.kill_reason = "MDD 15% 초과"

    _assert_blocked_then_run(_Writer(watchdog._alert_dead_worker), holder)
    holder.close()

    final = _row(factory, day)
    assert final.kill_switch is True
    assert final.kill_reason == "MDD 15% 초과"


# ── B: the reset vs a halt that lands during it ───────────────────────────────

def test_halt_arriving_during_a_reset_survives(factory, watchdog):
    """The fail-open case. The reset clears what it locked; a halt that arrives
    while it holds the lock waits, then lands on the cleared row."""
    day = trading_day()
    _seed(factory, day, kill_switch=True, reason="일손실 3% 초과")

    reset = factory()
    halted = [r for r in lock_risk_rows(reset, [day]) if r.kill_switch]
    assert [r.trade_date for r in halted] == [day]
    for r in halted:
        r.kill_switch = False
        r.kill_reason = None

    _assert_blocked_then_run(_Writer(watchdog._alert_dead_worker), reset)
    reset.close()

    final = _row(factory, day)
    assert final.kill_switch is True
    assert final.kill_reason == "Worker 하트비트 없음 — 프로세스 재시작 필요"


def test_reset_endpoint_does_not_erase_a_halt_committed_after_its_read(factory, monkeypatch):
    """The endpoint itself (local only — FastAPI is not installed in the
    Postgres CI jobs). A watchdog halt waits behind the reset's lock and
    survives it, instead of being overwritten by the reset's stale ``False``."""
    pytest.importorskip("fastapi")
    import os
    if not os.environ.get("JWT_SECRET_KEY"):   # api.auth refuses to import without it
        monkeypatch.setenv("JWT_SECRET_KEY", "test-only-secret")
    from api.routers.risk import KillSwitchResetRequest, reset_kill_switch

    monkeypatch.setenv("OPERATOR_USER_IDS", "7")
    day = trading_day()
    _seed(factory, day, kill_switch=True, reason="일손실 3% 초과")

    # Commit a new halt from the moment the reset has locked (and so read) its
    # rows, i.e. the window the unlocked reset lost halts in.
    import api.routers.risk as risk_router
    real_lock = risk_router._lock_halted_rows
    halter = {}

    def lock_then_race(db):
        rows = real_lock(db)

        def halt():
            with factory() as s:
                r, _ = lock_risk_row(s, day)
                r.kill_switch = True
                r.kill_reason = "MDD 15% 초과"
                s.commit()

        halter["t"] = _Writer(halt)
        halter["t"].start()
        halter["t"].join(_BLOCK_SEC)
        assert halter["t"].is_alive(), "the halt did not wait for the reset's lock"
        return rows

    monkeypatch.setattr(risk_router, "_lock_halted_rows", lock_then_race)

    user = SimpleNamespace(id=7, email="ops@example.com")
    with factory() as db:
        resp = reset_kill_switch(KillSwitchResetRequest(reason="원인 확인 후 해제"),
                                 current_user=user, db=db)
    assert resp.code == 1, resp
    halter["t"].join(10)
    assert halter["t"].error is None

    final = _row(factory, day)
    assert final.kill_switch is True
    assert final.kill_reason == "MDD 15% 초과"


# ── new trading day: concurrent creation ──────────────────────────────────────

def test_two_writers_opening_a_new_day_do_not_collide(factory):
    """``INSERT … ON CONFLICT DO NOTHING``: the second waits on the first's
    uncommitted insert, then finds and locks that row — no IntegrityError."""
    day = trading_day()

    first = factory()
    row, created = lock_risk_row(first, day)
    assert created is True
    row.kill_switch = True
    row.kill_reason = "first"

    def second():
        with factory() as s:
            r, c = lock_risk_row(s, day)
            seen = (c, r.kill_switch, r.kill_reason)
            s.commit()
            return seen

    w = _Writer(second)
    _assert_blocked_then_run(w, first)
    first.close()

    assert w.result == (False, True, "first")
    with factory() as s:
        assert len(s.execute(select(DailyRiskState)).scalars().all()) == 1


def test_shutdown_checkpoint_racing_a_new_day_does_not_fail(factory, monkeypatch):
    """D: the worker's shutdown checkpoint used to insert the new day's row
    itself; racing another writer's uncommitted insert it waited, then died on
    the duplicate key. Now it waits, finds that row, and writes only equity —
    keeping the other writer's halt."""
    import backend.worker.runner as runner_mod
    from contextlib import contextmanager

    @contextmanager
    def _session():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(runner_mod, "_session", _session)
    day = trading_day()

    tracker = SimpleNamespace(
        _lock=threading.Lock(), daily_pnl=-1.0, weekly_pnl=-2.0,
        peak_equity=1_000_000.0, kill_switch=False, kill_reason="")
    worker = SimpleNamespace(_loss_tracker=tracker)

    holder = factory()
    row, created = lock_risk_row(holder, day)
    assert created is True
    row.kill_switch = True
    row.kill_reason = "외부 halt"

    w = _Writer(lambda: runner_mod.StrategyWorker._checkpoint_equity(worker))
    _assert_blocked_then_run(w, holder)
    holder.close()

    final = _row(factory, day)
    assert final.kill_switch is True
    assert final.kill_reason == "외부 halt"
    assert final.daily_pnl == -1.0


# ── A: the loss tracker ───────────────────────────────────────────────────────

def test_tracker_adopts_the_halt_committed_ahead_of_it(factory, safe_mode, monkeypatch):
    """The tracker decides against the row as committed: a halt committed while
    it waited is adopted on this write (and closes SAFE_MODE), not missed until
    the next one."""
    from backend.quant.risk.engine import PersistentLossTracker, RiskConfig
    monkeypatch.setattr(PersistentLossTracker, "_do_kill_switch_io",
                        lambda *_a, **_k: None, raising=False)

    day = trading_day()
    _seed(factory, day)
    tracker = PersistentLossTracker(RiskConfig(), db_factory=factory)
    tracker._ks_written = tracker._ks_epoch   # no intent of its own to assert

    holder = factory()
    row, _ = lock_risk_row(holder, day)
    row.kill_switch = True
    row.kill_reason = "Worker 하트비트 없음"

    w = _Writer(tracker._write_db)
    _assert_blocked_then_run(w, holder)
    holder.close()

    assert w.result is True
    assert tracker.kill_switch is True
    assert tracker.kill_reason == "Worker 하트비트 없음"
    assert safe_mode.can_trade is False
    assert _row(factory, day).kill_switch is True


# ── lock order ────────────────────────────────────────────────────────────────

def test_multi_day_locks_are_taken_in_date_order(factory):
    """Whatever order the caller passes, rows are locked oldest first, so two
    multi-day writers cannot hold one day each and wait on the other."""
    today = trading_day()
    yesterday = today - timedelta(days=1)
    _seed(factory, today)
    _seed(factory, yesterday)

    holder = factory()
    assert [r.trade_date for r in lock_risk_rows(holder, [yesterday])] == [yesterday]

    # Passed newest first. If it locked today before waiting on yesterday, the
    # probe below could not lock today while the writer waits.
    w = _Writer(lambda: _lock_and_commit(factory, [today, yesterday]))
    w.start()
    w.join(_BLOCK_SEC)
    assert w.is_alive(), "multi-day writer did not wait for yesterday's lock"

    probe = factory()
    probe.execute(select(DailyRiskState).where(DailyRiskState.trade_date == today)
                  .with_for_update(nowait=True))
    probe.rollback()
    probe.close()

    holder.commit()
    holder.close()
    w.join(10)
    assert not w.is_alive() and w.error is None
    assert w.result == [yesterday, today]


def _lock_and_commit(factory, days):
    with factory() as s:
        order = [r.trade_date for r in lock_risk_rows(s, days)]
        s.commit()
        return order


# ── kis-api: two concurrent strategy starts, one slot ────────────────────────

def test_concurrent_strategy_starts_take_the_slot_once(pg_trading_engine):
    """B1: without the advisory lock both transactions read an empty slot and
    both insert an active run."""
    import threading
    import time
    from sqlalchemy.orm import Session
    from backend.api import server as srv
    from backend.database.models import StrategyRun

    with Session(pg_trading_engine) as db:
        db.query(StrategyRun).delete()
        db.commit()

    a_has_lock = threading.Event()
    results = {}

    def first():
        with Session(pg_trading_engine) as db:
            results["a"] = srv._occupying_run(db)
            db.add(StrategyRun(name="a", strategy_type="indicator", config="{}", is_active=True))
            db.flush()
            a_has_lock.set()
            time.sleep(0.5)          # hold the lock while the second start arrives
            db.commit()

    def second():
        a_has_lock.wait(5)
        with Session(pg_trading_engine) as db:
            results["b"] = srv._occupying_run(db)
            if results["b"] is None:
                db.add(StrategyRun(name="b", strategy_type="indicator", config="{}",
                                   is_active=True))
            db.commit()

    ta, tb = threading.Thread(target=first), threading.Thread(target=second)
    ta.start(); tb.start(); ta.join(10); tb.join(10)

    assert results["a"] is None
    assert results["b"] is not None, "the second start must see the first run"
    with Session(pg_trading_engine) as db:
        assert db.query(StrategyRun).count() == 1
        db.query(StrategyRun).delete()
        db.commit()
