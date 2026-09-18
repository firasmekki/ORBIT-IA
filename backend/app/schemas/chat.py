import uuid
from datetime import datetime

from pydantic import BaseModel


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


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    message: MessageOut


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime


class ConversationDetail(ConversationSummary):
    messages: list[MessageOut]
