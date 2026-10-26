"""Shared fakes for the personal mailbox tests: simulated Gmail / Microsoft
Graph / OAuth endpoints (httpx.MockTransport) and test settings. No real
mailbox, account or provider is ever contacted: any request to a host the
fakes do not know fails the test."""

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
from cryptography.fernet import Fernet

from app.core.config import get_settings

TEST_KEY = Fernet.generate_key().decode()
GMAIL_CLIENT = "test-client.apps.googleusercontent.com"
OUTLOOK_CLIENT = "11111111-2222-3333-4444-555555555555"
GMAIL_READ = "https://www.googleapis.com/auth/gmail.readonly"
SECRET_VALUES = ("gm-access-1", "gm-refresh-1", "ms-access-1", "ms-refresh-1", "gm-access-2", "ms-refresh-2",
                 "test-client-secret", "auth-code-xyz")

PATCHED_MODULES = (
    "app.connectivity.email_accounts.providers.base",
    "app.connectivity.email_accounts.providers.gmail",
    "app.connectivity.email_accounts.providers.outlook",
    "app.connectivity.email_accounts.providers.yahoo",
    "app.connectivity.email_accounts.crypto",
    "app.connectivity.email_accounts.service",
    "app.connectivity.email_accounts.legacy",
    "app.agent.email_turn",
    "app.routers.email_accounts",
    "app.connectivity.registry",
)


def email_settings(**overrides):
    values = dict(
        gmail_oauth_client_id=GMAIL_CLIENT, gmail_oauth_client_secret="test-client-secret",
        gmail_oauth_redirect_uri="http://localhost:8081/api/connectivity/gmail/callback",
        outlook_oauth_client_id=OUTLOOK_CLIENT, outlook_oauth_client_secret="test-client-secret",
        outlook_oauth_redirect_uri="http://localhost:8081/api/connectivity/outlook/callback",
        outlook_oauth_authority="common", yahoo_mail_enabled=False,
        email_token_encryption_key=TEST_KEY, email_default_timezone="Africa/Tunis",
        email_frontend_redirect_uri="http://localhost:8081/connectivity",
    )
    values.update(overrides)
    return get_settings().model_copy(update=values)


def use_settings(monkeypatch, **overrides):
    import importlib

    settings = email_settings(**overrides)
    for name in PATCHED_MODULES:
        monkeypatch.setattr(importlib.import_module(name), "get_settings", lambda: settings, raising=False)
    return settings


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_message(message_id, *, sender="Service Factures <factures@exemple.tn>", subject="Facture octobre",
                  body="Bonjour, merci de régler la facture avant le 31/10/2026.", html=None, attachments=(),
                  unread=True, minutes_ago=30, plain=True):
    parts = []
    if plain and body is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": b64(body)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html)}})
    for name, size in attachments:
        parts.append({"mimeType": "application/pdf", "filename": name,
                      "body": {"attachmentId": f"att-{name}", "size": size},
                      "headers": [{"name": "Content-Disposition", "value": f'attachment; filename="{name}"'}]})
    headers = [{"name": "Subject", "value": subject}, {"name": "To", "value": "moi@exemple.tn"}]
    if sender is not None:
        headers.append({"name": "From", "value": sender})
    return {
        "id": message_id, "threadId": f"t{message_id}", "labelIds": ["INBOX", *(["UNREAD"] if unread else [])],
        "snippet": (body or "")[:80], "internalDate": str(int((time.time() - minutes_ago * 60) * 1000)),
        "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts},
    }


