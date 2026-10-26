"""The only code that reads or writes `email_connections`.

Isolation: every function takes the authenticated user's id and every query
filters on it; a connection (or a message reference) belonging to someone
else is reported exactly like a missing one (account_not_found -> 404),
without any network call.

Message references handed to the agent and the frontend are opaque and
HMAC-signed (connection id + provider message id): a forged reference, or a
genuine one replayed by another user, resolves to nothing.
"""

import base64
import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectivity.email_accounts import crypto, oauth
from app.connectivity.email_accounts.contract import EmailQuery, to_jsonable
from app.connectivity.email_accounts.errors import EmailProviderError, describe
from app.connectivity.email_accounts.providers import ADAPTERS, PROVIDER_LABELS, get_adapter
from app.connectivity.email_accounts.providers.outlook import REVOKE_HELP
from app.core.config import get_settings
from app.models.email_connection import EmailConnection, EmailOAuthState

logger = logging.getLogger("orbitia.email")

REFRESH_MARGIN = timedelta(seconds=60)
STATUS_LABELS = {
    "not_configured": "Non configuré",
    "config_required": "Configuration requise",
    "pending": "Connexion en attente",
    "authorization_required": "Autorisation requise",
    "connected": "Connecté",
    "token_expired_or_revoked": "Jeton expiré ou révoqué",
    "policy_blocked": "Bloqué par une politique",
    "temporary_error": "Erreur temporaire",
    "disconnected": "Compte déconnecté",
}
READ_SCOPE_LABELS = {"gmail": "Lecture seule (gmail.readonly)", "outlook": "Lecture seule (Mail.Read)",
                     "yahoo": "Lecture seule (mail-r)"}


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(value) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise EmailProviderError("account_not_found") from None


# --- provider readiness --------------------------------------------------------


def provider_issues(provider: str) -> list[str]:
    adapter = get_adapter(provider)
    issues = adapter.configuration_issues()
    if provider != "yahoo" or get_settings().yahoo_mail_enabled:
        issues = [*issues, *crypto.key_issues()]
    return issues


def providers_status(db: Session, user_id) -> list[dict]:
    uid = _uuid(user_id)
    pending = {
        row.provider for row in db.scalars(
            select(EmailOAuthState).where(EmailOAuthState.user_id == uid, EmailOAuthState.consumed_at.is_(None),
                                          EmailOAuthState.expires_at > _now())
        )
    }
    result = []
    for provider, cls in ADAPTERS.items():
        issues = provider_issues(provider)
        disabled = provider == "yahoo" and not get_settings().yahoo_mail_enabled
        result.append({
            "provider": provider, "label": cls.label, "configured": not issues, "enabled": not disabled,
            "issues": issues, "pending": provider in pending,
            "status": "not_configured" if disabled else ("config_required" if issues else ("pending" if provider in pending else "ready")),
            "capabilities": to_jsonable(cls.capabilities),
            "permission": READ_SCOPE_LABELS[provider],
        })
    return result


# --- public view of a connection --------------------------------------------------


def public_account(conn: EmailConnection) -> dict:
    expires = conn.access_token_expires_at
    if conn.disconnected_at is not None or not conn.secret_ciphertext:
        token_state = "absent"
    elif expires and expires <= _now():
        token_state = "expired_refreshable"
    else:
        token_state = "valid"
    adapter_cls = ADAPTERS[conn.provider]
    status = conn.status if conn.disconnected_at is None else "disconnected"
    return {
        "id": str(conn.id), "provider": conn.provider, "provider_label": PROVIDER_LABELS[conn.provider],
        "email_address": conn.email_address, "display_name": conn.display_name, "label": conn.label,
        "account_kind": conn.account_kind,
        "status": status, "status_label": STATUS_LABELS.get(status, status),
        "error": describe(conn.last_error_code),
        "permission": READ_SCOPE_LABELS[conn.provider],
        "read_permission_granted": status != "authorization_required",
        "is_enabled": conn.is_enabled, "is_primary": conn.is_primary,
        "token_state": token_state,
        "access_token_expires_at": expires.isoformat() if expires else None,
        "last_checked_at": conn.last_checked_at.isoformat() if conn.last_checked_at else None,
        "last_success_at": conn.last_success_at.isoformat() if conn.last_success_at else None,
        "connected_at": conn.connected_at.isoformat() if conn.connected_at else None,
        "capabilities": to_jsonable(adapter_cls.capabilities),
    }


def _active(db: Session, user_id) -> list[EmailConnection]:
    return list(db.scalars(
        select(EmailConnection)
        .where(EmailConnection.user_id == _uuid(user_id), EmailConnection.disconnected_at.is_(None))
        .order_by(EmailConnection.is_primary.desc(), EmailConnection.connected_at)
    ))


