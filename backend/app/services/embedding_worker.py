"""Background embedding of synced files.

A synced file is stored and exact-searchable as soon as it is uploaded
(app/services/workspace_sync.py writes its text and pages, status
"indexing"). The embeddings - only semantic search needs them, and a big
CSV has tens of thousands of chunks - are computed here, one document at a
time, in batches, outside any HTTP request.

The queue IS the database (documents with status "indexing"), so nothing is
lost on a restart: the worker simply picks them up again. If the embedding
service is down, it waits and retries; the document stays exact-searchable
meanwhile.
"""

import asyncio
import logging
from datetime import UTC, datetime

from app.core.activity import WorkerActivity
from app.core.audit_logger import log_event
from app.core.database import SessionLocal
from app.models.document import Document
from app.rag import ingest
from app.rag.embeddings import EmbeddingServiceError
from app.rag.ingest import embed_document

logger = logging.getLogger("orbitia.embedding_worker")

IDLE_SECONDS = 3
RETRY_SECONDS = 30
MAX_INDEX_ATTEMPTS = 3


def _embedding_service_up() -> bool:
    """Same entry point as embed_document: if THIS answers, the service is
    up and the failure belongs to the document."""
    try:
        ingest.embed_texts(["ping"])
        return True
    except Exception:  # noqa: BLE001
        return False


def _record_failure(db, doc_id, content_hash, error: str) -> None:
    """One more failed attempt for this version of the document. Below the
    limit it goes to the back of the queue; at the limit it is "failed",
    with the reason. Its text stays exact-searchable either way."""
    doc = (
        db.query(Document)
        .filter(Document.id == doc_id, Document.content_hash == content_hash, Document.status == "indexing")
        .with_for_update()
        .one_or_none()
    )
    if doc is None:
        db.rollback()
        return
    doc.index_attempts = (doc.index_attempts or 0) + 1
    doc.index_error = (
        f"{error} - le texte reste consultable par recherche exacte" if doc.index_attempts >= MAX_INDEX_ATTEMPTS else error
    )[:2000]
    if doc.index_attempts >= MAX_INDEX_ATTEMPTS:
        doc.status = "failed"
        doc.issue_kind = "embedding"
        doc.index_progress = None
    db.commit()
    log_event(
        user_id=doc.owner_id, username=None, role=None, action="DOCUMENT_EMBEDDING_FAILED", decision="ERROR",
        resource_type="document", resource_id=str(doc.id), reason=error[:500],
        extra={"relative_path": doc.relative_path, "attempt": doc.index_attempts},
    )


def process_next_document() -> bool:
    """Embeds the oldest pending document. Returns False when the queue is
    empty. Raises EmbeddingServiceError when the embedding service is down."""
    db = SessionLocal()
    try:
        doc = (
            db.query(Document)
            .filter(Document.status == "indexing", Document.is_active.is_(True))
            # Documents that already failed go after the others.
            .order_by(Document.index_attempts, Document.created_at)
            .first()
        )
        if doc is None:
            return False
        started_hash = doc.content_hash

        def still_wanted() -> bool:
            # Re-synced (new content) or removed from the folder meanwhile:
            # stop - the new version is queued again on its own.
            current = (
                db.query(Document.content_hash, Document.is_active, Document.status)
                .filter(Document.id == doc.id)
                .one_or_none()
            )
            return current is not None and current.content_hash == started_hash and current.is_active and current.status == "indexing"

        def on_progress(done: int, total: int) -> None:
            db.query(Document).filter(Document.id == doc.id, Document.content_hash == started_hash).update(
                {Document.index_progress: int(done * 100 / total) if total else 100}
            )
            db.commit()

        try:
            chunk_count = embed_document(db, doc, on_progress=on_progress, still_wanted=still_wanted)
        except EmbeddingServiceError as exc:
            db.rollback()
            if not _embedding_service_up():
                raise  # the service is down: wait, every document stays queued
            # The service works, THIS document does not: count it, so one bad
            # file can never block the whole queue (oldest first).
            _record_failure(db, doc.id, started_hash, f"embeddings impossibles : {exc}")
            return True
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception("embedding crashed on %s", doc.relative_path or doc.title)
            _record_failure(db, doc.id, started_hash, f"embeddings impossibles : {exc.__class__.__name__}: {exc}")
            return True
        if chunk_count < 0 or not still_wanted():
            return True
        doc.status = "indexed"
        doc.index_progress = 100
        doc.index_error = None
        doc.index_attempts = 0
        doc.indexed_at = datetime.now(UTC)
        db.commit()
        log_event(
            user_id=doc.owner_id, username=None, role=None, action="DOCUMENT_EMBEDDED", decision="ALLOW",
            resource_type="document", resource_id=str(doc.id), reason="ok",
            extra={"relative_path": doc.relative_path, "chunks": chunk_count},
        )
        logger.info("embedded %s (%d chunks)", doc.relative_path or doc.title, chunk_count)
        return True
    finally:
        db.close()


async def run_embedding_worker() -> None:
    activity = WorkerActivity("embedding")
    while True:
        try:
            worked = await asyncio.to_thread(process_next_document)
            activity.record(worked)
        except asyncio.CancelledError:
            raise
        except EmbeddingServiceError as exc:
            logger.warning("embedding service unavailable, retrying in %ss: %s", RETRY_SECONDS, exc)
            await asyncio.sleep(RETRY_SECONDS)
            continue
        except Exception:  # noqa: BLE001 - never let one bad document stop the worker
            logger.exception("background embedding failed, retrying in %ss", RETRY_SECONDS)
            await asyncio.sleep(RETRY_SECONDS)
            continue
        if not worked:
            await asyncio.sleep(IDLE_SECONDS)
