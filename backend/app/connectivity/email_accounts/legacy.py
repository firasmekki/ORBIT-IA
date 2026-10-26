"""One-way, idempotent copy of the first Gmail connector's accounts
(runtime_settings["gmail_accounts"]) into email_connections.

Nothing is deleted or rewritten in runtime_settings: the old row stays as a
fallback until an administrator removes it on purpose. An account that had
also authorized gmail.send is NOT given its tokens back: it is created as
"authorization_required", so its owner reconnects with the read-only scope
(the broader grant is never used by ORBIT again).
"""

import base64
import hashlib
import logging
import uuid
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectivity.email_accounts import crypto
from app.connectivity.email_accounts.providers.gmail import READ_SCOPE
from app.core.config import get_settings
from app.models.email_connection import EmailConnection
from app.models.runtime_setting import RuntimeSetting
from app.models.user import User

logger = logging.getLogger("orbitia.email")
LEGACY_KEY = "gmail_accounts"
LEGACY_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def _legacy_fernet() -> Fernet:
    # Exactly how the first connector derived its key.
    s = get_settings()
    secret = (s.gmail_token_encryption_key or s.jwt_secret).encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"orbit-gmail-tokens|" + secret).digest()))


def migrate_legacy_gmail_accounts(db: Session) -> dict:
    counts = {"migrated": 0, "reauthorization_required": 0, "skipped": 0}
    row = db.get(RuntimeSetting, LEGACY_KEY)
    if row is None or not isinstance(row.value, dict) or not row.value:
        return counts
    if crypto.key_issues():
        logger.warning("legacy Gmail accounts not migrated: EMAIL_TOKEN_ENCRYPTION_KEY missing or invalid")
        counts["skipped"] = sum(len(v) for v in row.value.values() if isinstance(v, dict))
        return counts
    for user_key, accounts in row.value.items():
        try:
            user_id = uuid.UUID(str(user_key))
        except ValueError:
            counts["skipped"] += 1
            continue
        if not isinstance(accounts, dict) or db.get(User, user_id) is None:
            counts["skipped"] += len(accounts) if isinstance(accounts, dict) else 1
            continue
        for legacy_id, account in accounts.items():
            email = str((account or {}).get("email") or "").strip()
            if "@" not in email:
                counts["skipped"] += 1
                continue
            exists = db.scalars(select(EmailConnection).where(
                EmailConnection.user_id == user_id, EmailConnection.provider == "gmail",
                EmailConnection.external_account_id == email.lower(), EmailConnection.disconnected_at.is_(None),
            )).first()
            if exists is not None:
                continue
            scopes = str(account.get("scope") or "").split()
            conn = EmailConnection(
                id=uuid.uuid4(), user_id=user_id, provider="gmail", auth_method="oauth2", email_address=email,
                external_account_id=email.lower(), granted_scopes=[s for s in scopes if s == READ_SCOPE],
                is_enabled=bool(account.get("enabled", True)), legacy_account_id=str(legacy_id)[:64],
                is_primary=bool(account.get("primary")) and not db.scalars(select(EmailConnection).where(
                    EmailConnection.user_id == user_id, EmailConnection.is_primary.is_(True),
                    EmailConnection.disconnected_at.is_(None))).first(),
                connected_at=datetime.now(UTC),
            )
            tokens = None
            if READ_SCOPE in scopes and LEGACY_SEND_SCOPE not in scopes:
                try:
                    f = _legacy_fernet()
                    tokens = {
                        "access_token": f.decrypt(account["token"].encode()).decode(),
                        "refresh_token": f.decrypt(account["refresh_token"].encode()).decode(),
                        "expires_at": account.get("expires_at") or datetime.now(UTC).isoformat(),
                    }
                except (InvalidToken, KeyError, ValueError, AttributeError):
                    tokens = None
            if tokens and tokens["refresh_token"]:
                conn.secret_ciphertext = crypto.encrypt(crypto.context(conn.id, user_id, "gmail"), tokens)
                conn.access_token_expires_at = datetime.fromisoformat(tokens["expires_at"])
                conn.status = "connected"
                counts["migrated"] += 1
            else:
                conn.status = "authorization_required"
                conn.last_error_code = "permission_missing"
                counts["reauthorization_required"] += 1
            db.add(conn)
            db.commit()
    if any(counts.values()):
        logger.info("legacy Gmail accounts: %s", counts)  # counts only, no address
    return counts
