import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.core.database import Base

settings = get_settings()

DEPARTMENTS = ("HR", "FINANCE", "TECH", "GENERAL", "EXEC")
CONFIDENTIALITY_LEVELS = ("PUBLIC", "INTERNAL", "CONFIDENTIAL", "SECRET")
CONFIDENTIALITY_RANK = {level: i for i, level in enumerate(CONFIDENTIALITY_LEVELS)}


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    department: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    confidentiality: Mapped[str] = mapped_column(String(32), nullable=False)
    # Denormalized integer rank of `confidentiality`, computed at write time.
    # Lets the retriever filter with a plain "<=" comparison in SQL instead
    # of a CASE expression on every vector query.
    confidentiality_rank: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    minio_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # sha256 of the raw uploaded/watched file bytes, set by app/rag/watcher.py
    # (NULL for hand-typed/manually-uploaded documents) - lets the watcher
    # tell "this filename was already imported" apart from "this filename
    # was already imported AND hasn't changed since", so editing a file
    # already sitting in the watched folder gets picked up on the next scan
    # instead of being silently ignored forever.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim), nullable=False)

    document: Mapped["Document"] = relationship(back_populates="chunks")


class DocumentPage(Base):
    """Non-overlapping, page/section-addressable plain text, kept separate
    from `DocumentChunk` on purpose: RAG chunks overlap by design (better
    semantic recall), which would double-count any exact keyword search run
    against them. This table is the source of truth for search_keyword and
    get_document_section - never touched by the embedding pipeline.

    `page_no` is set for real paginated formats (PDF); `section` is set for
    formats addressed by heading/block instead (DOCX/MD/TXT/XLSX) - see
    app/rag/extract.py for how each format is split. `line_offset` is the
    1-indexed line number, within `text`, that line 1 of this page/section
    corresponds to in the document's original extracted text - lets
    search_keyword report a line number that means something to a human
    re-opening the source file.
    """

    __tablename__ = "document_pages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(255), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    line_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    document: Mapped["Document"] = relationship()
