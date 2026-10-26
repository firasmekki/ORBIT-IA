"""HTTP layer of personal mailboxes (routers/email_accounts.py) and the chat
persistence of mailbox answers: browser-bound OAuth cookie, readable
callback outcomes, 404 across users, no send route, no secret in responses,
email results stored with an expiry."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.deps import get_current_user
from app.routers import connectivity, email_accounts
from email_support import SECRET_VALUES, FakeMail, connect, install, use_settings


@pytest.fixture
def fake(monkeypatch):
    use_settings(monkeypatch)
    mail = FakeMail()
    install(monkeypatch, mail)
    monkeypatch.setattr(email_accounts, "log_event", lambda **_: None)
    return mail


def client_for(db, current):
    app = FastAPI()
    app.include_router(email_accounts.router)
    app.include_router(connectivity.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    return TestClient(app, follow_redirects=False)


def test_providers_are_listed_and_yahoo_stays_disabled(db, make_test_user, fake):
    user = make_test_user(username="routes_providers", role="EMPLOYEE")
    body = client_for(db, {"user": user}).get("/api/connectivity/email-providers").json()
    providers = {p["provider"]: p for p in body["providers"]}
    assert set(providers) == {"gmail", "outlook", "yahoo"}
    assert providers["gmail"]["configured"] and providers["outlook"]["configured"]
    assert providers["yahoo"]["enabled"] is False and providers["yahoo"]["status"] == "not_configured"


def test_oauth_round_trip_sets_and_clears_the_browser_binding_cookie(db, make_test_user, fake):
    user = make_test_user(username="routes_oauth", role="EMPLOYEE")
    client = client_for(db, {"user": user})
    start = client.post("/api/connectivity/email-accounts/gmail/oauth/start")
    assert start.status_code == 200
    cookie = start.headers["set-cookie"]
    assert "orbit_email_oauth=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    state = parse_qs(urlparse(start.json()["url"]).query)["state"][0]
    done = client.get(f"/api/connectivity/gmail/callback?state={state}&code=auth-code-xyz")
    assert done.status_code == 303
    assert done.headers["location"] == "http://localhost:8081/connectivity?email_oauth=connected&provider=gmail"
    assert 'orbit_email_oauth=""' in done.headers["set-cookie"] or "Max-Age=0" in done.headers["set-cookie"]
    accounts = client.get("/api/connectivity/email-accounts").json()["accounts"]
    assert [a["email_address"] for a in accounts] == ["jean@gmail.example"]
    assert not any(v in str(accounts) for v in SECRET_VALUES)


@pytest.mark.parametrize("query,outcome", [
    ("state=forged&code=x", "oauth_state_invalid"),
    ("code=x", "oauth_state_invalid"),
    ("state=" + "a" * 5000, "oauth_state_invalid"),
    ("error=access_denied&state=forged", "oauth_state_invalid"),
])
def test_bad_callbacks_redirect_with_a_code_never_a_500(db, fake, query, outcome):
    client = client_for(db, {"user": None})
    response = client.get(f"/api/connectivity/gmail/callback?{query}")
    assert response.status_code == 303 and response.headers["location"].endswith(f"email_oauth={outcome}&provider=gmail")


def test_callback_from_another_browser_is_refused(db, make_test_user, fake):
    """The account-confusion attack: A starts, B finishes in B's browser."""
    attacker = make_test_user(username="routes_attacker", role="EMPLOYEE")
    client = client_for(db, {"user": attacker})
    start = client.post("/api/connectivity/email-accounts/gmail/oauth/start")
    state = parse_qs(urlparse(start.json()["url"]).query)["state"][0]
    victim_browser = client_for(db, {"user": None})  # no binding cookie
    response = victim_browser.get(f"/api/connectivity/gmail/callback?state={state}&code=auth-code-xyz")
    assert response.headers["location"].endswith("email_oauth=oauth_browser_mismatch&provider=gmail")
    assert client.get("/api/connectivity/email-accounts").json()["accounts"] == []


def test_another_users_connection_is_a_404_everywhere(db, make_test_user, fake):
    owner = make_test_user(username="routes_owner", role="EMPLOYEE")
    other = make_test_user(username="routes_other", role="EMPLOYEE")
    conn = connect(db, owner, "gmail", fake)
    current = {"user": owner}
    client = client_for(db, current)
    ref = client.post(f"/api/connectivity/email-accounts/{conn.id}/messages/search", json={"limit": 1}).json()["items"][0]["ref"]
    current["user"] = other
    base = f"/api/connectivity/email-accounts/{conn.id}"
    for method, path, body in (("patch", base, {"is_enabled": False}), ("delete", base, None),
                               ("post", f"{base}/test", {}), ("post", f"{base}/refresh-status", {}),
                               ("post", f"{base}/messages/search", {"limit": 1}),
                               ("post", f"{base}/messages/read", {"ref": ref})):
        response = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
        assert response.status_code == 404, (method, path, response.status_code)
    assert client.post("/api/connectivity/email-accounts/not-a-uuid/test").status_code == 404


def test_old_send_routes_no_longer_exist(db, make_test_user, fake):
    user = make_test_user(username="routes_nosend", role="EMPLOYEE")
    client = client_for(db, {"user": user})
    for path in ("/api/connectivity/gmail/send/prepare", "/api/connectivity/gmail/send",
                 "/api/connectivity/gmail/accounts"):
        assert client.post(path, json={}).status_code in (404, 405)
    paths = {getattr(r, "path", "") for r in email_accounts.router.routes} | {getattr(r, "path", "") for r in connectivity.router.routes}
    assert not [p for p in paths if "gmail/send" in p or "outlook/send" in p]


def test_provider_errors_are_readable_http_errors(db, make_test_user, fake):
    user = make_test_user(username="routes_errors", role="EMPLOYEE")
    conn = connect(db, user, "gmail", fake)
    fake.fail_with, fake.fail_times = 503, 100
    response = client_for(db, {"user": user}).post(f"/api/connectivity/email-accounts/{conn.id}/messages/search", json={})
    assert response.status_code == 424 and "Fournisseur indisponible" in response.json()["detail"]


def test_chat_persists_email_results_with_an_expiry(db, make_test_user, fake, monkeypatch):
    import app.routers.chat as chat_router
    from app.agent.orchestrator import AgentTurnResult
    from app.models.chat import Conversation, Message

    user = make_test_user(username="routes_chat", role="EMPLOYEE")
    conv = Conversation(user_id=user.id, title="t")
    db.add(conv)
    db.commit()
    monkeypatch.setattr(chat_router, "log_event", lambda **_: None)
    result = AgentTurnResult(answer="1 email", answer_mode="email",
                             email_results={"type": "email_results", "items": [], "notices": []})
    out = chat_router._persist_assistant_turn(db, conv=conv, user=user, original_message="mes emails", result=result)
    assert out.message.email_results == {"type": "email_results", "items": [], "notices": []}
    stored = db.get(Message, out.message.id)
    assert stored.answer_mode == "email" and stored.email_results_expires_at > datetime.now(UTC)
