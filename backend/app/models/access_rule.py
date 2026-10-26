import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ACCESS_EFFECTS = ("allow", "deny")


class DocumentAccessRule(Base):
    """A Director's explicit exception for ONE user, on one document or on a
    whole folder of a synced workspace: "allow" opens it whatever the
    user's role, "deny" hides it whatever the role (deny always wins).
    Evaluated inside the same SQL filter as every read
    (app/policy/engine.document_scope) - never by the LLM."""

    __tablename__ = "document_access_rules"
    __table_args__ = (
        CheckConstraint("effect IN ('allow', 'deny')", name="ck_document_access_rules_effect"),
        CheckConstraint(
            "(document_id IS NOT NULL) <> (folder_path IS NOT NULL)", name="ck_document_access_rules_one_target"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Folder of the synced tree, always stored with a trailing "/" ("Finance/"),
    # so "Finance/" never matches "Finances2/...".
    folder_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    effect: Mapped[str] = mapped_column(String(8), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RestrictedFolder(Base):
    """A folder shared "like Dropbox": once restricted, its documents are no
    longer visible through roles - only to the people it is shared with
    (DocumentAccessRule "allow" on it or on a document in it), to the
    workspace owner and to the Director. Who can then open a given file is
    still decided in the one SQL filter (engine.document_scope)."""

    __tablename__ = "restricted_folders"
    __table_args__ = (UniqueConstraint("workspace_id", "folder_path", name="uq_restricted_folders_workspace_path"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Trailing "/" like DocumentAccessRule.folder_path.
    folder_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
