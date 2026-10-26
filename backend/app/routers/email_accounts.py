"""Personal mailbox accounts (Gmail, Outlook; Yahoo prepared, disabled) -
read-only. Every route works on the authenticated user's own connections
only (app/connectivity/email_accounts/service.py); a connection or message
reference of another user is a plain 404.

No route here can send, modify or delete an email: there is none.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.connectivity.email_accounts import oauth, service
from app.connectivity.email_accounts.contract import EmailQuery
from app.connectivity.email_accounts.errors import EmailProviderError
from app.connectivity.email_accounts.providers import ADAPTERS
from app.core.audit_logger import log_event
from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.user import User

router = APIRouter(tags=["email-accounts"])
PREFIX = "/api/connectivity/email-accounts"

_HTTP_STATUS = {
    "account_not_found": 404, "message_not_found": 404, "invalid_request": 422, "account_disabled": 409,
    "oauth_config_missing": 503, "oauth_config_invalid": 503, "provider_disabled": 503,
    "provider_rate_limited": 429,
}


def _fail(error: EmailProviderError) -> HTTPException:
    status = _HTTP_STATUS.get(error.code, 424)  # 424: the provider failed, not this API
    return HTTPException(status, f"{error.category} — {error.user_message}")


def _audit(user: User, action: str, decision: str = "ALLOW", reason: str = "ok", **extra) -> None:
    log_event(user_id=user.id, username=user.username, role=user.role, action=action, decision=decision,
              resource_type="email_account", resource_id="redacted", reason=reason, extra=extra or None)


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_enabled: bool | None = None
    is_primary: bool | None = None
    label: str | None = Field(default=None, max_length=80)


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = Field(default=None, max_length=200)
    from_: str | None = Field(default=None, alias="from", max_length=254)
    subject: str | None = Field(default=None, max_length=200)
    since: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    before: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    unread_only: bool = False
    limit: int | None = Field(default=None, ge=1, le=25)
    cursor: str | None = Field(default=None, max_length=2048)


class RefIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str = Field(min_length=10, max_length=2048)


@router.get("/api/connectivity/email-providers")
def email_providers(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"providers": service.providers_status(db, user.id)}


@router.get(PREFIX)
def email_accounts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"accounts": service.list_accounts(db, user.id), "providers": service.providers_status(db, user.id)}


@router.post(PREFIX + "/{provider}/oauth/start")
def oauth_start(provider: str, reconnect: str | None = None, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    if provider not in ADAPTERS:
        raise HTTPException(404, "Fournisseur inconnu.")
    try:
        url, binding = service.start_authorization(db, user.id, provider, reconnect_id=reconnect)
    except EmailProviderError as error:
        raise _fail(error) from None
    _audit(user, "EMAIL_OAUTH_STARTED", reason="consent_requested", provider=provider)
    response = JSONResponse({"url": url})
    secure = get_settings().email_frontend_redirect_uri.startswith("https://")
    response.set_cookie(oauth.BINDING_COOKIE, binding, max_age=oauth.STATE_TTL_SECONDS, httponly=True,
                        samesite="lax", secure=secure, path="/api/connectivity")
    return response


def _callback(provider: str, request: Request, db: Session) -> RedirectResponse:
    front = get_settings().email_frontend_redirect_uri
    params = request.query_params
    outcome = "connected"
    try:
        user_id, _ = service.complete_authorization(
            db, provider, state=params.get("state"), code=params.get("code"),
            binding=request.cookies.get(oauth.BINDING_COOKIE),
            error=params.get("error"), error_description=params.get("error_description"),
        )
        user = db.get(User, user_id)
        if user is not None:
            _audit(user, "EMAIL_ACCOUNT_CONNECTED", reason="consent_granted", provider=provider)
    except EmailProviderError as error:
        outcome = error.code
    except Exception:  # noqa: BLE001 - never a 500 page on the provider's redirect
        outcome = "provider_unavailable"
    # No code, token or provider message travels back to the frontend.
    response = RedirectResponse(f"{front}?email_oauth={outcome}&provider={provider}", status_code=303)
    response.delete_cookie(oauth.BINDING_COOKIE, path="/api/connectivity")
    return response


# Callback paths are fixed: they are registered as-is with each provider.
@router.get("/api/connectivity/gmail/callback")
def gmail_callback(request: Request, db: Session = Depends(get_db)):
    return _callback("gmail", request, db)


@router.get("/api/connectivity/outlook/callback")
def outlook_callback(request: Request, db: Session = Depends(get_db)):
    return _callback("outlook", request, db)


@router.get("/api/connectivity/yahoo/callback")
def yahoo_callback(request: Request, db: Session = Depends(get_db)):
    return _callback("yahoo", request, db)


@router.patch(PREFIX + "/{connection_id}")
def account_update(connection_id: str, payload: AccountPatch, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    try:
        accounts = service.update_account(db, user.id, connection_id, **payload.model_dump(exclude_unset=True))
    except EmailProviderError as error:
        raise _fail(error) from None
    _audit(user, "EMAIL_ACCOUNT_UPDATED", reason="settings_changed", fields=sorted(payload.model_fields_set))
    return {"accounts": accounts}


@router.delete(PREFIX + "/{connection_id}")
def account_disconnect(connection_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        result = service.disconnect(db, user.id, connection_id)
    except EmailProviderError as error:
        raise _fail(error) from None
    _audit(user, "EMAIL_ACCOUNT_DISCONNECTED", reason=f"revocation_{result['revocation']}")
    return result


@router.post(PREFIX + "/{connection_id}/test")
def account_test(connection_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        account = service.test_connection(db, user.id, connection_id)
    except EmailProviderError as error:
        _audit(user, "EMAIL_ACCOUNT_TEST", decision="ERROR", reason=error.code)
        raise _fail(error) from None
    _audit(user, "EMAIL_ACCOUNT_TEST")
    return {"account": account}


@router.post(PREFIX + "/{connection_id}/refresh-status")
def account_refresh_status(connection_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        return {"account": service.refresh_status(db, user.id, connection_id)}
    except EmailProviderError as error:
        raise _fail(error) from None


@router.post(PREFIX + "/{connection_id}/messages/search")
def messages_search(connection_id: str, payload: SearchIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    from datetime import datetime

    def day(value):
        return datetime.fromisoformat(value + "T00:00:00+00:00") if value else None

    sender = payload.from_
    query = EmailQuery(text=payload.text, subject=payload.subject,
                       from_address=sender if sender and "@" in sender else None,
                       from_name=sender if sender and "@" not in sender else None,
                       received_after=day(payload.since), received_before=day(payload.before),
                       unread_only=payload.unread_only, limit=service.clamp_limit(payload.limit))
    has_filters = any([payload.text, payload.subject, sender])
    try:
        result = service.search(db, user.id, connection_id, query, payload.cursor, recent=not has_filters)
    except EmailProviderError as error:
        _audit(user, "EMAIL_SEARCH", decision="ERROR", reason=error.code)
        raise _fail(error) from None
    _audit(user, "EMAIL_SEARCH", result_count=len(result["items"]))
    return result


@router.post(PREFIX + "/{connection_id}/messages/read")
def messages_read(connection_id: str, payload: RefIn, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    try:
        ref_connection, _ = service.parse_ref(payload.ref)
        if ref_connection != connection_id:
            raise EmailProviderError("message_not_found")
        result = service.get_message(db, user.id, payload.ref)
    except EmailProviderError as error:
        raise _fail(error) from None
    _audit(user, "EMAIL_READ")
    return result


@router.post(PREFIX + "/{connection_id}/threads/read")
def threads_read(connection_id: str, payload: RefIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    try:
        ref_connection, _ = service.parse_ref(payload.ref)
        if ref_connection != connection_id:
            raise EmailProviderError("message_not_found")
        result = service.get_thread(db, user.id, payload.ref)
    except EmailProviderError as error:
        raise _fail(error) from None
    _audit(user, "EMAIL_THREAD_READ", message_count=len(result["messages"]))
    return result
