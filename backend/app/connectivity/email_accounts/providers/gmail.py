"""Gmail API adapter - scope gmail.readonly only.

The consent URL never sets include_granted_scopes, so an older grant of
another scope (the first connector's optional sending scope) is not carried
into a new token.
"""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode

from app.connectivity.email_accounts import mime
from app.connectivity.email_accounts.contract import (
    Capabilities,
    ConnectedIdentity,
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

READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _quote_term(value: str) -> str:
    """A user value inside a Gmail query, unable to inject operators."""
    cleaned = re.sub(r'["(){}\[\]\\]', " ", value).strip()[:200]
    return f'"{cleaned}"' if cleaned else ""


def build_query(query: EmailQuery, *, inbox_only: bool) -> str:
    terms = ["in:inbox"] if inbox_only else ["-in:sent", "-in:drafts", "-in:spam", "-in:trash"]
    if query.from_address:
        terms.append(f"from:{_quote_term(query.from_address)}")
    elif query.from_name:
        terms.append(f"from:{_quote_term(query.from_name)}")
    if query.subject:
        terms.append(f"subject:{_quote_term(query.subject)}")
    if query.text:
        terms.append(_quote_term(query.text))
    if query.unread_only:
        terms.append("is:unread")
    if query.has_attachments:
        terms.append("has:attachment")
    if query.received_after:
        terms.append(f"after:{int(query.received_after.timestamp())}")
    if query.received_before:
        terms.append(f"before:{int(query.received_before.timestamp())}")
    return " ".join(t for t in terms if t)


def _headers(payload: dict) -> dict[str, str]:
    return {(h.get("name") or "").lower(): h.get("value") or "" for h in (payload.get("payload") or {}).get("headers") or []}


def _received_at(payload: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(payload["internalDate"]) / 1000, tz=UTC)
    except (KeyError, ValueError, TypeError):
        return None


def _web_link(account_email: str | None, message_id: str) -> str:
    # Not a contractual Gmail URL format: opens the message in the account's
    # web UI in practice. authuser selects the right signed-in account.
    prefix = f"https://mail.google.com/mail/?authuser={quote(account_email, safe='')}" if account_email else "https://mail.google.com/mail/"
    return f"{prefix}#all/{message_id}"


class GmailAdapter:
    provider = "gmail"
    label = "Gmail"
    callback_path = "/api/connectivity/gmail/callback"
    capabilities = Capabilities(server_side_search=True, unread_filter=True, threads="native",
                                deep_link=True, programmatic_revoke=True, max_page_size=25)

    def __init__(self, account_email: str | None = None):
        self.account_email = account_email
        self.http = MailHttp(api_hosts={"gmail.googleapis.com"}, auth_post_urls={TOKEN_URL, REVOKE_URL})

    # --- configuration / authorization -------------------------------------

    def configuration_issues(self) -> list[str]:
        s = get_settings()
        issues = []
        client_id = s.gmail_oauth_client_id or ""
        if not client_id.strip():
            issues.append("GMAIL_OAUTH_CLIENT_ID manquant")
        elif client_id != client_id.strip() or not client_id.endswith(".apps.googleusercontent.com"):
            issues.append("GMAIL_OAUTH_CLIENT_ID invalide (client OAuth Web Google attendu)")
        secret = s.gmail_oauth_client_secret or ""
        if not secret.strip():
            issues.append("GMAIL_OAUTH_CLIENT_SECRET manquant")
        elif secret != secret.strip():
            issues.append("GMAIL_OAUTH_CLIENT_SECRET invalide (espaces en début ou fin)")
        if not valid_redirect_uri(s.gmail_oauth_redirect_uri or "", self.callback_path):
            issues.append("GMAIL_OAUTH_REDIRECT_URI invalide (URI exacte du callback, HTTPS sauf localhost)")
        return issues

    def authorization_url(self, *, state: str, code_challenge: str, nonce: str, login_hint: str | None) -> str:
        s = get_settings()
        params = {
            "client_id": s.gmail_oauth_client_id, "redirect_uri": s.gmail_oauth_redirect_uri,
            "response_type": "code", "scope": READ_SCOPE, "access_type": "offline", "prompt": "consent",
            "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256",
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{AUTH_URL}?{urlencode(params)}"

    def complete_authorization(self, *, code: str, code_verifier: str, nonce: str) -> ConnectedIdentity:
        s = get_settings()
        response = self.http.post_form(TOKEN_URL, {
            "code": code, "client_id": s.gmail_oauth_client_id, "client_secret": s.gmail_oauth_client_secret,
            "redirect_uri": s.gmail_oauth_redirect_uri, "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        })
        if response.status_code != 200:
            error = token_error(response)
            raise EmailProviderError("oauth_exchange_failed" if error.code == "token_revoked" else error.code)
        tokens = response.json()
        scopes = tuple(str(tokens.get("scope") or "").split())
        if READ_SCOPE not in scopes or not tokens.get("access_token"):
            raise EmailProviderError("permission_missing")
        bundle = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token") or "",
            "expires_at": (datetime.now(UTC) + timedelta(seconds=int(tokens.get("expires_in", 3600)))).isoformat(),
        }
        profile = self.http.get_json(f"{API}/profile", token=bundle["access_token"])
        address = str(profile.get("emailAddress") or "").strip()
        if "@" not in address:
            raise EmailProviderError("oauth_exchange_failed")
        return ConnectedIdentity(email_address=address, external_account_id=address.lower(),
                                 display_name=None, granted_scopes=scopes, tokens=bundle)

    def refresh(self, tokens: dict) -> dict:
        if not tokens.get("refresh_token"):
            raise EmailProviderError("token_expired")
        s = get_settings()
        response = self.http.post_form(TOKEN_URL, {
            "client_id": s.gmail_oauth_client_id, "client_secret": s.gmail_oauth_client_secret,
            "refresh_token": tokens["refresh_token"], "grant_type": "refresh_token",
        })
        if response.status_code != 200:
            raise token_error(response)
        fresh = response.json()
        return {
            "access_token": fresh["access_token"],
            "refresh_token": fresh.get("refresh_token") or tokens["refresh_token"],
            "expires_at": (datetime.now(UTC) + timedelta(seconds=int(fresh.get("expires_in", 3600)))).isoformat(),
        }

    def revoke(self, tokens: dict) -> str:
        # Revoking the refresh token revokes the whole grant at Google.
        token = tokens.get("refresh_token") or tokens.get("access_token")
        if not token:
            return "failed"
        try:
            response = self.http.post_form(REVOKE_URL, {"token": token})
        except EmailProviderError:
            return "failed"
        return "revoked" if response.status_code == 200 else "failed"

    def verify(self, tokens: dict) -> None:
        self.http.get_json(f"{API}/profile", token=tokens["access_token"])

    # --- reading ------------------------------------------------------------

    def list_recent(self, tokens: dict, query: EmailQuery, cursor: str | None) -> EmailPage:
        return self._list(tokens, query, cursor, inbox_only=True)

    def search(self, tokens: dict, query: EmailQuery, cursor: str | None) -> EmailPage:
        return self._list(tokens, query, cursor, inbox_only=False)

    def _list(self, tokens: dict, query: EmailQuery, cursor: str | None, *, inbox_only: bool) -> EmailPage:
        params = {"q": build_query(query, inbox_only=inbox_only), "maxResults": max(1, min(query.limit, 25))}
        if cursor:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", cursor):
                raise EmailProviderError("invalid_request")
            params["pageToken"] = cursor
        listing = self.http.get_json(f"{API}/messages", token=tokens["access_token"], params=params)
        items = []
        for ref in (listing.get("messages") or [])[: params["maxResults"]]:
            message_id = str(ref.get("id") or "")
            if not _ID.match(message_id):
                continue
            payload = self.http.get_json(
                f"{API}/messages/{message_id}", token=tokens["access_token"],
                params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
            )
            items.append(self._summary(payload))
        next_cursor = listing.get("nextPageToken")
        return EmailPage(items=items, next_cursor=str(next_cursor) if next_cursor else None)

    def _summary(self, payload: dict) -> EmailSummaryItem:
        headers = _headers(payload)
        labels = payload.get("labelIds") or []
        message_id = str(payload.get("id"))
        return EmailSummaryItem(
            message_id=message_id,
            thread_id=payload.get("threadId"),
            sender=mime.parse_address(headers.get("from")),
            to=mime.parse_address_list(headers.get("to")),
            subject=mime.decode_mime_words(headers.get("subject")) or None,
            received_at=_received_at(payload),
            snippet=(mime.normalize_text(mime.html_to_text(payload.get("snippet") or ""))[:300] or None),
            is_unread="UNREAD" in labels,
            has_attachments=None,  # unknown from metadata; known once the message is read
            web_link=_web_link(self.account_email, message_id),
        )

    def get_message(self, tokens: dict, message_id: str, *, body_chars: int) -> EmailMessage:
        if not _ID.match(message_id or ""):
            raise EmailProviderError("invalid_request")
        payload = self.http.get_json(f"{API}/messages/{message_id}", token=tokens["access_token"],
                                     params={"format": "full"})
        return self._message(payload, body_chars)

    def _message(self, payload: dict, body_chars: int) -> EmailMessage:
        summary = self._summary(payload)
        body, source, attachments = mime.gmail_parts(payload.get("payload") or {})
        text, truncated = mime.truncate(body, body_chars)
        headers = _headers(payload)
        return EmailMessage(
            **{**summary.__dict__, "has_attachments": bool(attachments)},
            cc=mime.parse_address_list(headers.get("cc")),
            body_text=text, body_truncated=truncated, body_source=source, attachments=attachments,
        )

    def get_thread(self, tokens: dict, thread_id: str, *, max_messages: int, body_chars: int) -> EmailThread:
        if not _ID.match(thread_id or ""):
            raise EmailProviderError("invalid_request")
        payload = self.http.get_json(f"{API}/threads/{thread_id}", token=tokens["access_token"],
                                     params={"format": "full"})
        raw = payload.get("messages") or []
        messages = [self._message(m, body_chars) for m in raw[-max_messages:]]
        return EmailThread(thread_id=thread_id, messages=messages, complete=len(raw) <= max_messages)
