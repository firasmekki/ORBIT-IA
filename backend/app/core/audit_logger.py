"""Append-only audit trail writer.

Uses its own short-lived DB session (rather than reusing whatever session
the caller has open) so that an audit entry is durably written even if the
request that triggered it later fails or rolls back - a denied access must
never silently disappear because the surrounding transaction was aborted.
"""

import uuid

from app.core.database import SessionLocal
from app.models.audit import AuditLog


def log_event(
    *,
    user_id: uuid.UUID | None,
    username: str | None,
    role: str | None,
    action: str,
    decision: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    reason: str | None = None,
    extra: dict | None = None,
) -> None:
    db = SessionLocal()
    try:
        entry = AuditLog(
            user_id=user_id,
            username=username,
            role=role,
            action=action,
            decision=decision,
            resource_type=resource_type,
            resource_id=resource_id,
            reason=reason,
            extra=extra,
        )
        db.add(entry)
        db.commit()
    finally:
        db.close()
