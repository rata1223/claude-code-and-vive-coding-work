"""Issue #160 — one definition of "today" for the daily risk row.

`DailyRiskState.trade_date` is a primary key, and until now the writers did not
agree on how to build it. The readers used the Seoul date (`_seoul_today()` in
`backend/quant/risk/engine.py`), the writers used `date.today()`. There is no
`TZ` in `docker-compose.yml`, so the containers run on UTC, and the two values
differ for nine hours a day — **UTC 15:00–24:00, which is KST 00:00–09:00**.

That window is not a curiosity. The rest of the platform is already on KST: the
scheduler is `BackgroundScheduler(timezone="Asia/Seoul")`, the daily reset fires
06:01 KST, and `LossTracker` rolls its in-memory day over on the Seoul date. Only
the row key and the scheduler's Redis delete were on UTC. Three consequences,
two of which are not in the issue as filed:

1. **Boot restore cannot see a live halt** (the filed one). Written under one
   key, read back under another; a worker restarting inside the window comes up
   with the halt cleared, and `StartupRecovery._step_risk` branches on
   `tracker.kill_switch`. Fail-open.
2. **The 06:01 KST reset touched the wrong day.** 06:01 KST is 21:01 UTC *the
   previous day*, so it zeroed **yesterday's** `daily_pnl` and deleted a Redis
   key the engine never reads. Aligning the keys turned that dead write into a
   live one — and 06:01 is six hours *into* the Seoul day, which already holds
   the overnight US session. So the reset does not reset those counters at all
   any more: `LossTracker` already rolls the day over at Seoul midnight and owns
   them. See `TestTheDailyResetLeavesTheLiveCounterAlone`.
3. **The "did the kill switch fire yesterday?" check looked two days back.**
   `date.today() - 1` at 06:01 KST is the day before KST-yesterday, so a real
   halt went unseen and `SAFE_MODE.enable()` resumed trading — the exact
   opposite of what the code says it is doing. A second, independent fail-open.

These tests reproduce the window by patching `backend.database.models.trading_day`
so the KST date is one day ahead of `date.today()`, which is precisely the state
of the world between KST 00:00 and 09:00. Patching the single helper covers every
call site, because they all import it inside the function that uses it.
"""

from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, DailyRiskState
from backend.database.testing import make_test_engine
from backend.quant.risk.engine import PersistentLossTracker, RiskConfig


UTC_DAY = date.today()
#: What KST reads during the 00:00–09:00 window: one day ahead of the UTC date.
KST_DAY = UTC_DAY + timedelta(days=1)


@pytest.fixture(autouse=True)
def _no_alert_thread(monkeypatch):
    """`_fire_kill_switch_alert` dispatches Telegram/WebSocket/audit I/O on a
    daemon thread that races these writes on SQLite's single StaticPool
    connection. Not under test here (see test_kill_switch_convergence.py)."""
    monkeypatch.setattr(PersistentLossTracker, "_do_kill_switch_io",
                        lambda self, reason: None)


@pytest.fixture(autouse=True)
def _isolate_safe_mode():
    """SAFE_MODE is a module-level singleton these tests read and write."""
    from backend.worker.recovery import SAFE_MODE
    saved = (SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause)
    yield
    SAFE_MODE._can_trade, SAFE_MODE._reason, SAFE_MODE._cause = saved


@pytest.fixture()
def factory():
    engine = make_test_engine(poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def in_window(monkeypatch):
    """Put the clock inside KST 00:00–09:00: KST is a day ahead of `date.today()`."""
    monkeypatch.setattr("backend.database.models.trading_day", lambda: KST_DAY)
    return KST_DAY


@pytest.fixture()
def outside_window(monkeypatch):
    """The other fifteen hours, when the two dates agree."""
    monkeypatch.setattr("backend.database.models.trading_day", lambda: UTC_DAY)
    return UTC_DAY


def _tracker(factory):
    return PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                 db_factory=factory)


def _breach(tracker):
    """Drive a real MDD breach through the public API."""
    tracker.peak_equity = 1_000_000.0
    tracker.record_pnl(-500_000.0, 500_000.0)


def _row(factory, key):
    sess = factory()
    try:
        return sess.get(DailyRiskState, key)
    finally:
        sess.close()


def _seed(factory, key, **kw):
    sess = factory()
    try:
        row = sess.get(DailyRiskState, key) or DailyRiskState(trade_date=key)
        for k, v in kw.items():
            setattr(row, k, v)
        sess.add(row)
        sess.commit()
    finally:
        sess.close()


