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


class DocumentDetail(DocumentSummary):
    content: str


class DocumentCreate(BaseModel):
    title: str
    department: str
    confidentiality: str
    content: str


class DocumentAccessDenied(BaseModel):
    detail: str = "Accès refusé : vous n'avez pas la permission de consulter ce document."