class FakeMail:
    """In-memory Gmail + Graph + OAuth endpoints. `requests` records every
    (method, host, path) - tests assert on it (read-only, hosts used)."""

    def __init__(self):
        self.requests: list[tuple[str, str, str]] = []
        self.gmail_messages = [gmail_message(f"m{i}", minutes_ago=10 * i, subject=f"Sujet {i}") for i in range(1, 4)]
        self.gmail_profile = "jean@gmail.example"
        self.gmail_scope = GMAIL_READ
        self.gmail_refresh_status = 200
        self.outlook_messages = [self._outlook(i) for i in range(1, 4)]
        self.outlook_claims = {"tid": "9188040d-6c67-4c5b-b112-36a304b66dad", "sub": "personal-sub",
                               "email": "jean@outlook.example", "name": "Jean D."}
        self.outlook_scope = "https://graph.microsoft.com/Mail.Read openid profile email"
        self.outlook_refresh_body = None  # None = success
        self.outlook_nonce = None
        self.revoked: list[str] = []
        self.fail_with: int | None = None  # status for every mail API GET
        self.fail_times = 0
        self.page_size_seen: list[int] = []
        self.refresh_calls = 0

    @staticmethod
    def _outlook(i):
        return {"id": f"AAMk{i}=", "conversationId": f"conv{i}", "subject": f"Outlook {i}",
                "from": {"emailAddress": {"address": "rh@entreprise.example", "name": "rh@entreprise.example"}},
                "toRecipients": [{"emailAddress": {"address": "jean@outlook.example", "name": "Jean"}}],
                "receivedDateTime": (datetime.now(UTC) - timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "bodyPreview": f"Aperçu {i}", "isRead": i != 1, "hasAttachments": i == 1,
                "webLink": f"https://outlook.live.com/owa/?ItemID=AAMk{i}%3D",
                "body": {"contentType": "html", "content": f"<p>Corps <b>{i}</b></p><script>x()</script>"},
                "ccRecipients": []}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host, path, method = request.url.host, request.url.path, request.method
        self.requests.append((method, host, path))
        if host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            if path == "/revoke":
                self.revoked.append(form["token"][0])
                return httpx.Response(200)
            if form.get("grant_type") == ["refresh_token"]:
                self.refresh_calls += 1
                if self.gmail_refresh_status != 200:
                    return httpx.Response(self.gmail_refresh_status, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": "gm-access-2", "expires_in": 3600, "scope": GMAIL_READ})
            assert form["code_verifier"][0], "PKCE verifier must be sent"
            return httpx.Response(200, json={"access_token": "gm-access-1", "refresh_token": "gm-refresh-1",
                                             "expires_in": 3600, "scope": self.gmail_scope})
        if host == "login.microsoftonline.com":
            form = parse_qs(request.content.decode())
            if form.get("grant_type") == ["refresh_token"]:
                self.refresh_calls += 1
                if self.outlook_refresh_body is not None:
                    return httpx.Response(400, json=self.outlook_refresh_body)
                return httpx.Response(200, json={"access_token": "ms-access-2", "refresh_token": "ms-refresh-2",
                                                 "expires_in": 3600, "scope": self.outlook_scope})
            claims = {"aud": OUTLOOK_CLIENT, "iss": f"https://login.microsoftonline.com/{self.outlook_claims['tid']}/v2.0",
                      "nonce": self.outlook_nonce, "exp": time.time() + 3600, **self.outlook_claims}
            id_token = "e30." + b64(json.dumps(claims)) + ".sig"
            return httpx.Response(200, json={"access_token": "ms-access-1", "refresh_token": "ms-refresh-1",
                                             "expires_in": 3600, "scope": self.outlook_scope, "id_token": id_token})
        if self.fail_with and self.fail_times:
            self.fail_times -= 1
            return httpx.Response(self.fail_with, headers={"Retry-After": "0"}, text="error")
        if host == "gmail.googleapis.com":
            return self._gmail(request, path)
        if host == "graph.microsoft.com":
            return self._graph(request, path)
        raise AssertionError(f"unexpected host contacted: {host}")

    def _gmail(self, request, path):
        assert request.method == "GET"
        base = "/gmail/v1/users/me"
        if path == f"{base}/profile":
            return httpx.Response(200, json={"emailAddress": self.gmail_profile})
        if path == f"{base}/messages":
            size = int(request.url.params["maxResults"])
            self.page_size_seen.append(size)
            start = int(request.url.params.get("pageToken", "0") or 0)
            chunk = self.gmail_messages[start:start + size]
            body = {"messages": [{"id": m["id"]} for m in chunk]}
            if start + size < len(self.gmail_messages):
                body["nextPageToken"] = str(start + size)
            return httpx.Response(200, json=body)
        for message in self.gmail_messages:
            if path == f"{base}/messages/{message['id']}":
                return httpx.Response(200, json=message)
        return httpx.Response(404, json={"error": {"code": 404}})

    def _graph(self, request, path):
        assert request.method == "GET"
        if path == "/v1.0/me/mailFolders/inbox":
            return httpx.Response(200, json={"id": "inbox"})
        if path == "/v1.0/me/mailFolders/inbox/messages":
            top = int(request.url.params.get("$top", "10"))
            skip = int(request.url.params.get("$skip", "0"))
            chunk = self.outlook_messages[skip:skip + top]
            body = {"value": chunk}
            if skip + top < len(self.outlook_messages):
                body["@odata.nextLink"] = (f"https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
                                           f"?$top={top}&$skip={skip + top}")
            return httpx.Response(200, json=body)
        for message in self.outlook_messages:
            if path == f"/v1.0/me/messages/{message['id']}":
                return httpx.Response(200, json=message)
            if path == f"/v1.0/me/messages/{message['id']}/attachments":
                assert "contentBytes" not in request.url.params.get("$select", "")
                return httpx.Response(200, json={"value": [{"name": "contrat.pdf", "contentType": "application/pdf",
                                                            "size": 2048, "isInline": False}]})
        return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})


def install(monkeypatch, fake: FakeMail):
    from app.connectivity.email_accounts.providers import base

    monkeypatch.setattr(base, "transport", fake.transport())
    monkeypatch.setattr(base, "sleep", lambda _seconds: None)


def connect(db, user, provider, fake: FakeMail):
    """Runs the real start + callback flow against the fakes."""
    from urllib.parse import parse_qs as qs, urlparse

    from app.connectivity.email_accounts import service

    url, binding = service.start_authorization(db, user.id, provider)
    params = {k: v[0] for k, v in qs(urlparse(url).query).items()}
    if provider == "outlook":
        fake.outlook_nonce = params["nonce"]
    _, conn = service.complete_authorization(db, provider, state=params["state"], code="auth-code-xyz",
                                             binding=binding, error=None, error_description=None)
    return conn
