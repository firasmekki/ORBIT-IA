import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ALERT_TYPES = (
    "CHAT_ACCESS_DENIED",
    "DOCUMENT_ACCESS_DENIED",
    "HISTORY_CONVERSATION_DELETED",
    "HISTORY_ALL_DELETED",
)


class Alert(Base):
    """A Director-facing notification raised whenever an employee's action
    is refused by the policy engine - so a manager can see who is probing
    outside their access, not just find it buried in the audit log."""

    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # Links a notification back to the immutable AuditLog row that proves
    # it (e.g. HISTORY_DELETED's own deletion record) - set once at insert
    # time, never updated. Nullable: CHAT_ACCESS_DENIED/DOCUMENT_ACCESS_DENIED
    # predate this column and have no corresponding audit row to point at.
    audit_log_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
