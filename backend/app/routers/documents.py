import dataclasses
import hashlib
import mimetypes
import uuid
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.alert_service import create_alert
from app.core.audit_logger import log_event
from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import get_current_user, require_role
from app.core.storage import download_file, upload_file
from app.models.access_rule import DocumentAccessRule
from app.models.document import CONFIDENTIALITY_RANK, Document
from app.models.user import User
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace
from app.policy.engine import PolicyDecision, check_document_access, check_document_visibility, document_scope
from app.policy.rules import Grant, resolve_effective_grant
from app.policy.workspace import (
    WorkspaceAccessError,
    can_use_workspace,
    ensure_default_workspace,
    grant_in_workspace,
    resolve_workspace,
)
from app.services.errors import ServiceError
from app.services.folders import ancestor_folders, can_write_path, normalize_folder
from app.services.personal_documents import personal_subject_for_path
from app.services.workspace_sync import validate_relative_path
from app.rag.extract import ExtractionError, UnsupportedFileTypeError, extract
from app.rag.ingest import ingest_document, write_pages
from app.schemas.document import DocumentCreate, DocumentDetail, DocumentSummary, DocumentUpdate

router = APIRouter(prefix="/api/documents", tags=["documents"])

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB - same cap as a synced file


@router.get("/watch-status")
def watch_status(_user: User = Depends(get_current_user)) -> dict:
    settings = get_settings()
    return {
        "enabled": settings.watch_folder_enabled,
        "department": settings.watch_folder_department,
        "confidentiality": settings.watch_folder_confidentiality,
    }


def _resolve_workspace_or_403(db: Session, user: User, workspace_id) -> Workspace:
    try:
        return resolve_workspace(db, user, workspace_id)
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message) from None


def _document_workspace(db: Session, doc: Document) -> Workspace | None:
    if doc.workspace_id == DEFAULT_WORKSPACE_ID:
        return ensure_default_workspace(db)
    return db.get(Workspace, doc.workspace_id)


def _check_document(db: Session, user: User, grant: Grant | None, doc: Document) -> PolicyDecision:
    """Direct access by id (no "current" workspace here): the user must be
    allowed in the document's workspace AND see it there - role
    department/confidentiality, personal documents, Director rules
    (engine.check_document_visibility, the twin of document_scope). Both
    refusals are audited and alerted by the callers."""
    if grant is None:
        return PolicyDecision(False, "unknown role")
    workspace = _document_workspace(db, doc)
    if workspace is None or not can_use_workspace(db, user, workspace):
        return PolicyDecision(False, "user is not allowed in this document's workspace")
    return check_document_visibility(grant=grant_in_workspace(grant, workspace, db), document=doc)


@router.get("", response_model=list[DocumentSummary])
def list_documents(
    workspace_id: Annotated[uuid.UUID | None, Query(description="Espace de travail (défaut : Général)")] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Document]:
    """Documents of ONE workspace the user may use, filtered in SQL by the
    same document_scope as the RAG (workspace + active + RBAC/ABAC)."""
    workspace = _resolve_workspace_or_403(db, user, workspace_id)
    grant = resolve_effective_grant(user)
    if grant is None:
        return []
    return (
        db.query(Document)
        .filter(*document_scope(grant_in_workspace(grant, workspace, db)))
        .order_by(Document.created_at.desc())
        .all()
    )


def _authorize_document_write(
    *, db: Session, user: User, action: str, title: str, department: str, confidentiality: str
) -> None:
    """Shared guard for create_document and upload_document: a user can
    never file a document at a department/confidentiality combination they
    could not themselves read back - prevents a low-privilege account from
    self-granting a visibility bump."""
    if confidentiality not in CONFIDENTIALITY_RANK:
        raise HTTPException(status_code=422, detail="Niveau de confidentialité inconnu.")

    grant = resolve_effective_grant(user)
    decision = check_document_access(grant=grant, department=department, confidentiality=confidentiality)
    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action=action,
        decision="ALLOW" if decision.allowed else "DENY",
        resource_type="document",
        reason=decision.reason,
        extra={"title": title, "department": department, "confidentiality": confidentiality},
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vous ne pouvez pas créer un document à un niveau d'accès supérieur au vôtre.",
        )


MAX_PREVIEW_CHARS = 200_000


