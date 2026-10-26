"""Generic IMAP/SMTP connector. Credentials never leave this backend."""

import email
import hashlib
import imaplib
import json
import logging
import smtplib
import ssl
import uuid
from datetime import UTC, datetime, timedelta
from email.header import decode_header
from email.message import EmailMessage, Message
from email.utils import parseaddr

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.audit_logger import log_event
from app.models.runtime_setting import RuntimeSetting
from app.models.user import User

logger = logging.getLogger("orbitia.connectivity.email")
SETTINGS_KEY = "connectivity_email"
SECRETS_KEY = "connectivity_secrets"
PENDING_PREFIX = "connectivity_email_pending_"
MAX_BODY_CHARS = 12000
MAX_SEARCH_RESULTS = 20

EMAIL_TOOLS = {
    "search_emails": {
        "type": "function", "function": {"name": "search_emails",
        "description": "Recherche des emails dans la boîte autorisée par expéditeur, destinataire, sujet et période.",
        "parameters": {"type": "object", "properties": {
            "from": {"type": "string", "maxLength": 254}, "to": {"type": "string", "maxLength": 254},
            "subject": {"type": "string", "maxLength": 200}, "since": {"type": "string", "maxLength": 10},
            "before": {"type": "string", "maxLength": 10}, "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        }, "additionalProperties": False}},
    },
    "read_email": {
        "type": "function", "function": {"name": "read_email",
        "description": "Lit le contenu d'un email autorisé identifié par l'UID renvoyé par search_emails.",
        "parameters": {"type": "object", "properties": {"uid": {"type": "string", "maxLength": 20}},
                         "required": ["uid"], "additionalProperties": False}},
    },
    "draft_email": {
        "type": "function", "function": {"name": "draft_email",
        "description": "Prépare un brouillon d'email. N'envoie jamais l'email.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string", "maxLength": 254}, "subject": {"type": "string", "maxLength": 200},
            "body": {"type": "string", "maxLength": 8000},
        }, "required": ["to", "subject", "body"], "additionalProperties": False}},
    },
}


def _fernet() -> Fernet:
    import base64
    secret = get_settings().jwt_secret.encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"orbit-connectivity-secrets|" + secret).digest()))


def _settings(db: Session) -> dict:
    row = db.get(RuntimeSetting, SETTINGS_KEY)
    return dict(row.value or {}) if row else {}


def _secrets(db: Session) -> dict:
    row = db.get(RuntimeSetting, SECRETS_KEY)
    return dict(row.value or {}) if row else {}


def _password(db: Session) -> str:
    token = _secrets(db).get("email_password")
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


def public_config(db: Session) -> dict:
    values = _settings(db)
    configured = bool(values.get("imap_host") and values.get("smtp_host") and values.get("username") and _password(db))
    last = values.get("last_status") or {}
    state = ("not_configured" if not configured else
             "disabled" if not values.get("enabled") else
             last.get("state") if last.get("state") in ("auth_expired", "connection_error") else
             "connected" if last.get("state") == "connected" else "configured")
    return {
        "enabled": bool(values.get("enabled", False)), "provider": "imap_smtp",
        "imap_host": values.get("imap_host", ""), "imap_port": int(values.get("imap_port", 993)),
        "imap_ssl": bool(values.get("imap_ssl", True)), "smtp_host": values.get("smtp_host", ""),
        "smtp_port": int(values.get("smtp_port", 465)), "smtp_ssl": bool(values.get("smtp_ssl", True)),
        "username": values.get("username", ""), "mailbox": values.get("mailbox", "INBOX"),
        "allowed_roles": values.get("allowed_roles", []),
        "tool_roles": values.get("tool_roles", {
            name: values.get("allowed_roles", []) for name in EMAIL_TOOLS
        }),
        "send_roles": values.get("send_roles", []),
        "tools": values.get("tools", {"search_emails": True, "read_email": True, "draft_email": True}),
        "credentials_configured": bool(_password(db)),
        "state": state,
        "last_status": last or None,
    }


