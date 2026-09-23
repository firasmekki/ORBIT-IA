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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
