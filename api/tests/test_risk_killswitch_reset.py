"""P0-12 — a halted system must have a reachable way back.

There are **two** halt stores in this codebase, and only one of them is the
problem this endpoint exists to solve:

1. **Daily-loss halt** — the Redis key ``risk:trading_halted``, set by
   ``strategy/risk.py:record_daily_loss()`` with ``ex=86400``. It expires by
   itself within a day and `backend/worker/scheduler.py:_reset_daily_risk()`
   deletes it at the daily reset. Halting for the rest of the session is the
   intended design (일손실 3% → 당일 매매 중단). **Not** what this endpoint
   touches.

2. **MDD kill switch** — ``DailyRiskState.kill_switch`` in Postgres. This one
   is persistent, and it is the one with no way out:

   * ``StartupRecovery._step_risk`` restores it on boot and sets
     ``_kill_switch_active``;
   * ``_step_enable_trading`` then calls ``SAFE_MODE.disable(...)`` and logs
     **"킬스위치 복원 — 매매 차단. 수동 해제 후 재시작 필요."**;
   * the daily scheduler only *reads* the flag for the Telegram summary — it
     never clears it;
   * the only code that writes it back to ``False`` is
     ``KillSwitch._clear_halt_in_db``, reached through ``KillSwitch.resume()``
     — and **``KillSwitch`` is never constructed in production**.

   So "수동 해제" has meant editing the row by hand in the database. This
   endpoint is that manual step, with an operator, a written reason, and an
   audit trail instead.

The endpoint deliberately does **not** claim to resume a running worker: the
worker caches ``_kill_switch_active`` at startup, so clearing the flag takes
effect on its next start. That matches the message the code already prints.

No broker, no network — the DB is a local SQLite session.
"""
import pytest
from sqlalchemy.pool import StaticPool

from api.tests.test_quick_trade_close_position import (  # reuse the proven harness
    db,          # noqa: F401 - pytest fixtures, imported for this module's use
    user,        # noqa: F401
)


@pytest.fixture()
def engine():
    """Both metadata sets on one SQLite engine.

    The shared harness creates only ``api.models``. This router reads
    `DailyRiskState` and writes `AuditLog`, which live in
    ``backend.database.models`` — a separate ``Base``. Shadowing the imported
    fixture (rather than editing the shared one) keeps the other suites that
    depend on it untouched.
    """
    from api.models import Base as ApiBase
    from api.tests.test_quick_trade_close_position import make_test_engine
    from backend.database.models import Base as BackendBase

    eng = make_test_engine(poolclass=StaticPool)
    ApiBase.metadata.create_all(bind=eng)
    BackendBase.metadata.create_all(bind=eng)
    yield eng
    BackendBase.metadata.drop_all(bind=eng)
    ApiBase.metadata.drop_all(bind=eng)
    eng.dispose()


@pytest.fixture(autouse=True)
def _authorized(monkeypatch):
    """Most tests exercise the happy path, so put the caller on the allowlist.

    The gate itself is covered by the authorization tests below, which clear
    this. ``a@example.com`` is the ``user`` fixture's address.
    """
    monkeypatch.setenv("KILL_SWITCH_ADMINS", "a@example.com")


def _risk_row(db, *, kill_switch: bool, reason: str | None = None, day=None):
    """Seed a risk row. Defaults to the current trading day.

    Keyed by ``trading_day()`` rather than ``date.today()`` so the seed matches
    what the router reads; the two differ for nine hours a day (issue #160).
    """
    from backend.database.models import DailyRiskState, trading_day
    row = DailyRiskState(trade_date=day or trading_day(), kill_switch=kill_switch,
                         kill_reason=reason, peak_equity=2_000_000.0)
    db.add(row)
    db.commit()
    return row


def _yesterday():
    """The preceding trading day — where a halt fired before Seoul midnight sits."""
    from backend.database.models import trading_days_in_play
    return trading_days_in_play()[1]


# ── the endpoint must exist and be reachable ─────────────────────────────────

def test_the_reset_endpoint_is_registered():
    """The whole point of P0-12: there is a route at all. Before this, the only
    way out of a persistent kill switch was hand-editing the DB."""
    from api.main import app

    paths = {r.path for r in app.routes}
    assert "/api/risk/kill-switch/reset" in paths, (
        f"no kill-switch reset route registered (have: "
        f"{sorted(p for p in paths if 'risk' in p)})")


def test_the_status_endpoint_is_registered():
    """An operator has to be able to see the flag before deciding to clear it."""
    from api.main import app

    paths = {r.path for r in app.routes}
    assert "/api/risk/kill-switch" in paths


