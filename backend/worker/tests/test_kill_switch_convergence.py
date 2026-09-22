"""Issue #158 — who owns the kill-switch flag, memory or the database?

`PersistentLossTracker._write_db()` wrote `row.kill_switch` from its in-memory
value on every PnL write, without re-reading the row. Two other components write
that same column from outside this process:

| writer | where | direction |
|---|---|---|
| operator reset API | `api/routers/risk.py` | `True -> False` |
| `WorkerWatchdog` (API process) | `backend/worker/heartbeat.py` | `False -> True` |

So the last writer won, and **both** directions were wrong:

* **fail-closed** — an operator clears the halt, the worker's stale `True` puts
  it back on the next PnL write. PR #157 could only paper over this with an
  operational procedure ("stop the worker, then clear, then start").
* **fail-open, and worse** — Redis dies, the heartbeat key expires, the watchdog
  sets `True` in the DB while this worker is alive holding `False`; the next
  write erases the halt with no trace. PR #159 had to route its shutdown
  checkpoint around this path rather than fix it.

The fix records **intent**, not state: state alone cannot tell a fresh breach
from a stale `True` sitting on top of an external clear. A monotonic counter
(`_ks_epoch` vs `_ks_written`) says whether *this process* has something to
assert; when it does not, the row on disk wins and memory converges to it.

These tests drive the real `PersistentLossTracker` against SQLite. Mocks cannot
show this bug — it only exists in the read/write interleaving.
"""
import threading
import time
from datetime import timedelta as _timedelta

import pytest
from backend.database.testing import make_test_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, DailyRiskState
from backend.quant.risk.engine import PersistentLossTracker, RiskConfig


@pytest.fixture(autouse=True)
def _no_alert_thread(monkeypatch):
    """`_fire_kill_switch_alert` dispatches Telegram/WebSocket/audit I/O onto a
    daemon thread. None of that is under test here, and on SQLite's single
    StaticPool connection it races the writes these tests inspect — roughly one
    run in twelve. Stubbed so the module measures convergence, not thread timing.

    It does not weaken anything asserted below: the SAFE_MODE gating these tests
    check is closed by `_write_db`'s own settle step, not by that thread.
    """
    monkeypatch.setattr(PersistentLossTracker, "_do_kill_switch_io",
                        lambda self, reason: None)


@pytest.fixture(autouse=True)
def _isolate_safe_mode():
    """SAFE_MODE is a module-level singleton that these tests read and write."""
    from backend.worker.recovery import SAFE_MODE
    saved = (SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause)
    yield
    SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause = saved


@pytest.fixture()
def factory():
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


#: The key `_write_db` uses. This used to be `date.today()` while the tracker
#: *read* with `_seoul_today()` — a split that was its own bug (issue #160) and
#: out of scope for this module, so these tests deliberately keyed off the write
#: side. #160 closed the split: both sides now go through `trading_day()`, and
#: this follows the write side as it always did.
def _write_key():
    from backend.database.models import trading_day
    return trading_day()


def _row(factory):
    """Today's risk row, as `_write_db` keys it."""
    sess = factory()
    try:
        return sess.get(DailyRiskState, _write_key())
    finally:
        sess.close()


def _external_write(factory, **fields):
    """Write the row the way the reset endpoint / watchdog do — outside the
    tracker, with no knowledge of its in-memory state.

    Both of those use `date.today()` (`api/routers/risk.py`,
    `backend/worker/heartbeat.py`), so this matches production.
    """
    sess = factory()
    try:
        today = _write_key()
        row = sess.get(DailyRiskState, today)
        if row is None:
            row = DailyRiskState(trade_date=today)
            sess.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        sess.commit()
    finally:
        sess.close()


def _tracker(factory, **cfg):
    return PersistentLossTracker(config=RiskConfig(**cfg), redis_client=None,
                                 db_factory=factory)


