import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.connectivity import email as mail
from app.connectivity import registry
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.runtime_setting import RuntimeSetting
from app.routers.connectivity import router
from app.agent.orchestrator import _record_tool_call


def admin_client(db, user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def configured_mail(db, user, *, roles=None, send_roles=None):
    mail.save_config(db, {
        "enabled": True, "imap_host": "imap.example.test", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.example.test", "smtp_port": 465, "smtp_ssl": True, "username": "orbit@example.test",
        "mailbox": "INBOX", "allowed_roles": roles or [user.role], "send_roles": send_roles or [],
        "tools": {"search_emails": True, "read_email": True, "draft_email": True},
    }, user.username)
    mail.set_password(db, "test-application-password", user.username)


def test_unknown_and_future_connectors_are_never_reported_as_connected(db, make_test_user):
    director = make_test_user(username="connectivity_admin", role="DIRECTOR")
    client = admin_client(db, director)
    snapshot = client.get("/api/admin/connectivity").json()
    cards = {item["id"]: item for item in snapshot["connectors"]}
    assert cards["email_imap"]["state"] == "not_configured"
    assert cards["email_imap"]["configured"] is False
    assert cards["web_search"]["state"] in ("disabled", "not_configured")
    assert cards["google_gmail"]["state"] == "not_configured"
    assert cards["microsoft_outlook"]["state"] == "not_configured"
    assert cards["yahoo_mail"]["state"] == "development" and cards["yahoo_mail"]["enabled"] is False
    assert cards["whatsapp_business"]["state"] == "development"
    assert cards["calendar"]["tools"] == []


def test_personal_connectivity_overview_hides_admin_provider_settings(db, make_test_user):
    employee = make_test_user(username="connectivity_personal", role="EMPLOYEE")
    client = admin_client(db, employee)
    response = client.get("/api/connectivity/overview")
    assert response.status_code == 200
    body = response.json()
    assert {card["id"] for card in body["connectors"]} == {"google_gmail", "microsoft_outlook", "yahoo_mail", "whatsapp_business"}
    assert body["email"]["imap_host"] == ""
    assert body["history"] == []
    assert client.get("/api/admin/connectivity").status_code == 403


def test_web_provider_is_only_connected_after_a_successful_real_call(db, monkeypatch):
    from types import SimpleNamespace
    config = SimpleNamespace(enabled=True, provider="brave", trigger="auto")
    monkeypatch.setattr(registry, "load_config", lambda _: config)
    monkeypatch.setattr(registry, "make_provider", lambda _: SimpleNamespace(configured=lambda: (True, None)))
    status = {"connection": "invalid_key", "error": "clé refusée"}
    monkeypatch.setattr(registry, "web_last_status", lambda: status)
    card = next(item for item in registry.list_connectors(db) if item["id"] == "web_search")
    assert card["state"] == "auth_expired"
    status.clear()
    card = next(item for item in registry.list_connectors(db) if item["id"] == "web_search")
    assert card["state"] == "configured"
    status["connection"] = "connected"
    card = next(item for item in registry.list_connectors(db) if item["id"] == "web_search")
    assert card["state"] == "connected"


def test_email_settings_validation_encryption_and_masking(db, make_test_user):
    director = make_test_user(username="connectivity_secret", role="DIRECTOR")
    client = admin_client(db, director)
    assert client.put("/api/admin/connectivity/email", json={"imap_port": 70000}).status_code == 422
    assert client.put("/api/admin/connectivity/email", json={"unknown": "field"}).status_code == 422
    configured_mail(db, director)
    response = client.get("/api/admin/connectivity").json()
    assert response["email"]["credentials_configured"] is True
    assert "test-application-password" not in str(response)
    stored = db.get(RuntimeSetting, mail.SECRETS_KEY).value["email_password"]
    assert "test-application-password" not in stored
    assert mail._password(db) == "test-application-password"


def test_tools_are_discovered_only_when_enabled_and_role_is_authorized(db, make_test_user):
    director = make_test_user(username="connectivity_tools", role="DIRECTOR")
    configured_mail(db, director, roles=["HR"])
    assert mail.tools_for_role(db, "DIRECTOR") == []
    assert len(mail.tools_for_role(db, "HR")) == 3
    mail.save_config(db, {
        "tool_roles": {"search_emails": ["DIRECTOR"], "read_email": ["HR"], "draft_email": []},
        "tools": {"search_emails": True, "read_email": True, "draft_email": True},
    }, director.username)
    assert [x["function"]["name"] for x in mail.tools_for_role(db, "DIRECTOR")] == ["search_emails"]
    assert [x["function"]["name"] for x in mail.tools_for_role(db, "HR")] == ["read_email"]
    assert mail.tools_for_role(db, "EMPLOYEE") == []


def test_disabled_email_tool_is_refused_even_if_called_directly(db, make_test_user, monkeypatch):
    director = make_test_user(username="connectivity_disabled_tool", role="DIRECTOR")
    configured_mail(db, director)
    mail.save_config(db, {"tools": {"search_emails": False}}, director.username)
    monkeypatch.setattr(mail, "SessionLocal", lambda: db)
    assert mail.execute_tool(str(director.id), director.role, "search_emails", {}) == {
        "error": "Outil email désactivé ou non autorisé."
    }


def test_email_search_uses_validated_filters_and_bounds_results(monkeypatch):
    class Imap:
        def select(self, mailbox, readonly=False): return "OK", [b"2"]
        def uid(self, action, *args):
            if action == "search":
                criteria = args[1:]
                assert criteria == ("FROM", '"client@example.test"', "SINCE", "01-Jan-2026")
                return "OK", [b"1 2"]
            if action == "fetch":
                uid, command = args
                return "OK", [(b"header", b"From: client@example.test\r\nTo: orbit@example.test\r\nSubject: Demande\r\nDate: Tue, 01 Jan 2026 00:00:00 +0000\r\nMessage-ID: <one>\r\n\r\n")]
        def logout(self): return "BYE", []

    monkeypatch.setattr(mail, "_connect_imap", lambda *_: Imap())
    conf = {"mailbox": "INBOX"}
    result = mail._run_imap_tool("search_emails", {"from": "client@example.test", "since": "2026-01-01", "limit": 500}, conf, "secret")
    assert result["count"] == 2
    assert result["messages"][0]["subject"] == "Demande"
    assert result["messages"][0]["thread_id"] == "<one>"
    with pytest.raises(ValueError):
        mail._criteria({"since": "2026-99-01"})


def test_email_draft_tool_never_sends_and_send_requires_exact_confirmation(db, make_test_user, monkeypatch):
    director = make_test_user(username="connectivity_send", role="DIRECTOR")
    configured_mail(db, director, send_roles=["DIRECTOR"])
    monkeypatch.setattr(mail, "SessionLocal", lambda: db)
    draft_result = mail.execute_tool(str(director.id), director.role, "draft_email",
                                     {"to": "client@example.test", "subject": "Réponse", "body": "Bonjour."})
    assert draft_result["status"] == "draft_only" and draft_result["confirmation_required"]

    draft = {"to": "client@example.test", "subject": "Réponse", "body": "Bonjour."}
    pending = mail.create_pending(db, director, draft)
    sent = []

    class Smtp:
        def __init__(self, *args, **kwargs): pass
        def login(self, *_): pass
        def send_message(self, message): sent.append(message)
        def quit(self): pass

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", Smtp)
    monkeypatch.setattr(mail, "log_event", lambda **_: None)
    altered = {**draft, "body": "Modified."}
    with pytest.raises(ValueError, match="contenu modifié"):
        mail.send_confirmed(db, director, altered, pending["confirmation"])
    assert sent == []
    assert mail.send_confirmed(db, director, draft, pending["confirmation"])["status"] == "sent"
    assert len(sent) == 1
    assert sent[0]["To"] == draft["to"]
    with pytest.raises(ValueError):
        mail.send_confirmed(db, director, draft, pending["confirmation"])


def test_email_send_roles_are_separate_from_read_roles(db, make_test_user):
    director = make_test_user(username="connectivity_role_separation", role="DIRECTOR")
    configured_mail(db, director, roles=["DIRECTOR"], send_roles=[])
    draft = {"to": "client@example.test", "subject": "Réponse", "body": "Bonjour."}
    pending = mail.create_pending(db, director, draft)
    with pytest.raises(ValueError, match="n'est pas autorisé"):
        mail.send_confirmed(db, director, draft, pending["confirmation"])


def test_connectivity_secret_and_future_tool_names_are_not_accepted(db, make_test_user):
    director = make_test_user(username="connectivity_future", role="DIRECTOR")
    client = admin_client(db, director)
    configured_mail(db, director)
    invalid = client.put("/api/admin/connectivity/email",
                         json={"tools": {"delete_emails": True, "send_email": True}})
    assert invalid.status_code == 422
    assert "test-application-password" not in invalid.text


def test_email_calls_are_bounded_and_trace_never_keeps_mail_content():
    trace = []
    _record_tool_call(trace, "draft_email",
                      {"to": "private.person@example.test", "subject": "Confidentiel", "body": "SECRET_BODY"},
                      {"status": "draft_only"})
    _record_tool_call(trace, "search_emails", {"from": "private.person@example.test", "subject": "Confidentiel"},
                      {"messages": [], "count": 0})
    rendered = str(trace)
    assert "private.person@example.test" not in rendered
    assert "Confidentiel" not in rendered
    assert "SECRET_BODY" not in rendered
    assert trace[0]["arguments"]["body_length"] == len("SECRET_BODY")


def test_smtp_and_imap_are_tested_without_sending_a_message(db, make_test_user, monkeypatch):
    director = make_test_user(username="connectivity_test_transport", role="DIRECTOR")
    configured_mail(db, director)
    calls = []

    class Imap:
        def select(self, mailbox, readonly=False): calls.append(("select", mailbox, readonly)); return "OK", [b"0"]
        def logout(self): calls.append(("imap_logout",)); return "BYE", []

    class Smtp:
        def __init__(self, *args, **kwargs): calls.append(("smtp_connect",))
        def login(self, *_): calls.append(("smtp_login",))
        def quit(self): calls.append(("smtp_quit",))
        def send_message(self, *_): calls.append(("smtp_send",))

    monkeypatch.setattr(mail, "_connect_imap", lambda *_: Imap())
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", Smtp)
    result = mail.test_connection(db)
    assert result["state"] == "connected"
    assert ("smtp_login",) in calls and not any(call[0] == "smtp_send" for call in calls)
