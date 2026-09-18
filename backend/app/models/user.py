import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Kept as plain strings (not a DB enum) so adding a role/department later is
# a one-line change to app/policy/rules.py, not a migration.
ROLES = ("DIRECTOR", "HR", "ACCOUNTANT", "DEVELOPER", "EMPLOYEE")


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Per-user grants layered on top of the role's default permissions
    # (RBAC + ABAC overrides), set by a Director from the Admin UI. Kept
    # separate from `role` so the common case (permissions = role) stays a
    # one-line policy table lookup, and overrides are visible/auditable as
    # an explicit, deliberate exception rather than a role redefinition.
    # Both a Python-side default (for ORM objects flushed one at a time)
    # and a server_default (so the column is never NULL regardless of
    # SQLAlchemy's insert strategy - bulk/insertmany flushes have been
    # observed to skip client-side defaults for array columns).
    extra_departments: Mapped[list[str]] = mapped_column(
        ARRAY(String(32)), nullable=False, default=list, server_default=text("'{}'")
    )
    confidentiality_override: Mapped[str | None] = mapped_column(String(32), nullable=True)
    extra_tools: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default=text("'{}'")
    )
