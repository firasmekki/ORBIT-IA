"""routers/chat.py hands the pending chart question to the agent only on
the very next turn - never a stale one from further back - and persists
the structured clarification on the assistant message."""

import base64

import pytest
from fastapi import HTTPException

import app.routers.chat as chat_router
from app.agent.orchestrator import AgentTurnResult
from app.models.audit import AuditLog
from app.models.chat import Conversation, Message
from app.schemas.chat import ChatRequest, ChatResumeRequest

PENDING = {"kind": "chart_clarification", "asked": "metric", "request": "Donne-moi une courbe", "answers": []}


def _conversation(db, user, assistant_contexts):
    conv = Conversation(user_id=user.id, title="t")
    db.add(conv)
    db.commit()
    for i, context in enumerate(assistant_contexts):
        db.add(Message(conversation_id=conv.id, role="user", content=f"q{i}"))
        db.commit()
        db.add(Message(conversation_id=conv.id, role="assistant", content=f"r{i}", reference_context=context))
        db.commit()
    return conv


def _capture(monkeypatch, result=None):
    seen = {}

    async def fake_turn(**kwargs):
        seen.update(kwargs)
        return result or AgentTurnResult(answer="ok")

    monkeypatch.setattr(chat_router, "run_agent_turn", fake_turn)
    return seen


async def test_pending_question_reaches_the_next_turn(db, make_test_user, monkeypatch):
    user = make_test_user(username="ds_next", role="DIRECTOR")
    conv = _conversation(db, user, [PENDING])
    seen = _capture(monkeypatch)

    await chat_router.chat(ChatRequest(message="Le budget", conversation_id=conv.id), db=db, user=user)

    assert seen["last_reference_context"] == PENDING


async def test_stale_pending_question_is_never_continued(db, make_test_user, monkeypatch):
    user = make_test_user(username="ds_stale", role="DIRECTOR")
    conv = _conversation(db, user, [PENDING, None])  # answered by something else since
    seen = _capture(monkeypatch)

    await chat_router.chat(ChatRequest(message="Le budget", conversation_id=conv.id), db=db, user=user)

    assert seen["last_reference_context"] is None


async def test_clarification_is_persisted_and_returned(db, make_test_user, monkeypatch):
    user = make_test_user(username="ds_persist", role="DIRECTOR")
    clarification = {
        "type": "clarification", "status": "need_more_information", "message": "Que souhaitez-vous représenter ?",
        "questions": ["Budget RH"], "options": [{"label": "Budget RH", "value": "Budget RH"}],
    }
    _capture(monkeypatch, AgentTurnResult(answer="Que souhaitez-vous représenter ?", clarification=clarification, reference_context=PENDING))

    response = await chat_router.chat(ChatRequest(message="Donne-moi une courbe"), db=db, user=user)

    assert response.message.clarification == clarification
    stored = db.get(Message, response.message.id)
    assert stored.clarification == clarification
    detail = chat_router.get_conversation(response.conversation_id, db=db, user=user)
    assert detail.messages[-1].clarification == clarification


# --- /api/chat/resume with several Workspace files (search_local_files) -------------------------

def _b64(text):
    return base64.b64encode(text.encode()).decode()


async def test_resume_with_several_files_runs_the_search_and_audits_each_read(db, make_test_user, monkeypatch):
    user = make_test_user(username="ds_ws", role="EMPLOYEE")
    conv = _conversation(db, user, [])
    db.add(Message(conversation_id=conv.id, role="user", content="rechercher tarek au niveau de workspace"))
    db.commit()
    seen = _capture(monkeypatch)

    await chat_router.chat_resume(
        ChatResumeRequest(
            conversation_id=conv.id,
            action_id="00000000-0000-0000-0000-000000000001",
            files=[
                {"relative_path": "docs/rapport.txt", "name": "rapport.txt", "content_base64": _b64("Tarek = 5")},
                {"relative_path": "cv.txt", "name": "cv.txt", "content_base64": _b64("Firas")},
            ],
            skipped=[{"name": "gros.pdf", "reason": "fichier trop volumineux"}],
        ),
        db=db,
        user=user,
    )

    assert seen["message"] == "rechercher tarek au niveau de workspace"
    assert seen["client_files"] == [("rapport.txt", "docs/rapport.txt", b"Tarek = 5"), ("cv.txt", "cv.txt", b"Firas")]
    assert seen["client_skipped"] == [{"name": "gros.pdf", "reason": "fichier trop volumineux"}]
    db.expire_all()
    reads = db.query(AuditLog).filter(AuditLog.user_id == user.id, AuditLog.action == "FILE_READ").all()
    assert sorted(r.resource_id for r in reads) == ["cv.txt", "docs/rapport.txt"]
    assert all(r.extra["purpose"] == "search" for r in reads)


async def test_resume_rejects_a_path_escaping_the_workspace(db, make_test_user, monkeypatch):
    user = make_test_user(username="ds_ws_bad", role="EMPLOYEE")
    conv = _conversation(db, user, [])
    _capture(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await chat_router.chat_resume(
            ChatResumeRequest(
                conversation_id=conv.id, action_id="00000000-0000-0000-0000-000000000002",
                files=[{"relative_path": "../../etc/passwd", "name": "passwd", "content_base64": _b64("x")}],
            ),
            db=db,
            user=user,
        )
    assert exc.value.status_code == 400
