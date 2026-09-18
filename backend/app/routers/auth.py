from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.audit_logger import log_event
from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.policy.rules import resolve_effective_grant
from app.schemas.auth import LoginRequest, MeResponse, SelfUpdateRequest, TokenResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(User).filter(User.username == payload.username).first()

    if user is None or not user.is_active or not verify_password(payload.password, user.hashed_password):
        log_event(
            user_id=user.id if user else None,
            username=payload.username,
            role=user.role if user else None,
            action="LOGIN",
            decision="DENY",
            reason="invalid credentials",
            extra={"ip": request.client.host if request.client else None},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Identifiants incorrects.")

    token = create_access_token(user_id=str(user.id), role=user.role, full_name=user.full_name)
    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="LOGIN",
        decision="ALLOW",
        extra={"ip": request.client.host if request.client else None},
    )
    return TokenResponse(access_token=token)


def _me_response(user: User) -> MeResponse:
    grant = resolve_effective_grant(user)
    departments = sorted(grant.departments) if grant else []
    max_level = grant.max_level if grant else "NONE"
    tools = sorted(grant.tools) if grant else []
    return MeResponse(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        email=user.email,
        role=user.role,
        departments=departments,
        max_confidentiality=max_level,
        allowed_tools=tools,
    )


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user)) -> MeResponse:
    return _me_response(user)


@router.patch("/me", response_model=MeResponse)
def update_me(
    payload: SelfUpdateRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> MeResponse:
    """Self-service profile edit. Role and access overrides are deliberately
    not editable here - only a Director can grant those, from Admin."""
    if payload.new_password:
        if not payload.current_password or not verify_password(payload.current_password, user.hashed_password):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Mot de passe actuel incorrect.")
        user.hashed_password = hash_password(payload.new_password)

    if payload.email is not None and payload.email != user.email:
        if db.query(User).filter(User.email == payload.email, User.id != user.id).first() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cette adresse email est déjà utilisée.")
        user.email = payload.email

    if payload.full_name is not None:
        user.full_name = payload.full_name

    db.commit()
    db.refresh(user)

    log_event(
        user_id=user.id,
        username=user.username,
        role=user.role,
        action="SELF_UPDATE",
        decision="ALLOW",
        resource_type="user",
        resource_id=str(user.id),
        extra={"password_changed": bool(payload.new_password)},
    )
    return _me_response(user)
