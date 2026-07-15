from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from api.auth import create_access_token, hash_password, verify_password
from api.compat import CompatLoginRequest
from api.database import get_db
from api.deps import get_current_user
from api.models import User
from api.schemas import (
    ChangePasswordRequest,
    RegisterRequest,
    Resp,
)

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register")
@limiter.limit("5/minute")
def register(request: Request, body: RegisterRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        return Resp.err("Email already registered", code=-1)
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        nickname=body.nickname or body.email.split("@")[0],
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id, user.email)
    return Resp.ok({"token": token, "user_id": user.id, "email": user.email})


@router.post("/login")
@limiter.limit("5/minute")
def login(request: Request, body: CompatLoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.password_hash):
        return Resp.err("Invalid email or password", code=-1)
    token = create_access_token(user.id, user.email)
    return Resp.ok({"token": token, "user_id": user.id, "email": user.email})


@router.post("/logout")
def logout(current_user: User = Depends(get_current_user)):
    # JWT is stateless; client drops the token.
    return Resp.ok(None, "Logged out")


@router.get("/info")
def get_info(current_user: User = Depends(get_current_user)):
    return Resp.ok(
        {
            "id": current_user.id,
            "email": current_user.email,
            "nickname": current_user.nickname,
            "avatar": current_user.avatar,
            "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
        }
    )


@router.get("/security-config")
def security_config():
    return Resp.ok({"email_verification": False, "captcha": False})


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(body.old_password, current_user.password_hash):
        return Resp.err("Current password is incorrect", code=-1)
    current_user.password_hash = hash_password(body.new_password)
    db.commit()
    return Resp.ok(None, "Password changed")


@router.post("/send-code")
def send_code(request: Request):
    # No email verification required by spec
    return Resp.ok(None, "Code sent (not implemented)")


@router.post("/reset-password")
def reset_password(request: Request):
    return Resp.ok(None, "Not implemented")