def save_config(db: Session, values: dict, updated_by: str) -> None:
    fields = {"imap_host", "imap_port", "imap_ssl", "smtp_host", "smtp_port", "smtp_ssl",
              "username", "mailbox", "enabled", "allowed_roles", "tool_roles", "send_roles", "tools"}
    clean = {k: v for k, v in values.items() if k in fields}
    for field in ("imap_port", "smtp_port"):
        if field in clean and (type(clean[field]) is not int or not 1 <= clean[field] <= 65535):
            raise ValueError(f"{field} doit être compris entre 1 et 65535")
    for field in ("imap_host", "smtp_host", "username", "mailbox"):
        if field in clean and (not isinstance(clean[field], str) or len(clean[field]) > 255 or any(c in clean[field] for c in "\r\n")):
            raise ValueError(f"{field} invalide")
    roles = {"DIRECTOR", "HR", "ACCOUNTANT", "DEVELOPER", "EMPLOYEE"}
    if any(not isinstance(clean.get(field), list) or not set(clean[field]) <= roles for field in ("allowed_roles", "send_roles") if field in clean):
        raise ValueError("Rôles autorisés invalides")
    if "tool_roles" in clean and (
        not isinstance(clean["tool_roles"], dict) or set(clean["tool_roles"]) - set(EMAIL_TOOLS)
        or any(not isinstance(v, list) or not set(v) <= roles for v in clean["tool_roles"].values())
    ):
        raise ValueError("Permissions par outil email invalides")
    if "tools" in clean and (not isinstance(clean["tools"], dict) or set(clean["tools"]) - set(EMAIL_TOOLS) or any(type(v) is not bool for v in clean["tools"].values())):
        raise ValueError("Configuration des outils email invalide")
    row = db.get(RuntimeSetting, SETTINGS_KEY) or RuntimeSetting(key=SETTINGS_KEY, value={})
    row.value = {**(row.value or {}), **clean}
    row.updated_by = updated_by
    db.add(row)
    db.commit()


def set_password(db: Session, password: str, updated_by: str) -> None:
    if not 8 <= len(password) <= 500 or any(c in password for c in "\r\n"):
        raise ValueError("Mot de passe invalide")
    row = db.get(RuntimeSetting, SECRETS_KEY) or RuntimeSetting(key=SECRETS_KEY, value={})
    values = dict(row.value or {})
    values["email_password"] = _fernet().encrypt(password.encode()).decode()
    row.value = values
    row.updated_by = updated_by
    db.add(row)
    db.commit()


def tools_for_role(db: Session, role: str) -> list[dict]:
    conf = public_config(db)
    if conf["state"] not in ("configured", "connected") or not conf["enabled"]:
        return []
    return [EMAIL_TOOLS[n] for n in EMAIL_TOOLS if conf["tools"].get(n, False) and role in conf["tool_roles"].get(n, [])]