def _breach(tracker):
    """Drive a real MDD breach through the public API."""
    tracker.peak_equity = 1_000_000.0
    tracker.record_pnl(-500_000.0, 500_000.0)


# ── this process has something to say: it must reach the DB ──────────────────

class TestLocalIntentWins:
    def test_a_new_breach_is_written(self, factory):
        t = _tracker(factory)
        assert t.kill_switch is False

        _breach(t)

        assert t.kill_switch is True
        assert _row(factory).kill_switch is True

    def test_manual_reset_is_written(self, factory):
        t = _tracker(factory)
        _breach(t)
        assert _row(factory).kill_switch is True

        t.manual_reset()

        assert _row(factory).kill_switch is False
        assert _row(factory).kill_reason is None

    def test_a_breach_still_wins_over_a_cleared_row(self, factory):
        """The DB winning must not mean new breaches get swallowed."""
        t = _tracker(factory)
        _external_write(factory, kill_switch=False, kill_reason=None)

        _breach(t)

        assert _row(factory).kill_switch is True


# ── this process has nothing to say: the row on disk wins ────────────────────

class TestExternalWriteSurvives:
    def test_an_operator_clear_is_not_undone(self, factory):
        """PR #157's endpoint clears the flag while the worker runs. The stale
        in-memory True must not put it back.

        The day is rolled over first so the breach condition no longer holds —
        otherwise `_evaluate()` re-fires and re-halts, which is a *different*
        mechanism (a fresh, deliberate decision, not a stale overwrite). That
        one is pinned separately in `TestABreachStillReAsserts`.
        """
        t = _tracker(factory)
        _breach(t)
        assert t.kill_switch is True

        _external_write(factory, kill_switch=False, kill_reason=None)
        t.reset_daily()                       # 00:01 daily reset
        t.reset_weekly()                      # ...and the weekly counter too,
        t.record_pnl(0.0, 1_000_000.0)        # or the weekly limit re-fires

        assert _row(factory).kill_switch is False, (
            "the worker overwrote an operator's reset — #157's whole procedure "
            "exists because of this")

    def test_memory_converges_to_the_clear(self, factory):
        """Not just the row: the tracker's own `can_buy()` and
        `StartupRecovery._step_risk` read the in-memory flag."""
        t = _tracker(factory)
        _breach(t)

        _external_write(factory, kill_switch=False, kill_reason=None)
        t.reset_daily()
        t.reset_weekly()
        t.record_pnl(0.0, 1_000_000.0)

        assert t.kill_switch is False
        assert t.can_buy()[0] is True

    def test_a_watchdog_halt_is_not_erased(self, factory):
        """The fail-open. Redis dies, the heartbeat expires, the API-side
        WorkerWatchdog sets the halt — while this worker is alive and believes
        nothing is wrong."""
        t = _tracker(factory)
        t.peak_equity = 1_000_000.0
        assert t.kill_switch is False

        _external_write(factory, kill_switch=True,
                        kill_reason="Worker 하트비트 없음 — 프로세스 재시작 필요")
        t.record_pnl(0.0, 1_000_000.0)

        row = _row(factory)
        assert row.kill_switch is True, (
            "the worker erased a halt the watchdog set — it would keep trading")
        assert row.kill_reason is not None

    def test_memory_converges_to_the_watchdog_halt(self, factory):
        t = _tracker(factory)
        t.peak_equity = 1_000_000.0

        _external_write(factory, kill_switch=True, kill_reason="워치독")
        t.record_pnl(0.0, 1_000_000.0)

        assert t.kill_switch is True
        assert t.can_buy()[0] is False


# ── restoring from the DB is not an opinion of this process ──────────────────

