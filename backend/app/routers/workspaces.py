"""/api/workspaces - list/create workspaces, see their indexed files, sync a
connected local folder into one (app/services/workspace_sync.py), share it.

Every route resolves the workspace against the authenticated user
(app/policy/workspace.py) - an id in the URL is a claim, never a right.
"""

import uuid
from datetime import UTC, datetime

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.audit_logger import log_event
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.access_rule import DocumentAccessRule, RestrictedFolder
from app.models.document import CONFIDENTIALITY_RANK, DEPARTMENTS, Document
from app.models.user import User
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace, WorkspaceMember
from app.policy.engine import check_document_access, document_scope
from app.policy.rules import resolve_effective_grant
from app.policy.workspace import (
    WorkspaceAccessError,
    accessible_workspaces,
    can_manage_workspace,
    grant_in_workspace,
    resolve_workspace,
)
from app.core.config import get_settings
from app.models.source_file import SourceFile
from app.services import folder_tree as folder_tree_service
from app.services import folders as folder_service
from app.services import source_roots, workspace_sync
from app.services.errors import ServiceError
from app.services.source_scanner import inventory_summary

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


class SourceInfo(BaseModel):
    """A folder of the server PC read by the backend itself (no browser)."""

    root_id: str
    path: str
    host_path: str
    enabled: bool
    status: str | None
    error: str | None
    scan_started_at: datetime | None
    scan_seen: int | None


class WorkspaceOut(BaseModel):
    source: SourceInfo | None = None
    id: uuid.UUID
    name: str
    is_default: bool
    is_owner: bool
    can_sync: bool
    default_department: str
    default_confidentiality: str
    folder_name: str | None
    last_synced_at: datetime | None
    document_count: int
    # Documents whose text is stored but embeddings are still being computed.
    pending_count: int = 0
    visibility: str = "private"
    owner_name: str | None = None
    member_count: int = 0


class WorkspaceUpdate(BaseModel):
    visibility: Literal["private", "organization"]


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    default_department: str = "GENERAL"
    default_confidentiality: str = "INTERNAL"
    # A connected company folder is created shared ("organization"): every
    # user finds the synced copy, each one still filtered document by
    # document (role, personal documents, Director rules).
    visibility: Literal["private", "organization"] = "private"
    # A folder of the server PC (app/routers/sources.py): read by the backend
    # itself, continuously, with no browser tab. Director only.
    source_root: str | None = None
    source_path: str | None = None


class SourceUpdate(BaseModel):
    enabled: bool


class SourceStatusOut(BaseModel):
    source: SourceInfo | None
    last_synced_at: datetime | None
    detected_files: int
    excluded_directories: int
    excluded_files: int = 0
    issues: dict[str, int] = {}
    visual_described: int = 0
    visual_pending: int = 0
    ignored: int
    unreadable: int
    documents: dict[str, int]
    text_ready: int
    fully_indexed: int
    in_progress: int
    complete: bool
    reasons: list[dict]


class InventoryItemOut(BaseModel):
    relative_path: str
    state: str
    reason: str | None
    size_bytes: int | None
    issue_kind: str | None = None


class WorkspaceFileOut(BaseModel):
    id: uuid.UUID
    title: str
    relative_path: str | None
    status: str
    index_progress: int | None = None
    department: str
    confidentiality: str
    size_bytes: int | None
    indexed_at: datetime | None


class ManifestEntryIn(BaseModel):
    relative_path: str
    sha256: str = Field(min_length=64, max_length=64)
    size: int = Field(ge=0)
    last_modified: datetime | None = None


class SyncPlanIn(BaseModel):
    folder_name: str | None = None
    files: list[ManifestEntryIn]
    excluded_directories: list[str] = Field(default_factory=list)


class SyncPlanOut(BaseModel):
    to_upload: list[str]
    unchanged: list[str]
    removed: list[str]
    rejected: list[dict]


class SyncFileOut(BaseModel):
    document_id: uuid.UUID
    relative_path: str
    outcome: str
    status: str


class MemberIn(BaseModel):
    username: str


class MemberOut(BaseModel):
    user_id: uuid.UUID
    username: str
    full_name: str


def _workspace_or_403(db: Session, user: User, workspace_id: uuid.UUID) -> Workspace:
    try:
        return resolve_workspace(db, user, workspace_id)
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message) from None


