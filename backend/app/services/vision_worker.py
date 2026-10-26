"""Progressive visual analysis of images that have no OCR text.

A photo, an icon or a wiring diagram without printed text is not an error
(status "no_text", kind "no_text_image"): it stays in the inventory. When
the owner asks for it ("Analyser visuellement", status "visual_queued"),
this worker sends those images ONE AT A TIME to the local vision model
(Qwen3-VL, app/agent/vision_client.py) - never the whole folder, never at
question time - and stores the description as the document's text, marked
as an automatic, unverified observation. From then on the image is found
by the normal search like any document, and the result stays attached to
its source file (re-done only if the file changes).

    no_text --(owner asks)--> visual_queued -> visual_running -> indexing -> indexed
                                    ^               |                (embedding_worker)
                                    +-- retry ------+-> no_text (analysis impossible after
                                                                 MAX_ATTEMPTS, reason kept)

The queue is processed only while there is something to do. Each pass:
  - nothing queued -> one cheap database query, no model call at all;
  - paused by an administrator (runtime_control) -> nothing is claimed, the
    queue is kept as it is;
  - a chat answer is being generated -> waits: chat has the GPU first;
  - vision server absent -> nothing is claimed, the next check waits longer
    each time (30 s, 60 s... up to 10 min) - never a tight retry loop;
  - an image fails (bad answer, too slow) -> back in the queue, after the
    others, at most MAX_ATTEMPTS times; then "no_text" with the reason;
  - the server goes away mid-image -> back in the queue untouched (no
    attempt counted); after a backend restart, an image left
    "visual_running" is queued again (requeue_interrupted).
"""

import asyncio
import logging
import re
import time
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from app.agent.llm_client import LLMServiceError
from app.agent.vision_client import VisionUnavailableError, describe_image, get_vision_status
from app.core.activity import WorkerActivity, foreground_turns
from app.core.audit_logger import log_event
from app.core.database import SessionLocal
from app.core.storage import download_file
from app.models.document import Document
from app.rag.extract import ExtractedPage
from app.rag.ingest import write_pages
from app.services.runtime_control import is_vision_paused

logger = logging.getLogger("orbitia.vision_worker")

IDLE_SECONDS = 10
RETRY_SECONDS = 60
BACKOFF_START_SECONDS = 30
BACKOFF_MAX_SECONDS = 600
MAX_ATTEMPTS = 3
MAX_SIDE = 1600  # downscaled before sending: faster, and well within the pixel limit
IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "webp")
VISUAL_PREFIX = "[Analyse visuelle automatique - observation du modèle vision, à vérifier]"
PROMPT = (
    "Cette image provient des dossiers d'un bureau d'études (énergie, électricité, industrie). "
    "Commence la description par exactement « Catégorie : technique. », « Catégorie : photo. » ou "
    "« Catégorie : décorative. » (technique = schéma, plan, synoptique, capture d'écran, tableau, document ; "
    "photo = site, équipement, chantier ; décorative = icône, logo, illustration sans information). "
    "Décris ensuite en français ce qui est visible et recopie tout texte lisible."
)
_CATEGORY_RE = re.compile(r"cat[ée]gorie\s*:\s*(technique|photo|d[ée]corative)", re.IGNORECASE)

# What one pass of the worker did.
WORKED = "worked"  # an image was handled (described, or set aside with its reason)
IDLE = "idle"  # nothing queued
PAUSED = "paused"  # queue paused by an administrator
YIELDED = "yielded"  # a chat answer is being generated
UNAVAILABLE = "unavailable"  # vision server absent / loading


def _prepare(content: bytes) -> bytes:
    """Re-encoded as a bounded JPEG: the declared extension may lie (a .jpg
    that is a PNG), and a 12 MP photo needs no 12 MP for a description."""
    with Image.open(BytesIO(content)) as image:
        image = image.convert("RGB")
        image.thumbnail((MAX_SIDE, MAX_SIDE))
        out = BytesIO()
        image.save(out, format="JPEG", quality=88)
        return out.getvalue()


def _category(description: str) -> str:
    match = _CATEGORY_RE.search(description or "")
    if not match:
        return "photo"
    return {"technique": "technique", "photo": "photo"}.get(match.group(1).lower(), "decorative")


def _claim(db) -> Document | None:
    """SKIP LOCKED: two workers can never take the same image. Images that
    already failed come last, so one bad image never blocks the queue."""
    doc = (
        db.query(Document)
        .filter(Document.status == "visual_queued", Document.is_active.is_(True))
        .order_by(Document.index_attempts, Document.size_bytes.asc().nulls_first(), Document.created_at)
        .with_for_update(skip_locked=True)
        .first()
    )
    if doc is None:
        db.rollback()
        return None
    doc.status = "visual_running"
    db.commit()
    return doc


def requeue_interrupted() -> int:
    db = SessionLocal()
    try:
        count = (
            db.query(Document)
            .filter(Document.status == "visual_running")
            .update({Document.status: "visual_queued"}, synchronize_session=False)
        )
        db.commit()
        return count
    finally:
        db.close()


