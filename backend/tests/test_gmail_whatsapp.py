from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.connectivity import gmail
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.runtime_setting import RuntimeSetting
from app.routers.connectivity import router


def test_gmail_readiness_requires_valid_oauth_and_encryption_configuration(monkeypatch):
    base = dict(jwt_secret="jwt", gmail_oauth_client_id="client.apps.googleusercontent.com",
        gmail_oauth_client_secret="server-secret", gmail_oauth_redirect_uri="https://orbit.test/api/connectivity/gmail/callback",
        gmail_token_encryption_key="x" * 32)
    monkeypatch.setattr(gmail, "get_settings", lambda: SimpleNamespace(**base))
    assert gmail.configuration_status() == {"ready": True, "issues": []}
    for field, bad in (("gmail_oauth_client_id", "client"), ("gmail_oauth_client_id", " client.apps.googleusercontent.com "),
                       ("gmail_oauth_client_secret", ""), ("gmail_oauth_client_secret", " secret "),
                       ("gmail_oauth_redirect_uri", "https://orbit.test/wrong"),
                       ("gmail_oauth_redirect_uri", " https://orbit.test/api/connectivity/gmail/callback "),
                       ("gmail_token_encryption_key", "short")):
        invalid = {**base, field: bad}
        monkeypatch.setattr(gmail, "get_settings", lambda invalid=invalid: SimpleNamespace(**invalid))
        status = gmail.configuration_status()
        assert status["ready"] is False and status["issues"]
        assert "server-secret" not in str(status)


def test_gmail_tools_and_access_are_blocked_until_oauth_is_ready(db, monkeypatch):
    monkeypatch.setattr(gmail, "get_settings", lambda: SimpleNamespace(jwt_secret="s", gmail_token_encryption_key="",
        gmail_oauth_client_id="", gmail_oauth_client_secret="", gmail_oauth_redirect_uri="http://localhost/api/connectivity/gmail/callback"))
    db.add(RuntimeSetting(key=gmail.ACCOUNTS_KEY, value={"owner": {"acct": {"id": "acct", "enabled": True}}}))
    db.commit()
    assert gmail.tools_for_user(db, "owner") == []
    with __import__("pytest").raises(ValueError, match="configuration serveur invalide"):
        gmail._access_token(db, "owner", "acct")


def test_gmail_tokens_are_encrypted_and_accounts_are_isolated(db, monkeypatch):
    monkeypatch.setattr(gmail, "get_settings", lambda: SimpleNamespace(jwt_secret="test-secret", gmail_token_encryption_key="separate", gmail_oauth_client_id="id.apps.googleusercontent.com", gmail_oauth_client_secret="secret", gmail_oauth_redirect_uri="http://localhost/api/connectivity/gmail/callback"))
    row = RuntimeSetting(key=gmail.ACCOUNTS_KEY, value={"user-a": {"a1": {"id": "a1", "email": "a@example.test", "token": gmail._fernet().encrypt(b"access-secret").decode(), "refresh_token": gmail._fernet().encrypt(b"refresh-secret").decode()}}})
    db.add(row); db.commit()
    assert gmail.accounts(db, "user-a")[0]["email"] == "a@example.test"
    assert gmail.accounts(db, "user-b") == []
    assert "access-secret" not in str(row.value) and "refresh-secret" not in str(row.value)


def test_gmail_oauth_state_is_one_time_and_short_lived(db, monkeypatch):
    monkeypatch.setattr(gmail, "get_settings", lambda: SimpleNamespace(jwt_secret="s", gmail_token_encryption_key="x" * 32, gmail_oauth_client_id="client.apps.googleusercontent.com", gmail_oauth_client_secret="secret", gmail_oauth_redirect_uri="https://orbit.test/api/connectivity/gmail/callback"))
    url = gmail.start(db, "user-id", "person")
    values = parse_qs(urlparse(url).query)
    assert values["scope"] == [gmail.SCOPE]
    assert values["redirect_uri"] == ["https://orbit.test/api/connectivity/gmail/callback"]
    assert values["state"][0]
    send_url = gmail.start(db, "user-id", "person", enable_send=True)
    assert gmail.SEND_SCOPE in parse_qs(urlparse(send_url).query)["scope"][0]
    with __import__("pytest").raises(ValueError):
        gmail.finish(db, "wrong-state", "code")