def _service_error(exc: ServiceError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.message)


# --- folders shared "like Dropbox" (app/services/folders.py) -----------------------------


class FolderShareOut(BaseModel):
    id: uuid.UUID
    username: str
    full_name: str
    effect: str


class FolderOut(BaseModel):
    path: str
    document_count: int
    restricted: bool
    # This folder is restricted because a parent one is.
    restricted_by: str | None = None
    # Director / owner only.
    shares: list[FolderShareOut] = []
    can_upload: bool = True


class FoldersOut(BaseModel):
    can_manage: bool
    folders: list[FolderOut]


class FolderRestrictionIn(BaseModel):
    folder_path: str
    restricted: bool


class FolderShareIn(BaseModel):
    folder_path: str
    username: str
    effect: Literal["allow", "deny"] = "allow"


def _folder_or_422(path: str) -> str:
    try:
        return folder_service.normalize_folder(path)
    except ServiceError as exc:
        raise HTTPException(status_code=422, detail=f"Dossier invalide : {exc.message}") from None


def _managed_workspace(db: Session, user: User, workspace_id: uuid.UUID) -> Workspace:
    ws = _workspace_or_403(db, user, workspace_id)
    if not folder_service.can_manage_folders(user, ws):
        raise HTTPException(status_code=403, detail="Seul le directeur ou le propriétaire de l'espace gère le partage des dossiers.")
    return ws


def _source_info(ws: Workspace) -> SourceInfo | None:
    if not ws.has_server_source:
        return None
    try:
        host_path = source_roots.host_display(source_roots.get_root(ws.source_root), ws.source_path)
    except ServiceError:
        host_path = f"(emplacement {ws.source_root} retiré de la configuration)/{ws.source_path}"
    return SourceInfo(
        root_id=ws.source_root,
        path=ws.source_path,
        host_path=host_path,
        enabled=ws.source_enabled,
        status=ws.source_status,
        error=ws.source_error,
        scan_started_at=ws.source_scan_started_at,
        scan_seen=ws.source_scan_seen,
    )


def _validate_new_source(db: Session, user: User, root_id: str, path: str | None) -> tuple[str, str]:
    """Director only; the folder must exist under a configured root and must
    not overlap a folder already connected (the same file indexed twice
    would be counted - and searched - twice: a sub-folder of a connected
    folder is chosen as a chat scope instead)."""
    if user.role not in get_settings().source_admin_roles_set:
        raise HTTPException(status_code=403, detail="Seul le directeur peut connecter un dossier du serveur.")
    try:
        root = source_roots.get_root(root_id)
        relative = source_roots.normalize_relative(path)
        if not source_roots.resolve_dir(root, relative).is_dir():
            raise HTTPException(status_code=404, detail=f"Dossier introuvable : {source_roots.host_display(root, relative)}")
    except ServiceError as exc:
        raise _service_error(exc) from None
    for other in (
        db.query(Workspace)
        .filter(Workspace.archived_at.is_(None), Workspace.source_root == root.id, Workspace.source_path.isnot(None))
        .all()
    ):
        if source_roots.paths_overlap(other.source_path, relative):
            if relative.startswith(other.source_path + "/") or other.source_path == "":
                hint = (
                    f"Il est déjà inclus dans l'espace « {other.name} » : ouvrez cet espace et choisissez "
                    f"« {relative[len(other.source_path):].lstrip('/')} » comme périmètre dans le chat - rien n'est réindexé."
                )
            elif other.source_path == relative:
                hint = f"Ce dossier est déjà connecté : espace « {other.name} »."
            else:
                hint = (
                    f"Il contient le dossier déjà connecté par l'espace « {other.name} ». "
                    "Supprimez d'abord cet espace, ou connectez un dossier qui ne le contient pas."
                )
            raise HTTPException(status_code=409, detail=hint)
    return root.id, relative