@router.get("/{document_id}", response_model=DocumentDetail)
def get_document(
    document_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> DocumentDetail:
    doc = db.get(Document, document_id)
    if doc is None or not doc.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document introuvable.")

    grant = resolve_effective_grant(user)
    decision = _check_document(db, user, grant, doc)
    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="GET_DOCUMENT",
        decision="ALLOW" if decision.allowed else "DENY",
        resource_type="document",
        resource_id=str(doc.id),
        reason=decision.reason,
        extra={"title": doc.title, "department": doc.department, "confidentiality": doc.confidentiality},
    )
    if not decision.allowed:
        create_alert(
            alert_type="DOCUMENT_ACCESS_DENIED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title=f"Tentative d'accès refusée à un document ({user.full_name})",
            description=f"Document visé : « {doc.title} » ({doc.department} / {doc.confidentiality})\nRefus : {decision.reason}",
            resource_type="document",
            resource_id=str(doc.id),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès refusé : vous n'avez pas la permission de consulter ce document.",
        )
    detail = DocumentDetail.model_validate(doc, from_attributes=True)
    if len(detail.content) > MAX_PREVIEW_CHARS:
        # Display only: the full text stays indexed and searchable.
        detail.content = detail.content[:MAX_PREVIEW_CHARS] + "\n\n[… aperçu tronqué : document très long …]"
    return detail


class _PersonOut(BaseModel):
    full_name: str
    username: str
    role: str
    # Why they see it: "role" | "personal" (it is their own document) |
    # "granted" (explicit Director rule).
    basis: str | None = None


class _WorkspaceRef(BaseModel):
    id: uuid.UUID
    name: str
    visibility: str
    is_default: bool
    is_owner: bool


class DocumentAccessOut(BaseModel):
    workspace: _WorkspaceRef
    # Who can open this document right now: in its workspace AND visible to
    # them (role, own personal document, or a Director rule).
    visible_to: list[_PersonOut]
    # Would see it, but the document's workspace is private to them.
    blocked_by_workspace: list[_PersonOut]
    # Not allowed by their role / not their personal document - count only.
    blocked_by_role_count: int
    # Hidden by an explicit Director "deny" rule (names: Director only).
    denied_by_rule: list[_PersonOut] = []
    # The caller may manage personal assignment and access rules.
    can_manage_rules: bool = False


