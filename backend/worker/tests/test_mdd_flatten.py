"""P0-03 — an MDD breach liquidates the book.

The rule is "MDD 15% → 전량 청산". Until this change the MDD kill switch closed
``SAFE_MODE`` and sent alerts, and nothing called ``EmergencyFlattenManager`` —
the only automated capital-protection step existed as a manual endpoint.

Three things had to be true for wiring it to mean anything:

* **MDD is evaluated at all on a crash day.** ``_evaluate`` checked the daily
  limit first and returned; when 3% and 15% broke together, MDD never ran.
* **One request per breach.** Every PnL write during a breach re-evaluates, so
  an unguarded hook would liquidate on every fill.
* **Only a measured breach.** A halt restored at boot or adopted from another
  process must not liquidate by itself (ROADMAP R-CRIT-07).

No broker, no network: the end-to-end case runs on ``ScriptedPaperBroker``.
"""
import threading

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, DailyRiskState, trading_day
from backend.database.testing import make_test_engine
from backend.quant.risk.engine import LossTracker, PersistentLossTracker, RiskConfig

PEAK = 1_000_000.0


class _Quiet(LossTracker):
    """The base tracker minus the alert I/O (Telegram/WebSocket/SAFE_MODE)."""

    def _fire_kill_switch_alert(self, reason):
        pass


def _hooked(cls=_Quiet, **kw):
    calls = []
    t = cls(config=RiskConfig(), **kw)
    t.on_mdd_breach = calls.append
    t.peak_equity = PEAK
    return t, calls


@pytest.fixture(autouse=True)
def _isolate_safe_mode():
    from backend.worker.recovery import SAFE_MODE
    saved = (SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause)
    yield
    SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause = saved


# ── the tracker ───────────────────────────────────────────────────────────────

class TestTheTrackerRequestsAFlatten:
    def test_an_mdd_breach_requests_one_flatten(self):
        t, calls = _hooked()
        t.record_pnl(0.0, PEAK * 0.80)          # -20% from peak, no realized loss
        assert t.kill_switch is True
        assert len(calls) == 1 and calls[0].startswith("MDD 한도 초과")

    def test_further_fills_during_the_breach_do_not_request_again(self):
        t, calls = _hooked()
        for _ in range(5):
            t.record_pnl(0.0, PEAK * 0.80)
        assert len(calls) == 1

    def test_a_crash_day_that_breaks_daily_and_mdd_together_is_an_mdd_halt(self):
        """The ordering bug: the daily branch returned first and MDD never ran."""
        t, calls = _hooked()
        t.record_pnl(-200_000.0, PEAK * 0.80)   # -20% daily AND -20% MDD
        assert t.kill_reason.startswith("MDD 한도 초과")
        assert len(calls) == 1

    def test_mdd_breaking_after_a_daily_halt_still_flattens(self):
        """Keyed on its own flag, not on ``kill_switch`` — already halted is
        not the same as already liquidated."""
        t, calls = _hooked()
        t.record_pnl(-40_000.0, PEAK * 0.96)    # daily -4%, MDD -4%
        assert t.kill_switch is True and calls == []
        t.record_pnl(0.0, PEAK * 0.80)
        assert len(calls) == 1

    def test_a_daily_halt_alone_does_not_flatten(self):
        t, calls = _hooked()
        t.record_pnl(-40_000.0, PEAK * 0.96)    # daily -4%, MDD -4%
        assert t.kill_switch is True and calls == []

    def test_a_weekly_halt_alone_does_not_flatten(self):
        t, calls = _hooked()
        t.weekly_pnl = -70_000.0                # -7% on the week, under daily 3%
        t.record_pnl(-10_000.0, PEAK * 0.93)
        assert t.kill_reason.startswith("주간") and calls == []

    def test_a_manual_reset_rearms_it(self):
        """The reset rebases the drawdown on current equity (P0-12): the breach
        it accepted does not flatten again, a fresh 15% from there does."""
        t, calls = _hooked()
        t.record_pnl(0.0, PEAK * 0.80)
        t.manual_reset()
        t.record_pnl(0.0, PEAK * 0.80)
        assert len(calls) == 1
        t.record_pnl(0.0, PEAK * 0.80 * 0.84)
        assert len(calls) == 2

    def test_a_failing_hook_does_not_undo_the_halt(self):
        t = _Quiet(config=RiskConfig())
        t.peak_equity = PEAK

        def boom(_reason):
            raise RuntimeError("flatten unavailable")

        t.on_mdd_breach = boom
        assert t.record_pnl(0.0, PEAK * 0.80) == "triggered"
        assert t.kill_switch is True

    def test_without_a_hook_nothing_is_requested_and_nothing_breaks(self):
        t = _Quiet(config=RiskConfig())
        t.peak_equity = PEAK
        assert t.record_pnl(0.0, PEAK * 0.80) == "triggered"


