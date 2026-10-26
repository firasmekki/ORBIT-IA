"""Inventory of a server folder source: one row per file (or excluded
directory) the scanner found, whatever happened to it - so no file ever
disappears silently. A supported file links to its Document (whose status
says how far its processing got); anything else keeps the reason it was
not indexed ("format .rar non pris en charge", "fichier vide"...).

Kept separate from `documents` on purpose: an unsupported or unreadable
file has no text and must never reach a retrieval path, and the
documents table is what every retrieval path reads.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# "document": supported, handed to the indexing pipeline (see document_id).
# "ignored": deliberately not indexed - `reason` says why.
# "unreadable": could not be read from disk this scan - retried next scan.
SOURCE_FILE_STATES = ("document", "ignored", "unreadable")


class SourceFile(Base):
    __tablename__ = "source_files"
    __table_args__ = (UniqueConstraint("workspace_id", "relative_path", name="uq_source_files_workspace_path"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Relative to the workspace's source folder, "/"-separated. An excluded
    # directory ends with "/".
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Excluded directory only: how many files it holds (counted, never read),
    # so ORBIT's total can be reconciled with what Windows shows.
    file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
