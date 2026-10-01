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


class LocalFileEntry(BaseModel):
    """One entry of the lightweight workspace index the frontend builds by
    walking the user's chosen FileSystemDirectoryHandle - metadata only,
    never content. See app/agent/local_files.py."""

    name: str
    relative_path: str
    extension: str
    size: int
    modified_at: datetime | None = None
    parent_folder: str | None = None


class PendingClientAction(BaseModel):
    """Returned instead of a final answer when the agent needs a local
    file's content it doesn't have yet - the backend never has disk
    access, so the frontend (which holds the real directory handle) must
    execute this and POST the result to /api/chat/resume."""

    action_id: uuid.UUID
    tool: str
    relative_path: str
    name: str


class ChatRequest(BaseModel):
    message: str
    conversation_id: uuid.UUID | None = None
    # Present only when the user has an active local workspace - the
    # frontend sends this lightweight index (metadata only) with every
    # message so the Local File Agent can resolve "lis rapport.pdf"
    # without ever uploading the workspace's actual content.
    workspace_index: list[LocalFileEntry] | None = None


class ChatResumeRequest(BaseModel):
    conversation_id: uuid.UUID
    action_id: uuid.UUID
    relative_path: str
    name: str
    content_base64: str


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    sources: list[SourceRef] = []
    tool_trace: list[ToolTraceEntry] = []
    chart: dict | None = None
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
    # Exactly one of the two is set: `message` for a completed turn,
    # `pending_client_action` when the agent needs a local file's content
    # from the frontend before it can answer - nothing is persisted to
    # the DB for a pending turn (see routers/chat.py::chat), so `message`
    # has no row to point to yet.
    message: MessageOut | None = None
    pending_client_action: PendingClientAction | None = None


class ConversationSummary(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime


class ConversationDetail(ConversationSummary):
    messages: list[MessageOut]


class DeleteHistoryResult(BaseModel):
    deleted_conversation_count: int
    deleted_message_count: int
    audit_log_id: uuid.UUID | None
