"""Director-facing alerts, raised whenever the policy engine refuses an
employee's action - a live feed on top of the audit log, not a replacement
for it (the audit log stays the complete, immutable record; alerts are the
subset a Director actually needs to be pinged about, with read/unread state).

Uses its own short-lived session, same rationale as audit_logger: an alert
must survive even if the request that triggered it later fails.

Deduplication: the same refusal repeated (a resent message, a retried tool
call) is still audited every time, but raises one alert, not one per
attempt. Callers pass a `dedup_key` (see security_event_key) and an alert
with that key created within DEDUP_WINDOW suppresses the new one. A
transaction-scoped advisory lock on the key serializes two concurrent
requests for the same event, so they cannot both pass the check.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.database import SessionLocal
from app.models.alert import Alert

DEDUP_WINDOW = timedelta(minutes=10)


def security_event_key(*parts: object) -> str:
    """Stable 64-char key for one security event, e.g. (user_id,
    alert_type, conversation_id, tool, reason, question). Whitespace and
    case in free-text parts are folded so a retried message still matches."""
    canonical = "\x1f".join(" ".join(str(p if p is not None else "").split()).lower() for p in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_alert(
    *,
    alert_type: str,
    user_id: uuid.UUID | None,
    username: str | None,
    role: str | None,
    title: str,
    description: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    dedup_key: str | None = None,
) -> bool:
    """Returns True if an alert was created, False if it was a duplicate."""
    db = SessionLocal()
    try:
        if dedup_key:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": dedup_key})
            since = datetime.now(timezone.utc) - DEDUP_WINDOW
            duplicate = (
                db.query(Alert.id).filter(Alert.dedup_key == dedup_key, Alert.created_at >= since).first()
            )
            if duplicate is not None:
                db.rollback()
                return False
        db.add(
            Alert(
                alert_type=alert_type,
                user_id=user_id,
                username=username,
                role=role,
                title=title,
                description=description,
                resource_type=resource_type,
                resource_id=resource_id,
                dedup_key=dedup_key,
            )
        )
        db.commit()
        return True
    finally:
        db.close()
