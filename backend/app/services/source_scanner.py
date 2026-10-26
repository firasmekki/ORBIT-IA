"""Server-side scan of a folder source (Workspace.source_root/source_path).

The backend reads the folder itself, through a read-only mount (see
app/services/source_roots.py) - no browser tab, no permission click, and it
keeps going when nobody has the app open. Each scan:

  1. walks the WHOLE tree recursively and records every file in the
     inventory (`source_files`), supported or not, with the reason when it
     is not indexed - no file ever disappears silently;
  2. skips files whose size and modification date did not change since the
     last scan (no re-read, no re-hash: a rescan of 10 GB costs a directory
     walk, not 10 GB of reads);
  3. hands new/changed supported files to the SAME pipeline as a browser
     sync (workspace_sync.index_file): stored, deduplicated by SHA-256,
     queued for background extraction (OCR included) and embedding;
  4. once the walk completed, retires the documents whose file left the
     folder - never when the folder is unreachable or suddenly empty
     (an unplugged drive must not wipe an index).

Only one scan runs per workspace at a time (claimed through
`source_status = 'scanning'`); a scan interrupted by a restart is simply
run again.
"""

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.audit_logger import log_event
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.document import Document
from app.models.source_file import SourceFile
from app.models.user import User
from app.models.workspace import Workspace
from app.services import workspace_sync
from app.services.errors import ServiceError
from app.services.source_roots import get_root, host_display, resolve_dir

logger = logging.getLogger("orbitia.source_scanner")

LOOP_SECONDS = 5
PROGRESS_EVERY = 500
# Tool/system directories, never business documents. Recorded once in the
# inventory as an excluded directory (with this reason) - not walked, not
# silently skipped. Deliberately NOT "logs", "build", "tmp", "cache"...:
# a client folder may well be called that.
TECHNICAL_DIRECTORIES = {
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".venv", "venv", ".next", ".idea", ".vs",
    "$recycle.bin", "system volume information",
}
SYSTEM_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}
# A stuck "scanning" state older than this is considered dead (crash).
STALE_SCAN = timedelta(hours=6)


@dataclass
class ScanResult:
    seen: int = 0
    queued: int = 0
    unchanged: int = 0
    ignored: int = 0
    unreadable: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)


def _mtime(stat_result: os.stat_result) -> datetime:
    return datetime.fromtimestamp(stat_result.st_mtime, UTC)


def _same_time(a: datetime | None, b: datetime | None) -> bool:
    return a is not None and b is not None and abs((a - b).total_seconds()) < 0.001


def ignore_reason(name: str, size: int) -> str | None:
    lowered = name.lower()
    if lowered in SYSTEM_FILES:
        return "fichier système"
    if name.startswith("~$"):
        return "fichier temporaire Office"
    extension = workspace_sync.extension_of(name)
    if extension not in workspace_sync.SYNC_EXTENSIONS:
        return f"format .{extension} non pris en charge" if extension else "fichier sans extension"
    if size == 0:
        return "fichier vide"
    if size > workspace_sync.MAX_SYNC_FILE_BYTES:
        return workspace_sync.too_large_reason(name)
    return None


