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
2. **The 06:01 KST reset touches the wrong day.** 06:01 KST is 21:01 UTC *the
   previous day*, so it zeroed **yesterday's** `daily_pnl` — destroying the
   record — and deleted a Redis key the engine never reads.
3. **The "did the kill switch fire yesterday?" check looked two days back.**
   `date.today() - 1` at 06:01 KST is the day before KST-yesterday, so a real
   halt went unseen and `SAFE_MODE.enable()` resumed trading — the exact
   opposite of what the code says it is doing. A second, independent fail-open.

These tests reproduce the window by patching `backend.database.models.trading_day`
so the KST date is one day ahead of `date.today()`, which is precisely the state
of the world between KST 00:00 and 09:00. Patching the single helper covers every
call site, because they all import it inside the function that uses it.
"""

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


class TestDailyResetTouchesToday:
    """Consequence (2): 06:01 KST is 21:01 UTC *yesterday*."""

    @pytest.fixture(autouse=True)
    def _wire_scheduler(self, monkeypatch, factory):
        monkeypatch.setattr("backend.worker.scheduler._get_db", lambda: factory())

    def test_reset_zeroes_todays_row_and_leaves_yesterdays_alone(
            self, in_window, factory, monkeypatch):
        """The reset must clear the day it is opening, not the one it is closing.

        Zeroing yesterday's `daily_pnl` is silent record loss: that row is the
        closed day's result, and nothing rewrites it.
        """
        monkeypatch.setattr("redis.from_url", lambda *a, **k: _FakeRedis())
        _seed(factory, KST_DAY, daily_pnl=-40_000.0)
        _seed(factory, KST_DAY - timedelta(days=1), daily_pnl=-11_000.0)

        from backend.worker.scheduler import _reset_daily_risk
        _reset_daily_risk()

        assert _row(factory, KST_DAY).daily_pnl == 0.0
        assert _row(factory, KST_DAY - timedelta(days=1)).daily_pnl == -11_000.0, (
            "the reset destroyed the closed day's record"
        )

    def test_reset_deletes_the_redis_key_the_engine_actually_reads(
            self, in_window, factory, monkeypatch):
        """A reset that deletes a key nobody reads is not a reset.

        The engine's `_redis_key()` is built from the Seoul date, so the UTC-keyed
        delete left the live counter in place for the whole window.
        """
        fake = _FakeRedis()
        monkeypatch.setattr("redis.from_url", lambda *a, **k: fake)
        engine_key = _tracker(factory)._redis_key()

        from backend.worker.scheduler import _reset_daily_risk
        _reset_daily_risk()

        assert engine_key in fake.deleted, (
            f"reset deleted {sorted(fake.deleted)}, but the engine reads {engine_key}"
        )


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
