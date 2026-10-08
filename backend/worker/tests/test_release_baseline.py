"""P0-12 — a released risk halt resumes without a restart, and halts again
only if the loss gets worse.

Before: an operator's release (``POST /api/risk/kill-switch/reset``) cleared
the rows, but

* the worker's order gate (``SAFE_MODE``) stayed shut until a restart, and
* the next fill halted straight back while the loss still stood past the limit.

Now (operator decisions, 2026-10-08):

* the worker polls every minute and reopens the gate — only for a risk-limit
  halt (``RISK_BREACH``) in a worker whose recovery succeeded;
* a release accepts the loss as it stands: the daily and weekly limits halt
  again only after another 1% of capital is lost (the next risk day starts
  fresh), and MDD is measured from equity at the release.

No broker, no network: SQLite rows, the alert I/O stubbed.
"""
from __future__ import annotations

import json
import threading
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import AuditLog, Base, DailyRiskState
from backend.database.testing import make_test_engine
from backend.quant.risk.engine import (RELEASE_BASELINE_EVENT,
                                       PersistentLossTracker, RiskConfig)
from backend.risk.halt_policy import HaltCause

DAY = date(2026, 10, 8)
PEAK = 1_000_000.0


@pytest.fixture()
def today(monkeypatch):
    """The risk day, movable by the test."""
    box = {"day": DAY}
    monkeypatch.setattr("backend.database.models.trading_day", lambda: box["day"])
    return box


@pytest.fixture(autouse=True)
def _sync_halt_io(monkeypatch):
    """Close the gate at once, as the alert thread would, and send nothing."""
    def _io(self, reason):
        from backend.worker.recovery import SAFE_MODE
        SAFE_MODE.disable(f"킬스위치: {reason}", cause=HaltCause.RISK_BREACH)
    monkeypatch.setattr(PersistentLossTracker, "_do_kill_switch_io", _io)


@pytest.fixture(autouse=True)
def _isolate_safe_mode():
    from backend.worker.recovery import SAFE_MODE
    saved = (SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause)
    SAFE_MODE.enable()
    yield
    SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause = saved


@pytest.fixture()
def factory():
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _tracker(factory):
    return PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                 db_factory=factory)


def _seed(factory, day, **kw):
    sess = factory()
    try:
        row = sess.get(DailyRiskState, day) or DailyRiskState(trade_date=day)
        for k, v in kw.items():
            setattr(row, k, v)
        sess.add(row)
        sess.commit()
    finally:
        sess.close()


def _row(factory, day=DAY):
    sess = factory()
    try:
        return sess.get(DailyRiskState, day)
    finally:
        sess.close()


def _release(factory):
    """What the app's reset does: every halted row cleared."""
    sess = factory()
    try:
        for row in sess.query(DailyRiskState).filter(DailyRiskState.kill_switch):
            row.kill_switch = False
            row.kill_reason = None
        sess.commit()
    finally:
        sess.close()


def _audit_release(factory):
    """The audit row the app's reset writes with the clear."""
    sess = factory()
    try:
        sess.add(AuditLog(event_type="kill_switch_reset", actor="operator:1",
                          detail=json.dumps({"reason": "test"})))
        sess.commit()
    finally:
        sess.close()


def _audits(factory):
    sess = factory()
    try:
        return [json.loads(r.detail) for r in sess.query(AuditLog)
                .filter(AuditLog.event_type == RELEASE_BASELINE_EVENT)
                .order_by(AuditLog.id)]
    finally:
        sess.close()


def _halted_daily(factory):
    """Down 3.5% today against the 3% limit, then released."""
    t = _tracker(factory)
    t.peak_equity = PEAK
    t.record_pnl(-35_000.0, PEAK - 35_000.0)
    assert t.kill_switch is True
    return t


# ── the release baseline ──────────────────────────────────────────────────────