class TestTheHelperItself:
    """Every other test here patches `trading_day`, so its body needs its own.

    Without these, reverting the helper to UTC passes the whole file — the
    mutation that survived the first run.
    """

    @staticmethod
    def _at(monkeypatch, utc_iso: str):
        """Freeze the clock at an aware UTC instant, seen through `models`."""
        from datetime import datetime as _dt, timezone as _tz
        import backend.database.models as models

        instant = _dt.fromisoformat(utc_iso).replace(tzinfo=_tz.utc)

        class _Frozen:
            @staticmethod
            def now(tz=None):
                return instant.astimezone(tz) if tz else instant

        monkeypatch.setattr(models, "datetime", _Frozen)
        return models.trading_day()

    def test_inside_the_window_it_is_a_day_ahead_of_utc(self, monkeypatch):
        """16:30 UTC is 01:30 the next day in Seoul — the whole point of #160."""
        assert self._at(monkeypatch, "2026-09-21T16:30:00") == date(2026, 9, 22)

    def test_outside_the_window_it_matches_the_utc_date(self, monkeypatch):
        """06:30 UTC is 15:30 the same day in Seoul."""
        assert self._at(monkeypatch, "2026-09-21T06:30:00") == date(2026, 9, 21)

    def test_it_is_right_at_both_edges_of_the_window(self, monkeypatch):
        """15:00 UTC is exactly KST midnight; 14:59:59 is still the day before."""
        assert self._at(monkeypatch, "2026-09-21T15:00:00") == date(2026, 9, 22)
        assert self._at(monkeypatch, "2026-09-21T14:59:59") == date(2026, 9, 21)


class TestHaltSurvivesRestartInsideTheWindow:
    """Consequence (1): the filed fail-open."""

    def test_halt_written_in_the_window_is_restored_by_a_new_tracker(
            self, in_window, factory):
        """A worker restarting between KST 00:00 and 09:00 must still be halted.

        This is the whole of issue #160 as filed. With a split key the halt was
        written under the UTC date and looked up under the KST date, so the
        replacement tracker came up clean and `StartupRecovery._step_risk` let
        trading resume.
        """
        t = _tracker(factory)
        _breach(t)
        assert t.kill_switch is True

        restarted = _tracker(factory)          # same DB, fresh process
        assert restarted.kill_switch is True, (
            "the halt did not survive the restart — the write and the read "
            "disagreed about which row is today"
        )

    def test_the_row_is_keyed_by_the_kst_date_not_the_utc_date(
            self, in_window, factory):
        """The key itself, asserted directly, so a future reader sees the contract."""
        _breach(_tracker(factory))
        assert _row(factory, KST_DAY) is not None
        assert _row(factory, UTC_DAY) is None

    def test_outside_the_window_behaviour_is_unchanged(
            self, outside_window, factory):
        """Regression guard: for fifteen hours a day the two dates agree, and
        this change must be invisible there."""
        t = _tracker(factory)
        _breach(t)
        assert _tracker(factory).kill_switch is True
        assert _row(factory, UTC_DAY) is not None


