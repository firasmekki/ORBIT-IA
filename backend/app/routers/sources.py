"""/api/sources - browse the server folders that may be connected as a
workspace source (app/services/source_roots.py). Restricted to
SOURCE_ADMIN_ROLES (the Director by default): even the folder NAMES of the
server PC are not every employee's business. Read-only - nothing here
opens or reads a file.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.audit_logger import log_event
from app.core.config import get_settings
from app.core.deps import get_current_user
from app.models.user import User
from app.services import source_roots
from app.services.errors import ServiceError

router = APIRouter(prefix="/api/sources", tags=["sources"])


class RootOut(BaseModel):
    id: str
    label: str
    host_path: str
    available: bool


class BrowseOut(BaseModel):
    root_id: str
    path: str
    host_path: str
    parent: str | None
    folders: list[str]
    file_count: int


class ResolveIn(BaseModel):
    host_path: str


class ResolveOut(BaseModel):
    root_id: str
    path: str
    host_path: str


def require_source_admin(user: User = Depends(get_current_user)) -> User:
    if user.role not in get_settings().source_admin_roles_set:
        log_event(
            user_id=user.id, username=user.username, role=user.role, action="SOURCE_BROWSE", decision="DENY",
            resource_type="source", reason="role may not browse server folders",
        )
        raise HTTPException(status_code=403, detail="Seul le directeur peut connecter un dossier du serveur.")
    return user


@router.get("/roots", response_model=list[RootOut])
def list_roots(user: User = Depends(require_source_admin)) -> list[RootOut]:
    return [
        RootOut(id=root.id, label=root.label, host_path=root.host_path, available=root.available)
        for root in source_roots.configured_roots()
    ]


@router.get("/browse", response_model=BrowseOut)
def browse(
    root: str = Query(...), path: str = Query(""), user: User = Depends(require_source_admin)
) -> dict:
    try:
        source_root = source_roots.get_root(root)
        if not source_root.available:
            raise HTTPException(
                status_code=503,
                detail=f"L'emplacement {source_root.host_path} n'est pas monté dans le serveur. Relancez start-orbit-ai.ps1.",
            )
        return source_roots.list_subfolders(source_root, path)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from None


@router.post("/resolve", response_model=ResolveOut)
def resolve(payload: ResolveIn, user: User = Depends(require_source_admin)) -> ResolveOut:
    """A Windows path pasted by the user -> (root, relative path), checked to exist."""
    try:
        root, relative = source_roots.parse_host_path(payload.host_path)
        if not source_roots.resolve_dir(root, relative).is_dir():
            raise HTTPException(status_code=404, detail=f"Dossier introuvable : {source_roots.host_display(root, relative)}")
    except ServiceError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.message) from None
    return ResolveOut(root_id=root.id, path=relative, host_path=source_roots.host_display(root, relative))
