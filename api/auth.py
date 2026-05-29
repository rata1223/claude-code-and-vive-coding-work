import os
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from jose import JWTError, jwt

SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "change-me-in-production-use-a-long-random-string")
ALGORITHM = "HS256"

_DEFAULT_SECRET = "change-me-in-production-use-a-long-random-string"
_IS_PROD = os.environ.get("ENV", os.environ.get("FLASK_ENV", "")).lower() == "production"
if SECRET_KEY == _DEFAULT_SECRET and _IS_PROD:
    import logging as _log
    import sys as _sys
    _log.getLogger(__name__).critical(
        "JWT_SECRET_KEY is the default insecure placeholder — refusing to start in production. "
        "Set JWT_SECRET_KEY to a cryptographically random value of at least 32 characters."
    )
    _sys.exit(1)
elif len(SECRET_KEY) < 32:
    import logging as _log
    _log.getLogger(__name__).warning("JWT_SECRET_KEY is shorter than 32 characters — security risk.")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "10080"))  # 7 days


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(user_id: int, email: str, expires_delta: Optional[timedelta] = None) -> str:
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    payload = {"sub": str(user_id), "email": email, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None
