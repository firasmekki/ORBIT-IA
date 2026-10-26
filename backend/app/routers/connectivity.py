"""Director-managed connectors (shared IMAP/SMTP mailbox, web search) and
WhatsApp click-to-chat links. Personal mailboxes (Gmail, Outlook) live in
routers/email_accounts.py - read-only, per user."""

import copy
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from app.connectivity import email as email_connector
from app.connectivity.email_accounts.service import provider_issues
from app.connectivity.registry import list_connectors
from app.core.audit_logger import log_event
from app.core.database import get_db
from app.core.deps import get_current_user, require_role
from app.models.audit import AuditLog
from app.models.user import User
from app.models.runtime_setting import RuntimeSetting
from app.websearch.config import load_config
from app.websearch.service import last_status as web_last_status

router = APIRouter(tags=["connectivity"])


class EmailSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    imap_host: str | None = Field(default=None, max_length=255)
    imap_port: int | None = Field(default=None, ge=1, le=65535)
    imap_ssl: bool | None = None
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_ssl: bool | None = None
    username: str | None = Field(default=None, max_length=255)
    mailbox: str | None = Field(default=None, max_length=255)
    allowed_roles: list[str] | None = None
    tool_roles: dict[str, list[str]] | None = None
    send_roles: list[str] | None = None
    tools: dict[str, bool] | None = None


class SecretIn(BaseModel):
    password: str = Field(min_length=8, max_length=500)

    @field_validator("password")
    @classmethod
    def no_newline(cls, value):
        if "\n" in value or "\r" in value:
            raise ValueError("invalid password")
        return value


class DraftIn(BaseModel):
    to: str = Field(min_length=3, max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)


class SendIn(DraftIn):
    confirmation: str = Field(min_length=36, max_length=36)


class EmailSearchIn(BaseModel):
    from_: str | None = Field(default=None, alias="from", max_length=254)
    to: str | None = Field(default=None, max_length=254)
    subject: str | None = Field(default=None, max_length=200)
    since: str | None = Field(default=None, max_length=10)
    before: str | None = Field(default=None, max_length=10)
    limit: int = Field(default=10, ge=1, le=20)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class EmailReadIn(BaseModel):
    uid: str = Field(min_length=1, max_length=20, pattern=r"^\d+$")


def _email_tool_access(db: Session, user: User, name: str) -> None:
    config = email_connector.public_config(db)
    if (not config["enabled"] or config["state"] not in ("configured", "connected")
            or user.role not in config["tool_roles"].get(name, [])):
        raise HTTPException(403, "Connecteur email non autorisé ou indisponible.")
    if not config["tools"].get(name):
        raise HTTPException(403, "Cet outil email est désactivé.")


def _snapshot(db: Session) -> dict:
    web = load_config(db)
    connectors = list_connectors(db)
    rows = (db.query(AuditLog).filter(AuditLog.resource_type.in_(("connector", "email", "email_account", "websearch")))
            .order_by(AuditLog.created_at.desc()).limit(50).all())
    history = [{"at": row.created_at.isoformat() if row.created_at else None, "user": row.username,
                "action": row.action, "connector": row.resource_id or row.resource_type,
                "status": row.decision, "correlation_id": str(row.id), "error": row.reason if row.decision == "ERROR" else None}
               for row in rows]
    return {"connectors": connectors, "email": email_connector.public_config(db),
            "gmail": {"configured": not provider_issues("gmail")},
            "web_search": {**web.public(), "last_status": web_last_status()}, "history": history}


class WhatsAppContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=8, max_length=16, pattern=r"^\+[1-9]\d{7,14}$")
    category: str | None = Field(default=None, max_length=80)


class WhatsAppPrepareIn(BaseModel):
    phone: str | None = Field(default=None, min_length=8, max_length=16, pattern=r"^\+[1-9]\d{7,14}$")
    contact_id: str | None = Field(default=None, max_length=40)
    message: str = Field(min_length=1, max_length=4000)


