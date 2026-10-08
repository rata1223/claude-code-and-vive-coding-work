"""Operator controls for the persistent kill switch (ROADMAP P0-12).

There are two halt stores, and this router deliberately touches only one.

**Not this router's business — the daily-loss halt.** `strategy/risk.py`
`record_daily_loss()` sets the Redis key ``risk:trading_halted`` with
``ex=86400``, and `backend/worker/scheduler.py:_reset_daily_risk()` deletes it
at the daily reset. Halting for the remainder of the session is the intended
behaviour of a 3% daily-loss limit, and it clears itself. Nothing to rescue.

**This router's business — the MDD kill switch.** ``DailyRiskState.kill_switch``
is a Postgres flag with no expiry. `StartupRecovery._step_risk` restores it on
every boot and `_step_enable_trading` then closes ``SAFE_MODE`` for it. The
daily scheduler reads it for the Telegram summary and carries it forward. The one function that writes
it back to ``False`` — ``KillSwitch._clear_halt_in_db``, via
``KillSwitch.resume()`` — sits in a class that is **never constructed in
production**.

So "수동 해제" has in practice meant an operator editing the row by hand. That
is the gap: a control that can only be set, never released, is not a safety
control — it is an outage. This router is the release path, with the thing a
hand-edited row never leaves behind: a named operator and a written reason.

**What clearing the flag achieves (P0-12).**

1. **A running worker resumes within a minute, without a restart** — if it was
   halted for a risk limit and its startup recovery succeeded (a halt restored
   at boot counts: recovery passed everything else). Its resume poll
   (``StrategyWorker._resume_if_released``) sees no halted row, settles the
   tracker with it, and reopens ``SAFE_MODE``. A worker halted because its
   state cannot be trusted — failed recovery — still needs a restart.

2. **A release accepts the loss as it stands.** Clearing the row does not clear
   the *breach*, and the next PnL write used to halt straight back. Now the
   tracker adopting the release sets a baseline: a daily or weekly limit that
   was past its setting halts again only once another 1% of capital is lost
   (one that was not stays as configured; the next risk day starts fresh), and
   MDD is measured from equity at the release.

What is no longer true: the reset used to be *silently* undone.
``PersistentLossTracker._write_db`` overwrote the column from its in-memory
value on every write, so a clear vanished with no log and no reason. That is
fixed (issue #158) — the tracker now re-reads the row and only asserts the flag
when it has a decision of its own to record, so an external clear survives and
the tracker converges to it. The old workaround ("stop the worker, then clear,
then start") is no longer required.

One residue, stated so it is not a surprise: if the worker breached a limit and
the write of *that* halt failed, the intent stays pending and is re-asserted on
its next successful write — including over a clear made in between. That
direction is fail-closed (a real breach wins), and the reverse — a failed clear
replaying over somebody else's halt — is explicitly dropped instead.
"""
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from api.database import get_db
from api.deps import get_current_user
from api.models import User
from api.schemas import Resp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/risk", tags=["risk"])

#: Told to the operator on a successful reset — what the release does to the
#: running worker (module docstring).
RELEASE_NOTICE = (
    "해제는 즉시 반영되며 실행 중인 워커가 덮어쓰지 않습니다. 리스크 한도"
    "(일손실·주간손실·MDD)로 정지한 워커는 **1분 안에 재시작 없이 매매를 "
    "재개**합니다. 해제는 지금의 손실을 받아들인 것으로 봅니다 — 해제 때 "
    "넘어 있던 일·주간 한도는 **해제 시점보다 자본의 1%를 더 잃으면 다시 정지**하고"
    "(넘지 않았던 한도는 그대로, 다음 "
    "리스크 데이는 새로 시작), MDD는 해제 시점 자산을 새 기준으로 잽니다. "
    "복구 실패처럼 상태를 믿을 수 없어 멈춘 워커는 여전히 재시작이 필요합니다."
)