class TestRestoreIsNotIntent:
    def test_a_restored_halt_does_not_re_assert_itself(self, factory):
        """A tracker built while the row said True holds True in memory. That is
        the DB's value, not this process's claim — so a later external clear must
        still win. Marking the restore as intent would recreate the whole bug for
        every worker that restarts into a halted day."""
        _external_write(factory, kill_switch=True, kill_reason="MDD 한도 초과")
        t = _tracker(factory)
        assert t.kill_switch is True, "precondition: restored from the row"

        _external_write(factory, kill_switch=False, kill_reason=None)
        t.record_pnl(0.0, 1_000_000.0)

        assert _row(factory).kill_switch is False
        assert t.kill_switch is False


# ── the equity columns are never the disputed part ───────────────────────────

class TestEquityAlwaysWritten:
    def test_written_when_this_process_asserts(self, factory):
        t = _tracker(factory)
        _breach(t)

        row = _row(factory)
        assert row.peak_equity == 1_000_000.0
        assert row.daily_pnl == -500_000.0

    def test_written_when_the_db_wins(self, factory):
        t = _tracker(factory)
        t.peak_equity = 2_000_000.0
        _external_write(factory, kill_switch=True, kill_reason="워치독")

        t.record_pnl(-1000.0, 2_000_000.0)

        row = _row(factory)
        assert row.daily_pnl == -1000.0
        assert row.peak_equity == 2_000_000.0
        assert row.kill_switch is True      # and the halt is still untouched


# ── the legacy long-lived-session branch behaves identically ─────────────────

class TestLegacySessionBranch:
    """`_write_db` has two branches — `db_factory` and a legacy `db_session`.
    They were copy-pasted, which is exactly how one gets fixed and the other
    does not."""

    def test_an_operator_clear_is_not_undone(self, factory):
        sess = factory()
        t = PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                  db_session=sess)
        t.peak_equity = 1_000_000.0
        t.record_pnl(-500_000.0, 500_000.0)
        assert t.kill_switch is True

        _external_write(factory, kill_switch=False, kill_reason=None)
        sess.expire_all()                     # the endpoint committed elsewhere
        t.reset_daily()
        t.reset_weekly()
        t.record_pnl(0.0, 1_000_000.0)

        assert _row(factory).kill_switch is False
        sess.close()

    def test_a_new_breach_is_written(self, factory):
        sess = factory()
        t = PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                  db_session=sess)
        t.peak_equity = 1_000_000.0
        t.record_pnl(-500_000.0, 500_000.0)

        assert _row(factory).kill_switch is True
        sess.close()


# ── a breach arriving mid-write is not swallowed ─────────────────────────────

class TestNoLostBreach:
    def test_a_breach_during_a_write_still_reaches_the_db(self, factory):
        """The reason the marker is a counter and not a boolean.

        A write snapshots the flag, commits, then records that it has caught up.
        A breach can land *inside* that window. With a plain bool the settle step
        would either clear the marker (discarding the breach) or adopt the row it
        just read (clobbering the breach in memory) — either way the halt is lost
        and no later write knows to assert it.

        The breach is injected from the session's ``commit()`` so it lands
        exactly between the snapshot and the settle, which is the only window
        where the epoch comparison does any work.
        """
        fired = threading.Event()
        holder = {}

        class _CommitHook:
            """Passes everything through, but fires once at commit time."""

            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def commit(self):
                self._inner.commit()
                tracker = holder.get("t")
                if tracker is not None and not fired.is_set():
                    fired.set()
                    with tracker._lock:
                        tracker.kill_switch = True
                        tracker.kill_reason = "쓰기 도중 발생한 위반"
                    tracker._mark_kill_switch_changed()

        t = PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                  db_factory=lambda: _CommitHook(factory()))
        holder["t"] = t
        t.peak_equity = 1_000_000.0

        t.record_pnl(0.0, 1_000_000.0)    # benign write; breach lands at commit
        assert fired.is_set(), "the hook never ran — the test proves nothing"
        t.record_pnl(0.0, 1_000_000.0)    # the next ordinary write

        assert _row(factory).kill_switch is True, (
            "a breach that landed during a write was swallowed")
        assert t.kill_switch is True


