"""Encryption of mailbox tokens at rest (Fernet).

EMAIL_TOKEN_ENCRYPTION_KEY holds one or more Fernet keys, comma-separated:
the first encrypts, every key decrypts (MultiFernet), so a key can be
rotated without losing the connections made with the previous one.

Fernet has no associated data, so the payload carries its own context
("connection id | user id | provider") and decryption refuses a token whose
context differs: a ciphertext copied onto another row (another user) is
unreadable there.
"""

import base64
import binascii
import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings


class CryptoUnavailable(Exception):
    """No valid key configured - mailboxes stay disabled."""


def _keys() -> list[bytes]:
    raw = get_settings().email_token_encryption_key or ""
    return [k.strip().encode() for k in raw.split(",") if k.strip()]


def key_issues() -> list[str]:
    """Readiness diagnostics; never includes a key value."""
    keys = _keys()
    if not keys:
        return ["EMAIL_TOKEN_ENCRYPTION_KEY manquant (clé Fernet requise)"]
    for key in keys:
        try:
            if len(base64.urlsafe_b64decode(key)) != 32:
                raise ValueError
            Fernet(key)
        except (ValueError, binascii.Error):
            return ["EMAIL_TOKEN_ENCRYPTION_KEY invalide (clé Fernet attendue : Fernet.generate_key())"]
    return []


def _fernet() -> MultiFernet:
    if key_issues():
        raise CryptoUnavailable()
    return MultiFernet([Fernet(k) for k in _keys()])


def context(connection_id, user_id, provider: str) -> str:
    return f"{connection_id}|{user_id}|{provider}"


def encrypt(ctx: str, data: dict) -> str:
    payload = json.dumps({"v": 1, "ctx": ctx, "data": data}, separators=(",", ":"))
    return _fernet().encrypt(payload.encode()).decode()


def decrypt(ctx: str, token: str) -> dict:
    """Raises ValueError if the token is unreadable or bound to another row."""
    try:
        payload = json.loads(_fernet().decrypt(token.encode()))
    except (InvalidToken, ValueError, TypeError):
        raise ValueError("unreadable") from None
    if not isinstance(payload, dict) or payload.get("ctx") != ctx or not isinstance(payload.get("data"), dict):
        raise ValueError("context mismatch")
    return payload["data"]