# ── PersistentLossTracker: restore and adoption ───────────────────────────────

@pytest.fixture()
def factory():
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _no_alert_thread(monkeypatch):
    monkeypatch.setattr(PersistentLossTracker, "_do_kill_switch_io",
                        lambda self, reason: None)


def _set_row(factory, **fields):
    with factory() as s:
        row = s.get(DailyRiskState, trading_day())
        if row is None:
            row = DailyRiskState(trade_date=trading_day(), peak_equity=PEAK)
            s.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        s.commit()


def _persistent(factory):
    calls = []
    t = PersistentLossTracker(config=RiskConfig(), db_factory=factory)
    t.on_mdd_breach = calls.append
    return t, calls


class TestOnlyAMeasuredBreachFlattens:
    def test_an_mdd_halt_restored_at_boot_does_not_flatten_again(self, factory):
        """The breach still holds after the restart and the first fill measures
        it — perhaps one of the pre-restart flatten's own sells. Requesting
        again would be the startup liquidation R-CRIT-07 forbids."""
        _set_row(factory, kill_switch=True, kill_reason="MDD 한도 초과 (-16.00%)")
        t, calls = _persistent(factory)
        assert t.kill_switch is True
        t.record_pnl(0.0, PEAK * 0.80)          # still -20% from the restored peak
        assert calls == []

    def test_a_restored_daily_halt_still_flattens_when_mdd_breaks(self, factory):
        _set_row(factory, kill_switch=True, kill_reason="일일 손실 한도 초과 (-4.00%)")
        t, calls = _persistent(factory)
        t.record_pnl(0.0, PEAK * 0.80)
        assert len(calls) == 1

    def test_the_restore_check_matches_what_evaluate_writes(self):
        from backend.quant.risk.engine import MDD_REASON_PREFIX
        t, _ = _hooked()
        t.record_pnl(0.0, PEAK * 0.80)
        assert t.kill_reason.startswith(MDD_REASON_PREFIX)

    def test_an_adopted_external_halt_does_not_flatten(self, factory):
        _set_row(factory)
        t, calls = _persistent(factory)
        t.peak_equity = PEAK
        _set_row(factory, kill_switch=True, kill_reason="Worker 하트비트 없음")
        assert t.record_pnl(0.0, PEAK) == "adopted"
        assert calls == []

    def test_a_clear_during_a_live_breach_holds_without_a_second_flatten(self, factory):
        """The operator's release accepts the drawdown as it stands (P0-12):
        the peak is rebased on current equity, so the book — already
        liquidated — is neither halted nor liquidated again by the same
        breach. A fresh 15% from the new peak is a new breach."""
        _set_row(factory)
        t, calls = _persistent(factory)
        t.peak_equity = PEAK
        t.record_pnl(0.0, PEAK * 0.80)
        _set_row(factory, kill_switch=False, kill_reason=None)

        t.record_pnl(0.0, PEAK * 0.80)
        assert t.kill_switch is False
        assert t.peak_equity == PEAK * 0.80
        assert len(calls) == 1

        t.record_pnl(0.0, PEAK * 0.80 * 0.84)
        assert t.kill_switch is True
        assert len(calls) == 2

    def test_a_clear_adopted_after_recovery_rearms_it(self, factory):
        """Once the breach no longer holds, the tracker adopts the operator's
        clear — and a later breach is a new one, which liquidates again."""
        _set_row(factory)
        t, calls = _persistent(factory)
        t.peak_equity = PEAK
        t.record_pnl(0.0, PEAK * 0.80)
        assert len(calls) == 1

        _set_row(factory, kill_switch=False, kill_reason=None)
        t.record_pnl(0.0, PEAK)                 # recovered: adopts the clear
        assert t.kill_switch is False
        t.record_pnl(0.0, PEAK * 0.80)
        assert len(calls) == 2


