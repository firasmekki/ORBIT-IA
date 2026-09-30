"""Automatic folder ingestion.

Polls a directory mounted into the backend container (see
docker-compose.yml `backend.volumes`) and ingests any new supported file it
finds, exactly like the manual upload endpoint does - same extraction,
same MinIO storage, same RAG indexing, same department/confidentiality
tagging (all files dropped in this folder share one classification, set
once via WATCH_FOLDER_* env vars).

A plain polling loop rather than an inotify-based watcher (no new
dependency, works identically across the bind-mount edge cases Docker
Desktop on Windows/macOS can have with native filesystem events) - a few
seconds of latency for a background import is an acceptable trade-off.
"""

import hashlib
import logging
import time
import uuid
from pathlib import Path

import anyio
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.storage import upload_file
from app.models.document import CONFIDENTIALITY_RANK, Document
from app.rag.extract import SUPPORTED_EXTENSIONS, ExtractionError, UnsupportedFileTypeError, extract
from app.rag.ingest import ingest_document

logger = logging.getLogger("orbitia.watcher")
settings = get_settings()


async def run_folder_watcher() -> None:
    if not settings.watch_folder_enabled:
        return
    watch_path = Path(settings.watch_folder_path)
    logger.info("Folder watcher started on %s (every %ss)", watch_path, settings.watch_folder_interval_seconds)
    while True:
        try:
            await anyio.to_thread.run_sync(_scan_once, watch_path)
        except Exception:  # noqa: BLE001 - one bad scan must never kill the loop
            logger.exception("Folder watcher scan failed")
        await anyio.sleep(settings.watch_folder_interval_seconds)


def _scan_once(watch_path: Path) -> None:
    if not watch_path.is_dir():
        return
    db = SessionLocal()
    try:
        for entry in sorted(watch_path.iterdir()):
            if entry.is_file():
                _maybe_ingest(db, entry)
    finally:
        db.close()


def _maybe_ingest(db: Session, path: Path) -> None:
    ext = path.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTENSIONS:
        return

    # Match on basename, not exact source_filename: manual folder uploads
    # store a relative path (e.g. "test/report.xlsx") while the watcher only
    # ever sees the bare filename - an exact-string compare would miss that
    # collision and re-import (and re-embed) the same file a second time.
    existing = (
        db.query(Document)
        .filter(
            Document.source_filename.ilike(f"%{path.name}"),
            Document.department == settings.watch_folder_department,
        )
        .first()
    )

    # Skip a file that's still being copied/written: size must be stable
    # across two checks a second apart before we touch it.
    try:
        size_before = path.stat().st_size
    except OSError:
        return
    if size_before == 0:
        return
    time.sleep(1)
    try:
        if not path.exists() or path.stat().st_size != size_before:
            return
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("Could not read %s, will retry next scan: %s", path.name, exc)
        return

    content_hash = hashlib.sha256(raw).hexdigest()
    if existing is not None and existing.content_hash == content_hash:
        return  # already imported, file unchanged since - nothing to do

    try:
        extraction = extract(path.name, raw)
    except (UnsupportedFileTypeError, ExtractionError) as exc:
        logger.warning("Skipping %s (not indexed - fix and it will be retried next scan): %s", path.name, exc)
        return

    confidentiality = settings.watch_folder_confidentiality
    object_key = f"watch-{uuid.uuid4()}-{path.name}"
    try:
        upload_file(object_key, raw, content_type="application/octet-stream")
    except Exception as exc:  # noqa: BLE001
        logger.warning("MinIO unavailable, will retry %s next scan: %s", path.name, exc)
        return

    if existing is not None:
        # Same filename, different (or never-hashed, i.e. imported before
        # content_hash existed) content - re-index in place rather than
        # creating a duplicate: same document_id, fresh content/pages/
        # chunks/embeddings, new MinIO object (the old one is left orphaned
        # rather than deleted, consistent with how re-uploads elsewhere in
        # the app never delete prior file bytes either).
        doc = existing
        doc.content = extraction.text
        doc.content_hash = content_hash
        doc.minio_object_key = object_key
        db.commit()
        action, verb = "re-indexed changed file", "Re-indexed"
    else:
        doc = Document(
            title=path.stem,
            department=settings.watch_folder_department,
            confidentiality=confidentiality,
            confidentiality_rank=CONFIDENTIALITY_RANK[confidentiality],
            content=extraction.text,
            content_hash=content_hash,
            source_filename=path.name,
            minio_object_key=object_key,
            owner_id=None,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        action, verb = "auto-imported", "Auto-imported"

    try:
        ingest_document(db, doc, pages=extraction.pages)
    except Exception:  # noqa: BLE001 - document exists even if embedding failed; ingest_document is re-run-safe
        logger.exception("Embedding failed for %s %s (document saved, will not retry)", action, path.name)
    else:
        logger.info("%s %s -> document %s", verb, path.name, doc.id)