class TestTheDailyLimitAfterARelease:
    def test_the_loss_as_it_stands_does_not_halt_again(self, factory, today):
        t = _halted_daily(factory)
        _release(factory)

        t.record_pnl(0.0, PEAK - 35_000.0)

        assert t.kill_switch is False
        assert _row(factory).kill_switch is False

    def test_up_to_one_percent_more_does_not_halt(self, factory, today):
        t = _halted_daily(factory)
        _release(factory)
        t.record_pnl(0.0, PEAK - 35_000.0)

        t.record_pnl(-9_000.0, PEAK - 44_000.0)        # -4.4%

        assert t.kill_switch is False

    def test_more_than_one_percent_more_halts(self, factory, today):
        t = _halted_daily(factory)
        _release(factory)
        t.record_pnl(0.0, PEAK - 35_000.0)

        assert t.record_pnl(-11_000.0, PEAK - 46_000.0) == "triggered"   # -4.6%
        assert t.kill_reason.startswith("일일 손실 한도 초과")
        assert _row(factory).kill_switch is True

    def test_the_next_risk_day_starts_fresh(self, factory, today):
        t = _halted_daily(factory)
        _release(factory)
        t.record_pnl(0.0, PEAK - 35_000.0)

        today["day"] = DAY + timedelta(days=1)
        t.record_pnl(-31_000.0, PEAK - 66_000.0)       # -3.1% on the new day

        assert t.kill_switch is True
        assert t.kill_reason.startswith("일일 손실 한도 초과")

    def test_a_halt_of_its_own_sets_no_floor(self, factory, today):
        t = _halted_daily(factory)
        assert t._daily_floor is None and t._weekly_floor is None
        assert _audits(factory) == []


class TestTheWeeklyLimitAfterARelease:
    def _halted_weekly(self, factory):
        for back in (1, 2, 3):
            _seed(factory, DAY - timedelta(days=back), daily_pnl=-20_000.0,
                  peak_equity=PEAK)
        t = _tracker(factory)
        t.record_pnl(-10_000.0, PEAK - 70_000.0)       # week -7%, day -1%
        assert t.kill_reason.startswith("주간 손실 한도 초과")
        return t

    def test_it_halts_again_only_one_percent_further(self, factory, today):
        t = self._halted_weekly(factory)
        _release(factory)
        t.record_pnl(0.0, PEAK - 70_000.0)

        t.record_pnl(-5_000.0, PEAK - 75_000.0)        # week -7.5%
        assert t.kill_switch is False

        t.record_pnl(-6_000.0, PEAK - 81_000.0)        # week -8.1%
        assert t.kill_switch is True
        assert t.kill_reason.startswith("주간 손실 한도 초과")

    def test_a_loss_that_rolls_out_of_the_window_is_no_longer_accepted(
            self, factory, today):
        """The weekly floor follows the rolling window: once the losses the
        release accepted drop out, fresh loss counts again — not 1% past an
        absolute level that no longer means anything."""
        _seed(factory, DAY - timedelta(days=6), daily_pnl=-50_000.0, peak_equity=PEAK)
        t = _tracker(factory)
        t.record_pnl(-20_000.0, PEAK - 70_000.0)        # week -7%
        assert t.kill_reason.startswith("주간")
        _release(factory)
        t.record_pnl(0.0, PEAK - 70_000.0)

        today["day"] = DAY + timedelta(days=1)          # D-6's -5% rolls out
        t.record_pnl(-25_000.0, PEAK - 95_000.0)        # week -4.5%, day -2.5%
        assert t.kill_switch is False
        today["day"] = DAY + timedelta(days=2)
        t.record_pnl(-25_000.0, PEAK - 120_000.0)       # week -7%, 5% of it fresh

        # An absolute floor (-8%, set at the release) would not halt here.

        assert t.kill_switch is True
        assert t.kill_reason.startswith("주간")

    def test_the_weekly_floor_still_holds_the_next_day(self, factory, today):
        t = self._halted_weekly(factory)
        _release(factory)
        t.record_pnl(0.0, PEAK - 70_000.0)

        today["day"] = DAY + timedelta(days=1)
        t.record_pnl(-5_000.0, PEAK - 75_000.0)        # week -7.5%, day -0.5%

        assert t.kill_switch is False