class TestYesterdaysHaltBlocksResume:
    """Consequence (3): the second fail-open, and the most dangerous one."""

    @pytest.fixture(autouse=True)
    def _wire_scheduler(self, monkeypatch, factory):
        monkeypatch.setattr("backend.worker.scheduler._get_db", lambda: factory())
        monkeypatch.setattr("redis.from_url", lambda *a, **k: _FakeRedis())

    def test_a_halt_on_kst_yesterday_stops_safe_mode_re_arming(
            self, in_window, factory):
        """`_reset_daily_risk` must not resume trading over yesterday's halt.

        Off by one day, the lookup landed on the day *before* KST-yesterday —
        normally an empty row — so the guard read "no halt" and called
        `SAFE_MODE.enable()`. The code's own log line says the opposite is
        intended: "어제 킬스위치 활성 — SAFE_MODE 재활성화 차단. 수동 해제 필요."
        """
        from backend.worker.recovery import SAFE_MODE
        _seed(factory, KST_DAY - timedelta(days=1),
              kill_switch=True, kill_reason="일일 손실 한도")
        SAFE_MODE.disable("야간 정지")
        assert SAFE_MODE.can_trade is False

        from backend.worker.scheduler import _reset_daily_risk
        _reset_daily_risk()

        assert SAFE_MODE.can_trade is False, (
            "trading resumed over a live halt from the previous trading day"
        )

    def test_a_halt_fired_after_kst_midnight_also_stops_the_re_arm(
            self, in_window, factory):
        """The US session straddles KST midnight, so "yesterday" is not enough.

        Trading runs 22:30–05:00 KST. A halt that fires after 00:00 lands on
        *today's* row, and the 06:01 re-arm an hour later looked only at
        yesterday's — so the halt that stopped the overnight session did not
        stop the Korean one that followed.

        This was equally broken before #160 (the halt went to one UTC row, the
        guard read another), so the date alignment alone does not close it; the
        guard has to read both days.
        """
        from backend.worker.recovery import SAFE_MODE
        _seed(factory, KST_DAY, kill_switch=True, kill_reason="MDD 한도 초과")
        SAFE_MODE.disable("MDD 한도 초과")

        from backend.worker.scheduler import _reset_daily_risk
        _reset_daily_risk()

        assert SAFE_MODE.can_trade is False, (
            "the 06:01 re-arm resumed trading over a halt fired earlier "
            "the same Seoul day, during the overnight US session"
        )

    def test_with_no_halt_on_either_day_safe_mode_still_re_arms(
            self, in_window, factory):
        """The guard must not become unconditional — the normal path still opens."""
        from backend.worker.recovery import SAFE_MODE
        _seed(factory, KST_DAY - timedelta(days=1), kill_switch=False)
        _seed(factory, KST_DAY, kill_switch=False)
        SAFE_MODE.disable("야간 정지")

        from backend.worker.scheduler import _reset_daily_risk
        _reset_daily_risk()

        assert SAFE_MODE.can_trade is True


class TestTheContractHolds:
    def test_no_production_writer_keys_the_risk_row_with_date_today(self):
        """`trading_day()` is worth having only if it is the single door.

        A `date.today()` sitting next to `DailyRiskState` is how this bug got
        in, and copying a nearby line is how it would come back. Asserted at the
        source level because that is the actual property: not "the paths under
        test agree today" but "no path can disagree tomorrow".

        Tokenised rather than grepped — prose in a docstring that *mentions*
        `date.today()`, including the one on `trading_day` itself, is not a call.
        """
        offenders = [
            f"{path}:{line}"
            for path in _risk_row_modules()
            for line in _date_today_calls(path)
        ]
        assert offenders == [], (
            "these production modules key the daily risk row off the UTC date "
            f"instead of trading_day(): {offenders}"
        )

    def test_the_guard_can_actually_see_a_violation(self, tmp_path):
        """The guard above passes; this proves it passes for the right reason.

        A source scan that silently matches nothing is the easiest green in
        testing. Feed it both shapes this repo used — bare and aliased — and a
        docstring mention it must ignore.
        """
        sample = tmp_path / "offender.py"
        sample.write_text(
            'from datetime import date, date as _date\n'
            '"""A docstring that merely says date.today() is wrong."""\n'
            'x = date.today()\n'
            'y = _date.today()\n'
            'z = trading_day()\n',
            encoding="utf-8",
        )
        assert _date_today_calls(str(sample)) == [3, 4]


def _repo_root() -> str:
    import os
    import backend
    return os.path.dirname(os.path.dirname(os.path.abspath(backend.__file__)))


def _risk_row_modules() -> list[str]:
    """Production modules that touch the daily risk row."""
    import os
    found = []
    for root, dirs, files in os.walk(_repo_root()):
        dirs[:] = [d for d in dirs
                   if d not in {".git", "node_modules", "tests", "__pycache__"}]
        for name in files:
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    if "DailyRiskState" in fh.read():
                        found.append(path)
            except OSError:
                continue
    assert found, "found no modules referencing DailyRiskState — scan is broken"
    return sorted(found)


def _date_today_calls(path: str) -> list[int]:
    """Line numbers of real `date.today()` / `_date.today()` calls in `path`.

    `tokenize` drops comments and string literals for us, so only code counts.
    """
    import tokenize
    with open(path, "rb") as fh:
        toks = [t for t in tokenize.tokenize(fh.readline)
                if t.type not in (tokenize.COMMENT, tokenize.NL,
                                  tokenize.NEWLINE, tokenize.INDENT,
                                  tokenize.DEDENT, tokenize.STRING)]
    hits = []
    for a, b, c in zip(toks, toks[1:], toks[2:]):
        if (a.type == tokenize.NAME and a.string in ("date", "_date")
                and b.string == "." and c.string == "today"):
            hits.append(a.start[0])
    return hits


