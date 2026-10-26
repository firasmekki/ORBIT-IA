"""Folders of a workspace, shared "like Dropbox".

A folder is just the common prefix of documents' relative paths - synced
from the server PC or uploaded from any PC into it. The Director (or the
workspace owner) can:
  - restrict a folder: its documents leave role-based visibility and are
    seen only by the people it is shared with (RestrictedFolder);
  - share it with / hide it from one person (DocumentAccessRule on the
    folder, "allow" or "deny" - a deny always wins).
Every read still goes through engine.document_scope; nothing here filters
documents by itself.
"""

from collections import Counter

from sqlalchemy.orm import Session

from app.models.access_rule import DocumentAccessRule, RestrictedFolder
from app.models.document import Document
from app.models.user import User
from app.models.workspace import DEFAULT_WORKSPACE_ID, Workspace
from app.policy.engine import document_scope, path_in_folders
from app.policy.rules import Grant
from app.services.errors import invalid
from app.services.workspace_sync import validate_relative_path


def normalize_folder(path: str) -> str:
    """"RH/Paie", "/RH/Paie/" -> "RH/Paie/". Same safety rules as a synced path."""
    cleaned = (path or "").strip().replace("\\", "/").strip("/")
    if not cleaned:
        raise invalid("dossier vide")
    return validate_relative_path(cleaned) + "/"


def ancestor_folders(relative_path: str | None) -> list[str]:
    """"RH/Paie/x.pdf" -> ["RH/", "RH/Paie/"]."""
    if not relative_path:
        return []
    parts = relative_path.split("/")[:-1]
    return ["/".join(parts[: i + 1]) + "/" for i in range(len(parts))]


def can_manage_folders(user: User, workspace: Workspace) -> bool:
    """The Director everywhere; a workspace owner in their own workspace."""
    if user.role == "DIRECTOR":
        return True
    return workspace.id != DEFAULT_WORKSPACE_ID and workspace.owner_id == user.id


def can_write_path(grant: Grant, relative_path: str) -> bool:
    """May this user drop a file at `relative_path`? Not into a folder
    hidden from them (deny rule), nor into a restricted folder that was not
    shared with them."""
    if path_in_folders(relative_path, grant.denied_folders):
        return False
    if grant.bypass_restrictions or not path_in_folders(relative_path, grant.restricted_folders):
        return True
    return path_in_folders(relative_path, grant.allowed_folders)


def list_folders(db: Session, user: User, workspace: Workspace, grant: Grant) -> dict:
    manage = can_manage_folders(user, workspace)
    query = db.query(Document.relative_path).filter(Document.relative_path.isnot(None))
    if manage:
        # Folder names (not contents) of the whole workspace: a manager needs
        # them to share them, even those hidden from themselves by a rule.
        query = query.filter(Document.workspace_id == workspace.id, Document.is_active.is_(True))
    else:
        query = query.filter(*document_scope(grant))
    counts: Counter[str] = Counter()
    for (path,) in query:
        for folder in ancestor_folders(path):
            counts[folder] += 1

    restricted = {
        r.folder_path for r in db.query(RestrictedFolder).filter(RestrictedFolder.workspace_id == workspace.id)
    }
    shares: dict[str, list[dict]] = {}
    if manage:
        rows = (
            db.query(DocumentAccessRule, User)
            .join(User, User.id == DocumentAccessRule.user_id)
            .filter(DocumentAccessRule.workspace_id == workspace.id, DocumentAccessRule.folder_path.isnot(None))
            .order_by(User.full_name)
        )
        for rule, person in rows:
            shares.setdefault(rule.folder_path, []).append(
                {"id": rule.id, "username": person.username, "full_name": person.full_name, "effect": rule.effect}
            )
        # A restricted or shared folder with no file yet still shows up.
        for folder in [*restricted, *shares]:
            for ancestor in ancestor_folders(folder + "x"):
                counts.setdefault(ancestor, 0)

    folders = []
    for path in sorted(counts):
        restricted_by = next((f for f in ancestor_folders(path)[:-1] if f in restricted), None)
        folders.append({
            "path": path,
            "document_count": counts[path],
            "restricted": path in restricted,
            # A parent folder is restricted: this one is too.
            "restricted_by": restricted_by,
            "shares": shares.get(path, []) if manage else [],
            "can_upload": can_write_path(grant, path + "x"),
        })
    return {"can_manage": manage, "folders": folders}
