import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.core.database import Base
from app.models.workspace import DEFAULT_WORKSPACE_ID

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
    # The workspace this document belongs to (app/models/workspace.py) - a
    # mandatory SQL filter on every retrieval path, on top of RBAC/ABAC.
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
        default=DEFAULT_WORKSPACE_ID,
        server_default=text(f"'{DEFAULT_WORKSPACE_ID}'::uuid"),
    )
    # Folder sync (app/services/workspace_sync.py): path relative to the
    # connected folder ("RH/procedure.docx" - never an absolute host path),
    # NULL for manual uploads. Unique per workspace (see migrations).
    relative_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_last_modified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "indexing" (text stored and exact-searchable, embeddings being computed
    # in the background - see app/services/embedding_worker.py) | "indexed" |
    # "removed" (file deleted from the synced folder).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="indexed", server_default="indexed")
    # 0-100 while status == "indexing": share of chunks already embedded.
    index_progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Why the last extraction/embedding attempt failed (status "failed", or
    # a transient error still being retried) - shown to the user, never
    # hidden in the logs only. Cleared by a successful pass or a retry.
    index_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Failed attempts of the current version: a file that keeps crashing the
    # extractor or the embedding service is marked "failed" after
    # MAX_INDEX_ATTEMPTS instead of blocking the queue forever.
    index_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    # The kind of problem behind status "failed"/"no_text" (rag.extract
    # ExtractionError.kind: no_text_image, no_text_scan, ocr_failed, corrupt,
    # empty; "embedding"), or how the text was obtained when it is not plain
    # extraction ("visual_description": written by the vision model).
    issue_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # False once the file left the synced folder: invisible to every
    # retrieval path (document_scope), kept for audit/traceability.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    # The employee this document is about (payslip, leave record, personal
    # tasks...). They always see it; colleagues never do through their role
    # alone - only the managers of its department (rules.PERSONAL_DOC_READERS)
    # or someone a Director explicitly allowed (DocumentAccessRule).
    subject_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # "sync" (a file of the folder synced from the server PC) | "upload"
    # (dropped from any PC through the app) | NULL (older documents). Only
    # synced files are retired when they leave the server's folder - an
    # upload does not exist on that disk and must never be "removed" by it.
    origin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    subject: Mapped["User | None"] = relationship("User", foreign_keys=[subject_user_id], lazy="selectin")  # noqa: F821
    added_by: Mapped["User | None"] = relationship("User", foreign_keys=[owner_id], lazy="selectin")  # noqa: F821

    @property
    def subject_name(self) -> str | None:
        return self.subject.full_name if self.subject is not None else None

    @property
    def file_mime_type(self) -> str | None:
        """The original file's type: as uploaded, else from its extension
        (older documents) - decides how the app previews it."""
        if self.mime_type:
            return self.mime_type
        if self.source_filename:
            import mimetypes

            return mimetypes.guess_type(self.source_filename)[0]
        return None

    @property
    def added_by_name(self) -> str | None:
        """Who added it: the uploader, or the owner who synced the folder."""
        return self.added_by.full_name if self.added_by is not None else None


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim), nullable=False)
    # Which DocumentPage this chunk's text starts within, in the same
    # flattened-text character space app.rag.extract's _assemble() and
    # app.rag.ingest's chunk_text() both work in - NULL when the position
    # couldn't be exactly verified (see chunk_text's self-check) or for
    # chunks ingested before this column existed. Never a guess: a chunk
    # is only linked once its exact character span is confirmed to
    # reproduce its own text and to fall inside a single page's range.
    document_page_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_pages.id", ondelete="SET NULL"), nullable=True, index=True
    )

    document: Mapped["Document"] = relationship(back_populates="chunks")
    document_page: Mapped["DocumentPage | None"] = relationship()


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