# ── regression guard ─────────────────────────────────────────────────────────

class TestResetDailyLeavesTheHalt:
    def test_the_daily_reset_does_not_clear_a_halt(self, factory):
        """`reset_daily()` zeroes the day's PnL and calls `_persist()`. It has
        never cleared the kill switch and must not start."""
        t = _tracker(factory)
        _breach(t)

        t.reset_daily()

        assert t.kill_switch is True
        assert _row(factory).kill_switch is True


# ── the other way a reset gets undone — deliberate, and NOT fixed here ───────

class TestABreachStillReAsserts:
    """Pinning a behaviour this change deliberately leaves alone.

    Clearing the row does not make the breach *condition* go away. While it
    still holds, the next `record_pnl()` runs `_evaluate()`, which halts again —
    bumping the epoch, so the new value is asserted. That is a fresh decision by
    this process, not the stale overwrite #158 is about, and suppressing it would
    mean trading on through a live limit breach.

    The consequence is worth stating plainly: for a daily-loss or MDD halt the
    condition normally holds for the rest of the session, so an operator's reset
    does **not** let trading resume intraday by itself. Fixing #158 does not
    change that. See the PR description.
    """

    def test_the_halt_returns_while_the_condition_holds(self, factory):
        t = _tracker(factory)
        _breach(t)
        _external_write(factory, kill_switch=False, kill_reason=None)

        t.record_pnl(0.0, 500_000.0)          # day's loss still past the limit

        assert _row(factory).kill_switch is True
        assert t.kill_switch is True

    def test_and_that_is_a_fresh_assertion_not_a_stale_write(self, factory):
        """The distinction matters: the tracker *decided* again rather than
        replaying an old value.

        Reads `_ks_epoch` directly because that is precisely the thing under
        test — whether `_evaluate()` formed a new opinion. `record_pnl()`'s
        return value cannot answer it: the tracker was already halted, so there
        is no transition to report and the call correctly says `"unchanged"`.
        """
        t = _tracker(factory)
        _breach(t)
        before = t._ks_epoch
        _external_write(factory, kill_switch=False, kill_reason=None)

        t.record_pnl(0.0, 500_000.0)

        assert t._ks_epoch > before, (
            "the halt came back without _evaluate() deciding — that would be "
            "the stale overwrite this change removes")


# ── review findings: cases where "the DB wins" must NOT apply ────────────────

class TestNewRowIsNotAnExternalOpinion:
    """A row that does not exist yet holds nobody's decision.

    Reading `kill_switch` off a freshly constructed `DailyRiskState` yields
    `None` before flush, and adopting `bool(None)` would **silently clear a live
    halt**. `origin/main` carried the flag into the new row instead, so getting
    this wrong is a regression, not just a gap — and it fires on the first write
    against every new date key, i.e. at the day boundary, which
    `backend/worker/scheduler.py` expects a halt to survive.
    """

    def _tomorrow_key(self, monkeypatch):
        """Advance the trading day.

        Patches `trading_day` itself. Before #160 this had to stub
        `engine.date`, because the read side and the write side derived the day
        separately; now there is one door, and moving it moves both.
        """
        from backend.database.models import trading_day
        tomorrow = trading_day() + _timedelta(days=1)
        monkeypatch.setattr("backend.database.models.trading_day",
                            lambda: tomorrow)
        return tomorrow

    def test_a_restored_halt_survives_the_day_boundary(self, factory, monkeypatch):
        _external_write(factory, kill_switch=True, kill_reason="MDD 한도 초과")
        t = _tracker(factory)
        t.peak_equity = 1_000_000.0
        assert t.kill_switch is True
        assert t._ks_epoch == t._ks_written, "precondition: nothing to assert"

        tomorrow = self._tomorrow_key(monkeypatch)
        t.record_pnl(0.0, 1_000_000.0)        # benign: no _evaluate decision

        assert t.kill_switch is True, "the day boundary cleared a live halt"
        assert t.can_buy()[0] is False
        sess = factory()
        try:
            assert sess.get(DailyRiskState, tomorrow).kill_switch is True
        finally:
            sess.close()