class _FakeRedis:
    """Records deletes. `_reset_daily_risk` only ever calls `delete`."""

    def __init__(self):
        self.deleted: set[str] = set()

    def delete(self, *keys):
        for k in keys:
            self.deleted.add(k.decode() if isinstance(k, bytes) else k)
        return len(keys)


class TestTheOvernightSessionStraddlesSeoulMidnight:
    """Follow-up to #160: moving the key to KST moved the boundary *into* a session.

    The US session runs 22:30–05:00 KST. Under the old UTC key the day rolled at
    09:00 KST, outside every session, so a halt and everything that later read
    it always addressed the same row. On the Seoul date the roll happens at
    midnight, mid-session — so a halt at 23:10 and a read at 00:40 land on
    different rows unless the reader looks at both.

    `trading_days_in_play()` is that shared definition. These cover the worker
    side; the operator endpoints are in `api/tests/test_risk_killswitch_reset.py`.
    """

    def test_the_helper_offers_today_and_the_day_before(self, in_window):
        from backend.database.models import trading_days_in_play
        assert trading_days_in_play() == (KST_DAY, KST_DAY - timedelta(days=1))

    def test_kill_switch_restores_a_halt_fired_before_midnight(
            self, in_window, factory):
        """`KillSwitch` starts RUNNING unless it finds a halt — and it has no
        carry-forward to the new day's row, so missing it fails open."""
        from backend.risk.kill_switch import KillSwitch, TradingState
        _seed(factory, KST_DAY - timedelta(days=1),
              kill_switch=True, kill_reason="MDD 한도 초과")

        assert KillSwitch(db_factory=factory).state is TradingState.HALTED

    def test_kill_switch_resume_clears_the_day_the_halt_is_on(
            self, in_window, factory):
        """Clearing only today's row left the real halt in place, unreachable."""
        from datetime import datetime, timezone
        from backend.risk.kill_switch import KillSwitch
        yesterday = KST_DAY - timedelta(days=1)
        _seed(factory, yesterday, kill_switch=True, kill_reason="MDD 한도 초과")

        ks = KillSwitch(db_factory=factory)
        # Well past the recovery cooldown — that policy is not what is under
        # test here, only which row the clear addresses.
        outcome = ks.resume("operator:test",
                            _now=datetime.now(timezone.utc) + timedelta(days=1))
        assert outcome.approved, f"resume denied, test proves nothing: {outcome.reason}"

        assert _row(factory, yesterday).kill_switch is False


class TestTheTrackerRestoresAcrossSeoulMidnight:
    """The production restore path — the one `StartupRecovery` branches on.

    `KillSwitch` is harness-only; `PersistentLossTracker` is what the live
    worker boots with, so this is where missing a halt actually costs money.
    """

    def test_a_halt_fired_before_midnight_is_restored_after_it(
            self, in_window, factory):
        yesterday = KST_DAY - timedelta(days=1)
        _seed(factory, yesterday, kill_switch=True, kill_reason="MDD 한도 초과")

        t = _tracker(factory)

        assert t.kill_switch is True, (
            "a worker restarting just after Seoul midnight came up unhalted "
            "while the overnight session's halt was still in force")
        assert t.kill_reason == "MDD 한도 초과"

    def test_equity_numbers_still_come_from_todays_row_only(
            self, in_window, factory):
        """Only the halt spans days. PnL and peak belong to their own day."""
        _seed(factory, KST_DAY - timedelta(days=1),
              weekly_pnl=-99_000.0, peak_equity=9_999_999.0)
        _seed(factory, KST_DAY, weekly_pnl=-1_000.0, peak_equity=2_000_000.0)

        t = _tracker(factory)

        assert t.peak_equity == 2_000_000.0
        assert t.weekly_pnl == -1_000.0

    def test_a_cleared_previous_day_does_not_resurrect_a_halt(
            self, in_window, factory):
        _seed(factory, KST_DAY - timedelta(days=1), kill_switch=False)
        assert _tracker(factory).kill_switch is False

    def test_equity_is_not_taken_from_yesterday_when_today_has_no_row(
            self, in_window, factory):
        """The likely shape of a wrong fix: falling back to yesterday wholesale.

        Before the first write of a Seoul day there is no row yet. Reaching
        back for the equity numbers would restore a stale peak — and peak
        equity is the denominator of the MDD limit, so a stale high one makes
        the drawdown look worse and a stale low one hides a real breach.
        """
        _seed(factory, KST_DAY - timedelta(days=1),
              kill_switch=True, kill_reason="MDD 한도 초과",
              weekly_pnl=-99_000.0, peak_equity=9_999_999.0)

        t = _tracker(factory)

        assert t.kill_switch is True, "the halt must still cross the boundary"
        assert t.peak_equity == 0.0
        assert t.weekly_pnl == 0.0