@router.get("/{document_id}/access", response_model=DocumentAccessOut)
def document_access(
    document_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> DocumentAccessOut:
    """"Qui peut voir ce document ?" - the real answer, computed with the
    very same rules as every read (workspace access + RBAC/ABAC), for
    someone who may read the document themselves."""
    doc = db.get(Document, document_id)
    if doc is None or not doc.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document introuvable.")
    if not _check_document(db, user, resolve_effective_grant(user), doc).allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès refusé.")

    workspace = _document_workspace(db, doc)
    is_default = doc.workspace_id == DEFAULT_WORKSPACE_ID
    is_director = user.role == "DIRECTOR"
    visible, blocked_ws, denied, blocked_role = [], [], [], 0
    for person in db.query(User).filter(User.is_active.is_(True)).order_by(User.full_name):
        grant = resolve_effective_grant(person)
        if grant is None or workspace is None:
            blocked_role += 1
            continue
        in_workspace = grant_in_workspace(grant, workspace, db)
        decision = check_document_visibility(grant=in_workspace, document=doc)
        entry = _PersonOut(full_name=person.full_name, username=person.username, role=person.role, basis=decision.basis)
        can_use = is_default or can_use_workspace(db, person, workspace)
        if decision.allowed and can_use:
            visible.append(entry)
        elif decision.basis == "denied":
            if is_director:
                denied.append(entry)
            else:
                blocked_role += 1
        elif not can_use and check_document_visibility(
            grant=dataclasses.replace(in_workspace, role_scope=True), document=doc
        ).allowed:
            # Would see it as a member: only the private workspace stops them.
            blocked_ws.append(entry)
        else:
            blocked_role += 1
    return DocumentAccessOut(
        workspace=_WorkspaceRef(
            id=doc.workspace_id,
            name=workspace.name if workspace else "Général",
            visibility="organization" if is_default or workspace is None else workspace.visibility,
            is_default=is_default,
            is_owner=workspace is not None and workspace.owner_id == user.id,
        ),
        visible_to=visible,
        blocked_by_workspace=blocked_ws,
        blocked_by_role_count=blocked_role,
        denied_by_rule=denied,
        can_manage_rules=is_director,
    )


# The folders a rule on a document may target.
_ancestor_folders = ancestor_folders


class AccessRuleIn(BaseModel):
    username: str
    effect: Literal["allow", "deny"]
    # None: this document only. Otherwise one of its parent folders.
    folder_path: str | None = None


class AccessRuleOut(BaseModel):
    id: uuid.UUID
    username: str
    full_name: str
    effect: str
    folder_path: str | None
    created_at: datetime | None


class AccessRulesOut(BaseModel):
    rules: list[AccessRuleOut]
    # Folders a new rule on this document may target (synced files only).
    folders: list[str]


def _active_document_or_404(db: Session, document_id: uuid.UUID) -> Document:
    doc = db.get(Document, document_id)
    if doc is None or not doc.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document introuvable.")
    return doc


def _rules_for(db: Session, doc: Document) -> list[tuple[DocumentAccessRule, User]]:
    folders = _ancestor_folders(doc.relative_path)
    target = DocumentAccessRule.document_id == doc.id
    if folders:
        target = target | DocumentAccessRule.folder_path.in_(folders)
    return (
        db.query(DocumentAccessRule, User)
        .join(User, User.id == DocumentAccessRule.user_id)
        .filter(DocumentAccessRule.workspace_id == doc.workspace_id, target)
        .order_by(User.full_name, DocumentAccessRule.created_at)
        .all()
    )


def _rule_out(rule: DocumentAccessRule, person: User) -> AccessRuleOut:
    return AccessRuleOut(
        id=rule.id, username=person.username, full_name=person.full_name, effect=rule.effect,
        folder_path=rule.folder_path, created_at=rule.created_at,
    )


@router.get("/{document_id}/access-rules", response_model=AccessRulesOut)
def list_access_rules(
    document_id: uuid.UUID, db: Session = Depends(get_db), _director: User = Depends(require_role("DIRECTOR"))
) -> AccessRulesOut:
    """The Director's exceptions that apply to this document (on it, or on
    one of its folders)."""
    doc = _active_document_or_404(db, document_id)
    return AccessRulesOut(
        rules=[_rule_out(rule, person) for rule, person in _rules_for(db, doc)],
        folders=_ancestor_folders(doc.relative_path),
    )


@router.post("/{document_id}/access-rules", response_model=AccessRuleOut, status_code=status.HTTP_201_CREATED)
def set_access_rule(
    document_id: uuid.UUID,
    payload: AccessRuleIn,
    db: Session = Depends(get_db),
    director: User = Depends(require_role("DIRECTOR")),
) -> AccessRuleOut:
    """Allow or deny ONE user this document - or the whole folder it sits
    in. Replaces that user's previous rule on the same target. Audited."""
    doc = _active_document_or_404(db, document_id)
    person = db.query(User).filter(User.username == payload.username.strip(), User.is_active.is_(True)).one_or_none()
    if person is None:
        raise HTTPException(status_code=422, detail="Utilisateur inconnu.")
    folder = None
    if payload.folder_path:
        try:
            folder = validate_relative_path(payload.folder_path.strip().strip("/")) + "/"
        except Exception:  # noqa: BLE001 - any invalid path is a 422 here
            raise HTTPException(status_code=422, detail="Dossier invalide.") from None
        if folder not in _ancestor_folders(doc.relative_path):
            raise HTTPException(status_code=422, detail="Ce dossier ne contient pas ce document.")

    target = DocumentAccessRule.folder_path == folder if folder else DocumentAccessRule.document_id == doc.id
    rule = (
        db.query(DocumentAccessRule)
        .filter(DocumentAccessRule.workspace_id == doc.workspace_id, DocumentAccessRule.user_id == person.id, target)
        .one_or_none()
    )
    previous = rule.effect if rule else None
    if rule is None:
        rule = DocumentAccessRule(
            workspace_id=doc.workspace_id,
            user_id=person.id,
            document_id=None if folder else doc.id,
            folder_path=folder,
            effect=payload.effect,
            created_by=director.id,
        )
        db.add(rule)
    else:
        rule.effect = payload.effect
        rule.created_by = director.id
    db.commit()
    db.refresh(rule)
    log_event(
        user_id=director.id, username=director.username, role=director.role, action="ACCESS_RULE_SET",
        decision="ALLOW", resource_type="document", resource_id=str(doc.id),
        reason=f"{payload.effect} for {person.username}",
        extra={
            "title": doc.title, "target_user": person.username, "effect": payload.effect,
            "previous_effect": previous, "folder_path": folder, "workspace_id": str(doc.workspace_id),
        },
    )
    return _rule_out(rule, person)


@router.delete("/{document_id}/access-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_access_rule(
    document_id: uuid.UUID,
    rule_id: uuid.UUID,
    db: Session = Depends(get_db),
    director: User = Depends(require_role("DIRECTOR")),
) -> None:
    doc = _active_document_or_404(db, document_id)
    match = next(((r, p) for r, p in _rules_for(db, doc) if r.id == rule_id), None)
    if match is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Règle introuvable.")
    rule, person = match
    effect, folder = rule.effect, rule.folder_path
    db.delete(rule)
    db.commit()
    log_event(
        user_id=director.id, username=director.username, role=director.role, action="ACCESS_RULE_REMOVED",
        decision="ALLOW", resource_type="document", resource_id=str(doc.id),
        reason=f"{effect} for {person.username} removed",
        extra={"title": doc.title, "target_user": person.username, "effect": effect, "folder_path": folder},
    )


@router.patch("/{document_id}", response_model=DocumentDetail)
def update_document(
    document_id: uuid.UUID,
    payload: DocumentUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Document:
    """Correct a document's classification (e.g. filed as PUBLIC by mistake).
    Content/file are untouched - only metadata changes, so no re-ingestion
    is needed: the RAG retriever joins on Document.department/confidentiality
    live at query time, it never bakes permissions into the embedding."""
    doc = db.get(Document, document_id)
    if doc is None or not doc.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document introuvable.")

    grant = resolve_effective_grant(user)

    current_decision = _check_document(db, user, grant, doc)
    if not current_decision.allowed:
        create_alert(
            alert_type="DOCUMENT_ACCESS_DENIED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title=f"Tentative de modification refusée ({user.full_name})",
            description=f"Document visé : « {doc.title} » ({doc.department} / {doc.confidentiality})\nRefus : {current_decision.reason}",
            resource_type="document",
            resource_id=str(doc.id),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès refusé à ce document.")

    new_department = payload.department or doc.department
    new_confidentiality = payload.confidentiality or doc.confidentiality
    if new_confidentiality not in CONFIDENTIALITY_RANK:
        raise HTTPException(status_code=422, detail="Niveau de confidentialité inconnu.")

    # Can only move a document within the same range they could themselves
    # read - same rule as creating one. Stops a low-privilege account from
    # using "correct a mistake" to relabel a document into a department or
    # confidentiality level above its own clearance.
    target_decision = check_document_access(grant=grant, department=new_department, confidentiality=new_confidentiality)
    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="UPDATE_DOCUMENT",
        decision="ALLOW" if target_decision.allowed else "DENY",
        resource_type="document",
        resource_id=str(doc.id),
        reason=target_decision.reason,
        extra={
            "title": doc.title,
            "from": {"department": doc.department, "confidentiality": doc.confidentiality},
            "to": {"department": new_department, "confidentiality": new_confidentiality},
        },
    )
    if not target_decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Vous ne pouvez pas déplacer ce document vers un niveau d'accès supérieur au vôtre.",
        )

    if "subject_user_id" in payload.model_fields_set and payload.subject_user_id != doc.subject_user_id:
        # Making a document personal opens it to that employee: an access
        # decision, so the Director's alone (like DocumentAccessRule).
        if user.role != "DIRECTOR":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Seul le directeur peut attribuer un document à un employé.",
            )
        subject = db.get(User, payload.subject_user_id) if payload.subject_user_id else None
        if payload.subject_user_id is not None and (subject is None or not subject.is_active):
            raise HTTPException(status_code=422, detail="Utilisateur inconnu.")
        log_event(
            user_id=user.id, username=user.username, role=user.role, action="DOCUMENT_SUBJECT_CHANGED",
            decision="ALLOW", resource_type="document", resource_id=str(doc.id),
            reason=f"personal document of {subject.username}" if subject else "no longer a personal document",
            extra={
                "title": doc.title,
                "from": str(doc.subject_user_id) if doc.subject_user_id else None,
                "to": str(subject.id) if subject else None,
            },
        )
        doc.subject_user_id = subject.id if subject else None

    if payload.title is not None:
        doc.title = payload.title
    doc.department = new_department
    doc.confidentiality = new_confidentiality
    doc.confidentiality_rank = CONFIDENTIALITY_RANK[new_confidentiality]
    db.commit()
    db.refresh(doc)
    return doc


