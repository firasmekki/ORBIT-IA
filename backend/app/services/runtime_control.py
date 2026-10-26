"""Pause / resume of the visual analysis queue.

Paused: the vision worker claims no new image (the one being described, if
any, finishes normally); the queue itself is kept untouched. Stored in the
database so a backend restart does not silently resume it.
"""

from sqlalchemy.orm import Session

from app.models.runtime_setting import RuntimeSetting

VISION_QUEUE_KEY = "vision_queue"


def vision_queue_state(db: Session) -> dict:
    row = db.get(RuntimeSetting, VISION_QUEUE_KEY)
    if row is None:
        return {"paused": False, "updated_by": None, "updated_at": None}
    return {
        "paused": bool((row.value or {}).get("paused")),
        "updated_by": row.updated_by,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def is_vision_paused(db: Session) -> bool:
    row = db.get(RuntimeSetting, VISION_QUEUE_KEY)
    return bool(row is not None and (row.value or {}).get("paused"))


def set_vision_paused(db: Session, paused: bool, updated_by: str) -> None:
    row = db.get(RuntimeSetting, VISION_QUEUE_KEY)
    if row is None:
        row = RuntimeSetting(key=VISION_QUEUE_KEY, value={})
        db.add(row)
    row.value = {"paused": paused}
    row.updated_by = updated_by
    db.commit()
