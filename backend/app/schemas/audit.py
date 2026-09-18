import uuid
from datetime import datetime

from pydantic import BaseModel


class AuditLogOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    username: str | None
    role: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    decision: str
    reason: str | None
    extra: dict | None
    created_at: datetime


class AuditLogPage(BaseModel):
    items: list[AuditLogOut]
    total: int
