import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit_logger import log_event
from app.core.database import get_db
from app.core.deps import require_role
from app.core.security import hash_password
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.document import DEPARTMENTS, Document
from app.models.user import ROLES, User
from app.policy.rules import ALL_TOOLS, ROLE_ACCESS, ROLE_TOOLS, is_valid_level, resolve_effective_grant
from app.schemas.alert import AlertOut, AlertPage
from app.schemas.user import UserAccessUpdate, UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _user_out(user: User) -> UserOut:
    grant = resolve_effective_grant(user)
    return UserOut(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        email=user.email,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        extra_departments=sorted(user.extra_departments or []),
        confidentiality_override=user.confidentiality_override,
        extra_tools=sorted(user.extra_tools or []),
        effective_departments=sorted(grant.departments) if grant else [],
        effective_max_confidentiality=grant.max_level if grant else "NONE",
        effective_tools=sorted(grant.tools) if grant else [],
    )


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _director: User = Depends(require_role("DIRECTOR"))) -> list[UserOut]:
    users = db.query(User).order_by(User.role, User.username).all()
    return [_user_out(u) for u in users]


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate, db: Session = Depends(get_db), director: User = Depends(require_role("DIRECTOR"))
) -> UserOut:
    if payload.role not in ROLES:
        raise HTTPException(status_code=422, detail=f"Rôle inconnu. Rôles valides : {', '.join(ROLES)}")

    if db.query(User).filter(User.username == payload.username).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cet identifiant est déjà utilisé.")
    if db.query(User).filter(User.email == payload.email).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette adresse email est déjà utilisée.")

    user = User(
        username=payload.username,
        email=payload.email,
        full_name=payload.full_name,
        role=payload.role,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    log_event(
        user_id=director.id,
        username=director.username,
        role=director.role,
        action="CREATE_USER",
        decision="ALLOW",
        resource_type="user",
        resource_id=str(user.id),
        extra={"created_username": user.username, "role": user.role},
    )
    return _user_out(user)


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    director: User = Depends(require_role("DIRECTOR")),
) -> UserOut:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utilisateur introuvable.")

    if user.id == director.id and (
        (payload.role is not None and payload.role != user.role)
        or (payload.is_active is not None and not payload.is_active)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vous ne pouvez pas modifier votre propre rôle ou vous désactiver vous-même.",
        )

    if payload.role is not None and payload.role not in ROLES:
        raise HTTPException(status_code=422, detail=f"Rôle inconnu. Rôles valides : {', '.join(ROLES)}")

    if payload.email is not None and payload.email != user.email:
        if db.query(User).filter(User.email == payload.email, User.id != user.id).first() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette adresse email est déjà utilisée.")
        user.email = payload.email

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.password:
        user.hashed_password = hash_password(payload.password)

    db.commit()
    db.refresh(user)

    log_event(
        user_id=director.id,
        username=director.username,
        role=director.role,
        action="UPDATE_USER",
        decision="ALLOW",
        resource_type="user",
        resource_id=str(user.id),
        extra={"updated_username": user.username, "role": user.role, "is_active": user.is_active},
    )
    return _user_out(user)