# ── clearing ─────────────────────────────────────────────────────────────────

def test_reset_clears_a_persistent_kill_switch(db, user):
    from datetime import date
    from backend.database.models import DailyRiskState
    from api.routers import risk

    _risk_row(db, kill_switch=True, reason="MDD 15% 초과")

    resp = risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="오탐 확인, 포지션 수동 정리 완료"),
        user, db)

    assert resp.code == 1, resp.msg
    from backend.database.models import trading_day
    row = db.get(DailyRiskState, trading_day())
    assert row.kill_switch is False


def test_reset_requires_a_reason(db, user):
    """A silent reset makes the kill switch meaningless — the next operator
    cannot tell whether the breach was investigated or waved through."""
    import pydantic
    from api.routers import risk

    with pytest.raises(pydantic.ValidationError):
        risk.KillSwitchResetRequest(reason="")


def test_reset_records_who_and_why(db, user):
    """The audit row is the deliverable, not a side effect."""
    from backend.database.models import AuditLog
    from api.routers import risk

    _risk_row(db, kill_switch=True, reason="MDD 15% 초과")
    risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="오탐 확인"), user, db)

    rows = db.query(AuditLog).filter(
        AuditLog.event_type == "kill_switch_reset").all()
    assert len(rows) == 1, "operator interventions must leave an audit row"
    assert "오탐 확인" in (rows[0].detail or "")
    assert str(user.id) in (rows[0].actor or "")


def test_reset_on_a_clear_system_is_reported_not_silently_ok(db, user):
    """Resetting something that is not set should say so — an operator who
    believes they just cleared a halt, and did not, is worse off than one who
    is told there was nothing to clear."""
    from api.routers import risk

    _risk_row(db, kill_switch=False)

    resp = risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="확인차"), user, db)

    assert resp.code == -1
    assert "활성" in resp.msg or "not active" in resp.msg.lower()


def test_reset_with_no_row_at_all_is_reported(db, user):
    """No row for today means nothing has halted today."""
    from api.routers import risk

    resp = risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="확인차"), user, db)

    assert resp.code == -1


# ── status ───────────────────────────────────────────────────────────────────

def test_status_reports_an_active_kill_switch(db, user):
    from api.routers import risk

    _risk_row(db, kill_switch=True, reason="MDD 15% 초과")

    resp = risk.kill_switch_status(user, db)

    assert resp.code == 1
    assert resp.data["active"] is True
    assert resp.data["reason"] == "MDD 15% 초과"


def test_status_reports_a_clear_system(db, user):
    from api.routers import risk

    resp = risk.kill_switch_status(user, db)

    assert resp.code == 1
    assert resp.data["active"] is False


def test_status_tells_the_operator_a_restart_is_needed(db, user):
    """``StartupRecovery`` caches the flag at boot, so clearing the row does not
    un-halt a worker that is already running. The response has to say that or
    the operator will think trading resumed when it has not."""
    from api.routers import risk

    _risk_row(db, kill_switch=True, reason="MDD")
    resp = risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="정리 완료"), user, db)

    assert resp.code == 1
    note = resp.msg + str(resp.data)
    assert "기동" in note, "the operator must be told the worker has to come back up"


# ── the reset actually reaches the thing that halts trading ──────────────────

def test_the_boot_time_reader_sees_the_cleared_flag(db, user):
    """Flipping a column is not the deliverable — un-halting the next boot is.

    ``StartupRecovery._step_risk`` does not read `DailyRiskState` directly; it
    constructs a ``PersistentLossTracker`` and branches on
    ``tracker.kill_switch``. This drives that exact reader before and after the
    reset, so the test fails if the endpoint ever clears a column the boot path
    does not consult.
    """
    from backend.quant.risk.engine import PersistentLossTracker, RiskConfig
    from api.routers import risk

    _risk_row(db, kill_switch=True, reason="MDD 15% 초과")

    # A *fresh* session per call, on the same engine: the tracker closes the
    # session it is handed, which would detach the fixtures' objects.
    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=db.get_bind(), autocommit=False, autoflush=False)

    def _factory():
        return Session()

    before = PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                   db_factory=_factory)
    assert before.kill_switch is True, "precondition: boot path sees the halt"

    risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="포지션 수동 정리 완료"), user, db)

    after = PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                  db_factory=_factory)
    assert after.kill_switch is False, (
        "the boot path still reports a kill switch — the reset cleared "
        "something StartupRecovery does not read")


# ── authorization: clearing a halt is a deployment-wide power ────────────────

