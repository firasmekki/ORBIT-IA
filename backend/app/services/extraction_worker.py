"""Background text extraction of synced files.

/sync/file only stores a file and queues it (status "queued", see
workspace_sync.index_file(defer_extraction=True)), so selecting a folder
takes seconds, not the hours its OCR would take inside each HTTP request.
The text is extracted here, several files at a time (Tesseract runs as a
separate process, so threads really run in parallel), smallest files
first: most documents become readable quickly while a scanned binder is
still being OCR'd.

queued -> extracting -> indexing (embedding_worker.py) -> indexed
                     -> failed (no extractable text; reason in the audit log)

The queue IS the database, like the embedding queue: nothing is lost on a
restart - documents left "extracting" by a stopped process are queued
again at startup. A row is claimed with FOR UPDATE SKIP LOCKED, so two
workers never extract the same file.
"""

import asyncio
import logging
import os

from app.core.activity import WorkerActivity
from app.core.audit_logger import log_event
from app.core.database import SessionLocal
from app.core.storage import download_file
from app.models.document import Document
from app.rag.extract import NO_TEXT_KINDS, ExtractionError, UnsupportedFileTypeError, extract
from app.rag.ingest import write_pages

logger = logging.getLogger("orbitia.extraction_worker")

IDLE_SECONDS = 2
RETRY_SECONDS = 30
# A file that crashes the extractor itself (not a clean "no text" answer)
# is retried this many times, then marked failed with the reason - it can
# never stay "extracting" forever.
MAX_INDEX_ATTEMPTS = 3

# One Tesseract thread per file: the parallelism comes from extracting
# several files at once, without oversubscribing the CPU the local LLM
# also needs.
os.environ.setdefault("OMP_THREAD_LIMIT", "1")


def requeue_interrupted() -> int:
    """Documents a stopped process left half-extracted go back to the queue."""
    db = SessionLocal()
    try:
        count = (
            db.query(Document)
            .filter(Document.status == "extracting")
            .update({Document.status: "queued"}, synchronize_session=False)
        )
        db.commit()
        return count
    finally:
        db.close()


def _claim_next(db) -> Document | None:
    doc = (
        db.query(Document)
        .filter(Document.status == "queued", Document.is_active.is_(True))
        .order_by(Document.index_attempts, Document.size_bytes.asc().nulls_first(), Document.created_at)
        .with_for_update(skip_locked=True)
        .first()
    )
    if doc is None:
        db.rollback()
        return None
    doc.status = "extracting"
    db.commit()
    return doc


def _lock_if_current(db, doc_id, content_hash) -> Document | None:
    """The document, row-locked, if it is still the version we extracted:
    re-synced (new content) or removed meanwhile -> None, the newer version
    is queued on its own."""
    return (
        db.query(Document)
        .filter(
            Document.id == doc_id,
            Document.content_hash == content_hash,
            Document.status == "extracting",
            Document.is_active.is_(True),
        )
        .with_for_update()
        .one_or_none()
    )


def process_next_extraction() -> bool:
    """Extracts the next queued document. Returns False when the queue is empty."""
    db = SessionLocal()
    try:
        doc = _claim_next(db)
        if doc is None:
            return False
        doc_id, content_hash = doc.id, doc.content_hash
        filename = doc.source_filename or doc.title
        label = doc.relative_path or doc.title

        try:
            content = download_file(doc.minio_object_key)
        except Exception:
            # Object storage hiccup: back to the queue, retried later.
            db.query(Document).filter(Document.id == doc_id, Document.status == "extracting").update(
                {Document.status: "queued"}, synchronize_session=False
            )
            db.commit()
            raise
        crashed = False
        kind = None
        try:
            extraction = extract(filename, content)
            error = None
        except ExtractionError as exc:
            extraction, error, kind = None, str(exc), exc.kind
        except UnsupportedFileTypeError as exc:
            extraction, error, kind = None, str(exc), "unsupported"
        except Exception as exc:  # noqa: BLE001 - a corrupt file crashing a parser library
            logger.exception("extractor crashed on %s", label)
            extraction, error, crashed = None, f"erreur de lecture du fichier : {exc.__class__.__name__}: {exc}", True
            kind = "corrupt"

        current = _lock_if_current(db, doc_id, content_hash)
        if current is None:
            db.rollback()
            return True
        if crashed:
            current.index_attempts = (current.index_attempts or 0) + 1
            current.index_error = error[:2000]
            if current.index_attempts < MAX_INDEX_ATTEMPTS:
                current.status = "queued"  # retried after the rest of the queue (smallest first)
                db.commit()
                return True
        if extraction is None:
            # An image/scan with no text is not an error: kept, listed apart,
            # and open to visual analysis (app/services/vision_worker.py).
            current.status = "no_text" if kind in NO_TEXT_KINDS else "failed"
            current.issue_kind = kind
            current.index_progress = None
            current.index_error = (error or "aucun texte extractible")[:2000]
            db.commit()
            log_event(
                user_id=current.owner_id, username=None, role=None, action="DOCUMENT_EXTRACTION_FAILED", decision="ALLOW",
                resource_type="document", resource_id=str(doc_id), reason=error,
                extra={"relative_path": current.relative_path},
            )
            logger.info("no text in %s: %s", label, error)
            return True

        current.content = extraction.text
        current.status = "indexing"
        current.index_progress = 0
        current.index_error = None
        current.index_attempts = 0
        current.issue_kind = None
        db.commit()
        db.refresh(current)
        write_pages(db, current, extraction.pages)
        log_event(
            user_id=current.owner_id, username=None, role=None, action="DOCUMENT_EXTRACTED", decision="ALLOW",
            resource_type="document", resource_id=str(doc_id), reason="ok",
            extra={"relative_path": current.relative_path, "pages": len(extraction.pages)},
        )
        logger.info("extracted %s (%d blocks)", label, len(extraction.pages))
        return True
    finally:
        db.close()


async def _extraction_loop(index: int = 0) -> None:
    activity = WorkerActivity(f"extraction-{index}")
    while True:
        try:
            worked = await asyncio.to_thread(process_next_extraction)
            activity.record(worked)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let one bad file stop the worker
            logger.exception("background extraction failed, retrying in %ss", RETRY_SECONDS)
            await asyncio.sleep(RETRY_SECONDS)
            continue
        if not worked:
            await asyncio.sleep(IDLE_SECONDS)


async def run_extraction_worker(concurrency: int) -> None:
    try:
        requeued = await asyncio.to_thread(requeue_interrupted)
        if requeued:
            logger.info("requeued %d document(s) interrupted mid-extraction", requeued)
    except Exception:  # noqa: BLE001 - the loops below retry on their own
        logger.exception("could not requeue interrupted extractions")
    await asyncio.gather(*(_extraction_loop(i) for i in range(max(1, concurrency))))
