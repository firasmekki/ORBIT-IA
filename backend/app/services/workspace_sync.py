"""Synchronising a local folder into a workspace.

The backend never reads the user's disk. The browser - which holds the
FileSystemDirectoryHandle the user explicitly granted - lists the folder,
hashes each file, and:
  1. sends the manifest (relative paths + SHA-256 + size + mtime) to
     `plan_sync`, which answers which files need uploading and retires the
     documents whose file left the folder;
  2. uploads each file to index to `index_file`, which RE-COMPUTES the
     SHA-256 itself (the client's hash only decides what to send - it is
     never trusted for what gets stored), extracts the text, stores the
     original in object storage and runs the normal ingestion pipeline
     (chunks + embeddings + pages) - the same one manual uploads use.

Idempotent: an unchanged file (same SHA-256) is never re-indexed, and one
document per (workspace, relative_path) is enforced by a unique index.
Only the workspace owner may sync it. Synced documents get the
workspace's default classification; RBAC/ABAC then applies to them exactly
like to any other document.
"""

import hashlib
import mimetypes
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.agent.local_files import is_safe_relative_path
from app.core.audit_logger import log_event
from app.core.storage import upload_file
from app.models.document import CONFIDENTIALITY_RANK, Document
from app.models.user import User
from app.models.workspace import Workspace
from app.policy.engine import check_document_access
from app.policy.rules import resolve_effective_grant
from app.policy.workspace import can_manage_workspace
from app.rag.extract import ExtractionError, UnsupportedFileTypeError, extract
from app.rag.ingest import write_pages
from app.services.errors import ServiceError, denied, invalid
from app.services.personal_documents import personal_subject_for_path

SYNC_EXTENSIONS = {"pdf", "docx", "pptx", "xlsx", "xls", "csv", "txt", "md", "jpg", "jpeg", "png", "webp"}
# The text is stored at once; embeddings are computed in the background
# (app/services/embedding_worker.py), so a large file no longer has to be
# indexed inside one HTTP request. 50 MB of CSV is ~100 000 chunks, ~25 min
# of background embedding on this machine; the reverse proxy accepts 60 MB.
MAX_SYNC_FILE_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class ManifestEntry:
    relative_path: str
    sha256: str
    size: int
    last_modified: datetime | None = None


def extension_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def validate_relative_path(path: str) -> str:
    """A path relative to the connected folder, '/'-separated - never an
    absolute or parent-escaping path (no "C:\\...", no "/Users/...")."""
    path = (path or "").replace("\\", "/").strip()
    if not path or len(path) > 1024 or path.startswith("/") or ":" in path or not is_safe_relative_path(path):
        raise invalid("chemin de fichier invalide")
    return path


def size_limit(path: str) -> int:
    return MAX_SYNC_FILE_BYTES


def too_large_reason(path: str) -> str:
    return f"fichier trop volumineux (max {MAX_SYNC_FILE_BYTES // (1024 * 1024)} Mo)"


def _require_owner(user: User, workspace: Workspace) -> None:
    if not can_manage_workspace(user, workspace):
        log_event(
            user_id=user.id, username=user.username, role=user.role, action="WORKSPACE_SYNC", decision="DENY",
            resource_type="workspace", resource_id=str(workspace.id), reason="only the workspace owner may sync it",
        )
        raise denied("Seul le propriétaire de cet espace de travail peut le synchroniser.")


def plan_sync(
    db: Session,
    user: User,
    workspace: Workspace,
    manifest: list[ManifestEntry],
    folder_name: str | None,
    excluded_directories: list[str] | None = None,
) -> dict:
    _require_owner(user, workspace)
    accepted: dict[str, ManifestEntry] = {}
    rejected: list[dict] = []
    for entry in manifest:
        try:
            path = validate_relative_path(entry.relative_path)
        except ServiceError:
            rejected.append({"relative_path": entry.relative_path, "reason": "chemin invalide"})
            continue
        if extension_of(path) not in SYNC_EXTENSIONS:
            rejected.append({"relative_path": path, "reason": f"format .{extension_of(path) or '?'} non pris en charge"})
        elif entry.size > size_limit(path):
            rejected.append({"relative_path": path, "reason": too_large_reason(path)})
        else:
            accepted[path] = entry

    existing = {
        d.relative_path: d
        for d in db.query(Document).filter(Document.workspace_id == workspace.id, Document.relative_path.isnot(None))
    }
    to_upload: list[str] = []
    unchanged: list[str] = []
    for path, entry in accepted.items():
        doc = existing.get(path)
        if doc is not None and doc.is_active and doc.content_hash and doc.content_hash == entry.sha256.lower():
            unchanged.append(path)
        else:
            to_upload.append(path)

    excluded_prefixes: list[str] = []
    for prefix in excluded_directories or []:
        try:
            excluded_prefixes.append(validate_relative_path(prefix).rstrip("/") + "/")
        except ServiceError:
            continue

    removed: list[str] = []
    for path, doc in existing.items():
        # A file dropped from another PC ("upload") is not on the server's
        # disk: its absence from the folder means nothing.
        in_excluded_directory = any(path.startswith(prefix) for prefix in excluded_prefixes)
        unsupported_existing_type = extension_of(path) not in SYNC_EXTENSIONS
        if path not in accepted and not in_excluded_directory and not unsupported_existing_type and doc.is_active and doc.origin != "upload":
            # Left the folder: retired from every retrieval path
            # (document_scope requires is_active), kept for traceability.
            doc.is_active = False
            doc.status = "removed"
            removed.append(path)
            log_event(
                user_id=user.id, username=user.username, role=user.role, action="WORKSPACE_FILE_REMOVED",
                decision="ALLOW", resource_type="document", resource_id=str(doc.id), reason="file left the synced folder",
                extra={"workspace_id": str(workspace.id), "relative_path": path},
            )

    if folder_name:
        workspace.folder_name = folder_name[:255]
    workspace.last_synced_at = datetime.now(UTC)
    db.commit()
    return {
        "to_upload": sorted(to_upload),
        "unchanged": sorted(unchanged),
        "removed": sorted(removed),
        "rejected": rejected,
    }


