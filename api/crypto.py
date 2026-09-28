"""Fernet-based field-level encryption for sensitive credential data."""
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_KEY_ENV = "KIS_CREDENTIAL_KEY"

#: How many stored credentials ``validate_key`` test-decrypts at startup. A key
#: that opens none of the first few will open none of the rest.
_CANARY_ROWS = 20


def _get_fernet() -> Fernet:
    raw_key = os.environ.get(_KEY_ENV, "")
    if not raw_key:
        raise RuntimeError(
            f"{_KEY_ENV} environment variable is not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    key = raw_key.strip()
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise RuntimeError(
            f"{_KEY_ENV} is not a valid Fernet key. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ) from exc


_fernet: Optional[Fernet] = None
_mismatch_reported = False


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = _get_fernet()
    return _fernet


def encrypt(plaintext: Optional[str]) -> Optional[str]:
    if not plaintext:
        return None
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: Optional[str]) -> Optional[str]:
    """Plaintext, or None when there is nothing to decrypt or it cannot be.

    A value that does not open under the current key usually means the key was
    rotated or mistyped, and every caller then passes an empty credential to
    the broker — an authentication failure with no visible cause. The return
    stays None (callers are unchanged); the first such failure in the process
    is logged so the cause is visible (P0-11).
    """
    global _mismatch_reported
    if not ciphertext:
        return None
    try:
        return get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        if not _mismatch_reported:
            _mismatch_reported = True
            logger.warning(
                "저장된 자격증명이 현재 %s로 복호화되지 않는다 — 키가 바뀌었거나 틀렸다. "
                "자격증명을 다시 입력하거나 이전 키를 복원할 것", _KEY_ENV)
        return None
    except Exception:
        return None


def validate_key(session_factory=None) -> int:
    """Check the credential key at startup (ROADMAP P0-11). Returns mismatches.

    * **Missing or malformed key** → ``RuntimeError``. The API used to start
      anyway and fail on the first credential request.
    * **A valid key that does not match stored data** (rotated, mistyped) →
      logged CRITICAL with a count, and the number returned. Startup is **not**
      refused: re-entering credentials through this same API is the way out,
      and a refusal would lock the operator out of it. Only counts are logged —
      never ciphertext or values.

    ``session_factory`` is optional; without it only the key itself is checked.
    """
    get_fernet()
    if session_factory is None:
        return 0

    from sqlalchemy import or_

    from api.models import Credential

    # Every encrypted field, not just the app key: the request paths read all
    # of them with ``decrypt(...) or ""``, so a credential whose app key opens
    # but whose secret does not is just as broken.
    columns = [Credential.app_key_enc, Credential.app_secret_enc,
               Credential.account_no_enc, Credential.hts_id_enc,
               Credential.api_key_enc]
    db = session_factory()
    try:
        rows = (db.query(*columns)
                .filter(or_(*(c.isnot(None) for c in columns)))
                .limit(_CANARY_ROWS).all())
    finally:
        db.close()

    fernet = get_fernet()
    bad = 0
    for row in rows:
        for ciphertext in row:
            if not ciphertext:
                continue
            try:
                fernet.decrypt(ciphertext.encode())
            except InvalidToken:
                bad += 1            # one per credential, however many fields fail
                break
    if bad:
        logger.critical(
            "저장된 자격증명 %d/%d건이 현재 %s로 복호화되지 않는다 — 키가 바뀌었거나 틀렸다. "
            "해당 자격증명으로는 브로커 호출이 빈 값으로 나간다. 다시 입력하거나 이전 키를 복원할 것",
            bad, len(rows), _KEY_ENV)
    return bad