@router.get("/{document_id}/file")
def download_document_file(
    document_id: uuid.UUID,
    inline: Annotated[bool, Query(description="Pour l'aperçu dans l'application plutôt qu'un téléchargement")] = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Streams the original uploaded file back (PDF/XLSX/DOCX/PNG...), not
    the extracted text - same permission check as reading the document.
    Served with its real type, so an image can be previewed as an image."""
    doc = db.get(Document, document_id)
    if doc is None or not doc.is_active or not doc.minio_object_key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Aucun fichier original pour ce document.")

    grant = resolve_effective_grant(user)
    decision = _check_document(db, user, grant, doc)
    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="DOWNLOAD_DOCUMENT_FILE",
        decision="ALLOW" if decision.allowed else "DENY",
        resource_type="document",
        resource_id=str(doc.id),
        reason=decision.reason,
        extra={"title": doc.title, "filename": doc.source_filename},
    )
    if not decision.allowed:
        create_alert(
            alert_type="DOCUMENT_ACCESS_DENIED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title=f"Tentative d'accès refusée à un document ({user.full_name})",
            description=f"Fichier visé : « {doc.title} » ({doc.department} / {doc.confidentiality})\nRefus : {decision.reason}",
            resource_type="document",
            resource_id=str(doc.id),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Accès refusé.")

    try:
        data = download_file(doc.minio_object_key)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Stockage indisponible : {exc}") from None

    filename = doc.source_filename or "document"
    # RFC 6266/5987: an ASCII fallback plus the exact UTF-8 name, so a name
    # like "Capture d'écran 2026.png" (or Arabic, or "’") never breaks the header.
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace("?", "_").replace('"', "_")
    disposition = "inline" if inline else "attachment"
    return StreamingResponse(
        iter([data]),
        media_type=doc.file_mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("", response_model=DocumentDetail, status_code=status.HTTP_201_CREATED)
def create_document(
    payload: DocumentCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Document:
    _authorize_document_write(
        db=db,
        user=user,
        action="CREATE_DOCUMENT",
        title=payload.title,
        department=payload.department,
        confidentiality=payload.confidentiality,
    )
    workspace = _resolve_workspace_or_403(db, user, payload.workspace_id)

    doc = Document(
        title=payload.title,
        workspace_id=workspace.id,
        department=payload.department,
        confidentiality=payload.confidentiality,
        confidentiality_rank=CONFIDENTIALITY_RANK[payload.confidentiality],
        content=payload.content,
        owner_id=user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    ingest_document(db, doc)
    return doc


@router.post("/upload", response_model=DocumentDetail, status_code=status.HTTP_201_CREATED)
def upload_document(
    title: str = Form(...),
    department: str = Form(...),
    confidentiality: str = Form(...),
    file: UploadFile = File(...),
    workspace_id: uuid.UUID | None = Form(None),
    folder: str | None = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Document:
    """Upload a real office file (PDF, Excel, Word, CSV, text/markdown) from
    any PC, into a workspace and optionally one of its folders - like
    dropping it into a shared Dropbox folder. The file is stored as-is for
    download; its text is readable and exact-searchable at once, embeddings
    follow in the background. The uploader is recorded (`owner_id`) and
    shown as "Ajouté par". An upload never disappears because the server's
    synced folder does not contain it (origin "upload").

    A plain `def`, like workspaces.sync_file: extraction and OCR are
    blocking, so they run in FastAPI's thread pool instead of freezing the
    event loop (and with it chat, login and the health check)."""
    if confidentiality not in CONFIDENTIALITY_RANK:
        raise HTTPException(status_code=422, detail="Niveau de confidentialité inconnu.")
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not filename:
        raise HTTPException(status_code=422, detail="Nom de fichier manquant.")
    workspace = _resolve_workspace_or_403(db, user, workspace_id)

    # Where it lands in the workspace's tree: the chosen folder, or the root
    # of a folder workspace. Plain uploads to "Général" keep no path.
    relative_path = None
    try:
        if folder and folder.strip(" /\\"):
            relative_path = validate_relative_path(normalize_folder(folder) + filename)
        elif workspace.id != DEFAULT_WORKSPACE_ID:
            relative_path = validate_relative_path(filename)
    except ServiceError as exc:
        raise HTTPException(status_code=422, detail=f"Dossier ou nom de fichier invalide : {exc.message}") from None

    previous: Document | None = None
    subject: User | None = None
    if relative_path:
        grant = resolve_effective_grant(user)
        if grant is None or not can_write_path(grant_in_workspace(grant, workspace, db), relative_path):
            log_event(
                user_id=user.id, username=user.username, role=user.role, action="CREATE_DOCUMENT", decision="DENY",
                resource_type="document", reason="folder not shared with this user",
                extra={"title": title, "workspace_id": str(workspace.id), "relative_path": relative_path},
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ce dossier n'est pas partagé avec vous.")
        previous = (
            db.query(Document)
            .filter(Document.workspace_id == workspace.id, Document.relative_path == relative_path)
            .one_or_none()
        )
        if previous is not None and previous.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Un fichier « {filename} » existe déjà dans ce dossier. Renommez-le ou choisissez un autre dossier.",
            )
        # Personnel/<identifiant>/... : that employee's personal document.
        subject = personal_subject_for_path(db, relative_path)
        if subject is not None:
            if subject.id != user.id and user.role not in ("DIRECTOR", "HR"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Vous ne pouvez pas déposer de document dans le dossier personnel d'un collègue.",
                )
            if department == "GENERAL":
                department = "HR"
            if CONFIDENTIALITY_RANK[confidentiality] < CONFIDENTIALITY_RANK["CONFIDENTIAL"]:
                confidentiality = "CONFIDENTIAL"

    if subject is not None and subject.id == user.id:
        # One's own personal document: always readable by its subject, so the
        # "not above your own level" rule has nothing to protect here.
        log_event(
            user_id=user.id, username=user.username, role=user.role, action="CREATE_DOCUMENT", decision="ALLOW",
            resource_type="document", reason="own personal document",
            extra={"title": title, "department": department, "confidentiality": confidentiality},
        )
    else:
        _authorize_document_write(
            db=db, user=user, action="CREATE_DOCUMENT", title=title, department=department, confidentiality=confidentiality
        )

    raw = file.file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Fichier trop volumineux (max {MAX_UPLOAD_BYTES // (1024 * 1024)} Mo).")
    if not raw:
        raise HTTPException(status_code=422, detail="Le fichier est vide.")

    try:
        extraction = extract(file.filename or "", raw)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except ExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    object_key = f"{uuid.uuid4()}-{filename}"
    try:
        upload_file(object_key, raw, content_type=file.content_type or "application/octet-stream")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Stockage de fichiers indisponible : {exc}") from None

    # A file once removed from this exact place is brought back (the path is
    # unique per workspace); otherwise a new document.
    doc = previous or Document(workspace_id=workspace.id, relative_path=relative_path)
    if previous is None:
        db.add(doc)
    doc.title = title
    doc.department = department
    doc.confidentiality = confidentiality
    doc.confidentiality_rank = CONFIDENTIALITY_RANK[confidentiality]
    doc.content = extraction.text
    doc.source_filename = filename
    doc.minio_object_key = object_key
    doc.owner_id = user.id
    doc.subject_user_id = subject.id if subject else None
    doc.origin = "upload"
    doc.size_bytes = len(raw)
    doc.mime_type = file.content_type or mimetypes.guess_type(filename)[0]
    doc.content_hash = hashlib.sha256(raw).hexdigest()
    doc.is_active = True
    # Readable and exact-searchable as soon as this returns; embeddings by
    # the background worker (app/services/embedding_worker.py), like a sync.
    doc.status = "indexing"
    doc.index_progress = 0
    db.commit()
    db.refresh(doc)
    write_pages(db, doc, extraction.pages)
    db.refresh(doc)
    log_event(
        user_id=user.id, username=user.username, role=user.role, action="DOCUMENT_UPLOADED", decision="ALLOW",
        resource_type="document", resource_id=str(doc.id), reason="reactivated" if previous else "created",
        extra={
            "title": title, "workspace_id": str(workspace.id), "relative_path": relative_path,
            "personal_document_of": subject.username if subject else None, "embeddings": "queued",
        },
    )
    return doc
