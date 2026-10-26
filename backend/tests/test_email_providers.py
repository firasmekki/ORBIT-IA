"""Mailbox adapters against simulated Gmail / Microsoft Graph APIs:
listing, pagination, MIME, attachments metadata, errors and retries, and
the read-only guarantees. No database, no real provider."""

import inspect
import pathlib
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.connectivity.email_accounts import mime
from app.connectivity.email_accounts.contract import EmailQuery
from app.connectivity.email_accounts.errors import EmailProviderError
from app.connectivity.email_accounts.providers import ADAPTERS, base
from app.connectivity.email_accounts.providers.gmail import GmailAdapter, build_query
from app.connectivity.email_accounts.providers.outlook import OutlookAdapter
from app.connectivity.email_accounts.providers.yahoo import YahooAdapter
from email_support import FakeMail, gmail_message, install, use_settings

TOKENS = {"access_token": "gm-access-1", "refresh_token": "gm-refresh-1", "expires_at": "2099-01-01T00:00:00+00:00"}


@pytest.fixture
def fake(monkeypatch):
    use_settings(monkeypatch)
    mail = FakeMail()
    install(monkeypatch, mail)
    return mail


def test_gmail_lists_recent_inbox_messages_with_provider_facts(fake):
    fake.gmail_messages.append(gmail_message("m9", sender="nobody@exemple.tn", subject="Sans nom", unread=False))
    page = GmailAdapter("jean@gmail.example").list_recent(TOKENS, EmailQuery(limit=10), None)
    assert [i.message_id for i in page.items] == ["m1", "m2", "m3", "m9"]
    first = page.items[0]
    assert first.sender.address == "factures@exemple.tn" and first.sender.name == "Service Factures"
    assert first.subject == "Sujet 1" and first.is_unread is True
    assert first.received_at is not None and first.web_link.startswith("https://mail.google.com/")
    no_name = page.items[-1]
    assert no_name.sender.name is None and no_name.sender.address == "nobody@exemple.tn"
    assert no_name.is_unread is False


def test_gmail_pagination_and_limits(fake):
    adapter = GmailAdapter()
    first = adapter.list_recent(TOKENS, EmailQuery(limit=2), None)
    assert len(first.items) == 2 and first.next_cursor == "2"
    second = adapter.list_recent(TOKENS, EmailQuery(limit=2), first.next_cursor)
    assert [i.message_id for i in second.items] == ["m3"] and second.next_cursor is None
    adapter.list_recent(TOKENS, EmailQuery(limit=500), None)
    assert max(fake.page_size_seen) == 25
    with pytest.raises(EmailProviderError) as error:
        adapter.list_recent(TOKENS, EmailQuery(limit=2), "../../evil")
    assert error.value.code == "invalid_request"


def test_gmail_query_cannot_inject_operators():
    after = datetime(2026, 10, 20, tzinfo=UTC)
    query = build_query(EmailQuery(from_name='x") OR (in:sent', text="facture", unread_only=True,
                                   received_after=after, has_attachments=True), inbox_only=False)
    # the user value stays one quoted phrase; nothing outside quotes is the user's
    assert re.search(r'from:"x\s+OR\s+in:sent"', query)
    outside = re.sub(r'"[^"]*"', "", query)
    assert "(" not in outside and ")" not in outside and outside.count("in:sent") == 1  # only our -in:sent
    assert "is:unread" in query and "has:attachment" in query and f"after:{int(after.timestamp())}" in query


def test_gmail_multipart_message_plain_text_and_attachment_metadata(fake):
    fake.gmail_messages = [gmail_message("m1", body="Texte brut", html="<p>HTML</p>",
                                         attachments=[("facture.pdf", 81234)])]
    message = GmailAdapter().get_message(TOKENS, "m1", body_chars=6000)
    assert message.body_text == "Texte brut" and message.body_source == "text/plain"
    assert [(a.filename, a.size_bytes, a.is_inline) for a in message.attachments] == [("facture.pdf", 81234, False)]
    assert message.has_attachments is True
    assert not any("attachments" in path for _, _, path in fake.requests), "attachments are never downloaded"