def _contacts(db: Session, user_id: str) -> dict:
    row = db.get(RuntimeSetting, "whatsapp_contacts")
    return copy.deepcopy((row.value or {}).get(user_id, {})) if row else {}


def _save_contacts(db: Session, user_id: str, username: str, contacts: dict) -> None:
    row = db.get(RuntimeSetting, "whatsapp_contacts") or RuntimeSetting(key="whatsapp_contacts", value={})
    values = copy.deepcopy(row.value or {})
    values[user_id] = contacts
    row.value = values; row.updated_by = username
    db.add(row); db.commit()


@router.get("/api/connectivity/whatsapp/contacts")
def whatsapp_contacts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"contacts": _contacts(db, str(user.id))}


@router.post("/api/connectivity/whatsapp/contacts")
def whatsapp_contact_add(payload: WhatsAppContactIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    contacts = _contacts(db, str(user.id)); contact_id = secrets.token_urlsafe(12)
    contacts[contact_id] = payload.model_dump()
    _save_contacts(db, str(user.id), user.username, contacts)
    return {"contacts": contacts}


@router.delete("/api/connectivity/whatsapp/contacts/{contact_id}")
def whatsapp_contact_delete(contact_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    contacts = _contacts(db, str(user.id)); contacts.pop(contact_id, None)
    _save_contacts(db, str(user.id), user.username, contacts)
    return {"contacts": contacts}


@router.post("/api/connectivity/whatsapp/prepare")
def whatsapp_prepare(payload: WhatsAppPrepareIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    phone = payload.phone
    if payload.contact_id:
        contact = _contacts(db, str(user.id)).get(payload.contact_id)
        if not contact: raise HTTPException(404, "Contact WhatsApp introuvable.")
        phone = contact["phone"]
    if not phone: raise HTTPException(422, "Choisissez un contact ou saisissez un numéro international.")
    digits = phone[1:]
    log_event(user_id=user.id, username=user.username, role=user.role, action="WHATSAPP_LINK_PREPARED", decision="ALLOW",
              resource_type="whatsapp", resource_id="redacted", reason="confirmation_required")
    return {"status": "ready_to_open", "sent": False, "phone": phone,
            "message": payload.message, "url": f"https://wa.me/{digits}?text={quote(payload.message)}"}


@router.get("/api/admin/connectivity")
def get_connectivity(db: Session = Depends(get_db), _user: User = Depends(require_role("DIRECTOR"))):
    return _snapshot(db)


@router.get("/api/connectivity/overview")
def connectivity_overview(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role == "DIRECTOR":
        return _snapshot(db)
    cards = [c for c in list_connectors(db) if c["id"] in ("google_gmail", "microsoft_outlook", "yahoo_mail", "whatsapp_business")]
    # Do not expose the administrator-managed IMAP/SMTP or web provider settings.
    empty_email = {"enabled": False, "provider": "imap_smtp", "imap_host": "", "imap_port": 993,
        "imap_ssl": True, "smtp_host": "", "smtp_port": 465, "smtp_ssl": True, "username": "", "mailbox": "INBOX",
        "allowed_roles": [], "tool_roles": {}, "send_roles": [], "tools": {}, "credentials_configured": False,
        "state": "not_configured", "last_status": None}
    return {"connectors": cards, "email": empty_email, "history": []}


@router.put("/api/admin/connectivity/email")
def update_email(payload: EmailSettingsIn, db: Session = Depends(get_db), user: User = Depends(require_role("DIRECTOR"))):
    try:
        email_connector.save_config(db, payload.model_dump(exclude_none=True), user.username)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    log_event(user_id=user.id, username=user.username, role=user.role, action="CONNECTOR_SETTINGS_UPDATED",
              decision="ALLOW", resource_type="connector", resource_id="email_imap", reason="ok",
              extra={"fields": list(payload.model_dump(exclude_none=True))})
    return _snapshot(db)


@router.put("/api/admin/connectivity/email/secret")
def update_email_secret(payload: SecretIn, db: Session = Depends(get_db), user: User = Depends(require_role("DIRECTOR"))):
    email_connector.set_password(db, payload.password, user.username)
    log_event(user_id=user.id, username=user.username, role=user.role, action="CONNECTOR_SECRET_SET",
              decision="ALLOW", resource_type="connector", resource_id="email_imap", reason="credential_set")
    return _snapshot(db)


@router.delete("/api/admin/connectivity/email/secret")
def delete_email_secret(db: Session = Depends(get_db), user: User = Depends(require_role("DIRECTOR"))):
    from app.models.runtime_setting import RuntimeSetting
    row = db.get(RuntimeSetting, email_connector.SECRETS_KEY)
    if row:
        values = dict(row.value or {})
        values.pop("email_password", None)
        row.value = values
        db.add(row); db.commit()
    log_event(user_id=user.id, username=user.username, role=user.role, action="CONNECTOR_SECRET_REMOVED",
              decision="ALLOW", resource_type="connector", resource_id="email_imap", reason="credential_removed")
    return _snapshot(db)


@router.post("/api/admin/connectivity/email/test")
def test_email(db: Session = Depends(get_db), user: User = Depends(require_role("DIRECTOR"))):
    result = email_connector.test_connection(db)
    decision = "ALLOW" if result["state"] == "connected" else "ERROR"
    log_event(user_id=user.id, username=user.username, role=user.role, action="CONNECTOR_TEST",
              decision=decision, resource_type="connector", resource_id="email_imap",
              reason=result["state"])
    return result


@router.post("/api/connectivity/email/prepare")
def prepare_send(payload: DraftIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    conf = email_connector.public_config(db)
    if (not conf["enabled"] or conf["state"] not in ("configured", "connected")
            or user.role not in conf["send_roles"] or not conf["tools"].get("draft_email")
            or user.role not in conf["tool_roles"].get("draft_email", [])):
        raise HTTPException(403, "Envoi email non autorisé ou connecteur indisponible.")
    draft = payload.model_dump()
    try:
        result = email_connector.create_pending(db, user, draft)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    log_event(user_id=user.id, username=user.username, role=user.role, action="EMAIL_SEND_PREPARED",
              decision="ALLOW", resource_type="email", resource_id="redacted", reason="confirmation_required")
    return result


@router.post("/api/connectivity/email/search")
def search_emails(payload: EmailSearchIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _email_tool_access(db, user, "search_emails")
    args = payload.model_dump(by_alias=True, exclude_none=True)
    args.pop("limit", None)
    result = email_connector.execute_tool(str(user.id), user.role, "search_emails",
                                          {**args, "limit": payload.limit})
    if result.get("error"):
        raise HTTPException(502, result["error"])
    log_event(user_id=user.id, username=user.username, role=user.role, action="EMAIL_SEARCH",
              decision="ALLOW", resource_type="email", resource_id="redacted", reason="ok",
              extra={"result_count": result.get("count", 0)})
    return result


@router.post("/api/connectivity/email/read")
def read_email(payload: EmailReadIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _email_tool_access(db, user, "read_email")
    result = email_connector.execute_tool(str(user.id), user.role, "read_email", {"uid": payload.uid})
    if result.get("error"):
        raise HTTPException(404, result["error"])
    log_event(user_id=user.id, username=user.username, role=user.role, action="EMAIL_READ",
              decision="ALLOW", resource_type="email", resource_id="redacted", reason="ok")
    return result


@router.post("/api/connectivity/email/send")
def send_email(payload: SendIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        result = email_connector.send_confirmed(db, user, payload.model_dump(exclude={"confirmation"}), payload.confirmation)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return result


@router.get("/api/admin/connectivity/history")
def connectivity_history(limit: int = 50, db: Session = Depends(get_db), _user: User = Depends(require_role("DIRECTOR"))):
    return _snapshot(db)["history"][:max(1, min(limit, 100))]
