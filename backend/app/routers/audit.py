from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_role
from app.models.audit import AuditLog
from app.models.user import User
from app.schemas.audit import AuditLogOut, AuditLogPage

router = APIRouter(prefix="/api/audit-logs", tags=["audit"])


@router.get("", response_model=AuditLogPage)
def list_audit_logs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    decision: str | None = Query(None, pattern="^(ALLOW|DENY)$"),
    db: Session = Depends(get_db),
    _director: User = Depends(require_role("DIRECTOR")),
) -> AuditLogPage:
    query = db.query(AuditLog)
    if decision:
        query = query.filter(AuditLog.decision == decision)

    total = query.count()
    items = query.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit).all()
    return AuditLogPage(items=[AuditLogOut.model_validate(i, from_attributes=True) for i in items], total=total)
