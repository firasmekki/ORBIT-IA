"""Administration of the AI engine: state of the model servers, queues and
workers; pause / resume / cancel of the visual analysis queue.

The model servers (llama-server.exe) are native Windows processes started
by start-orbit-ai.ps1, outside Docker: this API reports their state but
does not start or kill them - orbit-models.ps1 does, on the host. Closing
the browser changes nothing here: the queues and servers live on their own,
and each model unloads itself after its configured idle period.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import activity
from app.core.audit_logger import log_event
from app.core.database import get_db
from app.core.deps import require_role
from app.models.document import Document
from app.models.user import User
from app.services import model_runtime, runtime_control

router = APIRouter(prefix="/api/admin/runtime", tags=["admin"])

QUEUE_STATUSES = ("queued", "extracting", "indexing", "visual_queued", "visual_running", "failed")


def _queues(db: Session) -> dict:
    counts = dict(
        db.query(Document.status, func.count(Document.id))
        .filter(Document.is_active.is_(True), Document.status.in_(QUEUE_STATUSES))
        .group_by(Document.status)
        .all()
    )
    retrying = (
        db.query(func.count(Document.id))
        .filter(Document.is_active.is_(True), Document.status == "visual_queued", Document.index_attempts > 0)
        .scalar()
    )
    return {**{status: counts.get(status, 0) for status in QUEUE_STATUSES}, "visual_retrying": retrying}


@router.get("")
async def runtime_state(db: Session = Depends(get_db), _director: User = Depends(require_role("DIRECTOR"))) -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "models": await model_runtime.models_state(),
        "chat_turns_in_progress": activity.foreground_turns(),
        "queues": _queues(db),
        "vision_queue": runtime_control.vision_queue_state(db),
        "workers": activity.workers_snapshot(),
        "docker_vm_memory": model_runtime.docker_vm_memory(),
    }


def _set_paused(db: Session, user: User, paused: bool) -> dict:
    runtime_control.set_vision_paused(db, paused, updated_by=user.username)
    log_event(
        user_id=user.id, username=user.username, role=user.role,
        action="VISION_QUEUE_PAUSED" if paused else "VISION_QUEUE_RESUMED", decision="ALLOW",
        resource_type="runtime", resource_id=runtime_control.VISION_QUEUE_KEY, reason="ok",
        extra={"queued": _queues(db)["visual_queued"]},
    )
    return runtime_control.vision_queue_state(db)


@router.post("/vision/pause")
def pause_vision(db: Session = Depends(get_db), user: User = Depends(require_role("DIRECTOR"))) -> dict:
    """No new image is started; the one in progress (if any) finishes; the
    queue is kept."""
    return _set_paused(db, user, True)


@router.post("/vision/resume")
def resume_vision(db: Session = Depends(get_db), user: User = Depends(require_role("DIRECTOR"))) -> dict:
    return _set_paused(db, user, False)


@router.post("/vision/cancel")
def cancel_vision(db: Session = Depends(get_db), user: User = Depends(require_role("DIRECTOR"))) -> dict:
    """Empties the queue: the images not started yet go back to "image
    without text", ready to be queued again later ("Analyser visuellement").
    The image in progress, if any, finishes normally. Nothing is deleted."""
    count = (
        db.query(Document)
        .filter(Document.is_active.is_(True), Document.status == "visual_queued")
        .update(
            {
                Document.status: "no_text",
                Document.index_attempts: 0,
                Document.index_error: "analyse visuelle annulée par un administrateur - peut être relancée",
            },
            synchronize_session=False,
        )
    )
    db.commit()
    log_event(
        user_id=user.id, username=user.username, role=user.role, action="VISION_QUEUE_CANCELLED", decision="ALLOW",
        resource_type="runtime", resource_id=runtime_control.VISION_QUEUE_KEY, reason="ok", extra={"images": count},
    )
    return {"cancelled": count}
