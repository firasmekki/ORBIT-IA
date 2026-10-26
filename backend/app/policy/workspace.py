"""Who may use which workspace - checked server-side on every request that
names one (chat, documents, sync, MCP). A workspace id coming from the
frontend or from a token is a claim, never a permission: it is always
resolved here against the authenticated user.

- Default workspace ("Général"): every user.
- A workspace shared with the organization (visibility "organization"): every user.
- Any other workspace: its owner and its explicit members.
- Someone a folder or a file of it was shared with ("allow" rule): only
  what was shared (Grant.role_scope = False), like a shared Dropbox folder.
Using a workspace never widens RBAC/ABAC: documents are still filtered by
the user's own department/confidentiality grant (engine.document_scope).
"""

import dataclasses
import uuid

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.alert_service import create_alert, security_event_key
from app.core.audit_logger import log_event
from app.models.access_rule import DocumentAccessRule, RestrictedFolder
from app.models.user import User
from app.models.workspace import DEFAULT_WORKSPACE_ID, DEFAULT_WORKSPACE_NAME, Workspace, WorkspaceMember
from app.policy.rules import Grant


class WorkspaceAccessError(Exception):
    """The workspace does not exist or the user may not use it - the
    message is identical in both cases (no existence oracle)."""

    message = "Espace de travail introuvable ou non autorisé."


def ensure_default_workspace(db: Session) -> Workspace:
    ws = db.get(Workspace, DEFAULT_WORKSPACE_ID)
    if ws is None:
        ws = Workspace(id=DEFAULT_WORKSPACE_ID, name=DEFAULT_WORKSPACE_NAME, owner_id=None)
        db.add(ws)
        db.commit()
        db.refresh(ws)
    return ws


def has_full_access(db: Session, user_id, workspace: Workspace) -> bool:
    """The whole workspace (each document still filtered by role): default
    or organization-wide workspace, owner, explicit member."""
    if workspace.id == DEFAULT_WORKSPACE_ID or workspace.visibility == "organization":
        return True
    if user_id is not None and workspace.owner_id == user_id:
        return True
    return (
        user_id is not None
        and db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == workspace.id, WorkspaceMember.user_id == user_id)
        .first()
        is not None
    )


def _has_share(db: Session, user_id, workspace_id) -> bool:
    return (
        db.query(DocumentAccessRule.id)
        .filter(
            DocumentAccessRule.workspace_id == workspace_id,
            DocumentAccessRule.user_id == user_id,
            DocumentAccessRule.effect == "allow",
        )
        .first()
        is not None
    )


def can_use_workspace(db: Session, user: User, workspace: Workspace) -> bool:
    if workspace.archived_at is not None:
        return False
    return has_full_access(db, user.id, workspace) or _has_share(db, user.id, workspace.id)


def can_manage_workspace(user: User, workspace: Workspace) -> bool:
    """Sync the connected folder, rename, add members: the owner only. The
    default workspace is fed by uploads and the watched folder, not synced."""
    return workspace.id != DEFAULT_WORKSPACE_ID and workspace.owner_id == user.id


def accessible_workspaces(db: Session, user: User) -> list[Workspace]:
    default = ensure_default_workspace(db)
    member_of = db.query(WorkspaceMember.workspace_id).filter(WorkspaceMember.user_id == user.id)
    shared_with = db.query(DocumentAccessRule.workspace_id).filter(
        DocumentAccessRule.user_id == user.id, DocumentAccessRule.effect == "allow"
    )
    others = (
        db.query(Workspace)
        .filter(Workspace.id != DEFAULT_WORKSPACE_ID, Workspace.archived_at.is_(None))
        .filter(
            or_(
                Workspace.owner_id == user.id,
                Workspace.id.in_(member_of),
                Workspace.visibility == "organization",
                Workspace.id.in_(shared_with),
            )
        )
        .order_by(Workspace.name)
        .all()
    )
    return [default, *others]


def resolve_workspace(db: Session, user: User, workspace_id: str | uuid.UUID | None) -> Workspace:
    """None -> the default workspace. Anything else must exist AND be usable
    by this user, otherwise WorkspaceAccessError (audited; an existing
    workspace the user is not part of also alerts the Director)."""
    if workspace_id is None or workspace_id == "":
        return ensure_default_workspace(db)
    try:
        ws_uuid = workspace_id if isinstance(workspace_id, uuid.UUID) else uuid.UUID(str(workspace_id))
    except ValueError:
        raise WorkspaceAccessError() from None
    if ws_uuid == DEFAULT_WORKSPACE_ID:
        return ensure_default_workspace(db)
    workspace = db.get(Workspace, ws_uuid)
    if workspace is not None and workspace.archived_at is not None:
        workspace = None  # deleted: answered like an unknown id, never alerted on
    if workspace is not None and can_use_workspace(db, user, workspace):
        return workspace

    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="WORKSPACE_ACCESS_DENIED",
        decision="DENY",
        resource_type="workspace",
        resource_id=str(ws_uuid),
        reason="workspace not found" if workspace is None else "user is neither owner nor member",
    )
    if workspace is not None:
        create_alert(
            alert_type="DOCUMENT_ACCESS_DENIED",
            user_id=user.id,
            username=user.username,
            role=user.role,
            title=f"Tentative d'accès refusée à un espace de travail ({user.full_name})",
            description=f"Espace de travail visé : « {workspace.name} »\nRefus : l'utilisateur n'en est ni propriétaire ni membre.",
            resource_type="workspace",
            resource_id=str(workspace.id),
            dedup_key=security_event_key(user.id, "WORKSPACE_ACCESS_DENIED", workspace.id),
        )
    raise WorkspaceAccessError()


def grant_in_workspace(grant: Grant, workspace: Workspace, db: Session) -> Grant:
    """The same RBAC/ABAC grant, evaluated inside `workspace` (already
    checked by resolve_workspace), with the Director's explicit rules for
    this user in this workspace (DocumentAccessRule). `db` is mandatory so
    a "deny" rule can never be skipped by forgetting to load it."""
    allowed_ids, denied_ids, allowed_folders, denied_folders = set(), set(), set(), set()
    if grant.user_id is not None:
        rules = (
            db.query(DocumentAccessRule)
            .filter(DocumentAccessRule.workspace_id == workspace.id, DocumentAccessRule.user_id == grant.user_id)
            .all()
        )
        for rule in rules:
            if rule.document_id is not None:
                (allowed_ids if rule.effect == "allow" else denied_ids).add(rule.document_id)
            elif rule.folder_path:
                (allowed_folders if rule.effect == "allow" else denied_folders).add(rule.folder_path)
    restricted = db.query(RestrictedFolder.folder_path).filter(RestrictedFolder.workspace_id == workspace.id)
    return dataclasses.replace(
        grant,
        workspace_id=workspace.id,
        allowed_document_ids=frozenset(allowed_ids),
        denied_document_ids=frozenset(denied_ids),
        allowed_folders=frozenset(allowed_folders),
        denied_folders=frozenset(denied_folders),
        restricted_folders=frozenset(path for (path,) in restricted),
        bypass_restrictions=grant.role == "DIRECTOR"
        or (workspace.owner_id is not None and workspace.owner_id == grant.user_id),
        role_scope=grant.user_id is None or has_full_access(db, grant.user_id, workspace),
    )
