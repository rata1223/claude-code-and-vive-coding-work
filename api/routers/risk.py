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

**What this does not do.** It does not resume a running worker. The worker
caches ``_kill_switch_active`` during `StartupRecovery` and gates ``SAFE_MODE``
from that snapshot, so clearing the flag takes effect on its next start — which
is exactly what the existing log message already tells the operator. Saying so
in the response matters: an operator who believes trading resumed when it has
not is worse off than one who is told to restart.
"""
import json
import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.database import get_db
from api.deps import get_current_user
from api.models import User
from api.schemas import Resp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/risk", tags=["risk"])

#: Told to the operator on a successful reset. The worker re-reads the flag at
#: startup, so the reset alone does not resume trading.
RESTART_NOTICE = "워커 재시작 후 매매가 재개됩니다 (기동 시 플래그를 다시 읽습니다)."


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
