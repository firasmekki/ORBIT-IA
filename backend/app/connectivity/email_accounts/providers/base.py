"""What every mailbox adapter implements, and the one HTTP client they use.

Read-only by construction, not only by convention:
- the adapter interface below has no send/modify/move/flag/delete method;
- MailHttp only allows GET towards a provider's mail API, and POST only to
  the exact OAuth token/revocation URLs the adapter declares - any other
  request raises before leaving the process (tests/test_email_read_only.py).

URLs are never logged (they carry search queries and message ids); the
httpx logger itself is set to WARNING in app/main.py.
"""

import logging
import time
from typing import ClassVar, Protocol
from urllib.parse import urlsplit

import httpx

from app.connectivity.email_accounts.contract import (
    Capabilities,
    ConnectedIdentity,
    EmailMessage,
    EmailPage,
    EmailQuery,
    EmailThread,
)
from app.connectivity.email_accounts.errors import EmailProviderError
from app.core.config import get_settings

# Test seams (same pattern as app/websearch/service.py): an httpx transport
# replacing the network, and the sleep used between retries.
transport: httpx.BaseTransport | None = None
sleep = time.sleep

MAX_ATTEMPTS = 3
MAX_RETRY_WAIT_SECONDS = 8.0

# httpx logs each request URL at INFO: a mailbox search query and message
# ids would end up in the logs. Also set in app/main.py; repeated here so it
# holds wherever the adapters are used (scripts, tests).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class ReadOnlyViolation(RuntimeError):
    """A request outside the declared read-only surface - a bug, never a user error."""


class MailHttp:
    def __init__(self, *, api_hosts: set[str], auth_post_urls: set[str]):
        self.api_hosts = api_hosts
        self.auth_post_urls = auth_post_urls

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=get_settings().email_provider_timeout_seconds, transport=transport,
                            follow_redirects=False)

    def get_json(self, url: str, *, token: str, params=None, headers=None) -> dict:
        host = urlsplit(url).hostname or ""
        if urlsplit(url).scheme != "https" or host not in self.api_hosts:
            raise ReadOnlyViolation("GET outside the mail API")
        response = self._send("GET", url, params=params,
                              headers={"Authorization": f"Bearer {token}", **(headers or {})})
        return self._json(response)

    def post_form(self, url: str, data: dict) -> httpx.Response:
        """Token exchange / refresh / revocation only. Never retried: a
        refresh token may rotate on use (Microsoft)."""
        if url not in self.auth_post_urls:
            raise ReadOnlyViolation("POST outside the OAuth endpoints")
        try:
            with self._client() as client:
                return client.post(url, data=data, headers={"Accept": "application/json"})
        except httpx.TimeoutException:
            raise EmailProviderError("timeout") from None
        except httpx.HTTPError:
            raise EmailProviderError("network_error") from None

    def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        last: EmailProviderError | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                with self._client() as client:
                    response = client.request(method, url, **kwargs)
            except httpx.TimeoutException:
                last = EmailProviderError("timeout")
            except httpx.HTTPError:
                last = EmailProviderError("network_error")
            else:
                error = map_status(response)
                if error is None:
                    return response
                if error.code not in ("provider_rate_limited", "provider_unavailable"):
                    raise error
                last = error
            if attempt < MAX_ATTEMPTS - 1:
                wait = last.retry_after if last.retry_after is not None else 2 ** attempt
                sleep(min(max(wait, 0.0), MAX_RETRY_WAIT_SECONDS))
        raise last

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            value = response.json()
        except ValueError:
            raise EmailProviderError("provider_unavailable") from None
        if not isinstance(value, dict):
            raise EmailProviderError("provider_unavailable")
        return value


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


def map_status(response: httpx.Response) -> EmailProviderError | None:
    code = response.status_code
    if code < 400:
        return None
    if code == 401:
        return EmailProviderError("token_expired")
    if code == 403:
        body = response.text[:2000].lower()
        if "ratelimit" in body or "userlimit" in body or "quota" in body:
            return EmailProviderError("provider_rate_limited", retry_after=_retry_after(response))
        return EmailProviderError("permission_missing")
    if code == 404:
        return EmailProviderError("message_not_found")
    if code == 429:
        return EmailProviderError("provider_rate_limited", retry_after=_retry_after(response))
    if code >= 500:
        return EmailProviderError("provider_unavailable", retry_after=_retry_after(response))
    return EmailProviderError("invalid_request")


def token_error(response: httpx.Response) -> EmailProviderError:
    """Maps an OAuth token-endpoint failure (RFC 6749 §5.2)."""
    if response.status_code >= 500:
        return EmailProviderError("provider_unavailable")
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = str(body.get("error") or "").lower()
    description = str(body.get("error_description") or "")
    if any(code in description for code in ("AADSTS65001", "AADSTS90094", "AADSTS90008", "AADSTS50105", "AADSTS53003")):
        return EmailProviderError("tenant_policy_blocked")
    if error == "invalid_grant":
        return EmailProviderError("token_revoked")
    if error in ("invalid_client", "unauthorized_client"):
        return EmailProviderError("oauth_config_invalid")
    return EmailProviderError("oauth_exchange_failed")


class EmailProviderAdapter(Protocol):
    provider: ClassVar[str]
    label: ClassVar[str]
    capabilities: ClassVar[Capabilities]
    callback_path: ClassVar[str]

    def configuration_issues(self) -> list[str]: ...
    def authorization_url(self, *, state: str, code_challenge: str, nonce: str, login_hint: str | None) -> str: ...
    def complete_authorization(self, *, code: str, code_verifier: str, nonce: str) -> ConnectedIdentity: ...
    def refresh(self, tokens: dict) -> dict: ...
    def revoke(self, tokens: dict) -> str: ...  # "revoked" | "not_supported" | "failed"
    def verify(self, tokens: dict) -> None: ...
    def list_recent(self, tokens: dict, query: EmailQuery, cursor: str | None) -> EmailPage: ...
    def search(self, tokens: dict, query: EmailQuery, cursor: str | None) -> EmailPage: ...
    def get_message(self, tokens: dict, message_id: str, *, body_chars: int) -> EmailMessage: ...
    def get_thread(self, tokens: dict, thread_id: str, *, max_messages: int, body_chars: int) -> EmailThread: ...
