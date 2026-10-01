import uuid
from datetime import datetime

from pydantic import BaseModel


class AlertOut(BaseModel):
    id: uuid.UUID
    alert_type: str
    user_id: uuid.UUID | None
    username: str | None
    role: str | None
    title: str
    description: str
    resource_type: str | None
    resource_id: str | None
    is_read: bool
    read_at: datetime | None
    created_at: datetime
    audit_log_id: uuid.UUID | None = None


class AlertPage(BaseModel):
    items: list[AlertOut]
    total: int
    unread_count: int