class TestMDDAfterARelease:
    def test_a_release_in_a_breach_rebases_the_peak(self, factory, today):
        t = _tracker(factory)
        t.peak_equity = PEAK
        t.record_pnl(0.0, PEAK * 0.80)
        _release(factory)

        t.record_pnl(0.0, PEAK * 0.80)

        assert t.kill_switch is False
        assert t.peak_equity == PEAK * 0.80
        assert _row(factory).peak_equity == PEAK * 0.80, "the rebased peak must reach the row"

    def test_a_small_further_drop_does_not_halt_but_fifteen_percent_does(
            self, factory, today):
        t = _tracker(factory)
        t.peak_equity = PEAK
        t.record_pnl(0.0, PEAK * 0.80)
        _release(factory)
        t.record_pnl(0.0, PEAK * 0.80)

        t.record_pnl(0.0, PEAK * 0.80 * 0.90)
        assert t.kill_switch is False
        t.record_pnl(0.0, PEAK * 0.80 * 0.84)
        assert t.kill_reason.startswith("MDD")

    def test_a_release_with_no_mdd_breach_keeps_the_peak(self, factory, today):
        t = _halted_daily(factory)
        _release(factory)
        t.record_pnl(0.0, PEAK - 35_000.0)

        assert t.peak_equity == PEAK

    def test_unknown_equity_rebases_at_the_first_reading(self, factory, today):
        """A halt restored at boot, released before any equity reading: the
        first fill must not measure the accepted drawdown against the old peak
        — that would halt and liquidate over the release."""
        _seed(factory, DAY, kill_switch=True, kill_reason="MDD 한도 초과 (-20%)",
              peak_equity=PEAK)
        t = _tracker(factory)
        assert t.current_equity == 0
        _release(factory)
        assert t.refresh_from_db() is False

        t.record_pnl(0.0, PEAK * 0.80)

        assert t.kill_switch is False
        assert t.peak_equity == PEAK * 0.80


class TestTheBaselineSurvivesARestart:
    def test_a_restart_on_the_day_keeps_both_floors(self, factory, today):
        t = _halted_daily(factory)
        _release(factory)
        t.record_pnl(0.0, PEAK - 35_000.0)
        (detail,) = _audits(factory)
        assert detail["trade_date"] == DAY.isoformat()

        t2 = _tracker(factory)
        t2.record_pnl(-9_000.0, PEAK - 44_000.0)

        assert t2.kill_switch is False, "a restart halted again over the release"
        t2.record_pnl(-2_000.0, PEAK - 46_000.0)
        assert t2.kill_switch is True

    def test_a_restart_the_next_day_keeps_only_the_weekly_floor(self, factory, today):
        t = _halted_daily(factory)
        _release(factory)
        t.record_pnl(0.0, PEAK - 35_000.0)

        today["day"] = DAY + timedelta(days=1)
        t2 = _tracker(factory)

        assert t2._daily_floor is None
        assert t2._weekly_floor_level() == -45_000.0

    def test_a_release_made_while_the_worker_was_down_is_applied_at_boot(
            self, factory, today):
        """Halted at -3.5% (MDD too), the worker down, the operator released.
        No tracker saw the cleared flag; the next boot finds the release on the
        audit trail and accepts the loss — the first fill neither halts nor
        liquidates."""
        _seed(factory, DAY, daily_pnl=-35_000.0, peak_equity=PEAK,
              kill_switch=True, kill_reason="MDD 한도 초과 (-20%)")
        _release(factory)
        _audit_release(factory)
        flattens = []

        t = _tracker(factory)
        t.on_mdd_breach = flattens.append
        t.write_pending()
        t.record_pnl(0.0, PEAK * 0.80)

        assert t.kill_switch is False
        assert flattens == []
        assert t.peak_equity == PEAK * 0.80
        assert _audits(factory)[-1]["release_id"] > 0

    def test_an_applied_release_is_not_applied_again(self, factory, today):
        _seed(factory, DAY, daily_pnl=-35_000.0, peak_equity=PEAK)
        _audit_release(factory)
        t = _tracker(factory)
        t.write_pending()
        t.record_pnl(-20_000.0, PEAK - 55_000.0)   # -5.5%: 2% past the release
        assert t.kill_switch is True

        _release(factory)                          # cleared by hand, no new release
        t2 = _tracker(factory)
        assert t2._daily_floor == (DAY, -45_000.0), "the old release was re-applied"

    def test_a_release_older_than_the_window_is_ignored(self, factory, today):
        from datetime import datetime
        sess = factory()
        sess.add(AuditLog(event_type="kill_switch_reset", detail="{}",
                          created_at=datetime.utcnow() - timedelta(days=8)))
        sess.commit()
        sess.close()

        assert _tracker(factory)._daily_floor is None

    def test_a_malformed_record_restores_nothing(self, factory, today, caplog):
        sess = factory()
        sess.add(AuditLog(event_type=RELEASE_BASELINE_EVENT, detail="{not json"))
        sess.commit()
        sess.close()

        with caplog.at_level("ERROR", logger="backend.quant.risk.engine"):
            t = _tracker(factory)

        assert t._daily_floor is None
        assert "해제 기준 복원 실패" in caplog.text