class TestFailedWriteDoesNotArmAClear:
    """A write that failed leaves this process's intent unwritten, so it is
    re-asserted later. That is right for a halt and wrong for a clear.

    Re-asserting a stale *halt* is fail-closed. Re-asserting a stale *clear* over
    a halt somebody else set in the meantime is fail-open, and it is exactly the
    #158 overwrite wearing a different hat.
    """

    def _breaking_factory(self, factory, fail):
        class _Boom:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def commit(self):
                if fail["now"]:
                    raise RuntimeError("DB down")
                self._inner.commit()

        return lambda: _Boom(factory())

    def test_a_pending_clear_does_not_overwrite_a_watchdog_halt(self, factory):
        fail = {"now": False}
        t = PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                  db_factory=self._breaking_factory(factory, fail))
        t.peak_equity = 1_000_000.0
        t.record_pnl(-500_000.0, 500_000.0)   # halt, persisted
        t.reset_daily()
        t.reset_weekly()

        fail["now"] = True
        t.manual_reset()                      # operator clears — write FAILS
        fail["now"] = False

        # Meanwhile the watchdog halts the deployment.
        _external_write(factory, kill_switch=True, kill_reason="워치독")
        t.record_pnl(0.0, 1_000_000.0)

        assert _row(factory).kill_switch is True, (
            "a clear whose write failed came back and erased a live halt")

    def test_a_pending_halt_is_still_re_asserted(self, factory):
        """The fail-closed direction must keep working."""
        fail = {"now": True}
        t = PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                  db_factory=self._breaking_factory(factory, fail))
        t.peak_equity = 1_000_000.0
        t.record_pnl(-500_000.0, 500_000.0)   # halt, write FAILS
        assert t.kill_switch is True
        fail["now"] = False

        t.record_pnl(0.0, 500_000.0)

        assert _row(factory).kill_switch is True


class TestAdoptedHaltActuallyStopsTrading:
    """`can_buy()` has no production callers — the real order gate is
    `SAFE_MODE` (`backend/strategy/base.py`). Adopting a halt into memory without
    closing that gate would converge the flag and change nothing."""

    def test_adopting_a_halt_closes_the_safe_mode_gate(self, factory):
        from backend.worker.recovery import SAFE_MODE

        SAFE_MODE.enable()
        assert SAFE_MODE.can_trade is True

        t = _tracker(factory)
        t.peak_equity = 1_000_000.0
        _external_write(factory, kill_switch=True, kill_reason="워치독 하트비트 없음")
        t.record_pnl(0.0, 1_000_000.0)

        assert t.kill_switch is True
        assert SAFE_MODE.can_trade is False, (
            "the halt converged into memory but trading was never actually "
            "blocked in this process")

    def test_adopting_a_clear_does_not_re_open_the_gate(self, factory):
        """Re-enabling SAFE_MODE from here would bypass `StartupRecovery`'s
        checks. Resuming is a restart's job (P0-12's remaining half)."""
        from backend.worker.recovery import SAFE_MODE

        t = _tracker(factory)
        _breach(t)
        SAFE_MODE.disable("킬스위치")
        _external_write(factory, kill_switch=False, kill_reason=None)
        t.reset_daily()
        t.reset_weekly()
        t.record_pnl(0.0, 1_000_000.0)

        assert t.kill_switch is False
        assert SAFE_MODE.can_trade is False


