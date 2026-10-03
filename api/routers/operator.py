"""Operator control of the worker's house strategy (B1 of
docs/STRATEGY_START_B_DESIGN.md).

The worker trades one ``.env`` account with one strategy — the indicator
strategy's house signal (``default_fusion``). Starting it writes a
``strategy_runs`` row and starts the 4-week paper clock. Until now the only way
in was ``curl`` against kis-api with ``KIS_API_KEY``.

This is a **thin proxy**, the same shape as the emergency-flatten proxy in
``api/routers/quick_trade.py``: the browser speaks JWT here, this process holds
``KIS_API_KEY`` and calls the existing kis-api routes. Nothing here writes a
worker table. kis-api enforces one occupied run at a time atomically
(``_occupying_run``); the check below is only an early, friendlier refusal.

Users' own strategies still cannot be started (``/api/strategies/start``, option
A). Only ``OPERATOR_USER_IDS`` may use these routes; everyone else is refused
before their input is looked at.
"""
import logging
import os
from datetime import datetime
from types import SimpleNamespace

from fastapi import APIRouter, Depends
from pydantic import ValidationError

from api.deps import get_current_user
from api.models import User
from api.schemas import OperatorStrategyStart, OperatorStrategyStop, Resp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/operator", tags=["operator"])

#: Same default and override as the emergency-flatten proxy. Plain HTTP is only
#: acceptable on a single-host compose network; split hosts need an https base.
_ADMIN_API_BASE = "http://kis-api:5001"
_TIMEOUT_SEC = 15

_NOT_AUTHORIZED = "Not authorized"


def _authorized(user) -> bool:
    from backend.security.operators import is_operator
    return is_operator(user)


def _admin_call(method, url, json=None, headers=None, timeout=None):
    """Seam for the outbound call — replaced wholesale in tests, so no test can
    reach a real ops API. Never retried: a start whose response was lost is
    checked by reading the list again, and kis-api refuses a second start."""
    import requests
    return requests.request(method, url, json=json, headers=headers, timeout=timeout)


def _upstream(method: str, path: str, json=None):
    """Call kis-api. Returns ``(status, payload, error)``; ``error`` is a string
    with the key scrubbed when the call itself failed."""
    api_key = os.environ.get("KIS_API_KEY", "")
    # `or`, not a get() default: compose declares ${KIS_ADMIN_API_BASE:-}, an
    # empty string rather than an absent variable.
    base = os.environ.get("KIS_ADMIN_API_BASE", "").strip() or _ADMIN_API_BASE
    try:
        resp = _admin_call(method, f"{base.rstrip('/')}{path}", json=json,
                           headers={"X-API-Key": api_key}, timeout=_TIMEOUT_SEC)
        try:
            payload = resp.json()
        except Exception:
            payload = None
        return resp.status_code, payload, None
    except Exception as e:  # noqa: BLE001 - never surface the key in an error
        detail = str(e).replace(api_key, "***") if api_key else str(e)
        logger.error("operator proxy %s %s failed: %s", method, path, detail)
        return None, None, detail


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _describe(run: dict, now: datetime) -> dict:
    """kis-api's row plus what the screen needs: whether it holds the slot and
    where it stands on the 4-week paper gate (same rule as the worker's
    ``promotion_guard.paper_run_qualifies``)."""
    from backend.worker.promotion_guard import PAPER_RUN_MIN, paper_run_qualifies

    started = _parse_ts(run.get("started_at"))
    stopped = _parse_ts(run.get("stopped_at"))
    is_active = bool(run.get("is_active"))
    row = dict(run)
    row["occupying"] = is_active or stopped is None
    row["paper_gate_met"] = paper_run_qualifies(
        SimpleNamespace(started_at=started, stopped_at=stopped, is_active=is_active), now)
    if started is not None:
        end = stopped or now
        row["run_days"] = round(max((end - started).total_seconds(), 0) / 86400, 1)
        row["paper_gate_at"] = (started + PAPER_RUN_MIN).isoformat()
    else:
        row["run_days"] = None
        row["paper_gate_at"] = None
    return row


def _runs():
    status, payload, error = _upstream("GET", "/api/strategies")
    if error is not None:
        return None, f"운영 API에 연결하지 못했습니다: {error}"
    if status != 200 or not isinstance(payload, list):
        reason = (payload or {}).get("error") if isinstance(payload, dict) else None
        return None, reason or f"운영 API 응답 오류 (HTTP {status})"
    now = datetime.utcnow()
    return [_describe(r, now) for r in payload if isinstance(r, dict)], None


@router.get("/strategies")
def list_runs(current_user: User = Depends(get_current_user)):
    if not _authorized(current_user):
        logger.warning("unauthorized operator strategy list by user_id=%s", current_user.id)
        return Resp.err(_NOT_AUTHORIZED)
    runs, error = _runs()
    if error is not None:
        return Resp.err(error)
    return Resp.ok({"runs": runs})


@router.post("/strategies/start")
def start_run(body: dict, current_user: User = Depends(get_current_user)):
    # Authorization first, then the input: an outsider learns nothing about the
    # control's shape (a typed body parameter would answer 422 before this ran).
    if not _authorized(current_user):
        logger.warning("unauthorized operator strategy start by user_id=%s", current_user.id)
        return Resp.err(_NOT_AUTHORIZED)
    try:
        req = OperatorStrategyStart.model_validate(body if isinstance(body, dict) else {})
    except ValidationError as e:
        return Resp.err("입력 오류: " + "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()))

    runs, error = _runs()
    if error is not None:
        return Resp.err(error)
    busy = next((r for r in runs if r["occupying"]), None)
    if busy is not None:
        return Resp.err(f"이미 점유 중인 실행이 있습니다 (run_id={busy['id']}) — 중지 후 시작하세요")

    logger.warning("operator strategy start by user_id=%s: universe=%s size=%s stop=%s",
                   current_user.id, req.universe, req.position_size_pct, req.stop_loss_pct)
    status, payload, error = _upstream("POST", "/api/strategies/start", json={
        "name": req.name,
        "strategy_type": "indicator",
        "broker": "kis",
        "config": {
            "universe": req.universe,
            "position_size_pct": req.position_size_pct,
            "stop_loss_pct": req.stop_loss_pct,
        },
    })
    if error is not None:
        return Resp.err(f"시작 요청 실패: {error} — 목록을 다시 읽어 상태를 확인하세요")
    if status != 201:
        reason = (payload or {}).get("error") if isinstance(payload, dict) else None
        return Resp.err(reason or f"HTTP {status}")
    return Resp.ok(payload)


@router.post("/strategies/stop")
def stop_run(body: dict, current_user: User = Depends(get_current_user)):
    if not _authorized(current_user):
        logger.warning("unauthorized operator strategy stop by user_id=%s", current_user.id)
        return Resp.err(_NOT_AUTHORIZED)
    try:
        req = OperatorStrategyStop.model_validate(body if isinstance(body, dict) else {})
    except ValidationError:
        return Resp.err("입력 오류: run_id(양의 정수)가 필요합니다")

    logger.warning("operator strategy stop by user_id=%s: run_id=%s", current_user.id, req.run_id)
    status, payload, error = _upstream("POST", f"/api/strategies/{req.run_id}/stop")
    if error is not None:
        return Resp.err(f"중지 요청 실패: {error}")
    if status != 200:
        reason = (payload or {}).get("error") if isinstance(payload, dict) else None
        return Resp.err(reason or f"HTTP {status}")
    return Resp.ok(payload)
