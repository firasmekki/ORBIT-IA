"""Hierarchical view of a workspace's folders, for navigation and for the
chat scope picker.

A folder is the real directory part of the documents' relative paths
("noveau consultation/Exeliencia/CLINIQUE/x.docx" -> "noveau consultation/",
"noveau consultation/Exeliencia/", "noveau consultation/Exeliencia/CLINIQUE/").
Nothing is copied, moved or invented: a parent/child link exists only when
a stored path (or, for a server folder source, the disk) says so. Two
folders with the same name stay two different paths.

Three counters per folder, never mixed:
  direct_documents  documents directly inside it
  subfolder_count   its direct sub-folders
  total_documents   documents in it and in all its sub-folders
Counts are the documents THIS user may read (engine.document_scope). The
Director / workspace owner also sees the names of folders whose documents
are hidden from them, folders shared or restricted while still empty, and
empty folders of the server source on disk - with a count of 0 - as the
flat folder list already did (they need them to share them).

One query for the readable paths, one for the names (managers) - never one
per folder; the tree is built in memory (~20 ms for 20 000 paths).
"""

import os
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.access_rule import DocumentAccessRule, RestrictedFolder
from app.models.document import Document
from app.models.user import User
from app.models.workspace import Workspace
from app.policy.engine import document_scope
from app.policy.rules import Grant
from app.rag.normalize import normalize
from app.services.folders import ancestor_folders, can_manage_folders, can_write_path, normalize_folder

MAX_CHILDREN = 500
MAX_DOCUMENTS = 200
MAX_SEARCH_RESULTS = 100


@dataclass
class FolderStats:
    direct: int = 0
    total: int = 0


@dataclass
class Tree:
    stats: dict[str, FolderStats] = field(default_factory=lambda: defaultdict(FolderStats))
    children: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))

    def add_folder(self, folder: str) -> None:
        """Registers a folder ("a/b/") and every ancestor, linked to its real parent."""
        parent = ""
        for current in ancestor_folders(folder + "x"):
            self.children[parent].add(current)
            self.stats[current]  # noqa: B018 - creates the entry (count 0)
            parent = current

    def add_document(self, relative_path: str | None) -> None:
        folders = ancestor_folders(relative_path)
        self.stats[""].total += 1
        self.stats[folders[-1] if folders else ""].direct += 1
        parent = ""
        for folder in folders:
            self.stats[folder].total += 1
            self.children[parent].add(folder)
            parent = folder


def build_tree(document_paths: Iterable[str | None], extra_folders: Iterable[str] = ()) -> Tree:
    tree = Tree()
    tree.stats[""]  # noqa: B018 - the root always exists
    for path in document_paths:
        tree.add_document(path)
    for folder in extra_folders:
        tree.add_folder(folder)
    return tree


def folder_name(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] if path else ""


def parent_of(path: str) -> str:
    stripped = path.rstrip("/")
    return stripped.rsplit("/", 1)[0] + "/" if "/" in stripped else ""


def breadcrumb(workspace_name: str, path: str) -> list[dict]:
    return [{"name": workspace_name, "path": ""}] + [
        {"name": folder_name(folder), "path": folder} for folder in ancestor_folders(path + "x")
    ]


def _source_subfolders(workspace: Workspace, folder: str) -> list[str] | None:
    """Sub-folders on disk of a server folder source (one directory read,
    technical directories skipped like the scanner does). None when the
    workspace has no server source or the folder is not reachable."""
    if not workspace.has_server_source:
        return None
    from app.services.source_roots import get_root, resolve_dir  # noqa: PLC0415 - optional mount
    from app.services.source_scanner import TECHNICAL_DIRECTORIES  # noqa: PLC0415

    try:
        root = get_root(workspace.source_root)
        base = (workspace.source_path or "").strip("/")
        directory = resolve_dir(root, "/".join(p for p in (base, folder.rstrip("/")) if p))
        names = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.name.lower() in TECHNICAL_DIRECTORIES or entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        names.append(folder + entry.name + "/")
                except OSError:
                    continue
        return names
    except Exception:  # noqa: BLE001 - disk unreachable: the indexed tree is still shown
        return None


