"""Microsoft Graph adapter (Outlook.com / Hotmail / Microsoft 365) -
delegated Mail.Read only, never a write or send permission.

Identity comes from the id_token returned by the token endpoint itself over
TLS (OpenID Connect Core 3.1.3.7 lets the client skip the signature check
for a token received that way); aud, nonce, issuer and expiry are checked.
No User.Read scope is requested for that reason.

Microsoft rotates refresh tokens on every use: the service stores the new
one under a row lock (service.py). Microsoft offers no API for an app to
revoke a delegated grant, so disconnecting erases the local tokens and
tells the user where to remove the app's access.
"""

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit

from app.connectivity.email_accounts import mime
from app.connectivity.email_accounts.contract import (
    AttachmentMeta,
    Capabilities,
    ConnectedIdentity,
    EmailAddress,
    EmailMessage,
    EmailPage,
    EmailQuery,
    EmailSummaryItem,
    EmailThread,
)
from app.connectivity.email_accounts.errors import EmailProviderError
from app.connectivity.email_accounts.oauth import valid_redirect_uri
from app.connectivity.email_accounts.providers.base import MailHttp, token_error
from app.core.config import get_settings

GRAPH = "https://graph.microsoft.com/v1.0"
MAIL_READ = "https://graph.microsoft.com/Mail.Read"
SCOPES = f"openid email profile offline_access {MAIL_READ}"
PERSONAL_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
REVOKE_HELP = {
    "personal": "https://account.live.com/consent/Manage",
    "work_school": "https://myapps.microsoft.com",
}
_SELECT = "id,conversationId,from,toRecipients,subject,receivedDateTime,bodyPreview,isRead,hasAttachments,webLink"
_PREFER = 'outlook.body-content-type="text", IdType="ImmutableId"'
_AUTHORITY = re.compile(r"^(common|consumers|organizations|[0-9a-fA-F-]{36}|[A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


def _authority() -> str:
    return (get_settings().outlook_oauth_authority or "common").strip()


def _login(path: str) -> str:
    return f"https://login.microsoftonline.com/{_authority()}/oauth2/v2.0/{path}"


def _odata_str(value: str) -> str:
    return value.replace("'", "''")[:200]


def _kql(value: str) -> str:
    return re.sub(r'["\\]', " ", value).strip()[:200]


def _has_mail_read(scopes: tuple[str, ...]) -> bool:
    return any(s.lower() in ("mail.read", MAIL_READ.lower()) for s in scopes)


def _decode_id_token(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise EmailProviderError("oauth_exchange_failed") from None


def _recipient(value: dict | None) -> EmailAddress | None:
    address = ((value or {}).get("emailAddress") or {})
    if not address.get("address") or "@" not in address["address"]:
        return None
    name = (address.get("name") or "").strip()
    # Graph repeats the address as the name when there is none: not a name.
    return EmailAddress(address=address["address"], name=name if name and name != address["address"] else None)


def _when(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


class OutlookAdapter:
    provider = "outlook"
    label = "Outlook"
    callback_path = "/api/connectivity/outlook/callback"
    capabilities = Capabilities(server_side_search=True, unread_filter=True, threads="native",
                                deep_link=True, programmatic_revoke=False, max_page_size=25)

    def __init__(self, account_email: str | None = None):
        self.account_email = account_email
        self.http = MailHttp(api_hosts={"graph.microsoft.com"}, auth_post_urls={_login("token")})

    def configuration_issues(self) -> list[str]:
        s = get_settings()
        issues = []
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", (s.outlook_oauth_client_id or "").strip()) \
                or s.outlook_oauth_client_id != s.outlook_oauth_client_id.strip():
            issues.append("OUTLOOK_OAUTH_CLIENT_ID manquant ou invalide (identifiant d'application Entra ID attendu)")
        secret = s.outlook_oauth_client_secret or ""
        if not secret.strip() or secret != secret.strip():
            issues.append("OUTLOOK_OAUTH_CLIENT_SECRET manquant ou invalide")
        if not valid_redirect_uri(s.outlook_oauth_redirect_uri or "", self.callback_path):
            issues.append("OUTLOOK_OAUTH_REDIRECT_URI invalide (URI exacte du callback, HTTPS sauf localhost)")
        if not _AUTHORITY.match(_authority()):
            issues.append("OUTLOOK_OAUTH_AUTHORITY invalide (common, consumers, organizations ou un tenant)")
        return issues

    def authorization_url(self, *, state: str, code_challenge: str, nonce: str, login_hint: str | None) -> str:
        s = get_settings()
        params = {
            "client_id": s.outlook_oauth_client_id, "redirect_uri": s.outlook_oauth_redirect_uri,
            "response_type": "code", "response_mode": "query", "scope": SCOPES,
            "state": state, "nonce": nonce, "code_challenge": code_challenge, "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{_login('authorize')}?{urlencode(params)}"

    def complete_authorization(self, *, code: str, code_verifier: str, nonce: str) -> ConnectedIdentity:
        s = get_settings()
        response = self.http.post_form(_login("token"), {
            "client_id": s.outlook_oauth_client_id, "client_secret": s.outlook_oauth_client_secret,
            "code": code, "redirect_uri": s.outlook_oauth_redirect_uri, "grant_type": "authorization_code",
            "code_verifier": code_verifier, "scope": SCOPES,
        })
        if response.status_code != 200:
            error = token_error(response)
            raise EmailProviderError("oauth_exchange_failed" if error.code == "token_revoked" else error.code)
        tokens = response.json()
        scopes = tuple(str(tokens.get("scope") or "").split())
        if not _has_mail_read(scopes) or not tokens.get("access_token"):
            raise EmailProviderError("permission_missing")
        claims = _decode_id_token(str(tokens.get("id_token") or ""))
        issuer = str(claims.get("iss") or "")
        tenant = str(claims.get("tid") or "")
        if (claims.get("aud") != s.outlook_oauth_client_id or claims.get("nonce") != nonce
                or not issuer.startswith("https://login.microsoftonline.com/") or not tenant
                or float(claims.get("exp") or 0) < datetime.now(UTC).timestamp()):
            raise EmailProviderError("oauth_exchange_failed")
        address = str(claims.get("email") or claims.get("preferred_username") or "").strip()
        if "@" not in address:
            raise EmailProviderError("oauth_exchange_failed")
        personal = tenant == PERSONAL_TENANT
        external = str(claims.get("sub")) if personal or not claims.get("oid") else f"{tenant}:{claims['oid']}"
        bundle = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token") or "",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=int(tokens.get("expires_in", 3600)))).isoformat(),
        }
        name = str(claims.get("name") or "").strip() or None
        return ConnectedIdentity(email_address=address, external_account_id=external, display_name=name,
                                 granted_scopes=scopes, tokens=bundle, tenant_id=tenant,
                                 account_kind="personal" if personal else "work_school")

    def refresh(self, tokens: dict) -> dict:
        if not tokens.get("refresh_token"):
            raise EmailProviderError("token_expired")
        s = get_settings()
        response = self.http.post_form(_login("token"), {
            "client_id": s.outlook_oauth_client_id, "client_secret": s.outlook_oauth_client_secret,
            "refresh_token": tokens["refresh_token"], "grant_type": "refresh_token", "scope": SCOPES,
        })
        if response.status_code != 200:
            raise token_error(response)
        fresh = response.json()
        return {
            "access_token": fresh["access_token"],
            # rotated: the new one replaces the old one (stored by the caller)
            "refresh_token": fresh.get("refresh_token") or tokens["refresh_token"],
            "expires_at": (datetime.now(UTC) + timedelta(seconds=int(fresh.get("expires_in", 3600)))).isoformat(),
        }

    def revoke(self, tokens: dict) -> str:
        return "not_supported"

    def verify(self, tokens: dict) -> None:
        self.http.get_json(f"{GRAPH}/me/mailFolders/inbox", token=tokens["access_token"], params={"$select": "id"})

    # --- reading ------------------------------------------------------------

    def list_recent(self, tokens: dict, query: EmailQuery, cursor: str | None) -> EmailPage:
        return self._list(tokens, query, cursor)

    def search(self, tokens: dict, query: EmailQuery, cursor: str | None) -> EmailPage:
        return self._list(tokens, query, cursor)

    def _list(self, tokens: dict, query: EmailQuery, cursor: str | None) -> EmailPage:
        top = max(1, min(query.limit, 25))
        headers = {"Prefer": _PREFER}
        if cursor:
            # @odata.nextLink, followed only on Graph itself
            parts = urlsplit(cursor)
            if parts.scheme != "https" or parts.hostname != "graph.microsoft.com":
                raise EmailProviderError("invalid_request")
            data = self.http.get_json(cursor, token=tokens["access_token"], headers=headers)
        else:
            url = f"{GRAPH}/me/mailFolders/inbox/messages"
            params = {"$select": _SELECT, "$top": top}
            if query.text or query.subject or query.from_name:
                # $search (KQL) cannot be combined with $orderby; dates are
                # filtered and the page sorted below instead.
                terms = []
                if query.text:
                    terms.append(_kql(query.text))
                if query.subject:
                    terms.append(f"subject:{_kql(query.subject)}")
                if query.from_name or query.from_address:
                    terms.append(f"from:{_kql(query.from_address or query.from_name)}")
                if query.received_after:
                    terms.append(f"received>={query.received_after.date().isoformat()}")
                if query.unread_only:
                    terms.append("isread:false")
                if query.has_attachments:
                    terms.append("hasattachments:true")
                params["$search"] = '"' + " ".join(t for t in terms if t) + '"'
            else:
                # Graph: properties used in $orderby must lead the $filter.
                after = (query.received_after or datetime(1970, 1, 1, tzinfo=UTC)).astimezone(UTC)
                filters = [f"receivedDateTime ge {after.strftime('%Y-%m-%dT%H:%M:%SZ')}"]
                if query.received_before:
                    filters.append(f"receivedDateTime lt {query.received_before.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
                if query.unread_only:
                    filters.append("isRead eq false")
                if query.has_attachments:
                    filters.append("hasAttachments eq true")
                if query.from_address:
                    filters.append(f"from/emailAddress/address eq '{_odata_str(query.from_address)}'")
                params["$filter"] = " and ".join(filters)
                params["$orderby"] = "receivedDateTime desc"
            data = self.http.get_json(url, token=tokens["access_token"], params=params, headers=headers)
        items = [self._summary(v) for v in (data.get("value") or [])[:top] if isinstance(v, dict) and v.get("id")]
        if query.received_after:
            items = [i for i in items if i.received_at is None or i.received_at >= query.received_after]
        if query.received_before:
            items = [i for i in items if i.received_at is None or i.received_at < query.received_before]
        items.sort(key=lambda i: i.received_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        next_link = data.get("@odata.nextLink")
        return EmailPage(items=items, next_cursor=str(next_link) if next_link else None)

    def _summary(self, value: dict) -> EmailSummaryItem:
        return EmailSummaryItem(
            message_id=str(value["id"]),
            thread_id=value.get("conversationId"),
            sender=_recipient(value.get("from")),
            to=[r for r in (_recipient(v) for v in (value.get("toRecipients") or [])[:20]) if r],
            subject=(value.get("subject") or None),
            received_at=_when(value.get("receivedDateTime")),
            snippet=(mime.normalize_text(value.get("bodyPreview") or "")[:300] or None),
            is_unread=(not value["isRead"]) if isinstance(value.get("isRead"), bool) else None,
            has_attachments=value.get("hasAttachments") if isinstance(value.get("hasAttachments"), bool) else None,
            web_link=value.get("webLink") if str(value.get("webLink") or "").startswith("https://") else None,
        )

    def _message(self, value: dict, tokens: dict, body_chars: int, *, with_attachments: bool) -> EmailMessage:
        summary = self._summary(value)
        body = value.get("body") or {}
        content = str(body.get("content") or "")
        if (body.get("contentType") or "").lower() == "html":
            text, source = mime.html_to_text(content[:400_000]), "text/html"
        else:
            text, source = mime.normalize_text(content[:400_000]), "text/plain"
        if not text:
            source = "none"
        text, truncated = mime.truncate(text, body_chars)
        attachments: list[AttachmentMeta] = []
        if with_attachments and summary.has_attachments:
            listing = self.http.get_json(
                f"{GRAPH}/me/messages/{quote(summary.message_id, safe='')}/attachments", token=tokens["access_token"],
                params={"$select": "name,contentType,size,isInline"}, headers={"Prefer": _PREFER},
            )
            for item in (listing.get("value") or [])[:30]:
                attachments.append(AttachmentMeta(
                    filename=item.get("name") or None, mime_type=item.get("contentType") or None,
                    size_bytes=item.get("size") if isinstance(item.get("size"), int) else None,
                    is_inline=bool(item.get("isInline")),
                ))
        return EmailMessage(
            **summary.__dict__,
            cc=[r for r in (_recipient(v) for v in (value.get("ccRecipients") or [])[:20]) if r],
            body_text=text, body_truncated=truncated, body_source=source, attachments=attachments,
        )

    def get_message(self, tokens: dict, message_id: str, *, body_chars: int) -> EmailMessage:
        if not message_id or len(message_id) > 512:
            raise EmailProviderError("invalid_request")
        value = self.http.get_json(
            f"{GRAPH}/me/messages/{quote(message_id, safe='')}", token=tokens["access_token"],
            params={"$select": f"{_SELECT},body,ccRecipients"}, headers={"Prefer": _PREFER},
        )
        return self._message(value, tokens, body_chars, with_attachments=True)

    def get_thread(self, tokens: dict, thread_id: str, *, max_messages: int, body_chars: int) -> EmailThread:
        if not thread_id or len(thread_id) > 512:
            raise EmailProviderError("invalid_request")
        data = self.http.get_json(
            f"{GRAPH}/me/messages", token=tokens["access_token"], headers={"Prefer": _PREFER},
            params={"$filter": f"conversationId eq '{_odata_str(thread_id)}'", "$top": max_messages + 1,
                    "$select": f"{_SELECT},body,ccRecipients"},
        )
        values = [v for v in (data.get("value") or []) if isinstance(v, dict) and v.get("id")]
        messages = [self._message(v, tokens, body_chars, with_attachments=False) for v in values[:max_messages]]
        messages.sort(key=lambda m: m.received_at or datetime.min.replace(tzinfo=UTC))
        return EmailThread(thread_id=thread_id, messages=messages, complete=len(values) <= max_messages)