def test_gmail_html_only_and_unreadable_bodies(fake):
    fake.gmail_messages = [
        gmail_message("m1", body=None, html="<html><style>p{}</style><p>Bonjour&nbsp;<b>Jean</b></p><script>bad()</script></html>"),
        gmail_message("m2", body=None, html=None),
    ]
    html = GmailAdapter().get_message(TOKENS, "m1", body_chars=6000)
    assert html.body_source == "text/html" and html.body_text == "Bonjour Jean"
    empty = GmailAdapter().get_message(TOKENS, "m2", body_chars=6000)
    assert empty.body_source == "none" and empty.body_text == ""


def test_body_size_is_bounded():
    text, truncated = mime.truncate("a" * 10_000, 6000)
    assert truncated and len(text) <= 6010
    assert mime.parse_address("=?utf-8?q?Soci=C3=A9t=C3=A9?= <x@y.tn>").name == "Société"
    assert mime.parse_address("not an address") is None


def test_outlook_lists_with_filter_and_follows_next_link_on_graph_only(fake):
    adapter = OutlookAdapter("jean@outlook.example")
    tokens = {**TOKENS, "access_token": "ms-access-1"}
    page = adapter.list_recent(tokens, EmailQuery(limit=2, unread_only=True,
                                                  received_after=datetime.now(UTC) - timedelta(days=1)), None)
    assert len(page.items) == 2 and page.next_cursor.startswith("https://graph.microsoft.com/")
    first = page.items[0]
    assert first.sender.address == "rh@entreprise.example" and first.sender.name is None  # name == address: no name
    assert first.is_unread is True and first.web_link.startswith("https://outlook.live.com/")
    assert len(adapter.list_recent(tokens, EmailQuery(limit=2), page.next_cursor).items) == 1
    with pytest.raises(EmailProviderError):
        adapter.list_recent(tokens, EmailQuery(limit=2), "https://evil.example/steal")


def test_outlook_message_html_body_and_attachment_metadata(fake):
    message = OutlookAdapter().get_message({**TOKENS, "access_token": "ms-access-1"}, "AAMk1=", body_chars=6000)
    assert message.body_text == "Corps 1" and message.body_source == "text/html"
    assert [(a.filename, a.size_bytes) for a in message.attachments] == [("contrat.pdf", 2048)]


def test_outlook_authorization_checks_nonce_scope_and_tenant(fake):
    adapter = OutlookAdapter()
    fake.outlook_nonce = "expected-nonce"
    identity = adapter.complete_authorization(code="auth-code-xyz", code_verifier="v" * 50, nonce="expected-nonce")
    assert identity.account_kind == "personal" and identity.external_account_id == "personal-sub"
    assert identity.display_name == "Jean D." and identity.tokens["refresh_token"] == "ms-refresh-1"
    with pytest.raises(EmailProviderError) as error:
        adapter.complete_authorization(code="auth-code-xyz", code_verifier="v" * 50, nonce="other-nonce")
    assert error.value.code == "oauth_exchange_failed"
    fake.outlook_scope = "openid profile email"
    with pytest.raises(EmailProviderError) as error:
        adapter.complete_authorization(code="auth-code-xyz", code_verifier="v" * 50, nonce="expected-nonce")
    assert error.value.code == "permission_missing"


def test_outlook_refresh_rotates_and_maps_policy_and_revocation(fake):
    adapter = OutlookAdapter()
    tokens = {"access_token": "ms-access-1", "refresh_token": "ms-refresh-1", "expires_at": "2000-01-01T00:00:00+00:00"}
    assert adapter.refresh(tokens)["refresh_token"] == "ms-refresh-2"
    fake.outlook_refresh_body = {"error": "invalid_grant", "error_description": "AADSTS70008: expired"}
    with pytest.raises(EmailProviderError) as error:
        adapter.refresh(tokens)
    assert error.value.code == "token_revoked"
    fake.outlook_refresh_body = {"error": "invalid_grant", "error_description": "AADSTS65001: consent required"}
    with pytest.raises(EmailProviderError) as error:
        adapter.refresh(tokens)
    assert error.value.code == "tenant_policy_blocked"
    assert adapter.revoke(tokens) == "not_supported"


