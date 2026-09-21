"""Operator controls for the persistent kill switch (ROADMAP P0-12).

There are two halt stores, and this router deliberately touches only one.

**Not this router's business — the daily-loss halt.** `strategy/risk.py`
`record_daily_loss()` sets the Redis key ``risk:trading_halted`` with
``ex=86400``, and `backend/worker/scheduler.py:_reset_daily_risk()` deletes it
at the daily reset. Halting for the remainder of the session is the intended
behaviour of a 3% daily-loss limit, and it clears itself. Nothing to rescue.

**This router's business — the MDD kill switch.** ``DailyRiskState.kill_switch``
is a Postgres flag with no expiry. `StartupRecovery._step_risk` restores it on
every boot, `_step_enable_trading` then calls ``SAFE_MODE.disable(...)`` and
logs *"킬스위치 복원 — 매매 차단. 수동 해제 후 재시작 필요."* The daily
scheduler only reads it for the Telegram summary. The one function that writes
it back to ``False`` — ``KillSwitch._clear_halt_in_db``, via
``KillSwitch.resume()`` — sits in a class that is **never constructed in
production**.

So "수동 해제" has in practice meant an operator editing the row by hand. That
is the gap: a control that can only be set, never released, is not a safety
control — it is an outage. This router is the release path, with the thing a
hand-edited row never leaves behind: a named operator and a written reason.

**What clearing the flag does and does not achieve.** Two things still stand
between a cleared row and a trading worker, and the response says both.

1. **A running worker does not resume.** It caches ``_kill_switch_active``
   during `StartupRecovery` and `_step_enable_trading` acts on that, so lifting
   ``SAFE_MODE`` needs a restart. That is the half of ROADMAP P0-12 still open
   (it depends on P0-04).

2. **A halt whose cause still holds comes straight back.** Clearing the row does
   not clear the *breach*. On the next PnL write ``LossTracker._evaluate()``
   re-checks the daily, weekly and MDD limits and halts again if any is still
   exceeded — a fresh decision, logged and alerted, not a stale overwrite. For a
   daily-loss or MDD halt the condition normally holds for the rest of the
   session, so **this endpoint alone does not resume intraday trading.**

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
import os
from datetime import date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from api.database import get_db
from api.deps import get_current_user
from api.models import User
from api.schemas import Resp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/risk", tags=["risk"])

#: Told to the operator on a successful reset. Clearing the row is necessary but
#: not sufficient — see the module docstring for both remaining gates.
RESTART_NOTICE = (
    "해제는 즉시 반영되며 실행 중인 워커가 덮어쓰지 않습니다. 다만 "
    "**매매 재개에는 워커 재시작이 필요합니다** — 워커가 기동 시 킬스위치를 "
    "캐시해 SAFE_MODE를 잠그기 때문입니다. 또한 **위반 조건 자체가 아직 "
    "유효하면**(일손실·주간손실·MDD 한도) 다음 PnL 기록에서 다시 정지됩니다. "
    "그건 덮어쓰기가 아니라 새 판단이며 로그와 알림이 남습니다."
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
    """Fail-closed break-glass allowlist, mirroring ``EMERGENCY_FLATTEN_ADMINS``
    in ``api/routers/quick_trade.py``.

    Clearing a kill switch re-enables trading for the **whole deployment**, not
    for the caller's own book, and this app has no role or admin column
    (``api/models.py:User``). So the same shape is used here: while
    ``KILL_SWITCH_ADMINS`` is unset — the default — **nobody** is authorized and
    the control is dormant. A dormant control is recoverable by setting one env
    var; a control every registered user can fire is not.

    A separate list from the flatten one on purpose: liquidating a book and
    releasing a risk halt are different powers and should be grantable apart.
    """
    allowed = {
        e.strip().lower()
        for e in os.environ.get("KILL_SWITCH_ADMINS", "").split(",")
        if e.strip()
    }
    if not allowed:
        return False
    return (getattr(user, "email", "") or "").lower() in allowed


def _today_row(db: Session):
    from backend.database.models import DailyRiskState
    return db.get(DailyRiskState, date.today())


@router.get("/kill-switch")
def kill_switch_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Whether today's persistent kill switch is set, and why."""
    row = _today_row(db)
    active = bool(row and row.kill_switch)
    return Resp.ok({
        "active": active,
        "reason": (row.kill_reason if row else None),
        "since": (row.updated_at.isoformat() if row and row.updated_at else None),
        "note": RESTART_NOTICE if active else None,
    })


@router.post("/kill-switch/reset")
def reset_kill_switch(
    body: KillSwitchResetRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Clear today's persistent kill switch, recording who did it and why."""
    from backend.database.models import AuditLog

    if not _is_risk_admin(current_user):
        # Fail closed, and say nothing about who is on the list.
        return Resp.err("권한이 없습니다 — KILL_SWITCH_ADMINS에 등록된 운영자만 해제할 수 있습니다.")

    row = _today_row(db)
    if row is None or not row.kill_switch:
        # Report rather than succeed quietly: an operator who thinks they just
        # released a halt, and did not, will not go looking for the real one.
        return Resp.err("킬스위치가 활성 상태가 아닙니다 — 해제할 것이 없습니다.")

    previous_reason = row.kill_reason

    db.add(AuditLog(
        event_type="kill_switch_reset",
        actor=f"operator:{current_user.id}",
        detail=json.dumps({
            "reason": body.reason,
            "previous_kill_reason": previous_reason,
            "user_id": current_user.id,
            "at": datetime.utcnow().isoformat(),
        }, ensure_ascii=False),
    ))

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
        "note": RESTART_NOTICE,
    }, msg=f"킬스위치를 해제했습니다. {RESTART_NOTICE}")
