"""Fernet-based field-level encryption for sensitive credential data."""
import os
import base64
from typing import Optional

from cryptography.fernet import Fernet

_KEY_ENV = "KIS_CREDENTIAL_KEY"


def _get_fernet() -> Fernet:
    raw_key = os.environ.get(_KEY_ENV, "")
    if not raw_key:
        # Deterministic dev-only fallback — 32 bytes exactly.
        # Production MUST set KIS_CREDENTIAL_KEY to a real Fernet key.
        dev_bytes = b"dev-cred-key-32b"  # 16 bytes, padded to 32
        dev_key = base64.urlsafe_b64encode(dev_bytes.ljust(32, b"\x00"))
        return Fernet(dev_key)
    key = raw_key.strip()
    # Accept raw 32-byte hex strings or proper Fernet keys
    if len(key) == 44:
        return Fernet(key.encode())
    # Try to pad/convert
    try:
        decoded = base64.urlsafe_b64decode(key + "==")
        if len(decoded) == 32:
            return Fernet(base64.urlsafe_b64encode(decoded))
    except Exception:
        pass
    return Fernet(base64.urlsafe_b64encode(key[:32].encode().ljust(32, b"\x00")))


_fernet: Optional[Fernet] = None


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
    if not ciphertext:
        return None
    try:
        return get_fernet().decrypt(ciphertext.encode()).decode()
    except Exception:
        return None