class TestTheDailyResetLeavesTheLiveCounterAlone:
    """06:01 KST is six hours *into* the Seoul day, not the start of it.

    That day already holds the 00:00–05:00 overnight US session. Zeroing its
    `daily_pnl` and deleting its Redis key wiped both stores
    `_restore_state()` reads, so a restart after 06:01 handed the Korean
    session a fresh 3% budget on top of the overnight loss.
    """

    @pytest.fixture(autouse=True)
    def _wire_scheduler(self, monkeypatch, factory):
        monkeypatch.setattr("backend.worker.scheduler._get_db", lambda: factory())

    def test_the_overnight_loss_survives_the_reset(
            self, in_window, factory, monkeypatch):
        monkeypatch.setattr("redis.from_url", lambda *a, **k: _FakeRedis())
        _seed(factory, KST_DAY, daily_pnl=-40_000.0, peak_equity=1_000_000.0)

        from backend.worker.scheduler import _reset_daily_risk
        _reset_daily_risk()

        assert _row(factory, KST_DAY).daily_pnl == -40_000.0, (
            "the reset erased the overnight session's realized loss")
        assert _tracker(factory).daily_pnl == -40_000.0, (
            "a restart after the reset came back with a clean slate")

    def test_the_reset_does_not_delete_the_live_pnl_key(
            self, in_window, factory, monkeypatch):
        """Redis is the other store `_restore_state()` reads."""
        fake = _FakeRedis()
        monkeypatch.setattr("redis.from_url", lambda *a, **k: fake)
        engine_key = _tracker(factory)._redis_key()

        from backend.worker.scheduler import _reset_daily_risk
        _reset_daily_risk()

        assert engine_key not in fake.deleted, (
            f"deleted {engine_key}, which is the counter the engine reads")

    def test_the_legacy_halt_keys_are_still_cleared(
            self, in_window, factory, monkeypatch):
        """The job's remaining purpose: the self-expiring daily-loss halt."""
        fake = _FakeRedis()
        monkeypatch.setattr("redis.from_url", lambda *a, **k: fake)

        from backend.worker.scheduler import _reset_daily_risk
        _reset_daily_risk()

        assert {"risk:daily_loss_pct", "risk:trading_halted"} <= fake.deleted


class TestOneTradingDayValuePerOrderRow:
    def test_the_idempotency_key_and_trade_date_cannot_disagree(
            self, factory, monkeypatch):
        """Two `trading_day()` calls could straddle midnight and split one row.

        Driven by making the helper return a different day on each call: with a
        single resolved value the row stays coherent regardless, and with two
        calls it does not.
        """
        import itertools
        from backend.database.models import Order as DBOrder
        from backend.worker import runner as runner_mod

        days = itertools.chain([KST_DAY, KST_DAY + timedelta(days=1)],
                               itertools.repeat(KST_DAY + timedelta(days=9)))
        monkeypatch.setattr("backend.database.models.trading_day",
                            lambda: next(days))
        _persist_via(monkeypatch, factory, runner_mod, _StubOrder())

        sess = factory()
        try:
            row = sess.query(DBOrder).one()
            key_day = row.idempotency_key.rsplit(":", 1)[1]
            assert key_day == row.trade_date.isoformat(), (
                f"key says {key_day}, column says {row.trade_date}")
        finally:
            sess.close()


class TestTheReArmFailsClosed:
    def test_a_failed_kill_switch_lookup_does_not_re_enable_trading(
            self, in_window, monkeypatch):
        """A swallowed lookup error used to fall through to `SAFE_MODE.enable()`.

        So a database outage resumed trading with nobody having checked the kill
        switch — the one question the guard exists to ask.
        """
        from backend.worker.recovery import SAFE_MODE

        class _Exploding:
            def get(self, *a, **k):
                raise RuntimeError("database is down")

            def close(self):
                pass

        monkeypatch.setattr("backend.worker.scheduler._get_db",
                            lambda: _Exploding())
        monkeypatch.setattr("redis.from_url", lambda *a, **k: _FakeRedis())
        SAFE_MODE.disable("야간 정지")

        from backend.worker.scheduler import _reset_daily_risk
        _reset_daily_risk()

        assert SAFE_MODE.can_trade is False, (
            "trading resumed although the kill-switch lookup never completed")