def _to_out(db: Session, user: User, ws: Workspace) -> WorkspaceOut:
    grant = resolve_effective_grant(user)
    # Count only what this user may actually see in it (RBAC/ABAC applies).
    count = (
        db.query(func.count(Document.id)).filter(*document_scope(grant_in_workspace(grant, ws, db))).scalar()
        if grant
        else 0
    )
    pending = (
        db.query(func.count(Document.id))
        .filter(
            *document_scope(grant_in_workspace(grant, ws, db)),
            Document.status.in_(("queued", "extracting", "indexing")),
        )
        .scalar()
        if grant
        else 0
    )
    owner = db.get(User, ws.owner_id) if ws.owner_id else None
    members = db.query(func.count()).select_from(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws.id).scalar()
    return WorkspaceOut(
        source=_source_info(ws),
        visibility="organization" if ws.id == DEFAULT_WORKSPACE_ID else ws.visibility,
        owner_name=owner.full_name if owner else None,
        member_count=members or 0,
        id=ws.id,
        name=ws.name,
        is_default=ws.id == DEFAULT_WORKSPACE_ID,
        is_owner=ws.owner_id == user.id,
        can_sync=can_manage_workspace(user, ws),
        default_department=ws.default_department,
        default_confidentiality=ws.default_confidentiality,
        folder_name=ws.folder_name,
        last_synced_at=ws.last_synced_at,
        document_count=count or 0,
        pending_count=pending or 0,
    )


