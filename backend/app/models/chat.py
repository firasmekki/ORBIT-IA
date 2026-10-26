import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="Nouvelle conversation")
    # The workspace this conversation searches in - fixed at creation, so a
    # conversation can never drift into another workspace. NULL (older
    # conversations) means the default workspace.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Soft delete only - see routers/chat.py's delete_history. The row (and
    # its messages) are kept for traceability, never hard-deleted; every
    # read endpoint filters deleted_at IS NULL. Deleting history is always
    # audited (AuditLog "HISTORY_DELETED") in the same transaction as this
    # column being set, never a bare UPDATE/DELETE on its own.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # [{document_id, title, department, confidentiality, chunk_excerpt}, ...]
    sources: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # tool call trace for this turn: [{tool, arguments, decision, reason}, ...]
    tool_trace: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Structured result of this turn's deterministic tool call, if any - lets
    # a later turn resolve "la première occurrence" / "ce document" / "le
    # deuxième" / "suite" without re-parsing this message's rendered text
    # (see app/agent/intent.py's follow-up detection and
    # app/agent/orchestrator.py's _resolve_document_section_request). Never
    # exposed to the frontend - internal continuity only. Its "kind" is one
    # of "keyword_search" | "document_choice" | "document_section".
    reference_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Chart spec for this turn, if any (app/agent/chart.py's ChartSpec,
    # dict-encoded) - unlike reference_context, this IS exposed to the
    # frontend (see schemas/chat.py's MessageOut) and rendered by
    # frontend/src/components/ChartBlock.tsx. Only ever built from data a
    # tool already fetched under ACL (search_database) or that the user
    # typed themselves in this same message - never invented.
    chart: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # A structured question back to the user, when the agent needs more
    # information before it can answer (see Clarification.to_dict() in
    # app/agent/chart_dialog.py) - rendered by the frontend as clickable
    # options. NULL for every ordinary answer.
    clarification: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Exact search result (term, total, per-document occurrences with their
    # location and excerpt) - rendered by the frontend as a results panel.
    # NULL for every other answer.
    search_results: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Personal mailbox answer (app/agent/email_turn.py): account, facts copied
    # from the provider and the local model's summaries - never a full email
    # body. Expires at email_results_expires_at (retention.py replaces it and
    # the answer text with a marker). NULL for every other answer.
    email_results: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    email_results_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # direct | general | documents | web | hybrid | email | unavailable (NULL: older rows)
    answer_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
