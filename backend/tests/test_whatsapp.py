"""WhatsApp click-to-chat links (routers/connectivity.py). The Gmail tests
that lived here moved to tests/test_email_*.py with the read-only personal
mailbox rework."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.core.deps import get_current_user
from app.routers.connectivity import router


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
