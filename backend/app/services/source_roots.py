"""The server folders the app may read: up to three host folders set once
in .env (ORBIT_SOURCE_ROOT_1..3), mounted read-only at /sources/1..3 (see
docker-compose.yml). Docker cannot reach an arbitrary Windows path on its
own, so a root is configured once; any folder BELOW a root can then be
chosen from the app, switched, paused or removed without touching Docker.

A source is always (root id, path relative to that root). Every
resolution goes through `resolve_dir`, which refuses anything that escapes
its root (`..`, absolute paths, a symlink pointing outside). The host path
("C:\\Users\\ORBIT\\Desktop") is display-only: it is never used to open a
file, only to show the user where a folder is and to understand a path
they paste.
"""

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.config import get_settings
from app.services.errors import invalid

ROOT_IDS = ("1", "2", "3")


@dataclass(frozen=True)
class SourceRoot:
    id: str
    host_path: str
    mount: Path

    @property
    def label(self) -> str:
        cleaned = self.host_path.rstrip("\\/")
        return cleaned.replace("/", "\\").rsplit("\\", 1)[-1] or cleaned

    @property
    def available(self) -> bool:
        return self.mount.is_dir()


def configured_roots() -> list[SourceRoot]:
    settings = get_settings()
    roots = []
    for root_id in ROOT_IDS:
        host = (getattr(settings, f"source_root_{root_id}_host") or "").strip()
        if host:
            roots.append(SourceRoot(id=root_id, host_path=host, mount=Path(settings.source_mount_base) / root_id))
    return roots


def get_root(root_id: str) -> SourceRoot:
    for root in configured_roots():
        if root.id == root_id:
            return root
    raise invalid("Emplacement inconnu. Les emplacements autorisés se configurent dans .env (ORBIT_SOURCE_ROOT_1..3).")


def normalize_relative(path: str | None) -> str:
    """'projets\\BIC\\' -> 'projets/BIC'. '' is the root itself."""
    raw = (path or "").replace("\\", "/").strip().strip("/")
    if not raw:
        return ""
    parts = PurePosixPath(raw).parts
    if any(part in ("..", ".") for part in parts) or ":" in raw or len(raw) > 1024:
        raise invalid("Chemin de dossier invalide.")
    return "/".join(parts)


def resolve_dir(root: SourceRoot, relative: str) -> Path:
    """The directory inside the container, guaranteed to stay under the root."""
    relative = normalize_relative(relative)
    target = (root.mount / relative) if relative else root.mount
    try:
        resolved = target.resolve(strict=False)
        mount = root.mount.resolve(strict=False)
    except OSError:
        raise invalid("Chemin de dossier invalide.") from None
    if resolved != mount and mount not in resolved.parents:
        raise invalid("Ce chemin sort de l'emplacement autorisé.")
    return target


def host_display(root: SourceRoot, relative: str) -> str:
    base = root.host_path.rstrip("\\/")
    return base + ("\\" + relative.replace("/", "\\") if relative else "")


def parse_host_path(text: str) -> tuple[SourceRoot, str]:
    """A path pasted by the user ("C:\\Users\\ORBIT\\Desktop\\projets") ->
    (root, "projets"). Case-insensitive, like Windows paths."""
    wanted = (text or "").strip().strip('"').replace("/", "\\").rstrip("\\")
    if not wanted:
        raise invalid("Indiquez un chemin de dossier.")
    for root in configured_roots():
        base = root.host_path.replace("/", "\\").rstrip("\\")
        if wanted.lower() == base.lower():
            return root, ""
        if wanted.lower().startswith(base.lower() + "\\"):
            return root, normalize_relative(wanted[len(base) + 1 :])
    allowed = ", ".join(root.host_path for root in configured_roots()) or "aucun"
    raise invalid(
        f"Ce dossier n'est pas dans un emplacement autorisé (autorisés : {allowed}). "
        "Ajoutez son disque ou son dossier parent dans .env (ORBIT_SOURCE_ROOT_2 ou _3) puis relancez start-orbit-ai.ps1."
    )


def list_subfolders(root: SourceRoot, relative: str) -> dict:
    """Direct sub-folders of a folder, plus how many files sit directly in
    it - one directory read, never a recursive walk (a 10 GB tree must
    stay instant to browse)."""
    relative = normalize_relative(relative)
    directory = resolve_dir(root, relative)
    if not directory.is_dir():
        raise invalid(f"Dossier introuvable : {host_display(root, relative)}")
    folders, files = [], 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        folders.append(entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        files += 1
                except OSError:
                    continue
    except PermissionError:
        raise invalid(f"Accès refusé à {host_display(root, relative)}") from None
    folders.sort(key=str.lower)
    return {
        "root_id": root.id,
        "path": relative,
        "host_path": host_display(root, relative),
        "parent": None if not relative else relative.rsplit("/", 1)[0] if "/" in relative else "",
        "folders": folders,
        "file_count": files,
    }


def paths_overlap(a: str, b: str) -> bool:
    """'projets' and 'projets/BIC' overlap; 'projets/BIC' and 'projets/BICX' do not."""
    if a == b or a == "" or b == "":
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")