class TestRefreshAndPendingWrites:
    def test_refresh_reports_a_halt_still_on_the_row(self, factory, today):
        t = _halted_daily(factory)
        assert t.refresh_from_db() is True

    def test_refresh_adopts_the_release(self, factory, today):
        t = _halted_daily(factory)
        _release(factory)
        assert t.refresh_from_db() is False
        assert t._daily_floor == (DAY, -45_000.0)

    def test_refresh_applies_a_release_its_memory_never_held(self, factory, today):
        """The watchdog halted the row and the operator cleared it before any
        write of this tracker adopted the halt: no cleared flag to see, but the
        release is on the audit trail, and it is applied."""
        _seed(factory, DAY, daily_pnl=-35_000.0, peak_equity=PEAK)
        t = _tracker(factory)
        _audit_release(factory)

        assert t.refresh_from_db() is False
        assert t._daily_floor == (DAY, -45_000.0)
        assert _audits(factory)[-1]["release_id"] > 0

    def test_a_carried_halt_is_written_before_it_can_overwrite_a_release(
            self, factory, today):
        """Halted three days ago, the worker down since: the boot carries the
        halt onto today's row at once (``write_pending``), so a release the
        operator makes after that is adopted, not overwritten by the carry."""
        _seed(factory, DAY - timedelta(days=3), kill_switch=True,
              kill_reason="일일 손실 한도 초과", peak_equity=PEAK)
        t = _tracker(factory)
        t.write_pending()
        assert _row(factory).kill_switch is True

        _release(factory)

        assert t.refresh_from_db() is False
        assert _row(factory).kill_switch is False

    def test_without_the_boot_write_the_carry_would_overwrite_it(self, factory, today):
        """The reason ``write_pending`` exists, pinned."""
        _seed(factory, DAY - timedelta(days=3), kill_switch=True,
              kill_reason="일일 손실 한도 초과", peak_equity=PEAK)
        t = _tracker(factory)
        _release(factory)

        assert t.refresh_from_db() is True

    def test_if_clear_does_not_run_while_halted(self, factory, today):
        t = _halted_daily(factory)
        ran = []
        assert t.if_clear(lambda: ran.append(1)) is False
        assert ran == []


# ── startup recovery: a restored halt is a risk halt ──────────────────────────