def test_reset_is_refused_when_the_allowlist_is_unset(db, user, monkeypatch):
    """Fail closed by default. Clearing the switch re-enables trading for the
    whole deployment, not for the caller's own book, and this app has no admin
    column — so the control stays dormant until an operator is named."""
    from api.routers import risk

    monkeypatch.delenv("KILL_SWITCH_ADMINS", raising=False)
    _risk_row(db, kill_switch=True, reason="MDD")

    resp = risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="해제 시도"), user, db)

    assert resp.code == -1
    assert "권한" in resp.msg


def test_reset_is_refused_for_a_user_not_on_the_allowlist(db, user, monkeypatch):
    from api.routers import risk

    monkeypatch.setenv("KILL_SWITCH_ADMINS", "someone-else@example.com")
    _risk_row(db, kill_switch=True, reason="MDD")

    resp = risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="해제 시도"), user, db)

    assert resp.code == -1


def test_an_unauthorized_reset_does_not_clear_the_flag(db, user, monkeypatch):
    """The refusal must be a refusal, not a message in front of a mutation."""
    from datetime import date
    from backend.database.models import DailyRiskState
    from api.routers import risk

    monkeypatch.delenv("KILL_SWITCH_ADMINS", raising=False)
    _risk_row(db, kill_switch=True, reason="MDD")

    risk.reset_kill_switch(risk.KillSwitchResetRequest(reason="시도"), user, db)

    assert db.get(DailyRiskState, date.today()).kill_switch is True


# ── a blank reason is not a reason ───────────────────────────────────────────

def test_a_whitespace_only_reason_is_rejected():
    """``min_length=1`` alone accepts "   ", which defeats the point of
    requiring a reason at all."""
    import pydantic
    from api.routers import risk

    with pytest.raises(pydantic.ValidationError):
        risk.KillSwitchResetRequest(reason="   ")


def test_the_reason_is_stored_trimmed(db, user):
    from backend.database.models import AuditLog
    from api.routers import risk

    _risk_row(db, kill_switch=True, reason="MDD")
    risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="  포지션 정리 완료  "), user, db)

    row = db.query(AuditLog).filter(
        AuditLog.event_type == "kill_switch_reset").one()
    assert '"reason": "포지션 정리 완료"' in row.detail


# ── what the operator is told, now that the overwrite is gone ────────────────

def test_the_response_states_both_remaining_gates(db, user):
    """Clearing the row is necessary but not sufficient, for two separate
    reasons, and an operator who is told neither will think trading resumed.

    1. ``StartupRecovery`` caches the flag at boot and ``_step_enable_trading``
       locks ``SAFE_MODE`` from it — lifting that needs a restart (the half of
       P0-12 still open).
    2. Clearing the row does not clear the *breach*. If a limit is still
       exceeded, ``LossTracker._evaluate()`` halts again on the next PnL write.
    """
    from api.routers import risk

    _risk_row(db, kill_switch=True, reason="MDD")
    resp = risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="정리 완료"), user, db)

    assert resp.code == 1
    note = resp.msg + str(resp.data)
    assert "재시작" in note, "the operator must be told a restart is needed"
    assert "위반 조건" in note or "다시 정지" in note, (
        "the operator must be told a live breach re-halts")


def test_the_response_no_longer_tells_them_to_stop_the_worker_first(db, user):
    """That instruction existed only because ``_write_db`` silently overwrote
    the cleared flag (issue #158). That is fixed, so repeating the advice would
    send operators through a needless outage."""
    from api.routers import risk

    _risk_row(db, kill_switch=True, reason="MDD")
    resp = risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="정리 완료"), user, db)

    note = resp.msg + str(resp.data)
    assert "정지한 뒤" not in note
    assert "되돌아갑니다" not in note


def test_a_live_tracker_does_not_undo_the_reset(db, user):
    """The claim the response now makes, driven end to end against the real
    risk engine rather than trusted from the wording."""
    from sqlalchemy.orm import sessionmaker

    from backend.quant.risk.engine import PersistentLossTracker, RiskConfig
    from api.routers import risk

    Session = sessionmaker(bind=db.get_bind(), autocommit=False, autoflush=False)

    def _factory():
        return Session()

    # A worker that halted earlier in the session and is still running.
    tracker = PersistentLossTracker(config=RiskConfig(), redis_client=None,
                                    db_factory=_factory)
    tracker.peak_equity = 1_000_000.0
    tracker.record_pnl(-500_000.0, 500_000.0)
    assert tracker.kill_switch is True

    db.expire_all()
    resp = risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="포지션 수동 정리 완료"), user, db)
    assert resp.code == 1, resp.msg

    # The day rolls over, so no limit is breached any more; the only thing that
    # could put the flag back now is the stale in-memory value.
    tracker.reset_daily()
    tracker.reset_weekly()
    tracker.record_pnl(0.0, 1_000_000.0)

    from datetime import date

    from backend.database.models import DailyRiskState
    sess = Session()
    try:
        row = sess.get(DailyRiskState, date.today())
        assert row.kill_switch is False, (
            "a live worker put the halt back — #158 is not actually fixed")
    finally:
        sess.close()
    assert tracker.kill_switch is False, "the tracker did not converge to the row"