def list_accounts(db: Session, user_id) -> list[dict]:
    return [public_account(c) for c in _active(db, user_id)]


def usable_accounts(db: Session, user_id) -> list[EmailConnection]:
    """Enabled connections the agent may read (an expired token is still
    usable: it gets refreshed)."""
    return [c for c in _active(db, user_id)
            if c.is_enabled and c.secret_ciphertext and c.status not in ("authorization_required", "policy_blocked")]


def get_owned(db: Session, user_id, connection_id, *, lock: bool = False) -> EmailConnection:
    stmt = select(EmailConnection).where(
        EmailConnection.id == _uuid(connection_id),
        EmailConnection.user_id == _uuid(user_id),
        EmailConnection.disconnected_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    conn = db.scalars(stmt).one_or_none()
    if conn is None:
        raise EmailProviderError("account_not_found")
    return conn


# --- authorization ---------------------------------------------------------------


def start_authorization(db: Session, user_id, provider: str, *, reconnect_id=None) -> tuple[str, str]:
    """Returns (authorization URL, browser-binding cookie value)."""
    if provider not in ADAPTERS:
        raise EmailProviderError("invalid_request")
    if provider == "yahoo" and not get_settings().yahoo_mail_enabled:
        raise EmailProviderError("provider_disabled")
    issues = provider_issues(provider)
    if issues:
        raise EmailProviderError("oauth_config_missing" if any("manquant" in i for i in issues) else "oauth_config_invalid")
    uid = _uuid(user_id)
    login_hint = None
    if reconnect_id is not None:
        existing = get_owned(db, uid, reconnect_id)
        if existing.provider != provider:
            raise EmailProviderError("invalid_request")
        login_hint = existing.email_address
    # Expired states of this user are dropped here (no secret in them).
    db.query(EmailOAuthState).filter(EmailOAuthState.user_id == uid,
                                     EmailOAuthState.expires_at <= _now()).delete(synchronize_session=False)
    state, binding, nonce = oauth.new_secret(), oauth.new_secret(), oauth.new_secret()[:48]
    verifier, challenge = oauth.pkce_pair()
    row_id = uuid.uuid4()
    db.add(EmailOAuthState(
        id=row_id, state_hash=oauth.sha256_hex(state), browser_binding_hash=oauth.sha256_hex(binding),
        user_id=uid, provider=provider, nonce=nonce,
        pkce_verifier_ciphertext=crypto.encrypt(crypto.context(row_id, uid, provider), {"v": verifier}),
        reconnect_connection_id=_uuid(reconnect_id) if reconnect_id is not None else None,
        expires_at=_now() + timedelta(seconds=oauth.STATE_TTL_SECONDS),
    ))
    db.commit()
    url = get_adapter(provider).authorization_url(state=state, code_challenge=challenge, nonce=nonce,
                                                  login_hint=login_hint)
    return url, binding


def complete_authorization(db: Session, provider: str, *, state: str | None, code: str | None,
                           binding: str | None, error: str | None, error_description: str | None
                           ) -> tuple[uuid.UUID | None, EmailConnection | None]:
    """Validates the callback and stores the connection. Raises
    EmailProviderError(code); returns (user_id, connection)."""
    if provider not in ADAPTERS or not state or len(state) > 200:
        raise EmailProviderError("oauth_state_invalid")
    row = db.scalars(
        select(EmailOAuthState).where(EmailOAuthState.state_hash == oauth.sha256_hex(state)).with_for_update()
    ).one_or_none()
    if row is None or row.provider != provider or row.consumed_at is not None:
        raise EmailProviderError("oauth_state_invalid")
    if not binding or not hmac.compare_digest(row.browser_binding_hash, oauth.sha256_hex(binding)):
        raise EmailProviderError("oauth_browser_mismatch")
    row.consumed_at = _now()  # single use, whatever happens next
    db.commit()
    if row.expires_at <= _now():
        raise EmailProviderError("oauth_state_expired")
    if error:
        description = error_description or ""
        if any(c in description for c in ("AADSTS65001", "AADSTS90094", "AADSTS90008", "AADSTS50105", "AADSTS53003")):
            raise EmailProviderError("tenant_policy_blocked")
        raise EmailProviderError("oauth_denied")
    if not code or len(code) > 4096:
        raise EmailProviderError("oauth_state_invalid")
    try:
        verifier = crypto.decrypt(crypto.context(row.id, row.user_id, provider), row.pkce_verifier_ciphertext)["v"]
    except (ValueError, KeyError, crypto.CryptoUnavailable):
        raise EmailProviderError("oauth_state_invalid") from None
    adapter = get_adapter(provider)
    identity = adapter.complete_authorization(code=code, code_verifier=verifier, nonce=row.nonce or "")
    # The mailbox must answer a read with these tokens before it is shown connected.
    adapter.verify(identity.tokens)

    user_accounts = _active(db, row.user_id)
    if row.reconnect_connection_id is not None:
        target = next((c for c in user_accounts if c.id == row.reconnect_connection_id), None)
        if target is None or target.external_account_id != identity.external_account_id:
            raise EmailProviderError("account_mismatch")
    existing = next((c for c in user_accounts if c.provider == provider
                     and c.external_account_id == identity.external_account_id), None)
    conn = existing or EmailConnection(id=uuid.uuid4(), user_id=row.user_id, provider=provider,
                                       external_account_id=identity.external_account_id,
                                       is_primary=not any(c.is_primary for c in user_accounts))
    conn.auth_method = "oauth2"
    conn.email_address = identity.email_address
    conn.display_name = identity.display_name
    conn.tenant_id = identity.tenant_id
    conn.account_kind = identity.account_kind
    conn.granted_scopes = list(identity.granted_scopes)
    conn.secret_ciphertext = crypto.encrypt(crypto.context(conn.id, row.user_id, provider), identity.tokens)
    conn.access_token_expires_at = datetime.fromisoformat(identity.tokens["expires_at"])
    conn.token_refreshed_at = _now()
    conn.status = "connected"
    conn.last_error_code = None
    conn.last_checked_at = conn.last_success_at = _now()
    conn.is_enabled = True
    if existing is None:
        conn.connected_at = _now()
        db.add(conn)
    db.commit()
    db.refresh(conn)
    return row.user_id, conn


# --- credentials -----------------------------------------------------------------


def _mark(db: Session, conn: EmailConnection, error: EmailProviderError | None) -> None:
    conn.last_checked_at = _now()
    if error is None:
        conn.status, conn.last_error_code, conn.last_success_at = "connected", None, _now()
    elif error.status is not None:
        conn.status, conn.last_error_code = error.status, error.code
    db.commit()


def _tokens(db: Session, user_id, connection_id) -> tuple[EmailConnection, dict]:
    """Decrypted tokens, refreshed first when (about to be) expired - under a
    row lock, so two concurrent turns refresh once and a rotated refresh
    token (Microsoft) is never lost."""
    conn = get_owned(db, user_id, connection_id, lock=True)
    if not conn.is_enabled:
        db.commit()
        raise EmailProviderError("account_disabled")
    if not conn.secret_ciphertext:
        db.commit()
        raise EmailProviderError("token_revoked")
    ctx = crypto.context(conn.id, conn.user_id, conn.provider)
    try:
        tokens = crypto.decrypt(ctx, conn.secret_ciphertext)
    except (ValueError, crypto.CryptoUnavailable):
        error = EmailProviderError("token_unreadable")
        _mark(db, conn, error)
        raise error from None
    expires = conn.access_token_expires_at
    if expires is None or expires <= _now() + REFRESH_MARGIN:
        try:
            tokens = get_adapter(conn.provider, conn.email_address).refresh(tokens)
        except EmailProviderError as error:
            _mark(db, conn, error)
            raise
        conn.secret_ciphertext = crypto.encrypt(ctx, tokens)
        conn.access_token_expires_at = datetime.fromisoformat(tokens["expires_at"])
        conn.token_refreshed_at = _now()
    db.commit()  # releases the lock
    return conn, tokens


def _call(db: Session, user_id, connection_id, operation):
    conn, tokens = _tokens(db, user_id, connection_id)
    adapter = get_adapter(conn.provider, conn.email_address)
    try:
        result = operation(adapter, tokens)
    except EmailProviderError as error:
        if error.code not in ("message_not_found", "invalid_request"):
            _mark(db, conn, error)
        raise
    _mark(db, conn, None)
    return conn, result


# --- opaque, signed message references --------------------------------------------


def _ref_key() -> bytes:
    return hashlib.sha256(b"orbit-email-ref|" + get_settings().jwt_secret.encode()).digest()


def make_ref(connection_id, provider_id: str) -> str:
    payload = base64.urlsafe_b64encode(json.dumps([str(connection_id), provider_id]).encode()).decode().rstrip("=")
    signature = hmac.new(_ref_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{signature}"


def parse_ref(ref: str) -> tuple[str, str]:
    try:
        payload, signature = ref.rsplit(".", 1)
        expected = hmac.new(_ref_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if len(ref) > 2048 or not hmac.compare_digest(signature, expected):
            raise ValueError
        connection_id, provider_id = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(connection_id), str(provider_id)
    except (ValueError, TypeError, AttributeError):
        raise EmailProviderError("message_not_found") from None


def _item(conn: EmailConnection, item) -> dict:
    data = to_jsonable(item)
    data["ref"] = make_ref(conn.id, data.pop("message_id"))
    thread = data.pop("thread_id", None)
    data["thread_ref"] = make_ref(conn.id, thread) if thread else None
    return data


def account_summary(conn: EmailConnection) -> dict:
    return {"connection_id": str(conn.id), "provider": conn.provider, "provider_label": PROVIDER_LABELS[conn.provider],
            "email_address": conn.email_address, "label": conn.label}


# --- reading ---------------------------------------------------------------------


def clamp_limit(limit: int | None) -> int:
    s = get_settings()
    return max(1, min(int(limit or s.email_default_limit), s.email_max_limit))


def search(db: Session, user_id, connection_id, query: EmailQuery, cursor: str | None = None,
           *, recent: bool = False) -> dict:
    query = EmailQuery(**{**query.__dict__, "limit": clamp_limit(query.limit)})
    conn, page = _call(db, user_id, connection_id,
                       lambda a, t: (a.list_recent if recent else a.search)(t, query, cursor))
    return {"account": account_summary(conn), "items": [_item(conn, i) for i in page.items],
            "next_cursor": page.next_cursor, "limit": query.limit}


def get_message(db: Session, user_id, ref: str, *, body_chars: int | None = None) -> dict:
    connection_id, message_id = parse_ref(ref)
    chars = min(body_chars or get_settings().email_max_body_chars, get_settings().email_max_body_chars)
    conn, message = _call(db, user_id, connection_id, lambda a, t: a.get_message(t, message_id, body_chars=chars))
    return {"account": account_summary(conn), "message": _item(conn, message)}


def get_thread(db: Session, user_id, ref: str) -> dict:
    connection_id, thread_id = parse_ref(ref)
    s = get_settings()
    conn, thread = _call(db, user_id, connection_id,
                         lambda a, t: a.get_thread(t, thread_id, max_messages=10, body_chars=min(s.email_max_body_chars, 2400)))
    return {"account": account_summary(conn), "complete": thread.complete,
            "messages": [_item(conn, m) for m in thread.messages]}


# --- account management ------------------------------------------------------------


def test_connection(db: Session, user_id, connection_id) -> dict:
    conn, _ = _call(db, user_id, connection_id, lambda a, t: a.verify(t))
    return public_account(conn)


def refresh_status(db: Session, user_id, connection_id) -> dict:
    """Re-checks the authorization: refresh if needed, then a minimal read.
    A failure is recorded on the connection and returned, not raised."""
    try:
        return test_connection(db, user_id, connection_id)
    except EmailProviderError as error:
        if error.code in ("account_not_found",):
            raise
        return public_account(get_owned(db, user_id, connection_id))


def update_account(db: Session, user_id, connection_id, *, is_enabled=None, is_primary=None, label=None) -> list[dict]:
    conn = get_owned(db, user_id, connection_id)
    if is_primary:
        for other in _active(db, user_id):
            if other.is_primary and other.id != conn.id:
                other.is_primary = False
        db.flush()
        conn.is_primary = True
    elif is_primary is False:
        conn.is_primary = False
    if is_enabled is not None:
        conn.is_enabled = bool(is_enabled)
    if label is not None:
        conn.label = label.strip()[:80] or None
    db.commit()
    return list_accounts(db, user_id)


def disconnect(db: Session, user_id, connection_id) -> dict:
    """Revokes at the provider when it allows it, then erases the local
    secret for good. The row is kept (disconnected_at) for history."""
    conn = get_owned(db, user_id, connection_id, lock=True)
    revocation = "failed"
    if conn.secret_ciphertext:
        try:
            tokens = crypto.decrypt(crypto.context(conn.id, conn.user_id, conn.provider), conn.secret_ciphertext)
            revocation = get_adapter(conn.provider, conn.email_address).revoke(tokens)
        except (ValueError, crypto.CryptoUnavailable, EmailProviderError):
            revocation = "failed"
    conn.secret_ciphertext = None
    conn.access_token_expires_at = None
    conn.status = "disconnected"
    conn.disconnected_at = _now()
    conn.disconnect_reason = "user"
    was_primary, conn.is_primary = conn.is_primary, False
    db.flush()
    if was_primary:
        remaining = _active(db, user_id)
        if remaining:
            remaining[0].is_primary = True
    db.commit()
    manual = None
    if revocation != "revoked":
        manual = REVOKE_HELP.get(conn.account_kind or "", REVOKE_HELP["work_school"]) if conn.provider == "outlook" else (
            "https://myaccount.google.com/permissions" if conn.provider == "gmail" else None)
    return {"revocation": revocation, "manual_revoke_url": manual, "accounts": list_accounts(db, user_id)}