def _workspace_tree(db: Session, user: User, workspace: Workspace, grant: Grant) -> tuple[Tree, bool, set[str], dict]:
    manage = can_manage_folders(user, workspace)
    readable = [p for (p,) in db.query(Document.relative_path).filter(*document_scope(grant))]
    extra: set[str] = set()
    restricted = {r.folder_path for r in db.query(RestrictedFolder).filter(RestrictedFolder.workspace_id == workspace.id)}
    shares: dict[str, list[dict]] = {}
    if manage:
        for (path,) in db.query(Document.relative_path).filter(
            Document.workspace_id == workspace.id, Document.is_active.is_(True), Document.relative_path.isnot(None)
        ):
            folders = ancestor_folders(path)
            if folders:
                extra.add(folders[-1])
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
        extra |= restricted | set(shares)
    return build_tree(readable, extra), manage, restricted, shares


def _entry(tree: Tree, path: str, grant: Grant, restricted: set[str], shares: dict, manage: bool) -> dict:
    stats = tree.stats.get(path, FolderStats())
    return {
        "path": path,
        "name": folder_name(path),
        "parent": parent_of(path),
        "direct_documents": stats.direct,
        "subfolder_count": len(tree.children.get(path, ())),
        "total_documents": stats.total,
        "has_children": bool(tree.children.get(path)),
        "restricted": path in restricted,
        "restricted_by": next((f for f in ancestor_folders(path + "x")[:-1] if f in restricted), None),
        "shares": shares.get(path, []) if manage else [],
        "can_upload": can_write_path(grant, path + "x"),
    }


def folder_listing(
    db: Session, user: User, workspace: Workspace, grant: Grant, *, parent: str = "", query: str | None = None
) -> dict:
    """One level of the tree (lazy loading): the folder `parent` ("" = the
    workspace root), its three counters, its breadcrumb, its direct
    sub-folders and the documents directly inside it. With `query`: the
    folders whose NAME matches, anywhere, each with its full path."""
    parent = normalize_folder(parent) if parent and parent.strip("/") else ""
    tree, manage, restricted, shares = _workspace_tree(db, user, workspace, grant)

    if query and query.strip():
        needle = normalize(query.strip())
        matches = [f for f in tree.stats if f and needle in normalize(folder_name(f))]
        matches.sort(key=lambda f: (f.count("/"), f.lower()))
        return {
            "can_manage": manage,
            "query": query.strip(),
            "results": [_entry(tree, f, grant, restricted, shares, manage) for f in matches[:MAX_SEARCH_RESULTS]],
            "result_count": len(matches),
        }

    if parent and parent not in tree.stats:
        on_disk = manage and _source_subfolders(workspace, parent) is not None
        if not on_disk:
            raise LookupError(parent)
        tree.add_folder(parent)
    if manage:
        # Empty folders of the server source (never indexed: nothing to index).
        for sub in _source_subfolders(workspace, parent) or []:
            tree.add_folder(sub)
            for grandchild in _source_subfolders(workspace, sub) or []:
                tree.add_folder(grandchild)

    children = sorted(tree.children.get(parent, ()), key=lambda f: folder_name(f).lower())
    documents = (
        db.query(Document.id, Document.title, Document.relative_path, Document.status)
        .filter(*document_scope(grant))
        .filter(_directly_in(parent))
        .order_by(func.lower(Document.relative_path))
        .limit(MAX_DOCUMENTS)
        .all()
    )
    return {
        "can_manage": manage,
        "workspace_name": workspace.name,
        "folder": {**_entry(tree, parent, grant, restricted, shares, manage), "name": folder_name(parent) or workspace.name},
        "breadcrumb": breadcrumb(workspace.name, parent),
        "folders": [_entry(tree, f, grant, restricted, shares, manage) for f in children[:MAX_CHILDREN]],
        "folders_truncated": len(children) > MAX_CHILDREN,
        "documents": [
            {"id": str(d.id), "title": d.title, "relative_path": d.relative_path, "status": d.status} for d in documents
        ],
        "documents_truncated": tree.stats.get(parent, FolderStats()).direct > len(documents),
    }


def _directly_in(folder: str):
    if not folder:
        return or_(Document.relative_path.is_(None), func.strpos(Document.relative_path, "/") == 0)
    return (Document.relative_path.startswith(folder, autoescape=True)) & (
        func.strpos(func.substr(Document.relative_path, len(folder) + 1), "/") == 0
    )