@router.put("/users/{user_id}/access", response_model=UserOut)
def update_user_access(
    user_id: uuid.UUID,
    payload: UserAccessUpdate,
    db: Session = Depends(get_db),
    director: User = Depends(require_role("DIRECTOR")),
) -> UserOut:
    """Grant (or revoke) a per-user exception on top of the role's default
    access - e.g. letting one Developer see FINANCE documents temporarily
    without turning the whole DEVELOPER role into a finance role."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utilisateur introuvable.")

    if payload.extra_departments is not None:
        unknown = set(payload.extra_departments) - set(DEPARTMENTS)
        if unknown:
            raise HTTPException(status_code=422, detail=f"Département(s) inconnu(s) : {', '.join(sorted(unknown))}")
        user.extra_departments = sorted(set(payload.extra_departments))

    if "confidentiality_override" in payload.model_fields_set:
        if payload.confidentiality_override is not None and not is_valid_level(payload.confidentiality_override):
            raise HTTPException(status_code=422, detail="Niveau de confidentialité inconnu.")
        user.confidentiality_override = payload.confidentiality_override

    if payload.extra_tools is not None:
        unknown_tools = set(payload.extra_tools) - ALL_TOOLS
        if unknown_tools:
            raise HTTPException(status_code=422, detail=f"Outil(s) inconnu(s) : {', '.join(sorted(unknown_tools))}")
        user.extra_tools = sorted(set(payload.extra_tools))

    db.commit()
    db.refresh(user)

    log_event(
        user_id=director.id,
        username=director.username,
        role=director.role,
        action="UPDATE_USER_ACCESS",
        decision="ALLOW",
        resource_type="user",
        resource_id=str(user.id),
        extra={
            "updated_username": user.username,
            "extra_departments": user.extra_departments,
            "confidentiality_override": user.confidentiality_override,
            "extra_tools": user.extra_tools,
        },
    )
    return _user_out(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: uuid.UUID, db: Session = Depends(get_db), director: User = Depends(require_role("DIRECTOR"))
) -> None:
    if user_id == director.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Vous ne pouvez pas supprimer votre propre compte.")

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Utilisateur introuvable.")

    username, role = user.username, user.role
    try:
        db.delete(user)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Impossible de supprimer cet utilisateur : il possède des documents ou des conversations "
                "enregistrés. Désactivez son compte à la place pour lui couper l'accès."
            ),
        ) from None

    log_event(
        user_id=director.id,
        username=director.username,
        role=director.role,
        action="DELETE_USER",
        decision="ALLOW",
        resource_type="user",
        resource_id=str(user_id),
        extra={"deleted_username": username, "role": role},
    )


@router.get("/policy")
def get_policy_matrix(_director: User = Depends(require_role("DIRECTOR"))) -> dict:
    """The RBAC/ABAC rule set actually enforced by the backend, for the Admin page to render."""
    return {
        role: {
            "departments": sorted(rules["departments"]),
            "max_level": rules["max_level"],
            "tools": sorted(ROLE_TOOLS.get(role, set())),
        }
        for role, rules in ROLE_ACCESS.items()
    }


@router.get("/stats")
def get_stats(db: Session = Depends(get_db), _director: User = Depends(require_role("DIRECTOR"))) -> dict:
    total_users = db.query(func.count(User.id)).scalar()
    total_documents = db.query(func.count(Document.id)).scalar()
    total_denies = db.query(func.count(AuditLog.id)).filter(AuditLog.decision == "DENY").scalar()
    total_events = db.query(func.count(AuditLog.id)).scalar()
    by_role = dict(db.query(User.role, func.count(User.id)).group_by(User.role).all())
    return {
        "total_users": total_users,
        "total_documents": total_documents,
        "total_audit_events": total_events,
        "total_denied_events": total_denies,
        "users_by_role": by_role,
    }


@router.get("/alerts", response_model=AlertPage)
def list_alerts(
    unread_only: bool = Query(False),
    alert_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _director: User = Depends(require_role("DIRECTOR")),
) -> AlertPage:
    query = db.query(Alert)
    if unread_only:
        query = query.filter(Alert.is_read.is_(False))
    if alert_type:
        query = query.filter(Alert.alert_type == alert_type)

    total = query.count()
    unread_count = db.query(func.count(Alert.id)).filter(Alert.is_read.is_(False)).scalar()
    items = query.order_by(Alert.created_at.desc()).offset(offset).limit(limit).all()
    return AlertPage(
        items=[AlertOut.model_validate(a, from_attributes=True) for a in items],
        total=total,
        unread_count=unread_count,
    )


@router.get("/alerts/unread-count")
def get_unread_alert_count(
    db: Session = Depends(get_db), _director: User = Depends(require_role("DIRECTOR"))
) -> dict:
    return {"unread_count": db.query(func.count(Alert.id)).filter(Alert.is_read.is_(False)).scalar()}


@router.post("/alerts/{alert_id}/read", response_model=AlertOut)
def mark_alert_read(
    alert_id: uuid.UUID, db: Session = Depends(get_db), _director: User = Depends(require_role("DIRECTOR"))
) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alerte introuvable.")
    if not alert.is_read:
        alert.is_read = True
        alert.read_at = datetime.now(UTC)
        db.commit()
        db.refresh(alert)
    return alert


@router.post("/alerts/read-all")
def mark_all_alerts_read(
    db: Session = Depends(get_db), _director: User = Depends(require_role("DIRECTOR"))
) -> dict:
    updated = (
        db.query(Alert)
        .filter(Alert.is_read.is_(False))
        .update({"is_read": True, "read_at": datetime.now(UTC)}, synchronize_session=False)
    )
    db.commit()
    return {"updated": updated}