def _persist_via(monkeypatch, factory, runner_mod, order):
    """Run the real `_persist_order` against `factory`'s database."""
    from contextlib import contextmanager

    @contextmanager
    def _sess():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(runner_mod, "_session", _sess)
    runner_mod.StrategyWorker._persist_order(
        object.__new__(runner_mod.StrategyWorker), order)


class _StubOrder:
    """The few fields `_persist_order` reads off a broker order."""

    id = "0000117"
    symbol = "AAPL"
    side = "buy"
    qty = 1
    price = 100.0
    filled_qty = 0
    avg_fill_price = None

    class status:
        value = "submitted"


@contextmanager
def _null_session():
    """A session that records nothing — the row construction is what is asserted."""
    class _S:
        def query(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def first(self):
            return None

        def add(self, *a, **k):
            pass

        def commit(self):
            pass

    yield _S()


class TestTheFlaskReportersSeeTheSameHalt:
    """`/api/status` and `/api/metrics` are how an operator checks from outside.

    They are read-only, but a dashboard saying "not halted" during the hours a
    halt is actually in force is how someone decides nothing is wrong. Same
    two-day read as the control endpoints.
    """

    @pytest.fixture()
    def client(self, factory, monkeypatch):
        from backend.api import server as srv
        monkeypatch.setattr(srv, "_get_factory", lambda: factory)
        monkeypatch.setattr(srv, "_API_KEY", "")
        srv.app.config.update(TESTING=True)
        return srv.app.test_client()

    def test_status_reports_a_halt_fired_before_seoul_midnight(
            self, in_window, factory, client):
        _seed(factory, KST_DAY - timedelta(days=1),
              kill_switch=True, kill_reason="MDD 한도 초과")

        body = client.get("/api/status").get_json()

        assert body["kill_switch"] is True
        assert body["kill_reason"] == "MDD 한도 초과"

    def test_status_stays_clear_when_neither_day_is_halted(
            self, in_window, factory, client):
        _seed(factory, KST_DAY - timedelta(days=1), kill_switch=False)
        assert client.get("/api/status").get_json()["kill_switch"] is False

    def test_metrics_reports_the_same_halt(self, in_window, factory, client):
        _seed(factory, KST_DAY - timedelta(days=1), kill_switch=True)
        assert client.get("/api/metrics").get_json()["kill_switch"] is True

    def test_metrics_pnl_still_comes_from_todays_row_only(
            self, in_window, factory, client):
        """The halt spans days; the percentage does not."""
        _seed(factory, KST_DAY - timedelta(days=1),
              daily_pnl=-500_000.0, peak_equity=1_000_000.0)
        _seed(factory, KST_DAY, daily_pnl=-10_000.0, peak_equity=1_000_000.0)

        assert client.get("/api/metrics").get_json()["daily_pnl_pct"] == -1.0


class TestOrderRowsSurviveTheDateChange:
    """`_persist_order` still matches on the broker id alone — see issue #168.

    Scoping that lookup by trading day was tried in this PR and reverted twice
    over (it split overnight orders, then dropped long-pending ones), so what is
    pinned here is that aligning the *date key* did not disturb the behaviour
    that was already there.
    """

    def _persist(self, monkeypatch, factory, order):
        from backend.worker import runner as runner_mod
        _persist_via(monkeypatch, factory, runner_mod, order)

    def test_the_same_order_seen_twice_today_still_updates_in_place(
            self, in_window, factory, monkeypatch):
        """The lookup must stay useful — narrowing it must not break the update."""
        from backend.database.models import Order as DBOrder

        self._persist(monkeypatch, factory, _StubOrder())

        class _Filled(_StubOrder):
            filled_qty = 1
            avg_fill_price = 101.5

            class status:
                value = "filled"

        self._persist(monkeypatch, factory, _Filled())

        sess = factory()
        try:
            rows = sess.query(DBOrder).all()
            assert len(rows) == 1, "the second sighting inserted a second row"
            assert rows[0].status == "filled"
            assert rows[0].filled_qty == 1
        finally:
            sess.close()

    def test_an_order_submitted_before_midnight_fills_into_the_same_row(
            self, in_window, factory, monkeypatch):
        """The US session crosses Seoul midnight, so one order spans two dates.

        Submitted at 23:50 the row carries yesterday's `trade_date`; its own
        fill at 00:10 is resolved against today's. Scoping the lookup to today
        alone missed it and inserted a *second* row, leaving the first stuck at
        "submitted" — nightly, not an edge case.
        """
        from backend.database.models import Order as DBOrder

        yesterday = KST_DAY - timedelta(days=1)
        sess = factory()
        sess.add(DBOrder(
            broker_order_id=_StubOrder.id, symbol="AAPL", side="buy",
            qty=1, price=100.0, filled_qty=0, status="submitted", market="US",
            idempotency_key=f"{_StubOrder.id}:AAPL:buy:{yesterday.isoformat()}",
            trade_date=yesterday,
        ))
        sess.commit()
        sess.close()

        class _Filled(_StubOrder):
            filled_qty = 1
            avg_fill_price = 101.5

            class status:
                value = "filled"

        self._persist(monkeypatch, factory, _Filled())

        sess = factory()
        try:
            rows = sess.query(DBOrder).all()
            assert len(rows) == 1, (
                f"the overnight fill created a second row: "
                f"{[(r.trade_date, r.status) for r in rows]}")
            assert rows[0].status == "filled"
            assert rows[0].filled_qty == 1
            assert rows[0].trade_date == yesterday, (
                "the fill should update the submitting day's row in place")
        finally:
            sess.close()

    def test_two_different_orders_on_the_same_day_stay_separate(
            self, in_window, factory, monkeypatch):
        """Scoping by day must not become matching by day.

        Dropping the broker-id predicate would make every order of the day
        collapse onto the first row — the same overwrite, one day wide.
        """
        from backend.database.models import Order as DBOrder

        class _Other(_StubOrder):
            id = "0000118"
            symbol = "MSFT"

        self._persist(monkeypatch, factory, _StubOrder())
        self._persist(monkeypatch, factory, _Other())

        sess = factory()
        try:
            rows = sess.query(DBOrder).order_by(DBOrder.broker_order_id).all()
            assert [r.broker_order_id for r in rows] == ["0000117", "0000118"]
            assert [r.symbol for r in rows] == ["AAPL", "MSFT"]
        finally:
            sess.close()


class TestFillsAttachToTheRightOrder:
    """`_persist_fill` resolves the order row too, and must agree with
    `_persist_order` — they run on the same fill event.

    Both still match on the broker id alone (issue #168). These pin that the
    date-key change did not disturb the cases that already worked, including the
    overnight one that a day-scoped lookup broke.
    """

    @staticmethod
    def _fill():
        from backend.execution.position_tracker import Fill
        return Fill(order_id=_StubOrder.id, symbol="AAPL", side="buy",
                    qty=1, price=101.5, market="US")

    @staticmethod
    def _order():
        class _Filled(_StubOrder):
            filled_qty = 1
            avg_fill_price = 101.5

            class status:
                value = "filled"
        return _Filled()

    def _persist_fill(self, monkeypatch, factory):
        from contextlib import contextmanager
        from backend.worker import runner as runner_mod

        @contextmanager
        def _sess():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        monkeypatch.setattr(runner_mod, "_session", _sess)
        runner_mod.StrategyWorker._persist_fill(
            object.__new__(runner_mod.StrategyWorker), self._fill(), self._order())

    @staticmethod
    def _seed_order(factory, day, status="submitted", filled_qty=0):
        from backend.database.models import Order as DBOrder
        sess = factory()
        row = DBOrder(
            broker_order_id=_StubOrder.id, symbol="AAPL", side="buy",
            qty=1, price=100.0, filled_qty=filled_qty, status=status,
            market="US", trade_date=day,
            idempotency_key=f"{_StubOrder.id}:AAPL:buy:{day.isoformat()}",
        )
        sess.add(row)
        sess.commit()
        pk = row.id
        sess.close()
        return pk

    def test_an_overnight_fill_attaches_to_the_submitting_days_order(
            self, in_window, factory, monkeypatch):
        """Submitted at 23:50, filled at 00:10 — still the same order."""
        from backend.database.models import Fill as DBFill, Order as DBOrder

        pk = self._seed_order(factory, KST_DAY - timedelta(days=1))

        self._persist_fill(monkeypatch, factory)

        sess = factory()
        try:
            row = sess.get(DBOrder, pk)
            assert row.status == "filled"
            assert row.filled_qty == 1
            assert sess.query(DBFill).filter(DBFill.order_id == pk).count() == 1
        finally:
            sess.close()

    def test_an_order_row_without_a_trade_date_still_receives_its_fill(
            self, in_window, factory, monkeypatch):
        """`trade_date` is nullable, and SQL `IN` never matches NULL.

        Other writers leave the column unset, so scoping by day alone would
        silently drop every fill for those orders. A row with no date claims no
        day and cannot be excluded on day grounds.
        """
        from backend.database.models import Fill as DBFill, Order as DBOrder

        sess = factory()
        row = DBOrder(
            broker_order_id=_StubOrder.id, symbol="AAPL", side="buy",
            qty=1, price=100.0, filled_qty=0, status="submitted", market="US",
        )
        sess.add(row)
        sess.commit()
        pk = row.id
        assert row.trade_date is None, "precondition: the row carries no date"
        sess.close()

        self._persist_fill(monkeypatch, factory)

        sess = factory()
        try:
            assert sess.query(DBFill).filter(DBFill.order_id == pk).count() == 1
            assert sess.get(DBOrder, pk).filled_qty == 1
        finally:
            sess.close()

    def test_persist_order_updates_a_dateless_row_instead_of_duplicating_it(
            self, in_window, factory, monkeypatch):
        """`_persist_order` needs the same NULL tolerance as `_persist_fill`.

        Excluding dateless rows would make every status update insert a second
        row for orders other writers created without a `trade_date`.
        """
        from backend.database.models import Order as DBOrder
        from backend.worker import runner as runner_mod

        sess = factory()
        row = DBOrder(
            broker_order_id=_StubOrder.id, symbol="AAPL", side="buy",
            qty=1, price=100.0, filled_qty=0, status="submitted", market="US",
        )
        sess.add(row)
        sess.commit()
        pk = row.id
        assert row.trade_date is None, "precondition: the row carries no date"
        sess.close()

        class _Filled(_StubOrder):
            filled_qty = 1
            avg_fill_price = 101.5

            class status:
                value = "filled"

        _persist_via(monkeypatch, factory, runner_mod, _Filled())

        sess = factory()
        try:
            rows = sess.query(DBOrder).all()
            assert len(rows) == 1, (
                f"the dateless row was duplicated: "
                f"{[(r.id, r.trade_date, r.status) for r in rows]}")
            assert rows[0].id == pk
            assert rows[0].status == "filled"
        finally:
            sess.close()



class TestARestoredHaltIsCarriedOntoTodaysRow:
    """Restoring a halt is not enough — it has to survive the next write.

    Issue #158 established that restoring from the DB is *not* this process's
    opinion, so it must not be asserted back. That rule is right for today's
    row, which already says it. For a halt found on an **earlier** day it is
    exactly wrong: today's row does not carry it, so with nothing to assert
    `_write_db` reads today's row, adopts its `False` as an external clear and
    **wipes the live halt on the first write** — after which the old row ages
    out of the window and the halt is gone for good.
    """

    def test_it_survives_the_first_write_and_lands_on_todays_row(
            self, in_window, factory):
        _seed(factory, KST_DAY - timedelta(days=1),
              kill_switch=True, kill_reason="MDD 한도 초과")
        _seed(factory, KST_DAY, kill_switch=False, peak_equity=1_000_000.0)

        t = _tracker(factory)
        assert t.kill_switch is True, "precondition: the halt was restored"

        t.record_pnl(0.0, 1_000_000.0)          # benign write, no new decision

        assert t.kill_switch is True, (
            "the first write adopted today's False and wiped the restored halt")
        assert _row(factory, KST_DAY).kill_switch is True, (
            "the halt was never carried onto today's row")

    def test_a_halt_restored_from_todays_own_row_is_not_re_asserted(
            self, in_window, factory):
        """The #158 rule still holds where it belongs.

        A halt read from today's row is the row's own statement, not this
        process's. Counting it as intent would let a stale True replay over an
        operator's clear — precisely what #158 fixed.
        """
        _seed(factory, KST_DAY, kill_switch=True, kill_reason="MDD 한도 초과",
              peak_equity=1_000_000.0)

        t = _tracker(factory)
        assert t.kill_switch is True
        assert t._ks_epoch == t._ks_written, (
            "restoring from today's row claimed intent this process does not have")