def process_next_visual() -> str:
    """Handles at most one queued image; returns what happened (WORKED,
    IDLE, PAUSED, YIELDED, UNAVAILABLE). Nothing is claimed unless the
    image can really be sent to a ready model."""
    db = SessionLocal()
    try:
        pending = db.query(Document.id).filter(Document.status == "visual_queued", Document.is_active.is_(True)).first()
        if pending is None:
            db.rollback()
            return IDLE
        if is_vision_paused(db):
            db.rollback()
            return PAUSED
        if foreground_turns() > 0:
            db.rollback()
            return YIELDED
        # /health answers 200 while the model sleeps: the request below
        # wakes it (llama-server --sleep-idle-seconds).
        if asyncio.run(get_vision_status()).get("status") != "ready":
            db.rollback()
            return UNAVAILABLE
        doc = _claim(db)
        if doc is None:
            return IDLE
        doc_id, content_hash = doc.id, doc.content_hash
        try:
            image = _prepare(download_file(doc.minio_object_key))
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            _finish_without_text(db, doc_id, content_hash, f"analyse visuelle impossible : image illisible ({exc})")
            return WORKED
        started = time.monotonic()
        try:
            observation, _ = asyncio.run(
                describe_image(image=image, mime_type="image/jpeg", prompt=PROMPT)
            )
        except VisionUnavailableError:
            # The server went away mid-way (stopped, restarted): back to the
            # queue untouched, no attempt counted.
            _requeue(db, doc_id)
            return UNAVAILABLE
        except LLMServiceError as exc:
            _record_failure(db, doc_id, content_hash, str(exc))
            return WORKED

        current = (
            db.query(Document)
            .filter(Document.id == doc_id, Document.content_hash == content_hash, Document.status == "visual_running")
            .with_for_update()
            .one_or_none()
        )
        if current is None:  # changed or removed meanwhile
            db.rollback()
            return WORKED
        description = (observation.get("description") or "").strip()
        if not description:
            db.rollback()
            _record_failure(db, doc_id, content_hash, "aucune description produite")
            return WORKED
        category = _category(description)
        visible = [str(t) for t in observation.get("text") or [] if str(t).strip()]
        text = "\n".join(
            [VISUAL_PREFIX, description]
            + (["Texte visible : " + " | ".join(visible)] if visible else [])
        )
        current.content = text
        # Not "indexed" yet: only the embedding worker sets that, once the
        # description is really searchable.
        current.status = "indexing"
        current.index_progress = 0
        current.index_error = None
        current.index_attempts = 0
        current.issue_kind = f"visual_{category}"
        db.commit()
        db.refresh(current)
        write_pages(db, current, [ExtractedPage(page_no=None, section="Analyse visuelle", line_offset=1, text=text)])
        log_event(
            user_id=current.owner_id, username=None, role=None, action="DOCUMENT_VISUAL_ANALYSIS", decision="ALLOW",
            resource_type="document", resource_id=str(doc_id), reason=category,
            extra={"relative_path": current.relative_path},
        )
        logger.info("described %s (%s) in %.1fs", current.relative_path or current.title, category, time.monotonic() - started)
        return WORKED
    finally:
        db.close()


def _requeue(db, doc_id) -> None:
    db.rollback()
    db.query(Document).filter(Document.id == doc_id, Document.status == "visual_running").update(
        {Document.status: "visual_queued"}, synchronize_session=False
    )
    db.commit()


def _record_failure(db, doc_id, content_hash, reason: str) -> None:
    """One failed attempt for this image: back in the queue (after the
    others) or, after MAX_ATTEMPTS, set aside as "no_text" with the reason."""
    doc = (
        db.query(Document)
        .filter(Document.id == doc_id, Document.content_hash == content_hash, Document.status == "visual_running")
        .with_for_update()
        .one_or_none()
    )
    if doc is None:
        db.rollback()
        return
    doc.index_attempts = (doc.index_attempts or 0) + 1
    if doc.index_attempts >= MAX_ATTEMPTS:
        doc.status = "no_text"
        doc.index_error = f"analyse visuelle impossible après {doc.index_attempts} tentatives : {reason}"[:2000]
        logger.warning("visual analysis gave up on %s: %s", doc.relative_path or doc.title, reason)
    else:
        doc.status = "visual_queued"
        doc.index_error = f"analyse visuelle : tentative {doc.index_attempts}/{MAX_ATTEMPTS} échouée ({reason})"[:2000]
        logger.info("visual analysis attempt %d failed for %s: %s", doc.index_attempts, doc.relative_path or doc.title, reason)
    db.commit()


def _finish_without_text(db, doc_id, content_hash, reason: str) -> None:
    db.query(Document).filter(
        Document.id == doc_id, Document.content_hash == content_hash, Document.status == "visual_running"
    ).update({Document.status: "no_text", Document.index_error: reason[:2000]}, synchronize_session=False)
    db.commit()


async def run_vision_worker() -> None:
    try:
        requeued = await asyncio.to_thread(requeue_interrupted)
        if requeued:
            logger.info("requeued %d image(s) interrupted mid-analysis", requeued)
    except Exception:  # noqa: BLE001
        logger.exception("could not requeue interrupted visual analyses")
    activity = WorkerActivity("vision")
    backoff = BACKOFF_START_SECONDS
    while True:
        try:
            outcome = await asyncio.to_thread(process_next_visual)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never let one image stop the worker
            logger.exception("visual analysis failed, retrying in %ss", RETRY_SECONDS)
            await asyncio.sleep(RETRY_SECONDS)
            continue
        if outcome == WORKED:
            activity.record(True)
            backoff = BACKOFF_START_SECONDS
            continue
        if outcome == UNAVAILABLE:
            activity.set_state("waiting_model")
            logger.warning("vision model unavailable, queue kept; next check in %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)
            continue
        backoff = BACKOFF_START_SECONDS
        if outcome == PAUSED:
            activity.set_state("paused")
        elif outcome == YIELDED:
            activity.set_state("waiting_chat")
        else:
            activity.record(False)
        await asyncio.sleep(IDLE_SECONDS)