def index_file(
    db: Session,
    user: User,
    workspace: Workspace,
    relative_path: str,
    content: bytes,
    *,
    last_modified: datetime | None = None,
    mime_type: str | None = None,
    defer_extraction: bool = False,
) -> tuple[Document, str]:
    """(document, "created" | "updated" | "unchanged" | "reactivated").

    With `defer_extraction`, the file is only stored and queued (status
    "queued"): the request returns in a fraction of a second and the text
    (pypdf, Tesseract OCR - minutes on a scanned binder) is extracted by
    app/services/extraction_worker.py, several files at a time. Until then
    an updated document keeps its previous text."""
    _require_owner(user, workspace)
    path = validate_relative_path(relative_path)
    filename = path.rsplit("/", 1)[-1]
    if extension_of(path) not in SYNC_EXTENSIONS:
        raise invalid(f"type de fichier non pris en charge : {filename}")
    if not content:
        raise invalid(f"le fichier « {filename} » est vide")
    if len(content) > size_limit(path):
        raise invalid(f"« {filename} » : {too_large_reason(path)}")

    sha256 = hashlib.sha256(content).hexdigest()
    # Row-locked and re-read from the database: the extraction worker may
    # have just claimed this document ("extracting"), and a stale status
    # here would leave the new version stuck instead of queued again.
    doc = (
        db.query(Document)
        .filter(Document.workspace_id == workspace.id, Document.relative_path == path)
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if doc is not None and doc.is_active and doc.content_hash == sha256:
        return doc, "unchanged"

    if doc is None:
        # The owner files new documents at the workspace's default level -
        # which they must be able to read themselves (same rule as uploads).
        grant = resolve_effective_grant(user)
        decision = check_document_access(
            grant=grant, department=workspace.default_department, confidentiality=workspace.default_confidentiality
        )
        if not decision.allowed:
            raise denied("La classification par défaut de cet espace dépasse vos propres droits.")

    extraction = None
    if not defer_extraction:
        try:
            extraction = extract(filename, content)
        except (UnsupportedFileTypeError, ExtractionError) as exc:
            raise invalid(f"« {filename} » : {exc}") from None

    object_key = f"workspaces/{workspace.id}/{uuid.uuid4()}-{filename}"
    upload_file(object_key, content, content_type=mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream")

    subject = None
    if doc is None:
        outcome = "created"
        department, confidentiality = workspace.default_department, workspace.default_confidentiality
        # Personnel/<identifiant>/... : a personal document of that employee.
        # Filed at least HR/CONFIDENTIAL (HR manages employee files) when the
        # owner may file there themselves; the Director can reclassify it
        # (e.g. FINANCE for a payslip) from the document card.
        subject = personal_subject_for_path(db, path)
        if subject is not None:
            personal_department = "HR" if department == "GENERAL" else department
            personal_level = (
                confidentiality
                if CONFIDENTIALITY_RANK[confidentiality] >= CONFIDENTIALITY_RANK["CONFIDENTIAL"]
                else "CONFIDENTIAL"
            )
            if check_document_access(
                grant=resolve_effective_grant(user), department=personal_department, confidentiality=personal_level
            ).allowed:
                department, confidentiality = personal_department, personal_level
        doc = Document(
            title=filename,
            department=department,
            confidentiality=confidentiality,
            confidentiality_rank=CONFIDENTIALITY_RANK[confidentiality],
            content=extraction.text if extraction else "",
            owner_id=user.id,
            workspace_id=workspace.id,
            relative_path=path,
            subject_user_id=subject.id if subject else None,
        )
        db.add(doc)
    else:
        outcome = "reactivated" if not doc.is_active else "updated"
        # Content only - a classification changed by hand afterwards
        # (PATCH /api/documents/{id}) is kept, never reset to the default.
        if extraction is not None:
            doc.content = extraction.text
        doc.is_active = True

    doc.origin = "sync"
    doc.source_filename = filename
    doc.minio_object_key = object_key
    doc.size_bytes = len(content)
    doc.mime_type = mime_type or mimetypes.guess_type(filename)[0]
    doc.file_last_modified = last_modified
    # The text is stored now (pages below): the document is readable and
    # exact-searchable as soon as this request returns. Embeddings (needed
    # only by semantic search) are queued for the background worker.
    doc.content_hash = sha256
    doc.status = "indexing" if extraction is not None else "queued"
    doc.index_progress = 0
    doc.index_attempts = 0
    doc.index_error = None
    doc.issue_kind = None
    db.commit()
    db.refresh(doc)
    if extraction is not None:
        write_pages(db, doc, extraction.pages)
        db.refresh(doc)

    log_event(
        user_id=user.id, username=user.username, role=user.role, action="WORKSPACE_FILE_INDEXED", decision="ALLOW",
        resource_type="document", resource_id=str(doc.id), reason=outcome,
        extra={
            "workspace_id": str(workspace.id), "relative_path": path, "sha256": sha256,
            "extraction": "queued" if extraction is None else "done", "embeddings": "queued",
            "personal_document_of": subject.username if subject else None,
        },
    )
    return doc, outcome