# ── the worker ────────────────────────────────────────────────────────────────

def _bare_worker(tracker=None):
    from backend.worker import runner
    w = runner.StrategyWorker.__new__(runner.StrategyWorker)
    w._lock = threading.Lock()
    w._aux_threads = []
    w._loss_tracker = tracker
    return w


@pytest.fixture()
def paper(monkeypatch):
    """A paper book holding two positions, wired in as the worker's broker."""
    from backend.brokers.paper_broker import ScriptedPaperBroker
    from backend.worker import runner
    b = ScriptedPaperBroker(default_price=100.0)
    b.set_prices({"AAPL": 150.0, "069500": 30_000.0})
    b.set_position("AAPL", 10, 170.0)
    b.set_position("069500", 3, 35_000.0)
    monkeypatch.setattr(runner, "get_kis_broker", lambda: b)
    monkeypatch.setattr(runner, "_get_session_factory", lambda: None)
    monkeypatch.setattr("bot.notifier.alert_emergency", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr("backend.websocket.server.publish_alert",
                        lambda *_a, **_k: None, raising=False)
    return b


def _armed_live(monkeypatch):
    """Live trading with the automatic flatten armed and no rollback lever."""
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
    monkeypatch.setenv("MDD_AUTO_FLATTEN", "true")
    monkeypatch.delenv("EMERGENCY_FLATTEN_DRY_RUN", raising=False)


def _orders(broker):
    return [so.order for so in broker._orders.values()]


def _breach_through_the_worker(worker=None):
    t = _Quiet(config=RiskConfig())
    t._lock = threading.RLock()   # the worker re-arms under it, as on the real tracker
    worker = worker or _bare_worker()
    worker._loss_tracker = t
    t.peak_equity = PEAK
    t.on_mdd_breach = worker._on_mdd_breach
    t.record_pnl(0.0, PEAK * 0.80)
    _join(worker)
    return t


def _join(worker):
    for th in list(worker._aux_threads):
        th.join(10)


class TestTheWorkerLiquidates:
    def test_the_flatten_runs_on_a_thread_shutdown_waits_for(self, paper, monkeypatch):
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
        w = _bare_worker()
        started = threading.Event()
        release = threading.Event()

        def slow(*_args):
            started.set()
            release.wait(5)

        monkeypatch.setattr(w, "_emergency_flatten", slow)
        w._on_mdd_breach("MDD 한도 초과")      # returns without waiting
        assert started.wait(5)
        assert [t.name for t in w._aux_threads] == ["emergency-flatten"]
        release.set()

    def test_a_live_mdd_breach_sells_every_holding_and_nothing_else(self, paper, monkeypatch):
        """ROADMAP P0-03 / R-CRIT-07: kill switch in paper mode → sell orders for
        the held symbols only; no buys, no unrelated symbols."""
        _armed_live(monkeypatch)
        _breach_through_the_worker(_bare_worker())

        orders = _orders(paper)
        assert sorted((o.symbol, o.side, o.qty) for o in orders) == [
            ("069500", "sell", 3), ("AAPL", "sell", 10)]

    def test_shadow_mode_does_not_send_orders(self, paper, monkeypatch):
        _armed_live(monkeypatch)
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
        _breach_through_the_worker(_bare_worker())
        assert _orders(paper) == []

    def test_the_rollback_lever_forces_a_dry_run_even_when_live(self, paper, monkeypatch):
        _armed_live(monkeypatch)
        monkeypatch.setenv("EMERGENCY_FLATTEN_DRY_RUN", "true")
        _breach_through_the_worker(_bare_worker())
        assert _orders(paper) == []

    def test_live_trading_alone_does_not_arm_the_automatic_flatten(self, paper, monkeypatch):
        """Until the equity reading behind MDD is verified, a live deployment
        only logs the automatic flatten; ``MDD_AUTO_FLATTEN=true`` arms it."""
        monkeypatch.setenv("ENABLE_LIVE_TRADING", "true")
        monkeypatch.delenv("MDD_AUTO_FLATTEN", raising=False)
        monkeypatch.delenv("EMERGENCY_FLATTEN_DRY_RUN", raising=False)
        _breach_through_the_worker()
        assert _orders(paper) == []

    def test_a_dry_run_failure_raises_no_retry_and_no_alarm(self, paper, monkeypatch):
        monkeypatch.delenv("MDD_AUTO_FLATTEN", raising=False)
        alerts = []
        monkeypatch.setattr("bot.notifier.alert_emergency", alerts.append, raising=False)
        paper.get_positions = lambda: (_ for _ in ()).throw(RuntimeError("broker down"))
        t = _breach_through_the_worker()
        assert t._mdd_flatten_requested is True
        assert not any("비상청산 실패" in a for a in alerts)

    def test_a_flatten_that_sent_nothing_is_retried_on_the_next_fill(self, paper, monkeypatch):
        """Broker down when the breach landed: without a retry the book stays
        invested through the whole drawdown."""
        _armed_live(monkeypatch)
        alerts = []
        monkeypatch.setattr("bot.notifier.alert_emergency", alerts.append, raising=False)
        w = _bare_worker()
        real = paper.get_positions
        paper.get_positions = lambda: (_ for _ in ()).throw(RuntimeError("broker down"))
        t = _breach_through_the_worker(w)
        assert _orders(paper) == []
        assert t._mdd_flatten_requested is False        # re-armed
        # The retry rides on a fill that may never come — the operator is told.
        assert any("비상청산 실패" in a for a in alerts)

        paper.get_positions = real
        t.record_pnl(0.0, PEAK * 0.80)                  # next fill, still breached
        _join(w)
        assert sorted(o.symbol for o in _orders(paper)) == ["069500", "AAPL"]

    def test_a_partial_flatten_is_not_retried(self, paper, monkeypatch):
        """Once a sell is resting, a second run would ask for the same shares."""
        _armed_live(monkeypatch)
        real_price = paper.get_price
        paper.get_price = lambda sym: None if sym == "AAPL" else real_price(sym)
        t = _breach_through_the_worker()
        assert [o.symbol for o in _orders(paper)] == ["069500"]
        assert t._mdd_flatten_requested is True

    def test_an_unverified_equity_reading_holds_the_flatten(self, paper, monkeypatch):
        """#178: the MDD came from a number the adapter flags as incomplete.
        The halt stands; selling the book on it does not happen."""
        from backend.brokers.models import Balance
        _armed_live(monkeypatch)
        alerts = []
        monkeypatch.setattr("bot.notifier.alert_emergency", alerts.append, raising=False)
        paper.get_balance = lambda: Balance(0.0, 250.0, 800_000.0, equity_verified=False)

        t = _breach_through_the_worker()

        assert t.kill_switch is True
        assert _orders(paper) == []
        assert any("비상청산 보류" in a for a in alerts)

    def test_a_hold_rearms_so_a_later_verified_reading_flattens(self, paper, monkeypatch):
        from backend.brokers.models import Balance
        _armed_live(monkeypatch)
        w = _bare_worker()
        paper.get_balance = lambda: Balance(0.0, 250.0, 800_000.0, equity_verified=False)
        t = _breach_through_the_worker(w)
        assert _orders(paper) == [] and t._mdd_flatten_requested is False

        paper.get_balance = lambda: Balance(0.0, 0.0, 800_000.0)
        t.record_pnl(0.0, PEAK * 0.80)               # next fill, reading now complete
        _join(w)
        assert sorted(o.symbol for o in _orders(paper)) == ["069500", "AAPL"]

    def test_the_reading_that_caused_the_breach_is_checked_not_only_a_fresh_one(
            self, paper, monkeypatch):
        """Review finding: an unverified breach reading, then a failed fresh
        call, must not fall through to a live liquidation."""
        from backend.worker import runner
        _armed_live(monkeypatch)
        paper.get_balance = lambda: (_ for _ in ()).throw(RuntimeError("balance down"))
        w = _bare_worker()
        t = _Quiet(config=RiskConfig())
        t._lock = threading.RLock()
        t.peak_equity = PEAK
        t.on_mdd_breach = w._on_mdd_breach
        w._loss_tracker = t

        runner._EQUITY_READING.verified = False      # what the fill path sets
        try:
            t.record_pnl(0.0, PEAK * 0.80)
        finally:
            runner._EQUITY_READING.verified = True
        _join(w)

        assert _orders(paper) == []

    def test_the_fill_path_hands_the_readings_flag_to_the_tracker(self):
        import inspect
        from backend.worker import runner
        src = inspect.getsource(runner.StrategyWorker)
        assert "_EQUITY_READING.verified = equity_verified" in src

    def test_a_failed_balance_check_holds_it_and_rearms(self, paper, monkeypatch):
        """PR #181 review: selling needs **both** readings verified, and a fresh
        call that failed verified nothing. Held, re-armed, and the next fill
        with a good reading liquidates."""
        from backend.brokers.models import Balance
        _armed_live(monkeypatch)
        w = _bare_worker()
        paper.get_balance = lambda: (_ for _ in ()).throw(RuntimeError("balance down"))
        t = _breach_through_the_worker(w)
        assert _orders(paper) == [] and t._mdd_flatten_requested is False

        paper.get_balance = lambda: Balance(0.0, 0.0, 800_000.0)
        t.record_pnl(0.0, PEAK * 0.80)
        _join(w)
        assert sorted(o.symbol for o in _orders(paper)) == ["069500", "AAPL"]

    def test_the_cached_reading_keeps_its_amount_and_flag_together(self):
        """PR #181 review: one value, so a failed lookup can never pair a new
        amount with an older reading's flag."""
        import inspect
        from backend.worker import runner
        src = inspect.getsource(runner.StrategyWorker)
        assert "current_equity, equity_verified = self._last_equity_reading" in src
        assert "_last_known_equity_verified" not in src
        w = _bare_worker()
        w._last_equity_reading = (123.0, False)
        assert w._last_known_equity == 123.0

    def test_the_real_worker_wires_the_tracker_to_it(self):
        """``__init__`` opens Redis and a broker, so it is checked at the source:
        the tracker it builds must be handed the flatten hook."""
        import inspect
        from backend.worker import runner
        src = inspect.getsource(runner.StrategyWorker.__init__)
        assert "self._loss_tracker.on_mdd_breach = self._on_mdd_breach" in src


# ── the manager ───────────────────────────────────────────────────────────────

def test_the_manager_has_no_silent_dry_run_default():
    """A caller that forgets ``dry_run`` used to get a dry run without noticing."""
    from backend.brokers.paper_broker import ScriptedPaperBroker
    from backend.worker.emergency import EmergencyFlattenManager
    with pytest.raises(TypeError):
        EmergencyFlattenManager(ScriptedPaperBroker())


@pytest.mark.parametrize("live, override, expected", [
    ("true", None, False),
    ("false", None, True),
    (None, None, True),
    ("true", "true", True),
    ("true", "false", False),
])
def test_flatten_dry_run_rule(monkeypatch, live, override, expected):
    from backend.worker.emergency import flatten_dry_run
    for name, value in (("ENABLE_LIVE_TRADING", live), ("EMERGENCY_FLATTEN_DRY_RUN", override)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert flatten_dry_run() is expected


@pytest.mark.parametrize("armed, live, expected", [
    (None, "true", True),
    ("false", "true", True),
    ("true", "true", False),
    ("true", "false", True),
])
def test_auto_flatten_dry_run_rule(monkeypatch, armed, live, expected):
    from backend.worker.emergency import auto_flatten_dry_run
    monkeypatch.delenv("EMERGENCY_FLATTEN_DRY_RUN", raising=False)
    monkeypatch.setenv("ENABLE_LIVE_TRADING", live)
    if armed is None:
        monkeypatch.delenv("MDD_AUTO_FLATTEN", raising=False)
    else:
        monkeypatch.setenv("MDD_AUTO_FLATTEN", armed)
    assert auto_flatten_dry_run() is expected