@router.get("", response_model=list[WorkspaceOut])
def list_workspaces(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[WorkspaceOut]:
    return [_to_out(db, user, ws) for ws in accessible_workspaces(db, user)]


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
def create_workspace(
    payload: WorkspaceCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> WorkspaceOut:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Le nom de l'espace de travail est obligatoire.")
    if payload.default_department not in DEPARTMENTS or payload.default_confidentiality not in CONFIDENTIALITY_RANK:
        raise HTTPException(status_code=422, detail="Département ou niveau de confidentialité inconnu.")
    # Files synced into it get this classification: never above what the
    # creator may read themselves (same rule as filing a document).
    decision = check_document_access(
        grant=resolve_effective_grant(user),
        department=payload.default_department,
        confidentiality=payload.default_confidentiality,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cette classification par défaut dépasse vos propres droits.",
        )
    if (
        db.query(Workspace)
        .filter(Workspace.owner_id == user.id, Workspace.name == name, Workspace.archived_at.is_(None))
        .first()
    ):
        raise HTTPException(status_code=409, detail="Vous avez déjà un espace de travail portant ce nom.")

    source_root = source_path = None
    if payload.source_root is not None:
        source_root, source_path = _validate_new_source(db, user, payload.source_root, payload.source_path)

    ws = Workspace(
        name=name,
        owner_id=user.id,
        default_department=payload.default_department,
        default_confidentiality=payload.default_confidentiality,
        visibility=payload.visibility,
        source_root=source_root,
        source_path=source_path,
        source_status="idle" if source_root else None,
        # Picked up by the scanner loop within seconds.
        source_scan_requested_at=datetime.now(UTC) if source_root else None,
        folder_name=(source_path.rsplit("/", 1)[-1] or name) if source_root else None,
    )
    db.add(ws)
    db.commit()
    db.refresh(ws)
    log_event(
        user_id=user.id, username=user.username, role=user.role, action="WORKSPACE_CREATED", decision="ALLOW",
        resource_type="workspace", resource_id=str(ws.id), reason="ok",
        extra={
            "name": name, "default_department": ws.default_department,
            "default_confidentiality": ws.default_confidentiality, "visibility": ws.visibility,
            "source": _source_info(ws).host_path if source_root else None,
        },
    )
    return _to_out(db, user, ws)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(workspace_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> None:
    """Remove a workspace from the app (owner only): it disappears for every
    user and its documents leave every search, list and the assistant.
    Nothing is erased (rows kept for the audit trail) and the folder on the
    owner's computer is never touched - the server cannot even reach it."""
    ws = _workspace_or_403(db, user, workspace_id)
    if not can_manage_workspace(user, ws):
        raise HTTPException(status_code=403, detail="Seul le propriétaire peut supprimer cet espace de travail.")
    retired = (
        db.query(Document)
        .filter(Document.workspace_id == ws.id, Document.is_active.is_(True))
        .update({Document.is_active: False, Document.status: "removed"}, synchronize_session=False)
    )
    original_name = ws.name
    ws.archived_at = datetime.now(UTC)
    # Frees the name (unique per owner): the same folder can be connected
    # again later as a fresh workspace. The original name stays in the audit.
    suffix = f" · supprimé {ws.archived_at:%Y-%m-%d %H:%M:%S}"
    ws.name = original_name[: 100 - len(suffix)] + suffix
    db.commit()
    log_event(
        user_id=user.id, username=user.username, role=user.role, action="WORKSPACE_DELETED", decision="ALLOW",
        resource_type="workspace", resource_id=str(ws.id), reason="removed from the app by its owner",
        extra={"name": original_name, "folder_name": ws.folder_name, "documents_retired": retired},
    )


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
def update_workspace(
    workspace_id: uuid.UUID, payload: WorkspaceUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> WorkspaceOut:
    """Share a workspace with the whole organization, or make it private
    again (owner only). Documents keep their own RBAC/ABAC filter."""
    ws = _workspace_or_403(db, user, workspace_id)
    if not can_manage_workspace(user, ws):
        raise HTTPException(status_code=403, detail="Seul le propriétaire peut modifier le partage de cet espace.")
    previous = ws.visibility
    ws.visibility = payload.visibility
    db.commit()
    db.refresh(ws)
    log_event(
        user_id=user.id, username=user.username, role=user.role, action="WORKSPACE_VISIBILITY_CHANGED", decision="ALLOW",
        resource_type="workspace", resource_id=str(ws.id), reason="ok",
        extra={"from": previous, "to": ws.visibility, "name": ws.name},
    )
    return _to_out(db, user, ws)


@router.get("/{workspace_id}/folders", response_model=FoldersOut)
def list_folders(workspace_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> FoldersOut:
    """The workspace's folders this user can see (all of them, with their
    sharing, for the Director / owner) - and where they may drop files."""
    ws = _workspace_or_403(db, user, workspace_id)
    grant = resolve_effective_grant(user)
    if grant is None:
        return FoldersOut(can_manage=False, folders=[])
    return FoldersOut(**folder_service.list_folders(db, user, ws, grant_in_workspace(grant, ws, db)))


@router.get("/{workspace_id}/folders/tree")
def folder_tree(
    workspace_id: uuid.UUID,
    parent: str = "",
    q: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """One level of the folder tree (lazy loading): the folder, its
    breadcrumb, its three counters (direct documents, sub-folders, total),
    its direct sub-folders and documents. `q`: folders whose name matches,
    anywhere in the workspace, with their full path. Counts are what this
    user may read (app/services/folder_tree.py)."""
    ws = _workspace_or_403(db, user, workspace_id)
    grant = resolve_effective_grant(user)
    if grant is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Votre rôle n'est pas reconnu.")
    try:
        return folder_tree_service.folder_listing(
            db, user, ws, grant_in_workspace(grant, ws, db), parent=parent, query=q
        )
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dossier introuvable.") from None
    except ServiceError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None


@router.put("/{workspace_id}/folders/restriction", response_model=FoldersOut)
def set_folder_restriction(
    workspace_id: uuid.UUID, payload: FolderRestrictionIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> FoldersOut:
    """Restricted: the folder's documents are seen only by the people it is
    shared with (+ Director, owner). Not restricted: role-based again."""
    ws = _managed_workspace(db, user, workspace_id)
    path = _folder_or_422(payload.folder_path)
    row = (
        db.query(RestrictedFolder)
        .filter(RestrictedFolder.workspace_id == ws.id, RestrictedFolder.folder_path == path)
        .one_or_none()
    )
    if payload.restricted and row is None:
        db.add(RestrictedFolder(workspace_id=ws.id, folder_path=path, created_by=user.id))
    elif not payload.restricted and row is not None:
        db.delete(row)
    db.commit()
    log_event(
        user_id=user.id, username=user.username, role=user.role,
        action="FOLDER_RESTRICTED" if payload.restricted else "FOLDER_UNRESTRICTED", decision="ALLOW",
        resource_type="workspace", resource_id=str(ws.id), reason="ok", extra={"folder_path": path},
    )
    return list_folders(ws.id, db=db, user=user)


@router.post("/{workspace_id}/folders/shares", response_model=FoldersOut, status_code=status.HTTP_201_CREATED)
def share_folder(
    workspace_id: uuid.UUID, payload: FolderShareIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> FoldersOut:
    """Share a folder with one person ("allow" - including its subfolders,
    whatever their role) or hide it from them ("deny" - always wins).
    Replaces that person's previous rule on this folder. Audited."""
    ws = _managed_workspace(db, user, workspace_id)
    path = _folder_or_422(payload.folder_path)
    person = db.query(User).filter(User.username == payload.username.strip(), User.is_active.is_(True)).one_or_none()
    if person is None:
        raise HTTPException(status_code=422, detail="Utilisateur inconnu.")
    rule = (
        db.query(DocumentAccessRule)
        .filter(
            DocumentAccessRule.workspace_id == ws.id,
            DocumentAccessRule.user_id == person.id,
            DocumentAccessRule.folder_path == path,
        )
        .one_or_none()
    )
    previous = rule.effect if rule else None
    if rule is None:
        rule = DocumentAccessRule(workspace_id=ws.id, user_id=person.id, folder_path=path, effect=payload.effect)
        db.add(rule)
    rule.effect = payload.effect
    rule.created_by = user.id
    db.commit()
    log_event(
        user_id=user.id, username=user.username, role=user.role, action="ACCESS_RULE_SET", decision="ALLOW",
        resource_type="workspace", resource_id=str(ws.id), reason=f"{payload.effect} for {person.username}",
        extra={"folder_path": path, "target_user": person.username, "effect": payload.effect, "previous_effect": previous},
    )
    return list_folders(ws.id, db=db, user=user)


@router.delete("/{workspace_id}/folders/shares/{rule_id}", response_model=FoldersOut)
def unshare_folder(
    workspace_id: uuid.UUID, rule_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> FoldersOut:
    ws = _managed_workspace(db, user, workspace_id)
    rule = db.get(DocumentAccessRule, rule_id)
    if rule is None or rule.workspace_id != ws.id or rule.folder_path is None:
        raise HTTPException(status_code=404, detail="Partage introuvable.")
    person = db.get(User, rule.user_id)
    extra = {"folder_path": rule.folder_path, "target_user": person.username if person else None, "effect": rule.effect}
    db.delete(rule)
    db.commit()
    log_event(
        user_id=user.id, username=user.username, role=user.role, action="ACCESS_RULE_REMOVED", decision="ALLOW",
        resource_type="workspace", resource_id=str(ws.id), reason="folder share removed", extra=extra,
    )
    return list_folders(ws.id, db=db, user=user)


@router.get("/{workspace_id}/files", response_model=list[WorkspaceFileOut])
def list_workspace_files(
    workspace_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[Document]:
    ws = _workspace_or_403(db, user, workspace_id)
    grant = resolve_effective_grant(user)
    if grant is None:
        return []
    return (
        db.query(Document)
        .filter(*document_scope(grant_in_workspace(grant, ws, db)))
        .order_by(Document.relative_path.asc().nulls_last(), Document.title)
        .all()
    )


def _owned_workspace(db: Session, user: User, workspace_id: uuid.UUID) -> Workspace:
    ws = _workspace_or_403(db, user, workspace_id)
    if not can_manage_workspace(user, ws):
        raise HTTPException(status_code=403, detail="Seul le propriétaire de cet espace peut gérer son dossier.")
    return ws


def _browser_syncable(db: Session, user: User, workspace_id: uuid.UUID) -> Workspace:
    ws = _workspace_or_403(db, user, workspace_id)
    if ws.has_server_source:
        # Two sources for one workspace would retire each other's files.
        raise HTTPException(
            status_code=409, detail="Cet espace est relié à un dossier du serveur : il se synchronise tout seul."
        )
    return ws


@router.get("/{workspace_id}/source/status", response_model=SourceStatusOut)
def source_status(workspace_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """Real counters of the folder and of its documents' processing - from
    the inventory and the documents table, never a simulated progress."""
    ws = _owned_workspace(db, user, workspace_id)
    return {"source": _source_info(ws), "last_synced_at": ws.last_synced_at, **inventory_summary(db, ws)}


@router.post("/{workspace_id}/source/scan", status_code=status.HTTP_202_ACCEPTED)
def request_scan(workspace_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    ws = _owned_workspace(db, user, workspace_id)
    if not ws.has_server_source:
        raise HTTPException(status_code=422, detail="Cet espace n'est pas relié à un dossier du serveur.")
    ws.source_scan_requested_at = datetime.now(UTC)
    if not ws.source_enabled:
        ws.source_enabled = True
    db.commit()
    return {"requested": True}


@router.patch("/{workspace_id}/source", response_model=WorkspaceOut)
def update_source(
    workspace_id: uuid.UUID, payload: SourceUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> WorkspaceOut:
    """Pause / resume the folder scan. Pausing keeps every indexed document
    searchable - it only stops looking for new or changed files."""
    ws = _owned_workspace(db, user, workspace_id)
    if not ws.has_server_source:
        raise HTTPException(status_code=422, detail="Cet espace n'est pas relié à un dossier du serveur.")
    ws.source_enabled = payload.enabled
    if payload.enabled:
        ws.source_scan_requested_at = datetime.now(UTC)
    db.commit()
    db.refresh(ws)
    log_event(
        user_id=user.id, username=user.username, role=user.role, action="WORKSPACE_SOURCE_UPDATED", decision="ALLOW",
        resource_type="workspace", resource_id=str(ws.id), reason="resumed" if payload.enabled else "paused",
    )
    return _to_out(db, user, ws)


@router.get("/{workspace_id}/inventory", response_model=list[InventoryItemOut])
def list_inventory(
    workspace_id: uuid.UUID,
    state: Literal["ignored", "unreadable", "failed", "no_text"],
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[dict]:
    """The files that are NOT (fully) indexed, each with its reason."""
    ws = _owned_workspace(db, user, workspace_id)
    limit = max(1, min(limit, 1000))
    if state in ("failed", "no_text"):
        rows = (
            db.query(Document)
            .filter(Document.workspace_id == ws.id, Document.is_active.is_(True), Document.status == state)
            .order_by(Document.relative_path)
            .offset(offset)
            .limit(limit)
        )
        return [
            {
                "relative_path": d.relative_path or d.title, "state": state, "reason": d.index_error,
                "size_bytes": d.size_bytes, "issue_kind": d.issue_kind,
            }
            for d in rows
        ]
    rows = (
        db.query(SourceFile)
        .filter(SourceFile.workspace_id == ws.id, SourceFile.state == state)
        .order_by(SourceFile.relative_path)
        .offset(offset)
        .limit(limit)
    )
    return [
        {"relative_path": r.relative_path, "state": r.state, "reason": r.reason, "size_bytes": r.size_bytes} for r in rows
    ]


@router.post("/{workspace_id}/documents/retry-failed")
def retry_failed(workspace_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    """Every failed document of the workspace back into the extraction
    queue, with a fresh attempt counter (and unreadable files re-read at
    the next scan)."""
    ws = _owned_workspace(db, user, workspace_id)
    count = (
        db.query(Document)
        .filter(Document.workspace_id == ws.id, Document.is_active.is_(True), Document.status == "failed")
        .update(
            {Document.status: "queued", Document.index_attempts: 0, Document.index_error: None, Document.index_progress: 0},
            synchronize_session=False,
        )
    )
    if ws.has_server_source:
        ws.source_scan_requested_at = datetime.now(UTC)
    db.commit()
    log_event(
        user_id=user.id, username=user.username, role=user.role, action="WORKSPACE_RETRY_FAILED", decision="ALLOW",
        resource_type="workspace", resource_id=str(ws.id), reason="ok", extra={"documents": count},
    )
    return {"requeued": count}


@router.post("/{workspace_id}/documents/visual-analysis")
def request_visual_analysis(
    workspace_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """Queue the workspace's images that have no OCR text for a description
    by the local vision model - processed one at a time in the background
    (app/services/vision_worker.py), never at question time."""
    ws = _owned_workspace(db, user, workspace_id)
    if not get_settings().vision_enabled:
        raise HTTPException(status_code=503, detail="Le modèle d'analyse d'images n'est pas activé sur ce serveur.")
    count = (
        db.query(Document)
        .filter(
            Document.workspace_id == ws.id,
            Document.is_active.is_(True),
            Document.status == "no_text",
            Document.issue_kind == "no_text_image",
        )
        .update(
            {Document.status: "visual_queued", Document.index_attempts: 0, Document.index_error: None},
            synchronize_session=False,
        )
    )
    db.commit()
    log_event(
        user_id=user.id, username=user.username, role=user.role, action="WORKSPACE_VISUAL_ANALYSIS", decision="ALLOW",
        resource_type="workspace", resource_id=str(ws.id), reason="queued", extra={"images": count},
    )
    return {"queued": count}


@router.post("/{workspace_id}/sync/plan", response_model=SyncPlanOut)
def plan_sync(
    workspace_id: uuid.UUID, payload: SyncPlanIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    ws = _browser_syncable(db, user, workspace_id)
    manifest = [
        workspace_sync.ManifestEntry(
            relative_path=f.relative_path, sha256=f.sha256, size=f.size, last_modified=f.last_modified
        )
        for f in payload.files
    ]
    try:
        return workspace_sync.plan_sync(db, user, ws, manifest, payload.folder_name, payload.excluded_directories)
    except ServiceError as exc:
        raise _service_error(exc) from None


@router.post("/{workspace_id}/sync/file", response_model=SyncFileOut)
def sync_file(
    workspace_id: uuid.UUID,
    relative_path: str = Form(...),
    last_modified: int | None = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SyncFileOut:
    """One file of the connected folder, read by the browser. `last_modified`
    is the browser's File.lastModified (ms since epoch).

    The file is only stored and queued: its text (pypdf, Tesseract OCR -
    minutes on a scanned PDF) is extracted by the background extraction
    worker, so a folder is connected in seconds instead of waiting for each
    file's OCR. A plain `def` all the same: hashing and storing are blocking
    I/O, and FastAPI runs a `def` route in its thread pool rather than on the
    event loop that serves chat, login and the health check."""
    ws = _browser_syncable(db, user, workspace_id)
    content = file.file.read(workspace_sync.MAX_SYNC_FILE_BYTES + 1)
    modified = datetime.fromtimestamp(last_modified / 1000, tz=UTC) if last_modified else None
    try:
        doc, outcome = workspace_sync.index_file(
            db, user, ws, relative_path, content, last_modified=modified, mime_type=file.content_type,
            defer_extraction=True,
        )
    except ServiceError as exc:
        raise _service_error(exc) from None
    return SyncFileOut(document_id=doc.id, relative_path=doc.relative_path or relative_path, outcome=outcome, status=doc.status)


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
def list_members(workspace_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[MemberOut]:
    ws = _workspace_or_403(db, user, workspace_id)
    rows = db.query(User).join(WorkspaceMember, WorkspaceMember.user_id == User.id).filter(WorkspaceMember.workspace_id == ws.id)
    return [MemberOut(user_id=u.id, username=u.username, full_name=u.full_name) for u in rows.order_by(User.username)]


@router.post("/{workspace_id}/members", response_model=MemberOut, status_code=status.HTTP_201_CREATED)
def add_member(
    workspace_id: uuid.UUID, payload: MemberIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> MemberOut:
    """Share a workspace. Membership grants nothing by itself: the member
    still only sees the documents their own RBAC/ABAC grant allows."""
    ws = _workspace_or_403(db, user, workspace_id)
    if not can_manage_workspace(user, ws):
        raise HTTPException(status_code=403, detail="Seul le propriétaire peut partager cet espace de travail.")
    member = db.query(User).filter(User.username == payload.username.strip()).first()
    if member is None:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    if member.id != ws.owner_id and db.get(WorkspaceMember, (ws.id, member.id)) is None:
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=member.id))
        db.commit()
        log_event(
            user_id=user.id, username=user.username, role=user.role, action="WORKSPACE_MEMBER_ADDED", decision="ALLOW",
            resource_type="workspace", resource_id=str(ws.id), reason="ok", extra={"member": member.username},
        )
    return MemberOut(user_id=member.id, username=member.username, full_name=member.full_name)


@router.delete("/{workspace_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(
    workspace_id: uuid.UUID, member_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    ws = _workspace_or_403(db, user, workspace_id)
    if not can_manage_workspace(user, ws):
        raise HTTPException(status_code=403, detail="Seul le propriétaire peut modifier les membres.")
    membership = db.get(WorkspaceMember, (ws.id, member_id))
    if membership is not None:
        db.delete(membership)
        db.commit()
        log_event(
            user_id=user.id, username=user.username, role=user.role, action="WORKSPACE_MEMBER_REMOVED", decision="ALLOW",
            resource_type="workspace", resource_id=str(ws.id), reason="ok", extra={"member_id": str(member_id)},
        )
