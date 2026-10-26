import uuid
from typing import Literal
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.secrets_filter import redact_payload, redact_secrets


class SourceRef(BaseModel):
    # "local": a document of the workspace; "web": a page found by the web
    # search (no document_id) - always shown apart, never mixed up.
    kind: Literal["local", "web"] = "local"
    document_id: uuid.UUID | None = None
    title: str
    department: str = ""
    confidentiality: str = ""
    excerpt: str = ""
    # Synced workspace file: its path inside the connected folder
    # ("RH/procedure.docx") - never a host path.
    relative_path: str | None = None
    page: int | None = None
    citation: str | None = None  # "W1" / "D2", as cited in the answer
    # web only
    url: str | None = None
    domain: str | None = None
    published: str | None = None
    accessed_at: str | None = None
    provider: str | None = None
    fetch_status: str | None = None  # fetched | not_fetched | blocked_robots | http_error | ...
    fetch_note: str | None = None


class ToolTraceEntry(BaseModel):
    tool: str
    arguments: dict
    decision: str  # ALLOW | DENY (permission refused) | ERROR (not found, invalid, unavailable)
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


class LocalFileRef(BaseModel):
    relative_path: str
    name: str


class SkippedFile(BaseModel):
    name: str
    reason: str


class LocalFileContent(BaseModel):
    relative_path: str
    name: str
    content_base64: str


class PendingClientAction(BaseModel):
    """Returned instead of a final answer when the agent needs a local
    file's content it doesn't have yet - the backend never has disk
    access, so the frontend (which holds the real directory handle) must
    execute this and POST the result to /api/chat/resume."""

    action_id: uuid.UUID
    # "read_local_file": read relative_path/name. "search_local_files": read
    # every file in `files` so the backend can search them for `query`.
    tool: str
    relative_path: str | None = None
    name: str | None = None
    query: str | None = None
    files: list[LocalFileRef] | None = None
    # Workspace files left out of the search (size limits) - echoed back on
    # resume so the answer can say which files were not analysed.
    skipped: list[SkippedFile] | None = None


class ImageInput(BaseModel):
    mime_type: str
    data_base64: str = Field(max_length=12 * 1024 * 1024)

    @field_validator("mime_type")
    @classmethod
    def _supported_image_type(cls, value: str) -> str:
        if value not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("Format d’image non pris en charge.")
        return value


class ChatRequest(BaseModel):
    message: str
    image: ImageInput | None = None
    conversation_id: uuid.UUID | None = None
    # The workspace to search in - a claim, checked server-side (the user
    # must be allowed in it); ignored in favour of the conversation's own
    # workspace for an existing conversation (409 if they differ).
    workspace_id: uuid.UUID | None = None
    # Present only when the user has an active local workspace - the
    # frontend sends this lightweight index (metadata only) with every
    # message so the Local File Agent can resolve "lis rapport.pdf"
    # without ever uploading the workspace's actual content.
    workspace_index: list[LocalFileEntry] | None = None
    # Sub-folders of the workspace to work in ("BIC", "Delice CLN"); empty or
    # absent = the whole workspace. Only narrows the search (never a right).
    folder_scope: list[str] | None = Field(default=None, max_length=20)
    # "recursive" (default): those folders and all their sub-folders;
    # "folder": only the documents directly inside them; "all": every
    # workspace this user may use (document search only).
    scope_mode: Literal["folder", "recursive", "all"] = "recursive"
    # The browser's IANA time zone ("Africa/Tunis"), so "aujourd'hui" /
    # "cette semaine" in a mailbox question mean the user's own day.
    # Validated server-side; an unknown value falls back to the default.
    timezone: str | None = Field(default=None, max_length=64)
    # No "where to search" field: documents / web / both is decided by the
    # server for each question (app/websearch/intent.py). A field sent by an
    # older client is ignored (pydantic's default), it cannot force the web.


class ChatResumeRequest(BaseModel):
    """Either ONE file (read_local_file: relative_path/name/content_base64)
    or several (search_local_files: `files`)."""

    conversation_id: uuid.UUID
    action_id: uuid.UUID
    relative_path: str | None = None
    name: str | None = None
    content_base64: str | None = None
    files: list[LocalFileContent] | None = None
    skipped: list[SkippedFile] | None = None
    folder_scope: list[str] | None = Field(default=None, max_length=20)
    scope_mode: Literal["folder", "recursive", "all"] = "recursive"


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    sources: list[SourceRef] = []
    tool_trace: list[ToolTraceEntry] = []
    chart: dict | None = None
    # {"type": "clarification", "status": "need_more_information", "message",
    #  "questions": [...], "options": [{"label", "value"}]} - None otherwise.
    clarification: dict | None = None
    # {"type": "keyword_search", "term", "total", "document_count",
    #  "workspace", "documents": [{..., "count", "occurrences": [...]}]}
    search_results: dict | None = None
    # {"type": "email_results", "account", "query", "items": [{facts, ai,
    #  deadlines, suggestions, status}], "report", "notices"} or
    #  {"type": "email_results", "expired": true} - None otherwise.
    email_results: dict | None = None
    # direct | general | documents | web | hybrid | email | unavailable (None: older messages)
    answer_mode: str | None = None
    created_at: datetime

    # Every message leaving the API - new or read back from the history - has
    # its credentials masked (app/core/secrets_filter.py). The stored rows are
    # not modified.
    @model_validator(mode="after")
    def _mask_secrets(self):
        self.content = redact_secrets(self.content)
        for source in self.sources:
            source.excerpt = redact_secrets(source.excerpt)
        if self.search_results:
            self.search_results = redact_payload(self.search_results)
        if self.email_results:
            self.email_results = redact_payload(self.email_results)
        return self

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
    workspace_id: uuid.UUID | None = None


class ConversationDetail(ConversationSummary):
    messages: list[MessageOut]


class DeleteHistoryResult(BaseModel):
    deleted_conversation_count: int
    deleted_message_count: int
    audit_log_id: uuid.UUID | None