def _walk(top: Path):
    """(relative_path, DirEntry | None, stat | None, excluded_reason | None,
    error | None) for every entry, depth-first, without following links."""
    stack: list[tuple[Path, str]] = [(top, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            yield (prefix + "/" if prefix else ""), None, None, None, f"dossier illisible : {exc.strerror or exc}"
            continue
        for entry in entries:
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                if entry.is_symlink():
                    yield relative, entry, None, "lien symbolique (non suivi)", None
                elif entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() in TECHNICAL_DIRECTORIES:
                        yield relative + "/", entry, None, "dossier technique exclu (logiciel, pas des documents)", None
                    else:
                        stack.append((Path(entry.path), relative))
                else:
                    yield relative, entry, entry.stat(follow_symlinks=False), None, None
            except OSError as exc:
                yield relative, entry, None, None, f"illisible : {exc.strerror or exc}"


def _count_files(directory: Path) -> int | None:
    """Files under an excluded directory - names only, no stat, no read."""
    count, stack = 0, [directory]
    try:
        while stack:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    else:
                        count += 1
    except OSError:
        return None
    return count


def _upsert(db: Session, workspace_id, rows: list[dict]) -> None:
    if not rows:
        return
    stmt = insert(SourceFile).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_source_files_workspace_path",
        set_={
            "size_bytes": stmt.excluded.size_bytes,
            "modified_at": stmt.excluded.modified_at,
            "sha256": stmt.excluded.sha256,
            "state": stmt.excluded.state,
            "reason": stmt.excluded.reason,
            "document_id": stmt.excluded.document_id,
            "seen_at": stmt.excluded.seen_at,
            "file_count": func.coalesce(stmt.excluded.file_count, SourceFile.file_count),
        },
    )
    db.execute(stmt)
    db.commit()


def scan_workspace(db: Session, workspace: Workspace) -> ScanResult:
    """One full scan. The caller has claimed the workspace (status 'scanning')."""
    result = ScanResult()
    now = datetime.now(UTC)
    root = get_root(workspace.source_root)
    top = resolve_dir(root, workspace.source_path or "")
    display = host_display(root, workspace.source_path or "")
    if not top.is_dir():
        raise FileNotFoundError(f"Dossier introuvable : {display}")
    owner = db.get(User, workspace.owner_id) if workspace.owner_id else None
    if owner is None or not owner.is_active:
        raise PermissionError("Le propriétaire de cet espace n'existe plus ou est désactivé.")

    inventory = {
        row.relative_path: row
        for row in db.query(
            SourceFile.relative_path, SourceFile.size_bytes, SourceFile.modified_at, SourceFile.sha256,
            SourceFile.state, SourceFile.reason, SourceFile.document_id, SourceFile.file_count,
        ).filter(SourceFile.workspace_id == workspace.id)
    }
    documents = {
        row.id: row
        for row in db.query(Document.id, Document.is_active, Document.content_hash).filter(
            Document.workspace_id == workspace.id
        )
    }
    seen: set[str] = set()
    document_paths: set[str] = set()
    # Keyed by path: one row per path per upsert batch (Postgres refuses an
    # ON CONFLICT batch that touches the same row twice).
    pending: dict[str, dict] = {}

    def record(relative, *, state, reason=None, size=None, modified=None, sha256=None, document_id=None, file_count=None):
        pending[relative] = ({
            "workspace_id": workspace.id, "relative_path": relative, "size_bytes": size, "modified_at": modified,
            "sha256": sha256, "state": state, "reason": (reason or "")[:255] or None, "document_id": document_id,
            "seen_at": now, "file_count": file_count,
        })
        if len(pending) >= 200:
            _upsert(db, workspace.id, list(pending.values()))
            pending.clear()

    for relative, entry, stat_result, excluded, error in _walk(top):
        if not relative:  # the top folder itself was unreadable
            raise PermissionError(error or f"Dossier illisible : {display}")
        result.seen += 1
        seen.add(relative)
        if result.seen % PROGRESS_EVERY == 0:
            db.execute(update(Workspace).where(Workspace.id == workspace.id).values(source_scan_seen=result.seen))
            db.commit()
        if len(relative) > 1024:
            result.ignored += 1
            seen.add(relative[:1024])
            record(relative[:1024], state="ignored", reason="chemin trop long (plus de 1024 caractères)")
            continue
        if excluded:
            result.ignored += 1
            known = inventory.get(relative)
            if relative.endswith("/") and (known is None or known.file_count is None):
                # Counted once (names only, nothing read), then kept.
                record(relative, state="ignored", reason=excluded, file_count=_count_files(Path(entry.path)))
            elif not (known and known.state == "ignored"):
                record(relative, state="ignored", reason=excluded)
            continue
        if error:
            result.unreadable += 1
            record(relative, state="unreadable", reason=error)
            continue

        size, modified = stat_result.st_size, _mtime(stat_result)
        reason = ignore_reason(entry.name, size)
        known = inventory.get(relative)
        if reason:
            result.ignored += 1
            if not (known and known.state == "ignored" and known.reason == reason and known.size_bytes == size):
                record(relative, state="ignored", reason=reason, size=size, modified=modified)
            continue

        doc = documents.get(known.document_id) if known and known.document_id else None
        if (
            known is not None and known.state == "document" and known.size_bytes == size
            and _same_time(known.modified_at, modified)
            and doc is not None and doc.is_active and doc.content_hash == known.sha256
        ):
            result.unchanged += 1
            document_paths.add(relative)
            continue

        try:
            content = Path(entry.path).read_bytes()
        except OSError as exc:
            result.unreadable += 1
            record(relative, state="unreadable", reason=f"illisible : {exc.strerror or exc}", size=size, modified=modified)
            continue
        try:
            document, outcome = workspace_sync.index_file(
                db, owner, workspace, relative, content, last_modified=modified, defer_extraction=True
            )
        except ServiceError as exc:
            db.rollback()
            result.ignored += 1
            record(relative, state="ignored", reason=exc.message, size=size, modified=modified)
            continue
        except Exception as exc:  # noqa: BLE001 - storage hiccup on one file: retried next scan
            db.rollback()
            logger.exception("could not store %s", relative)
            result.unreadable += 1
            record(relative, state="unreadable", reason=f"enregistrement impossible : {exc}"[:255], size=size, modified=modified)
            continue
        document_paths.add(relative)
        result.queued += outcome != "unchanged"
        result.unchanged += outcome == "unchanged"
        record(
            relative, state="document", size=size, modified=modified,
            sha256=hashlib.sha256(content).hexdigest(), document_id=document.id,
        )
    _upsert(db, workspace.id, list(pending.values()))

    # --- what left the folder ------------------------------------------------
    active_docs = (
        db.query(Document)
        .filter(Document.workspace_id == workspace.id, Document.is_active.is_(True), Document.relative_path.isnot(None))
        .filter(or_(Document.origin.is_(None), Document.origin != "upload"))
        .all()
    )
    gone_docs = [d for d in active_docs if d.relative_path not in document_paths]
    if result.seen == 0 and (active_docs or inventory):
        # An emptied folder and a broken mount look the same from here:
        # keep everything rather than wipe an index on a guess.
        raise RuntimeError(f"Le dossier {display} paraît vide : rien n'a été retiré par précaution.")
    for doc in gone_docs:
        doc.is_active = False
        doc.status = "removed"
        result.removed += 1
        log_event(
            user_id=owner.id, username=owner.username, role=owner.role, action="WORKSPACE_FILE_REMOVED",
            decision="ALLOW", resource_type="document", resource_id=str(doc.id), reason="file left the server folder",
            extra={"workspace_id": str(workspace.id), "relative_path": doc.relative_path},
        )
    stale = [path for path in inventory if path not in seen]
    for first in range(0, len(stale), 500):
        db.query(SourceFile).filter(
            SourceFile.workspace_id == workspace.id, SourceFile.relative_path.in_(stale[first : first + 500])
        ).delete(synchronize_session=False)
    db.commit()
    return result


# --- status for the UI ---------------------------------------------------------


def inventory_summary(db: Session, workspace: Workspace) -> dict:
    """Real counters, straight from the inventory and the documents table -
    never a simulated progress."""
    by_state = dict(
        db.query(SourceFile.state, func.count())
        .filter(SourceFile.workspace_id == workspace.id, ~SourceFile.relative_path.endswith("/"))
        .group_by(SourceFile.state)
        .all()
    )
    excluded_dirs = (
        db.query(func.count())
        .select_from(SourceFile)
        .filter(SourceFile.workspace_id == workspace.id, SourceFile.relative_path.endswith("/"))
        .scalar()
    )
    reasons = (
        db.query(SourceFile.reason, func.count())
        .filter(SourceFile.workspace_id == workspace.id, SourceFile.state != "document")
        .group_by(SourceFile.reason)
        .order_by(func.count().desc())
        .limit(12)
        .all()
    )
    excluded_files = (
        db.query(func.coalesce(func.sum(SourceFile.file_count), 0))
        .filter(SourceFile.workspace_id == workspace.id, SourceFile.relative_path.endswith("/"))
        .scalar()
    )
    doc_status = dict(
        db.query(Document.status, func.count())
        .filter(Document.workspace_id == workspace.id, Document.is_active.is_(True))
        .group_by(Document.status)
        .all()
    )
    documents = {
        key: doc_status.get(key, 0)
        for key in ("queued", "extracting", "indexing", "indexed", "failed", "no_text", "visual_queued", "visual_running")
    }
    # Why documents have no (or no full) index: failed vs. simply no text.
    issues = dict(
        db.query(Document.issue_kind, func.count())
        .filter(
            Document.workspace_id == workspace.id,
            Document.is_active.is_(True),
            Document.status.in_(("failed", "no_text")),
        )
        .group_by(Document.issue_kind)
        .all()
    )
    visual = (
        db.query(func.count())
        .select_from(Document)
        .filter(Document.workspace_id == workspace.id, Document.is_active.is_(True), Document.issue_kind.like("visual_%"))
        .scalar()
    )
    in_progress = documents["queued"] + documents["extracting"] + documents["indexing"]
    return {
        "detected_files": sum(by_state.values()),
        "excluded_directories": excluded_dirs or 0,
        # Files inside the excluded tool directories: counted (never read) so
        # the total matches what Windows shows for the folder.
        "excluded_files": int(excluded_files or 0),
        "issues": {kind or "inconnu": count for kind, count in issues.items()},
        "visual_described": visual or 0,
        "visual_pending": documents["visual_queued"] + documents["visual_running"],
        "ignored": by_state.get("ignored", 0),
        "unreadable": by_state.get("unreadable", 0),
        "documents": documents,
        # Text extracted (exact search works) vs. embeddings done too.
        "text_ready": documents["indexing"] + documents["indexed"],
        "fully_indexed": documents["indexed"],
        "in_progress": in_progress,
        "complete": in_progress == 0 and workspace.source_status != "scanning",
        "reasons": [{"reason": reason or "inconnue", "count": count} for reason, count in reasons],
    }


# --- background loop -------------------------------------------------------------


def reset_interrupted_scans() -> None:
    db = SessionLocal()
    try:
        db.execute(update(Workspace).where(Workspace.source_status == "scanning").values(source_status="idle"))
        db.commit()
    finally:
        db.close()


def _claim_due(db: Session) -> Workspace | None:
    settings = get_settings()
    now = datetime.now(UTC)
    due_before = now - timedelta(seconds=settings.source_scan_interval_seconds)
    candidates = (
        db.query(Workspace)
        .filter(
            Workspace.archived_at.is_(None),
            Workspace.source_root.isnot(None),
            Workspace.source_path.isnot(None),
            Workspace.source_enabled.is_(True),
            or_(
                Workspace.source_status.is_(None),
                Workspace.source_status != "scanning",
                Workspace.source_scan_started_at < now - STALE_SCAN,
            ),
            or_(
                Workspace.source_scan_requested_at.isnot(None),
                Workspace.source_scan_started_at.is_(None),
                Workspace.source_scan_started_at < due_before,
            ),
        )
        .order_by(Workspace.source_scan_requested_at.desc().nulls_last(), Workspace.source_scan_started_at.asc().nulls_first())
        .all()
    )
    for ws in candidates:
        claimed = db.execute(
            update(Workspace)
            .where(Workspace.id == ws.id, or_(Workspace.source_status.is_(None), Workspace.source_status != "scanning",
                                             Workspace.source_scan_started_at < now - STALE_SCAN))
            .values(source_status="scanning", source_scan_started_at=now, source_scan_requested_at=None, source_scan_seen=0)
        )
        db.commit()
        if claimed.rowcount:
            db.refresh(ws)
            return ws
    return None


def run_next_scan() -> bool:
    db = SessionLocal()
    try:
        ws = _claim_due(db)
        if ws is None:
            return False
        started = datetime.now(UTC)
        status, error, result = "idle", None, None
        try:
            result = scan_workspace(db, ws)
        except FileNotFoundError as exc:
            db.rollback()
            status, error = "missing", str(exc)
        except Exception as exc:  # noqa: BLE001 - reported on the workspace, retried next interval
            db.rollback()
            logger.exception("scan of workspace %s failed", ws.id)
            status, error = "error", str(exc) or exc.__class__.__name__
        values = {"source_status": status, "source_error": error}
        if result is not None:
            values.update(last_synced_at=datetime.now(UTC), source_scan_seen=result.seen)
        db.execute(update(Workspace).where(Workspace.id == ws.id).values(**values))
        db.commit()
        summary = result.__dict__ if result else {"error": error}
        log_event(
            user_id=ws.owner_id, username=None, role=None, action="WORKSPACE_SOURCE_SCANNED",
            decision="ALLOW" if result else "ERROR", resource_type="workspace", resource_id=str(ws.id),
            reason=status, extra={**{k: v for k, v in summary.items() if k != "errors"},
                                  "seconds": round((datetime.now(UTC) - started).total_seconds(), 1)},
        )
        logger.info("scanned workspace %s: %s", ws.name, summary)
        return True
    finally:
        db.close()


async def run_source_scanner() -> None:
    try:
        await asyncio.to_thread(reset_interrupted_scans)
    except Exception:  # noqa: BLE001
        logger.exception("could not reset interrupted scans")
    while True:
        try:
            worked = await asyncio.to_thread(run_next_scan)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let one scan stop the loop
            logger.exception("source scanner loop failed")
            worked = False
        if not worked:
            await asyncio.sleep(LOOP_SECONDS)