class KillSwitchResetRequest(BaseModel):
    """``reason`` is required and non-empty on purpose.

    A kill switch cleared without a recorded reason is indistinguishable, to
    the next person reading the audit trail, from one that was never
    investigated. The field is the deliberation, and it is also what stands in
    for ``RecoveryManager``'s time-based cooldown: that cooldown measures from
    ``halted_at``, which `DailyRiskState` does not persist, so it cannot express
    a real waiting period here.
    """

    reason: str = Field(..., min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        """``min_length=1`` alone accepts ``"   "``, which is a blank reason
        wearing a character. Trim, then require something left."""
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("사유는 공백일 수 없습니다")
        return trimmed


def _is_risk_admin(user: User) -> bool:
    """Whether ``user`` may clear the kill switch.

    Clearing a kill switch re-enables trading for the **whole deployment**, not
    for the caller's own book, and this app has no role or admin column
    (``api/models.py:User``). So the gate is the deployment's operator
    allow-list, ``OPERATOR_USER_IDS`` (``backend/security/operators.py``) — the
    same list as emergency flatten and the live feed. Unset — the default —
    authorizes **nobody** and the control is dormant.
    """
    from backend.security.operators import is_operator

    return is_operator(user)


def _halt_rows(db: Session):
    """Rows that can hold a live halt right now, newest first.

    Not just today's. A halt is not cleared by the risk day changing (07:00
    KST), so one fired earlier sits on its own row until someone clears it,
    while a reset addresses today's. Reading one row let an operator see
    "not halted" — and be told there was nothing to release — while the halt
    was still in force; reading two did the same once the worker had been down
    two whole risk days (``risk_days_in_play``). (With the old Seoul-midnight
    key the US session itself straddled the boundary, which is how this was
    found.)
    """
    from backend.database.models import DailyRiskState, risk_days_in_play
    rows = [db.get(DailyRiskState, key) for key in risk_days_in_play(db)]
    return [r for r in rows if r is not None]


def _halted_rows(db: Session):
    """The subset actually halted, newest first."""
    return [r for r in _halt_rows(db) if r.kill_switch]


def _lock_halted_rows(db: Session):
    """Like ``_halted_rows``, but locked until this transaction ends (#164).

    The reset decides what to clear from the rows *as locked*. Read unlocked,
    a halt committed between that read and the reset's commit was erased
    without anyone having seen it — failing open. Now a halt committed before
    the lock is part of what the operator clears (and is named in the audit
    row), and one committed after it waits and survives.
    """
    from backend.database.models import lock_risk_rows, risk_days_in_play
    rows = lock_risk_rows(db, risk_days_in_play(db))
    return sorted((r for r in rows if r.kill_switch),
                  key=lambda r: r.trade_date, reverse=True)


@router.get("/kill-switch")
def kill_switch_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Whether a persistent kill switch is set on any risk day still halted, and why."""
    halted = _halted_rows(db)
    active = bool(halted)
    # Newest row first, so the detail shown is the most recent decision when
    # several days are halted.
    row = halted[0] if halted else None
    return Resp.ok({
        "active": active,
        "reason": (row.kill_reason if row else None),
        "since": (row.updated_at.isoformat() if row and row.updated_at else None),
        "note": RELEASE_NOTICE if active else None,
    })


@router.post("/kill-switch/reset")
def reset_kill_switch(
    body: KillSwitchResetRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Clear the persistent kill switch on every live trading day, recording who and why."""
    from backend.database.models import AuditLog

    if not _is_risk_admin(current_user):
        # Fail closed, and say nothing about who is on the list.
        return Resp.err("권한이 없습니다 — OPERATOR_USER_IDS에 등록된 운영자만 해제할 수 있습니다.")

    halted = _lock_halted_rows(db)
    if not halted:
        db.rollback()   # release the row locks
        # Report rather than succeed quietly: an operator who thinks they just
        # released a halt, and did not, will not go looking for the real one.
        return Resp.err("킬스위치가 활성 상태가 아닙니다 — 해제할 것이 없습니다.")

    # Every halted row, in one transaction. Releasing only one leaves the other
    # blocking the worker's resume poll (any halted row does) with no endpoint
    # able to reach it. The audit row below is what the worker's tracker reads
    # as "a release" (`RELEASE_EVENT`) — keep it in this transaction.
    previous_reason = halted[0].kill_reason
    # Each day's own reason, so the audit row does not drop an older one when
    # several days are halted for different causes.
    cleared = [
        {"trade_date": r.trade_date.isoformat(), "kill_reason": r.kill_reason}
        for r in halted
    ]

    db.add(AuditLog(
        event_type="kill_switch_reset",
        actor=f"operator:{current_user.id}",
        detail=json.dumps({
            "reason": body.reason,
            "previous_kill_reason": previous_reason,
            "cleared": cleared,
            "user_id": current_user.id,
            "at": datetime.utcnow().isoformat(),
        }, ensure_ascii=False),
    ))

    for row in halted:
        row.kill_switch = False
        row.kill_reason = None
    db.commit()

    logger.warning(
        "킬스위치 수동 해제 — operator=%s 사유=%s 직전사유=%s",
        current_user.id, body.reason, previous_reason,
    )

    return Resp.ok({
        "active": False,
        "previous_reason": previous_reason,
        "note": RELEASE_NOTICE,
    }, msg=f"킬스위치를 해제했습니다. {RELEASE_NOTICE}")
