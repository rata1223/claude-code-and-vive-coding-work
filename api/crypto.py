"""Fernet-based field-level encryption for sensitive credential data."""
import os
from typing import Optional

from cryptography.fernet import Fernet

_KEY_ENV = "KIS_CREDENTIAL_KEY"


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
