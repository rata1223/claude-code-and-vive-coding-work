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

**What this does not do, and the order that matters.** It does not resume a
running worker: the worker caches ``_kill_switch_active`` during
`StartupRecovery`, so the clear takes effect on its next start.

Worse, resetting *while a worker is running* can be silently undone.
``PersistentLossTracker._write_db`` (backend/quant/risk/engine.py) writes
``row.kill_switch = ks`` from its **in-memory** value on every PnL write, so a
live worker that still believes the switch is set will put it back on its next
write. The safe order is therefore **stop the worker → reset → start**, which
the response states.

The durable fix is to make the tracker honour an external clear rather than
overwrite it — that belongs in the worker's risk engine, not here, and is
tracked as follow-up on P0-12.
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

#: Told to the operator on a successful reset. The worker re-reads the flag at
#: startup, so the reset alone does not resume trading.
RESTART_NOTICE = (
    "워커를 **정지한 뒤** 해제하고 다시 기동하세요. 실행 중인 워커는 "
    "PersistentLossTracker의 메모리 값으로 DailyRiskState를 덮어쓰므로, "
    "다음 PnL 기록 시 kill_switch가 True로 되돌아갑니다."
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
