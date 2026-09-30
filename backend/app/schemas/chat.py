import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator


class SourceRef(BaseModel):
    document_id: uuid.UUID
    title: str
    department: str
    confidentiality: str
    excerpt: str


class ToolTraceEntry(BaseModel):
    tool: str
    arguments: dict
    decision: str  # ALLOW | DENY
    reason: str


class ChatRequest(BaseModel):
    message: str
    conversation_id: uuid.UUID | None = None


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    sources: list[SourceRef] = []
    tool_trace: list[ToolTraceEntry] = []
    created_at: datetime

    # User messages have NULL sources/tool_trace in the DB (nullable
    # columns, never set for role="user") - the `= []` default above only
    # applies when the field is *missing*, not when the ORM attribute is
    # present but None, so loading any saved conversation via
    # GET /api/conversations/{id} (from_attributes) crashed on its first
    # user message.
    @field_validator("sources", "tool_trace", mode="before")
    @classmethod
    def _null_to_empty_list(cls, value):
        return value if value is not None else []


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    message: MessageOut


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime


class ConversationDetail(ConversationSummary):
    messages: list[MessageOut]
