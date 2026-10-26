"""Workspaces: the document source the chat works in.

Every Document belongs to exactly one Workspace. A workspace is an extra
SQL filter ON TOP of RBAC/ABAC, never a substitute for it: a document is
visible only if it is in the caller's current workspace AND its
department/confidentiality is within the caller's grant (see
app/policy/engine.py::document_scope).

- "Général" (DEFAULT_WORKSPACE_ID) holds every document that existed
  before workspaces, plus manual uploads and the watched folder by default.
  Every user may use it.
- Any other workspace has an owner and explicit members, and is usually
  fed by a local folder the owner connected in the browser
  (app/services/workspace_sync.py) - the backend never reads the user's
  disk, the browser sends the files.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Fixed, so it can be the column default and be backfilled into every
# pre-existing document by a plain ADD COLUMN ... DEFAULT.
DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
DEFAULT_WORKSPACE_NAME = "Général"


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_workspaces_owner_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Classification given to files synced from the connected folder (a
    # document can still be reclassified one by one afterwards).
    default_department: Mapped[str] = mapped_column(String(32), nullable=False, default="GENERAL")
    default_confidentiality: Mapped[str] = mapped_column(String(32), nullable=False, default="INTERNAL")
    # "private": owner + explicit members. "organization": every user may use
    # it. Either way each document is still filtered by the reader's own
    # RBAC/ABAC grant - sharing a workspace never shows a FINANCE/SECRET file
    # to someone whose role cannot read it.
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="private", server_default="private")
    # Name of the local folder last connected (display only - never a path).
    folder_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Removed from the app by its owner (DELETE /api/workspaces/{id}): gone
    # for every user, its documents retired - kept in the database for the
    # audit trail. The folder on the owner's computer is never touched.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Server folder source (app/services/source_scanner.py): a folder of the
    # server PC, read by the backend itself through a read-only mount - no
    # browser tab needed. `source_root` is the id of a mount configured in
    # .env (app/services/source_roots.py), `source_path` the folder inside
    # it ("projets" or "projets/BIC"). Both NULL for browser-synced or
    # upload-only workspaces.
    source_root: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Paused: the folder is no longer scanned, its documents stay indexed
    # and searchable.
    source_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    # "idle" | "scanning" | "missing" (folder not reachable - nothing is
    # retired while it is) | "error".
    source_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_scan_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Entries walked by the scan in progress (live progress for the UI).
    source_scan_seen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set by "Analyser maintenant": the scanner loop picks it up within seconds.
    source_scan_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def has_server_source(self) -> bool:
        return self.source_root is not None and self.source_path is not None


class WorkspaceMember(Base):
    """Users other than the owner allowed to work in a workspace. Being a
    member grants NOTHING on its own: each document still goes through the
    member's own RBAC/ABAC grant."""

    __tablename__ = "workspace_members"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