class TestDateRolloverDoesNotDeadlock:
    """Pre-existing, found in review, and severe enough to fix here.

    `PersistentLossTracker.record_pnl` holds `self._lock` while
    `LossTracker.record_pnl` calls the overridden `reset_daily()`, which
    re-acquires it. With a plain `Lock` that is a permanent hang — and it happens
    on the first fill after Seoul midnight, which lands inside the US session
    this system trades.
    """

    def test_record_pnl_across_the_date_boundary_returns(self, factory):
        t = _tracker(factory)
        t.trade_date = t.trade_date - _timedelta(days=1)

        done = threading.Event()
        threading.Thread(
            target=lambda: (t.record_pnl(0.0, 1_000_000.0), done.set()),
            daemon=True).start()

        assert done.wait(5), (
            "record_pnl deadlocked on the Seoul date rollover — the worker's "
            "fill pipeline would hang for good")


# ── the audit trail must not invent a cause ──────────────────────────────────

class TestAdoptedHaltIsNotBlamedOnAFill:
    """Convergence makes `kill_switch` flip `False -> True` on a write that
    merely *noticed* someone else's halt. The fill pipeline used to read any such
    flip as "this fill breached a limit" and audit it against that symbol and its
    realized P&L — inventing a cause for whoever reads the trail next.
    """

    def _worker_with(self, tracker, patched):
        import backend.worker.runner as runner
        from backend.execution.order_machine import OrderStateMachine
        from backend.execution.position_tracker import PositionTracker

        w = runner.StrategyWorker.__new__(runner.StrategyWorker)
        w._loss_tracker = tracker
        w._poller = None
        w._last_known_equity = 1_000_000.0
        w._publish_order_update = lambda order: None
        w._persist_fill = lambda fill, order: None
        w._upsert_position_db = lambda *a: None
        return w, PositionTracker(OrderStateMachine()), OrderStateMachine()

    def _sell_order(self):
        from backend.brokers.models import Order, OrderStatus
        return Order(id="O1", symbol="005930", side="sell", qty=10,
                     price=100.0, status=OrderStatus.FILLED,
                     filled_qty=10, avg_fill_price=100.0)

    def _audit_types(self, factory):
        from backend.database.models import AuditLog
        sess = factory()
        try:
            return [r.event_type for r in sess.query(AuditLog).all()]
        finally:
            sess.close()

    def test_an_external_halt_is_audited_as_adopted(self, factory, monkeypatch):
        import backend.worker.runner as runner
        monkeypatch.setattr(runner, "_SessionFactory", factory)

        t = _tracker(factory)
        t.peak_equity = 1_000_000.0
        w, tracker, machine = self._worker_with(t, factory)
        w._reconciler = None

        # Somebody else halts the deployment; this worker has decided nothing.
        _external_write(factory, kill_switch=True, kill_reason="워치독 하트비트 없음")

        w._make_fill_callback(tracker, machine, run_id=1)(self._sell_order())

        types = self._audit_types(factory)
        assert "kill_switch_adopted" in types, types
        assert "kill_switch_triggered" not in types, (
            "an externally-set halt was blamed on this fill's symbol and P&L")

    def test_a_real_breach_is_still_audited_as_triggered(self, factory, monkeypatch):
        import backend.worker.runner as runner
        monkeypatch.setattr(runner, "_SessionFactory", factory)

        t = _tracker(factory)
        t.peak_equity = 1_000_000.0
        w, tracker, machine = self._worker_with(t, factory)
        w._reconciler = None
        w._last_known_equity = 500_000.0     # a 50% drawdown — a real MDD breach

        w._make_fill_callback(tracker, machine, run_id=1)(self._sell_order())

        types = self._audit_types(factory)
        assert "kill_switch_triggered" in types, types
        assert "kill_switch_adopted" not in types


# ── review round 2: attribution must be per call, not per process ────────────

