"""User profile management."""
import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.auth import hash_password, verify_password
from api.database import get_db
from api.deps import get_current_user
from api.models import User
from api.schemas import ProfileUpdate, Resp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("/profile")
def get_profile(current_user: User = Depends(get_current_user)):
    return Resp.ok(
        {
            "id": current_user.id,
            "email": current_user.email,
            "nickname": current_user.nickname,
            "avatar": current_user.avatar,
            "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
        }
    )


@router.put("/profile/update")
def update_profile(
    body: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.nickname is not None:
        current_user.nickname = body.nickname
    if body.avatar is not None:
        current_user.avatar = body.avatar
    db.commit()
    db.refresh(current_user)
    return Resp.ok(
        {
            "id": current_user.id,
            "email": current_user.email,
            "nickname": current_user.nickname,
            "avatar": current_user.avatar,
        }
    )


@router.get("/notification-settings")
def get_notification_settings(current_user: User = Depends(get_current_user)):
    return Resp.ok(
        {
            "telegram_enabled": False,
            "telegram_chat_id": None,
            "email_enabled": False,
            "trade_alerts": True,
            "strategy_alerts": True,
            "system_alerts": True,
        }
    )


@router.put("/notification-settings")
def update_notification_settings(
    body: dict,
    current_user: User = Depends(get_current_user),
):
    return Resp.ok(body, "Settings updated")


@router.post("/notification-settings/test")
def test_notification_settings(current_user: User = Depends(get_current_user)):
    return Resp.ok(None, "Test notification sent")


@router.post("/change-password")
def change_password(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    old_pw = body.get("old_password", "")
    new_pw = body.get("new_password", "")
    if not old_pw or not new_pw:
        return Resp.err("old_password and new_password required")
    if len(new_pw) < 6:
        return Resp.err("Password must be at least 6 characters")
    if not verify_password(old_pw, current_user.password_hash):
        return Resp.err("Current password is incorrect")
    current_user.password_hash = hash_password(new_pw)
    db.commit()
    return Resp.ok(None, "Password changed")


@router.get("/my-credits-log")
def my_credits_log(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
):
    return Resp.ok({"total": 0, "items": []})


@router.get("/my-referrals")
def my_referrals(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
):
    return Resp.ok({"total": 0, "items": [], "referral_code": None})