class TestRecoveryRecordsARiskHalt:
    def _recovery(self, factory):
        from backend.worker.recovery import StartupRecovery
        rec = StartupRecovery(db_session_factory=factory)
        for name in ("_step_db", "_step_redis", "_step_balance", "_step_positions",
                     "_step_reconcile", "_step_pending_orders",
                     "_step_validate_state"):
            setattr(rec, name, lambda: True)
        return rec

    def test_a_halted_row_closes_the_gate_as_a_risk_breach(self, factory, today,
                                                            monkeypatch):
        from backend.worker.recovery import SAFE_MODE
        monkeypatch.setattr("bot.notifier.alert_emergency", lambda m: None)
        _seed(factory, DAY, kill_switch=True, kill_reason="MDD 한도 초과 (-16%)")
        rec = self._recovery(factory)

        assert rec.run() is False
        assert rec.halted_by_risk is True
        assert SAFE_MODE.halt_cause is HaltCause.RISK_BREACH

    def test_an_unreadable_risk_state_stays_untrusted(self, factory, today,
                                                      monkeypatch):
        from backend.worker.recovery import SAFE_MODE
        monkeypatch.setattr("bot.notifier.alert_emergency", lambda m: None)

        def _boom(*a, **k):
            raise RuntimeError("db hiccup")
        monkeypatch.setattr(PersistentLossTracker, "__init__", _boom)
        rec = self._recovery(factory)

        assert rec.run() is False
        assert rec.halted_by_risk is False
        assert SAFE_MODE.halt_cause is HaltCause.UNTRUSTED_STATE

    def test_another_failure_stays_untrusted(self, factory, today):
        from backend.worker.recovery import SAFE_MODE
        rec = self._recovery(factory)
        rec._step_balance = lambda: False

        assert rec.run() is False
        assert rec.halted_by_risk is False
        assert SAFE_MODE.halt_cause is HaltCause.UNTRUSTED_STATE


# ── the worker's resume poll ──────────────────────────────────────────────────

@pytest.fixture()
def worker(factory, monkeypatch):
    import backend.worker.runner as runner
    from backend.worker.runner import StrategyWorker
    monkeypatch.setattr(runner, "_get_session_factory", lambda: factory)
    monkeypatch.setattr(runner, "_run_uptime_factory", None)
    sent = []
    monkeypatch.setattr("bot.notifier.alert_emergency", sent.append)
    w = StrategyWorker.__new__(StrategyWorker)
    w._shutdown = threading.Event()
    w._risk_resume_allowed = True
    w._last_equity_reading = (PEAK - 35_000.0, True)
    w._loss_tracker = None
    w.sent = sent
    return w


