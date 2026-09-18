"""Director-facing alerts, raised whenever the policy engine refuses an
employee's action - a live feed on top of the audit log, not a replacement
for it (the audit log stays the complete, immutable record; alerts are the
subset a Director actually needs to be pinged about, with read/unread state).

Uses its own short-lived session, same rationale as audit_logger: an alert
must survive even if the request that triggered it later fails.
"""

import uuid

from app.core.database import SessionLocal
from app.models.alert import Alert


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
) -> None:
    db = SessionLocal()
    try:
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
            )
        )
        db.commit()
    finally:
        db.close()