# ── the overnight session straddles Seoul midnight (issue #160 follow-up) ────
#
# The US session runs 22:30–05:00 KST. Since the row key became the Seoul date,
# a halt fired at 23:10 lands on one row and an operator arriving at 00:40
# addresses the next. Reading only "today" made a live halt invisible here and
# told the operator there was nothing to release — and with the 06:01 re-arm
# guard reading both days, that unreleased row would then block trading with no
# endpoint able to reach it.
#
# Before #160 the key was the UTC date, whose boundary is 09:00 KST — outside
# every session — so this could not happen. It is a regression of that change,
# and these pin it shut.


def test_status_sees_a_halt_fired_before_seoul_midnight(db, user):
    from api.routers import risk

    _risk_row(db, kill_switch=True, reason="MDD 15% 초과", day=_yesterday())

    resp = risk.kill_switch_status(user, db)
    assert resp.data["active"] is True, (
        "a halt from earlier in the same overnight session read as inactive")
    assert resp.data["reason"] == "MDD 15% 초과"


def test_reset_clears_a_halt_fired_before_seoul_midnight(db, user):
    from backend.database.models import DailyRiskState
    from api.routers import risk

    yesterday = _yesterday()
    _risk_row(db, kill_switch=True, reason="MDD 15% 초과", day=yesterday)

    resp = risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="오탐 확인"), user, db)

    assert resp.code == 1, resp.msg
    assert db.get(DailyRiskState, yesterday).kill_switch is False


def test_reset_clears_every_halted_day_not_just_one(db, user):
    """Releasing one row and leaving the other is the trap this closes.

    The 06:01 SAFE_MODE re-arm reads both days, so a leftover halt keeps
    trading blocked — and no endpoint could reach it.
    """
    from backend.database.models import DailyRiskState, trading_day
    from api.routers import risk

    yesterday = _yesterday()
    _risk_row(db, kill_switch=True, reason="어제 MDD", day=yesterday)
    _risk_row(db, kill_switch=True, reason="오늘 일손실", day=trading_day())

    risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="양일 정리"), user, db)

    assert db.get(DailyRiskState, yesterday).kill_switch is False
    assert db.get(DailyRiskState, trading_day()).kill_switch is False


def test_status_prefers_todays_reason_when_both_days_are_halted(db, user):
    """Two halted rows, one answer: the operator should see the newer decision."""
    from backend.database.models import trading_day
    from api.routers import risk

    _risk_row(db, kill_switch=True, reason="어제 MDD", day=_yesterday())
    _risk_row(db, kill_switch=True, reason="오늘 일손실", day=trading_day())

    assert risk.kill_switch_status(user, db).data["reason"] == "오늘 일손실"


def test_a_cleared_previous_day_does_not_keep_reporting_a_halt(db, user):
    """Reading yesterday unconditionally must not resurrect a released halt."""
    from backend.database.models import trading_day
    from api.routers import risk

    _risk_row(db, kill_switch=False, day=_yesterday())
    _risk_row(db, kill_switch=False, day=trading_day())

    assert risk.kill_switch_status(user, db).data["active"] is False


def test_the_audit_row_names_every_day_it_cleared(db, user):
    """Which rows were touched is the part a hand-edit never leaves behind."""
    from backend.database.models import AuditLog, trading_day
    from api.routers import risk

    yesterday = _yesterday()
    _risk_row(db, kill_switch=True, reason="어제 MDD", day=yesterday)
    _risk_row(db, kill_switch=True, reason="오늘 일손실", day=trading_day())

    risk.reset_kill_switch(
        risk.KillSwitchResetRequest(reason="양일 정리"), user, db)

    detail = db.query(AuditLog).filter(
        AuditLog.event_type == "kill_switch_reset").one().detail
    assert yesterday.isoformat() in detail
    assert trading_day().isoformat() in detail