class TestRecordPnlReportsItsOwnOutcome:
    """`record_pnl()` returns what **that call** did.

    The previous revision had the fill pipeline diff a process-global decision
    counter across the call. Fills arrive concurrently on poller threads, so
    fill #1 could snapshot the counter, fill #2 could breach a limit, and fill #1
    would then see a changed counter and file `kill_switch_triggered` against its
    own symbol and P&L — a cause invented for whoever reads the audit trail next.
    """

    def test_a_breach_reports_triggered(self, factory):
        t = _tracker(factory)
        t.peak_equity = 1_000_000.0

        assert t.record_pnl(-500_000.0, 500_000.0) == "triggered"

    def test_an_uneventful_write_reports_unchanged(self, factory):
        t = _tracker(factory)
        t.peak_equity = 1_000_000.0

        assert t.record_pnl(0.0, 1_000_000.0) == "unchanged"

    def test_picking_up_an_external_halt_reports_adopted(self, factory):
        t = _tracker(factory)
        t.peak_equity = 1_000_000.0
        _external_write(factory, kill_switch=True, kill_reason="워치독")

        assert t.record_pnl(0.0, 1_000_000.0) == "adopted"

    def test_an_already_halted_tracker_does_not_re_report_adopted(self, factory):
        """Only the transition is news. Reporting every later write as `adopted`
        would file a fresh audit row on each fill."""
        t = _tracker(factory)
        t.peak_equity = 1_000_000.0
        _external_write(factory, kill_switch=True, kill_reason="워치독")
        assert t.record_pnl(0.0, 1_000_000.0) == "adopted"

        assert t.record_pnl(0.0, 1_000_000.0) == "unchanged"

    def test_a_halt_landing_while_this_call_waits_is_not_claimed(self, factory):
        """*Why* the per-call answer can be trusted: it is decided **inside** the
        lock, after waiting, not from a read taken before waiting.

        A benign call can sit on the lock while another thread halts. If the
        "was it halted?" read happens before acquiring the lock, that call wakes
        up, sees the switch on, and reports `"triggered"` — filing another
        thread's breach against its own symbol and P&L. Reading it after
        acquiring gives the honest answer.

        Sequence: hold the lock, let the benign call start and block on it, flip
        the switch, release.
        """
        t = _tracker(factory)
        t.peak_equity = 1_000_000.0

        lock_held = threading.Event()
        may_release = threading.Event()
        result = {}

        def _holder():
            with t._lock:
                lock_held.set()
                may_release.wait(5)
                # A breach decided by *this* holder, not by the benign caller.
                t.kill_switch = True
                t.kill_reason = "다른 스레드의 위반"

        holder = threading.Thread(target=_holder, daemon=True)
        holder.start()
        assert lock_held.wait(5), "holder never took the lock"

        benign = threading.Thread(
            target=lambda: result.update(
                outcome=t.record_pnl(0.0, 1_000_000.0)),
            daemon=True)
        benign.start()
        # Give the benign call time to reach the lock and block there. Anything
        # it reads before that point is what this test is about.
        time.sleep(0.2)
        may_release.set()
        holder.join(5)
        benign.join(5)

        assert result.get("outcome") != "triggered", (
            f"a benign call reported {result.get('outcome')!r} for a halt "
            f"another thread decided — it would be audited against the wrong "
            f"symbol and P&L")

    def test_the_decision_counter_is_not_exposed(self):
        """Re-adding a public accessor would invite the same wrong approach."""
        from backend.quant.risk.engine import PersistentLossTracker

        assert not hasattr(PersistentLossTracker, "kill_switch_decisions")


class TestFillPipelineUsesTheReturnedOutcome:
    def test_the_runner_no_longer_diffs_a_global_counter(self):
        """A grep-style guard: the attribution must come from `record_pnl()`'s
        return value, not from snapshots taken around it."""
        import inspect

        import backend.worker.runner as runner

        src = inspect.getsource(runner.StrategyWorker._make_fill_callback)
        assert "decisions_before" not in src
        assert "kill_switch_decisions" not in src
        assert 'outcome == "triggered"' in src