def test_gmail_adds_and_disconnects_multiple_accounts(db, monkeypatch):
    monkeypatch.setattr(gmail, "get_settings", lambda: SimpleNamespace(jwt_secret="s", gmail_token_encryption_key="x" * 32,
        gmail_oauth_client_id="client.apps.googleusercontent.com", gmail_oauth_client_secret="secret", gmail_oauth_redirect_uri="https://orbit.test/api/connectivity/gmail/callback"))
    class Response:
        def __init__(self, value): self.value = value
        def raise_for_status(self): pass
        def json(self): return self.value
    class Client:
        def __init__(self, **_): self.access = ""
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def post(self, *_args, **kwargs):
            self.access = kwargs["data"]["code"]
            return Response({"access_token": self.access, "refresh_token": "refresh-" + self.access, "expires_in": 3600, "scope": gmail.SCOPE})
        def get(self, *_args, **_kwargs): return Response({"emailAddress": self.access + "@example.test"})
    monkeypatch.setattr(gmail.httpx, "Client", Client)
    ids = []
    for code in ("one", "two"):
        state = parse_qs(urlparse(gmail.start(db, "owner", "owner")).query)["state"][0]
        ids.append(gmail.finish(db, state, code))
    owned = gmail.accounts(db, "owner")
    assert ids == ["owner", "owner"]
    assert {a["email"] for a in owned} == {"one@example.test", "two@example.test"}
    assert sum(bool(a["primary"]) for a in owned) == 1
    assert all("token" not in a and "refresh_token" not in a for a in owned)
    primary_id = next(a["id"] for a in owned if a["primary"])
    state = parse_qs(urlparse(gmail.start(db, "owner", "owner", enable_send=True, account_id=primary_id)).query)["state"][0]
    gmail.finish(db, state, "one")
    assert next(a for a in gmail.accounts(db, "owner") if a["id"] == primary_id)["primary"] is True
    revoked = []
    monkeypatch.setattr(gmail.httpx, "post", lambda *args, **kwargs: (revoked.append(kwargs["data"]["token"]) or Response({})))
    gmail.disconnect(db, "owner", "owner", owned[0]["id"])
    assert len(gmail.accounts(db, "owner")) == 1 and len(revoked) == 1


def test_gmail_revoked_refresh_token_sets_expired_status(db, monkeypatch):
    monkeypatch.setattr(gmail, "get_settings", lambda: SimpleNamespace(jwt_secret="s", gmail_token_encryption_key="x" * 32,
        gmail_oauth_client_id="client.apps.googleusercontent.com", gmail_oauth_client_secret="secret", gmail_oauth_redirect_uri="https://orbit.test/api/connectivity/gmail/callback"))
    account = {"id": "acct", "email": "a@example.test", "enabled": True, "primary": True,
        "token": gmail._fernet().encrypt(b"expired-access").decode(),
        "refresh_token": gmail._fernet().encrypt(b"revoked-refresh").decode(),
        "expires_at": "2000-01-01T00:00:00+00:00", "status": "connected"}
    db.add(RuntimeSetting(key=gmail.ACCOUNTS_KEY, value={"owner": {"acct": account}})); db.commit()
    class Response:
        is_error = True
    monkeypatch.setattr(gmail.httpx, "post", lambda *_, **__: Response())
    with __import__("pytest").raises(ValueError, match="révoquée"):
        gmail._access_token(db, "owner", "acct")
    assert gmail.accounts(db, "owner")[0]["status"] == "auth_expired"


def test_gmail_quota_reads_use_bounded_backoff(monkeypatch):
    responses = [SimpleNamespace(status_code=429, text="rate limit"), SimpleNamespace(status_code=200, text="ok")]
    class Client:
        calls = 0
        def get(self, *args, **kwargs):
            self.calls += 1
            return responses.pop(0)
    monkeypatch.setattr(gmail.time, "sleep", lambda _: None)
    monkeypatch.setattr(gmail.random, "random", lambda: 0)
    client = Client()
    assert gmail._gmail_get(client, "https://gmail.googleapis.com/test").status_code == 200
    assert client.calls == 2