def _connect_imap(conf: dict, password: str):
    if conf["imap_ssl"]:
        client = imaplib.IMAP4_SSL(conf["imap_host"], conf["imap_port"], ssl_context=ssl.create_default_context(), timeout=8)
    else:
        client = imaplib.IMAP4(conf["imap_host"], conf["imap_port"], timeout=8)
        client.starttls(ssl_context=ssl.create_default_context())
    client.login(conf["username"], password)
    return client


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    return "".join(chunk.decode(enc or "utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
                   for chunk, enc in decode_header(value))


def test_connection(db: Session) -> dict:
    conf, password = public_config(db), _password(db)
    if not conf["credentials_configured"] or not conf["smtp_host"]:
        result = {"state": "not_configured", "checked_at": datetime.now(UTC).isoformat(),
                  "error": "Paramètres IMAP/SMTP ou identifiants manquants."}
        row = db.get(RuntimeSetting, SETTINGS_KEY) or RuntimeSetting(key=SETTINGS_KEY, value={})
        row.value = {**(row.value or {}), "last_status": result}
        db.add(row); db.commit()
        return result
    checked_at = datetime.now(UTC).isoformat()
    client = None
    try:
        client = _connect_imap(conf, password)
        status, _ = client.select(conf["mailbox"], readonly=True)
        if status != "OK":
            raise imaplib.IMAP4.error("mailbox unavailable")
        if conf["smtp_ssl"]:
            smtp = smtplib.SMTP_SSL(conf["smtp_host"], conf["smtp_port"], timeout=8, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(conf["smtp_host"], conf["smtp_port"], timeout=8)
            smtp.starttls(context=ssl.create_default_context())
        try:
            smtp.login(conf["username"], password)
        finally:
            try: smtp.quit()
            except Exception: pass
        result = {"state": "connected", "checked_at": checked_at, "mailbox": conf["mailbox"]}
    except (imaplib.IMAP4.error, imaplib.IMAP4.abort, smtplib.SMTPAuthenticationError) as exc:
        text = str(exc).lower()
        auth_failure = (isinstance(exc, smtplib.SMTPAuthenticationError)
                        or "auth" in text or "login" in text or "password" in text)
        kind = "auth_expired" if auth_failure else "connection_error"
        error = "Authentification Email refusée ou expirée." if auth_failure else "Le serveur email a refusé la connexion."
        result = {"state": kind, "checked_at": checked_at, "error": error}
    except (OSError, ssl.SSLError, TimeoutError, smtplib.SMTPException) as exc:
        result = {"state": "connection_error", "checked_at": checked_at, "error": f"Connexion Email impossible ({type(exc).__name__})."}
    finally:
        if client:
            try: client.logout()
            except Exception: pass
    row = db.get(RuntimeSetting, SETTINGS_KEY)
    row.value = {**(row.value or {}), "last_status": result}
    db.add(row)
    db.commit()
    return result


def _criteria(args: dict) -> list[str]:
    criteria = []
    for key, name in (("from", "FROM"), ("to", "TO"), ("subject", "SUBJECT")):
        value = args.get(key)
        if value:
            if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
                raise ValueError("Filtre invalide")
            criteria.extend((name, '"' + value.replace('"', "") + '"'))
    for key, name in (("since", "SINCE"), ("before", "BEFORE")):
        value = args.get(key)
        if value:
            try: parsed = datetime.strptime(value, "%Y-%m-%d")
            except (TypeError, ValueError): raise ValueError("Date invalide (AAAA-MM-JJ)") from None
            criteria.extend((name, parsed.strftime("%d-%b-%Y")))
    return criteria or ["ALL"]


def _body(msg: Message) -> str:
    chunks = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() == "text/plain" and not part.get_filename():
            data = part.get_payload(decode=True) or b""
            chunks.append(data.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(chunks)[:MAX_BODY_CHARS]


def _validate_tool_args(name: str, args: dict) -> None:
    if not isinstance(args, dict) or name not in EMAIL_TOOLS:
        raise ValueError("Paramètres invalides")
    allowed = {
        "search_emails": {"from", "to", "subject", "since", "before", "limit"},
        "read_email": {"uid"},
        "draft_email": {"to", "subject", "body"},
    }[name]
    required = {"read_email": {"uid"}, "draft_email": {"to", "subject", "body"}}.get(name, set())
    if set(args) - allowed or not required <= set(args):
        raise ValueError("Paramètres invalides")
    if name == "search_emails":
        for key in ("from", "to", "subject", "since", "before"):
            if key in args and (not isinstance(args[key], str) or len(args[key]) > 254):
                raise ValueError("Filtre invalide")
        if "limit" in args and (type(args["limit"]) is not int or not 1 <= args["limit"] <= MAX_SEARCH_RESULTS):
            raise ValueError("Limite invalide")
    elif name == "read_email":
        if not isinstance(args["uid"], str) or not args["uid"].isdigit() or len(args["uid"]) > 20:
            raise ValueError("UID invalide")
    else:
        if any(not isinstance(args[key], str) for key in required):
            raise ValueError("Brouillon invalide")
        if not 3 <= len(args["to"]) <= 254 or len(args["subject"]) > 200 or not 1 <= len(args["body"]) <= 8000:
            raise ValueError("Brouillon invalide")
        if parseaddr(args["to"])[1] != args["to"] or any(c in args["to"] + args["subject"] for c in "\r\n"):
            raise ValueError("Destinataire ou objet invalide")


def _run_imap_tool(name: str, args: dict, conf: dict, password: str) -> dict:
    client = _connect_imap(conf, password)
    try:
        status, _ = client.select(conf["mailbox"], readonly=True)
        if status != "OK":
            raise RuntimeError("Mailbox inaccessible")
        if name == "search_emails":
            status, rows = client.uid("search", None, *_criteria(args))
            if status != "OK": raise RuntimeError("Search failed")
            limit = max(1, min(int(args.get("limit", 10)), MAX_SEARCH_RESULTS))
            summaries = []
            for uid in rows[0].split()[-limit:]:
                status, data = client.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])")
                if status != "OK" or not data or not isinstance(data[0], tuple): continue
                msg = email.message_from_bytes(data[0][1])
                summaries.append({"uid": uid.decode("ascii", errors="ignore"),
                    "from": _decode_header(msg.get("From")), "to": _decode_header(msg.get("To")),
                    "subject": _decode_header(msg.get("Subject")), "date": msg.get("Date", ""),
                    "thread_id": msg.get("References") or msg.get("In-Reply-To") or msg.get("Message-ID", "")})
            return {"messages": summaries, "count": len(summaries)}
        uid = str(args.get("uid", ""))
        if not uid.isdigit() or len(uid) > 20: raise ValueError("UID invalide")
        status, data = client.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple): return {"error": "Email introuvable."}
        msg = email.message_from_bytes(data[0][1])
        return {"uid": uid, "from": _decode_header(msg.get("From")), "to": _decode_header(msg.get("To")),
                "subject": _decode_header(msg.get("Subject")), "date": msg.get("Date", ""), "body": _body(msg),
                "attachments": [p.get_filename() for p in msg.walk() if p.get_filename()][:20],
                "thread_id": msg.get("References") or msg.get("In-Reply-To") or msg.get("Message-ID", "")}
    finally:
        try: client.logout()
        except Exception: pass


