"""Router-level tests for POST /api/chat/resume (app/routers/chat.py).
Uses a minimal isolated FastAPI app with get_db/get_current_user
dependency-overridden (same pattern as test_delete_history.py's
_audit_client) - never app.main.app, whose lifespan would try to
migrate/connect to the real privileged database and real MinIO.
run_agent_turn itself is monkeypatched so these tests never need a real
LLM - they verify the router's own responsibilities: path-safety
rejection, ownership/ 409 handling, base64 decoding, persisting the
final message, and auditing the file read.
"""

import base64
import uuid

import app.routers.chat as chat_router
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.audit import AuditLog
from app.models.chat import Message
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(db, user):
    test_app = FastAPI()
    test_app.include_router(chat_router.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(test_app)


def _resume_payload(conversation_id, **overrides):
    payload = {
        "conversation_id": str(conversation_id),
        "action_id": str(uuid.uuid4()),
        "relative_path": "rapport.txt",
        "name": "rapport.txt",
        "content_base64": base64.b64encode(b"contenu du fichier").decode(),
    }
    payload.update(overrides)
    return payload


def test_resume_rejects_path_traversal(db, make_test_user, make_conversation):
    agent = make_test_user(username="resume_u1", role="EMPLOYEE")
    conv = make_conversation(user_id=agent.id, message_count=1)
    client = _client(db, agent)

    response = client.post(
        "/api/chat/resume", json=_resume_payload(conv.id, relative_path="../../etc/passwd")
    )

    assert response.status_code == 400


def test_resume_conversation_not_owned_is_404(db, make_test_user, make_conversation):
    owner = make_test_user(username="resume_owner", role="EMPLOYEE")
    other = make_test_user(username="resume_other", role="EMPLOYEE")
    conv = make_conversation(user_id=owner.id, message_count=1)
    client = _client(db, other)

    response = client.post("/api/chat/resume", json=_resume_payload(conv.id))

    assert response.status_code == 404


def test_resume_unknown_conversation_is_404(db, make_test_user):
    agent = make_test_user(username="resume_u2", role="EMPLOYEE")
    client = _client(db, agent)

    response = client.post("/api/chat/resume", json=_resume_payload(uuid.uuid4()))

    assert response.status_code == 404


def test_resume_with_no_prior_user_message_is_409(db, make_test_user, make_conversation):
    agent = make_test_user(username="resume_u3", role="EMPLOYEE")
    conv = make_conversation(user_id=agent.id, message_count=0)
    client = _client(db, agent)

    response = client.post("/api/chat/resume", json=_resume_payload(conv.id))

    assert response.status_code == 409


def test_resume_invalid_base64_is_422(db, make_test_user, make_conversation):
    agent = make_test_user(username="resume_u4", role="EMPLOYEE")
    conv = make_conversation(user_id=agent.id, message_count=1)
    client = _client(db, agent)

    response = client.post(
        "/api/chat/resume", json=_resume_payload(conv.id, content_base64="not-valid-base64!!!")
    )

    assert response.status_code == 422


def test_resume_success_persists_message_and_audits(db, make_test_user, make_conversation, monkeypatch):
    agent = make_test_user(username="resume_u5", role="EMPLOYEE")
    conv = make_conversation(user_id=agent.id, title="Lis rapport.txt", message_count=1)

    async def fake_run_agent_turn(**kwargs):
        from app.agent.orchestrator import AgentTurnResult

        assert kwargs["client_file_content"] == b"contenu du fichier"
        assert kwargs["client_file_name"] == "rapport.txt"
        return AgentTurnResult(answer="Ce fichier contient un rapport.")

    monkeypatch.setattr(chat_router, "run_agent_turn", fake_run_agent_turn)
    client = _client(db, agent)

    response = client.post("/api/chat/resume", json=_resume_payload(conv.id))

    assert response.status_code == 200
    body = response.json()
    assert body["message"]["content"] == "Ce fichier contient un rapport."
    assert body["pending_client_action"] is None

    db.expire_all()
    persisted = db.query(Message).filter(Message.conversation_id == conv.id, Message.role == "assistant").all()
    assert len(persisted) == 1
    assert persisted[0].content == "Ce fichier contient un rapport."

    audit_entries = db.query(AuditLog).filter(AuditLog.action == "FILE_READ").all()
    assert len(audit_entries) == 1
    assert audit_entries[0].resource_id == "rapport.txt"
    assert audit_entries[0].user_id == agent.id
    assert audit_entries[0].extra["name"] == "rapport.txt"
