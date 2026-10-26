import uuid
from datetime import datetime

from pydantic import BaseModel


class DocumentSummary(BaseModel):
    id: uuid.UUID
    title: str
    department: str
    confidentiality: str
    source_filename: str | None
    created_at: datetime
    workspace_id: uuid.UUID
    # Synced workspace files only: path inside the connected folder (never a
    # host path) and indexing state.
    relative_path: str | None = None
    status: str = "indexed"
    index_progress: int | None = None
    indexed_at: datetime | None = None
    # Personal document: the employee it concerns (see Document.subject_user_id).
    subject_user_id: uuid.UUID | None = None
    subject_name: str | None = None
    # Who added it (uploader, or the owner who synced the folder) and how.
    added_by_name: str | None = None
    origin: str | None = None
    # Type of the original file ("image/png", "application/pdf"...), None
    # for a hand-typed document.
    file_mime_type: str | None = None


class DocumentDetail(DocumentSummary):
    content: str


class DocumentCreate(BaseModel):
    title: str
    department: str
    confidentiality: str
    content: str
    # Target workspace - checked server-side; None = the default workspace.
    workspace_id: uuid.UUID | None = None


class DocumentUpdate(BaseModel):
    title: str | None = None
    department: str | None = None
    confidentiality: str | None = None
    # Director only. Sent as null explicitly = "no longer a personal document".
    subject_user_id: uuid.UUID | None = None


class DocumentAccessDenied(BaseModel):
    detail: str = "Accès refusé : vous n'avez pas la permission de consulter ce document."