def execute_tool(user_id: str, role: str, name: str, args: dict) -> dict:
    db = SessionLocal()
    try:
        user = db.get(User, uuid.UUID(str(user_id)))
        conf, password = public_config(db), _password(db)
        _validate_tool_args(name, args)
        if (not user or role not in conf["tool_roles"].get(name, []) or not conf["enabled"]
                or not conf["tools"].get(name)):
            return {"error": "Outil email désactivé ou non autorisé."}
        if conf["state"] not in ("configured", "connected"): return {"error": "Connecteur email non configuré ou connexion en erreur."}
        if name == "draft_email":
            to, subject, body = args["to"], args["subject"], args["body"]
            if not subject.strip() or not body.strip():
                return {"error": "Destinataire, objet ou contenu invalide."}
            return {"draft": {"to": to, "subject": subject[:200], "body": body[:8000]}, "status": "draft_only",
                    "confirmation_required": True, "note": "Aucun email envoyé. L'envoi demande une confirmation explicite."}
        if name not in ("search_emails", "read_email"): return {"error": "Outil inconnu."}
        return _run_imap_tool(name, args, conf, password)
    except (ValueError, KeyError, TypeError):
        return {"error": "Paramètres de l'outil email invalides."}
    except Exception as exc:
        logger.warning("Email tool failed tool=%s error=%s", name, type(exc).__name__)
        return {"error": "Le fournisseur email n'a pas pu exécuter cette opération."}
    finally:
        db.close()


def create_pending(db: Session, user: User, draft: dict) -> dict:
    to = parseaddr(draft.get("to", ""))[1]
    if to != draft.get("to") or any(c in draft.get("subject", "") for c in "\r\n") or not draft.get("body", "").strip() or len(draft.get("body", "")) > 8000:
        raise ValueError("Brouillon invalide.")
    confirmation = str(uuid.uuid4())
    digest = hashlib.sha256(json.dumps(draft, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    key = f"{PENDING_PREFIX}{user.id}"
    row = db.get(RuntimeSetting, key) or RuntimeSetting(key=key, value={})
    row.value = {"confirmation": confirmation, "digest": digest,
                 "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat()}
    db.add(row); db.commit()
    return {"confirmation": confirmation, "expires_in_seconds": 600, "draft": draft}


def send_confirmed(db: Session, user: User, draft: dict, confirmation: str) -> dict:
    """Only sends a server-issued, unmodified draft after a separate user confirmation."""
    row = db.get(RuntimeSetting, f"{PENDING_PREFIX}{user.id}")
    pending = dict(row.value or {}) if row else {}
    digest = hashlib.sha256(json.dumps(draft, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if not pending or pending.get("confirmation") != confirmation or pending.get("digest") != digest:
        raise ValueError("Confirmation absente, expirée ou contenu modifié.")
    if datetime.fromisoformat(pending["expires_at"]) < datetime.now(UTC):
        db.delete(row); db.commit()
        raise ValueError("Confirmation expirée. Préparez à nouveau le brouillon.")
    conf, password = public_config(db), _password(db)
    if (not conf["enabled"] or conf["state"] not in ("configured", "connected")
            or user.role not in conf["send_roles"] or not conf["tools"].get("draft_email")
            or user.role not in conf["tool_roles"].get("draft_email", [])):
        raise ValueError("L'envoi n'est pas autorisé par la configuration actuelle.")
    to = parseaddr(draft.get("to", ""))[1]
    if to != draft.get("to") or any(c in draft.get("subject", "") for c in "\r\n"):
        raise ValueError("Destinataire ou objet invalide.")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = conf["username"], to, draft["subject"]
    message.set_content(draft["body"])
    client = None
    try:
        if conf["smtp_ssl"]:
            client = smtplib.SMTP_SSL(conf["smtp_host"], conf["smtp_port"], timeout=8, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(conf["smtp_host"], conf["smtp_port"], timeout=8)
            client.starttls(context=ssl.create_default_context())
        client.login(conf["username"], password)
        client.send_message(message)
    except Exception as exc:
        log_event(user_id=user.id, username=user.username, role=user.role, action="EMAIL_SEND", decision="ERROR",
                  resource_type="email", resource_id="redacted", reason=type(exc).__name__)
        logger.warning("Email send failed error=%s", type(exc).__name__)
        raise ValueError("L'envoi a échoué ; aucun succès n'est confirmé.") from None
    finally:
        if client:
            try: client.quit()
            except Exception: pass
    db.delete(row); db.commit()
    log_event(user_id=user.id, username=user.username, role=user.role, action="EMAIL_SEND", decision="ALLOW",
              resource_type="email", resource_id="redacted", reason="user_confirmed")
    return {"status": "sent", "to": to, "subject": draft["subject"]}