def test_gmail_chat_tools_are_conditional_and_trace_redacts_queries(db, monkeypatch):
    from app.agent.orchestrator import _record_tool_call
    monkeypatch.setattr(gmail, "get_settings", lambda: SimpleNamespace(jwt_secret="s", gmail_token_encryption_key="x" * 32,
        gmail_oauth_client_id="client.apps.googleusercontent.com", gmail_oauth_client_secret="secret",
        gmail_oauth_redirect_uri="https://orbit.test/api/connectivity/gmail/callback"))
    db.add(RuntimeSetting(key=gmail.ACCOUNTS_KEY, value={"owner": {"one": {"id": "one", "enabled": True},
                                                                  "off": {"id": "off", "enabled": False}}}))
    db.commit()
    assert {tool["function"]["name"] for tool in gmail.tools_for_user(db, "owner")} == {"gmail_search", "gmail_read", "gmail_read_thread"}
    assert gmail.tools_for_user(db, "other") == []
    trace = []
    secret_query = "from:private@example.test confidential project"
    _record_tool_call(trace, "gmail_search", {"query": secret_query, "limit": 5}, {"messages": [], "count": 0})
    assert secret_query not in str(trace)
    assert trace[0]["arguments"]["query_length"] == len(secret_query)


def test_gmail_send_requires_unchanged_one_time_confirmation(db, monkeypatch):
    account = {"id": "acct", "email": "sender@example.test", "enabled": True, "send_enabled": True,
               "token": "unused", "refresh_token": "unused", "expires_at": "2099-01-01T00:00:00+00:00"}
    db.add(RuntimeSetting(key=gmail.ACCOUNTS_KEY, value={"owner": {"acct": account}})); db.commit()
    monkeypatch.setattr(gmail, "_access_token", lambda *_: "access")
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"id": "sent-1"}
    monkeypatch.setattr(gmail.httpx, "post", lambda *a, **k: (calls.append(k["json"]) or Response()))
    draft = {"to": "recipient@example.test", "subject": "Meeting", "body": "Tomorrow at 10."}
    pending = gmail.prepare_send(db, "owner", "acct", draft)
    row = db.get(RuntimeSetting, gmail.PENDING_SEND_KEY)
    assert "Tomorrow at 10." not in str(row.value)
    altered = {**draft, "body": "Changed."}
    with __import__("pytest").raises(ValueError, match="modifié"):
        gmail.send_confirmed(db, "owner", "acct", altered, pending["confirmation"])
    assert calls == []
    with __import__("pytest").raises(ValueError):
        gmail.send_confirmed(db, "owner", "acct", draft, pending["confirmation"])
    pending = gmail.prepare_send(db, "owner", "acct", draft)
    result = gmail.send_confirmed(db, "owner", "acct", draft, pending["confirmation"])
    assert result["status"] == "sent" and len(calls) == 1
    with __import__("pytest").raises(ValueError, match="déjà utilisée"):
        gmail.send_confirmed(db, "owner", "acct", draft, pending["confirmation"])
    assert len(calls) == 1


def test_whatsapp_click_to_chat_never_reports_sent(db, make_test_user, monkeypatch):
    from app.routers import connectivity as routes
    monkeypatch.setattr(routes, "log_event", lambda **_: None)
    user = make_test_user(username="wa_test", role="EMPLOYEE")
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    response = client.post("/api/connectivity/whatsapp/prepare", json={"phone": "+2348012345678", "message": "Réunion demain ?"})
    assert response.status_code == 200
    result = response.json()
    assert result["sent"] is False and result["status"] == "ready_to_open"
    assert result["url"].startswith("https://wa.me/2348012345678?text=")
    assert "Réunion" not in result["url"]


def test_whatsapp_contacts_are_per_user_and_validate_international_numbers(db, make_test_user):
    user = make_test_user(username="wa_owner", role="EMPLOYEE")
    other = make_test_user(username="wa_other", role="EMPLOYEE")
    app = FastAPI(); app.include_router(router)
    current = {"user": user}
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    client = TestClient(app)
    assert client.post("/api/connectivity/whatsapp/contacts", json={"name": "Contact", "phone": "+2348012345678"}).status_code == 200
    current["user"] = other
    assert client.get("/api/connectivity/whatsapp/contacts").json()["contacts"] == {}
    assert client.post("/api/connectivity/whatsapp/contacts", json={"name": "Bad", "phone": "08012345678"}).status_code == 422