class TestTheResumePoll:
    def test_a_released_risk_halt_resumes(self, factory, today, worker):
        import backend.worker.runner as runner
        from backend.worker.recovery import SAFE_MODE
        worker._loss_tracker = _halted_daily(factory)
        assert SAFE_MODE.halt_cause is HaltCause.RISK_BREACH
        _release(factory)

        assert worker._resume_if_released() is True

        assert SAFE_MODE.can_trade is True
        assert worker._loss_tracker._daily_floor == (DAY, -45_000.0)
        assert runner._run_uptime_factory is factory, "run uptime stays off after a resume"
        assert any("매매 재개" in m for m in worker.sent)

    def test_nothing_happens_while_a_row_is_halted(self, factory, today, worker):
        from backend.worker.recovery import SAFE_MODE
        worker._loss_tracker = _halted_daily(factory)

        assert worker._resume_if_released() is False
        assert SAFE_MODE.can_trade is False

    def test_an_old_halted_row_blocks_it(self, factory, today, worker):
        from backend.worker.recovery import SAFE_MODE
        worker._loss_tracker = _halted_daily(factory)
        _release(factory)
        _seed(factory, DAY - timedelta(days=4), kill_switch=True, kill_reason="옛 정지")

        assert worker._resume_if_released() is False
        assert SAFE_MODE.can_trade is False

    def test_an_untrusted_halt_is_not_resumed(self, factory, today, worker):
        from backend.worker.recovery import SAFE_MODE
        worker._loss_tracker = _tracker(factory)
        SAFE_MODE.disable("복구 실패: 브로커 잔고 조회")

        assert worker._resume_if_released() is False
        assert SAFE_MODE.can_trade is False

    def test_a_cause_that_changes_during_the_poll_is_not_overridden(
            self, factory, today, worker):
        """The gate shut for another reason while the poll was reading."""
        from backend.worker.recovery import SAFE_MODE
        t = _halted_daily(factory)
        worker._loss_tracker = t
        _release(factory)
        real = t.refresh_from_db

        def _refresh(eq):
            SAFE_MODE.disable("일관성 검증 실패")      # untrusted, meanwhile
            return real(eq)
        t.refresh_from_db = _refresh

        assert worker._resume_if_released() is False
        assert SAFE_MODE.halt_cause is HaltCause.UNTRUSTED_STATE

    def test_not_after_a_failed_recovery(self, factory, today, worker):
        """Recovery failed (untrusted), then a risk halt replaced the cause."""
        from backend.worker.recovery import SAFE_MODE
        worker._risk_resume_allowed = False
        worker._loss_tracker = _halted_daily(factory)
        _release(factory)

        assert worker._resume_if_released() is False
        assert SAFE_MODE.can_trade is False

    def test_not_while_the_tracker_still_holds_an_unwritten_halt(
            self, factory, today, worker):
        """Its own halt never reached the row; the settle re-asserts it."""
        from backend.worker.recovery import SAFE_MODE
        t = _halted_daily(factory)
        t._ks_written -= 1                 # the halt's write "failed"
        worker._loss_tracker = t
        _release(factory)

        assert worker._resume_if_released() is False
        assert SAFE_MODE.can_trade is False
        assert _row(factory).kill_switch is True

    def test_a_database_failure_keeps_the_gate_shut(self, factory, today, worker,
                                                    monkeypatch):
        import backend.worker.runner as runner
        from backend.worker.recovery import SAFE_MODE
        worker._loss_tracker = _halted_daily(factory)
        _release(factory)

        def _down():
            raise RuntimeError("db down")
        monkeypatch.setattr(runner, "_get_session_factory", lambda: _down)

        assert worker._resume_if_released() is False
        assert SAFE_MODE.can_trade is False

    def test_not_during_shutdown(self, factory, today, worker):
        from backend.worker.recovery import SAFE_MODE
        worker._loss_tracker = _halted_daily(factory)
        _release(factory)
        worker._shutdown.set()

        assert worker._resume_if_released() is False
        assert SAFE_MODE.can_trade is False

    def test_a_session_already_running_starts_recording_on_resume(
            self, monkeypatch):
        """A run restored while the worker came up halted has no uptime
        recorder; turning recording on must reach it, not only new sessions."""
        import backend.worker.runner as runner
        started = []
        monkeypatch.setattr(runner, "_run_uptime_factory", None)
        monkeypatch.setattr(runner, "_start_run_uptime",
                            lambda run_id: started.append(run_id) or None
                            if runner._run_uptime_factory is None else
                            started.append(run_id) or _Rec())

        class _Strategy:
            def start(self):
                pass

            def stop(self):
                pass
        s = runner.WorkerSession.__new__(runner.WorkerSession)
        s.run_id = 7
        s.strategy = _Strategy()
        s._stop_event = threading.Event()
        s._callbacks_cv = threading.Condition()
        s._callbacks = 0
        s._deactivate_on_exit = False
        th = threading.Thread(target=s._run, daemon=True)
        th.start()
        import time
        time.sleep(0.3)
        runner._run_uptime_factory = object()
        time.sleep(1.5)
        s._stop_event.set()
        th.join(5)

        assert started == [7, 7], "the running session never started recording"

    def test_it_is_scheduled_every_minute(self, worker):
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler()
        worker.attach_scheduler(scheduler)

        job = scheduler.get_job("risk_resume")
        assert job.trigger.interval == timedelta(seconds=60)
        assert job.coalesce is True and job.max_instances == 1


class TestEndToEnd:
    def test_breach_release_resume_then_worse_halts_again(self, factory, today, worker):
        from backend.worker.recovery import SAFE_MODE
        t = _tracker(factory)
        t.peak_equity = PEAK
        worker._loss_tracker = t

        t.record_pnl(-35_000.0, PEAK - 35_000.0)              # -3.5% → halt
        assert SAFE_MODE.can_trade is False
        assert worker._resume_if_released() is False          # not released yet

        _release(factory)                                     # the app's reset
        assert worker._resume_if_released() is True
        assert SAFE_MODE.can_trade is True

        t.record_pnl(-5_000.0, PEAK - 40_000.0)               # -4.0%
        assert SAFE_MODE.can_trade is True

        t.record_pnl(-6_000.0, PEAK - 46_000.0)               # -4.6%
        assert SAFE_MODE.can_trade is False
        assert _row(factory).kill_switch is True


class _Rec:
    def stop(self):
        pass