@pytest.mark.parametrize("status,code", [(401, "token_expired"), (403, "permission_missing"), (404, "message_not_found")])
def test_http_errors_map_to_the_common_taxonomy(fake, status, code):
    fake.fail_with, fake.fail_times = status, 5
    with pytest.raises(EmailProviderError) as error:
        GmailAdapter().list_recent(TOKENS, EmailQuery(limit=2), None)
    assert error.value.code == code


def test_429_and_5xx_are_retried_then_reported(fake):
    fake.fail_with, fake.fail_times = 429, 1
    assert GmailAdapter().list_recent(TOKENS, EmailQuery(limit=1), None).items  # retried once, then fine
    fake.fail_with, fake.fail_times = 503, 10
    with pytest.raises(EmailProviderError) as error:
        GmailAdapter().list_recent(TOKENS, EmailQuery(limit=1), None)
    assert error.value.code == "provider_unavailable"
    fake.fail_with, fake.fail_times = 429, 10
    with pytest.raises(EmailProviderError) as error:
        GmailAdapter().list_recent(TOKENS, EmailQuery(limit=1), None)
    assert error.value.code == "provider_rate_limited"


def test_timeouts_and_network_errors(monkeypatch):
    use_settings(monkeypatch)
    monkeypatch.setattr(base, "sleep", lambda _s: None)

    def timeout(_request):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(base, "transport", httpx.MockTransport(timeout))
    with pytest.raises(EmailProviderError) as error:
        GmailAdapter().list_recent(TOKENS, EmailQuery(limit=1), None)
    assert error.value.code == "timeout"

    def refused(_request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(base, "transport", httpx.MockTransport(refused))
    with pytest.raises(EmailProviderError) as error:
        GmailAdapter().list_recent(TOKENS, EmailQuery(limit=1), None)
    assert error.value.code == "network_error"


def test_yahoo_is_prepared_but_disabled(monkeypatch):
    use_settings(monkeypatch)
    adapter = YahooAdapter()
    assert adapter.configuration_issues() and "désactivé" in adapter.configuration_issues()[0]
    for call in (lambda: adapter.list_recent(TOKENS, EmailQuery(), None),
                 lambda: adapter.authorization_url(state="s", code_challenge="c", nonce="n", login_hint=None)):
        with pytest.raises(EmailProviderError) as error:
            call()
        assert error.value.code == "provider_disabled"


def test_http_layer_refuses_anything_but_reads(monkeypatch):
    use_settings(monkeypatch)
    http = GmailAdapter().http
    with pytest.raises(base.ReadOnlyViolation):
        http.post_form("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", {"raw": "x"})
    with pytest.raises(base.ReadOnlyViolation):
        http.get_json("https://evil.example/collect", token="t")
    with pytest.raises(base.ReadOnlyViolation):
        http.get_json("http://gmail.googleapis.com/gmail/v1/users/me/profile", token="t")


def test_no_adapter_can_send_modify_move_or_delete():
    forbidden = re.compile(r"send|reply|forward|delete|trash|modify|move|archive|mark|draft|insert|import|batch", re.I)
    for cls in ADAPTERS.values():
        public = [name for name, _ in inspect.getmembers(cls, callable) if not name.startswith("_")]
        assert not [name for name in public if forbidden.search(name)], cls
    package = pathlib.Path(base.__file__).resolve().parents[1]
    sources = [package / "providers" / f for f in ("base.py", "gmail.py", "outlook.py", "yahoo.py")]
    sources += [package / "service.py", package.parents[1] / "agent" / "email_turn.py",
                package.parents[1] / "routers" / "email_accounts.py"]
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for scope in ("gmail.send", "gmail.modify", "gmail.compose", '"https://mail.google.com/"', "Mail.Send", "Mail.ReadWrite",
                      "mail-w", "/send", "sendMail", "smtplib"):
            assert scope not in text, f"{scope} in {path.name}"
