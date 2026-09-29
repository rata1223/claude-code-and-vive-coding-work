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


def _report_mismatch() -> None:
    global _mismatch_reported
    if not _mismatch_reported:
        _mismatch_reported = True
        logger.warning(
            "저장된 자격증명이 현재 %s로 복호화되지 않는다 — 키가 바뀌었거나 틀렸다. "
            "자격증명을 다시 입력하거나 이전 키를 복원할 것", _KEY_ENV)


def decrypt(ciphertext: Optional[str]) -> Optional[str]:
    """Plaintext, or None when there is nothing to decrypt or it cannot be.

    A value that does not open under the current key usually means the key was
    rotated or mistyped. The return stays None here; the first such failure in
    the process is logged so the cause is visible (P0-11). Anything that hands
    the value to a broker uses :func:`decrypt_required` instead (#182).
    """
    if not ciphertext:
        return None
    try:
        return get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        _report_mismatch()
        return None
    except Exception:
        return None


class CredentialUnreadable(Exception):
    """A stored credential field does not open under the current key.

    The message names the field only — never the value or the ciphertext — so
    it is safe to return to the caller and to log.
    """

    def __init__(self, field: str):
        self.field = field
        super().__init__(
            f"저장된 KIS 자격증명({field})이 현재 {_KEY_ENV}로 복호화되지 않습니다 — "
            "자격증명을 다시 입력하세요")


def decrypt_required(ciphertext: Optional[str], field: str) -> Optional[str]:
    """Like :func:`decrypt`, but a stored value that does not open **raises**.

    ``decrypt(...) or ""`` turned an unreadable app key into an empty one, and
    the broker was then called with it (#182). An absent value is still None —
    optional fields stay optional; only "stored but unreadable" fails closed.
    """
    if not ciphertext:
        return None
    try:
        return get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        _report_mismatch()
        raise CredentialUnreadable(field) from None


#: Credential column → ``KISCredentials`` field, for every value a KIS client needs.
_KIS_FIELDS = (
    ("app_key_enc", "app_key"),
    ("app_secret_enc", "app_secret"),
    ("account_no_enc", "account_no"),
    ("hts_id_enc", "hts_id"),
)


def kis_credential_fields(cred) -> dict:
    """The decrypted KIS fields of ``cred``, ready for ``KISCredentials(**…)``.

    A field that is not stored comes back as ``""`` (as before); a field that is
    stored but does not open raises :class:`CredentialUnreadable`, so no broker
    call is ever made with a blanked credential. Needs no network.
    """
    return {name: decrypt_required(getattr(cred, column, None), name) or ""
            for column, name in _KIS_FIELDS}


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
