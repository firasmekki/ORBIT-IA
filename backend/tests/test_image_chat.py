"""An attached image is analysed by the vision model (Qwen3-VL) and answered
as such - its observation is never parsed as a request.

Regression (seen live 2026-10-12): "Analyse l'image jointe" + the model's
JSON `"confidence": 0.95` glued to the message -> detect_document_request
read "0.95" as a file name -> "Le document « 0.95 » est introuvable".
The vision model is faked here (its real call is checked separately)."""

import asyncio
import base64
from contextlib import asynccontextmanager
from io import BytesIO

import pytest
from fastapi import HTTPException
from PIL import Image

import app.agent.orchestrator as orchestrator
import app.routers.chat as chat_router
from app.agent import vision_client
from app.agent.image_turn import needs_more_than_the_image
from app.agent.llm_client import LLMServiceError
from app.agent.vision_client import InvalidImageError, VisionUnavailableError
from app.core.config import get_settings
from app.policy.rules import Grant
from app.schemas.chat import ChatRequest

FACTORY = {
    "description": "Vue aérienne d'une zone industrielle avec des silos et des cheminées.",
    "objects": ["bâtiments industriels", "silos"], "measurements": [], "text": [], "warnings": [], "confidence": 0.95,
}


def _image(fmt: str) -> str:
    out = BytesIO()
    Image.new("RGB", (32, 24), "white").save(out, format=fmt)
    return base64.b64encode(out.getvalue()).decode()


class Vision:
    def __init__(self, monkeypatch, observation=None, *, error=None, status="ready"):
        self.calls = []
        self.status = status

        async def describe(**kwargs):
            self.calls.append(kwargs)
            if error is not None:
                raise error
            return (observation or FACTORY), {"completion_tokens": 10}

        async def ready(*a, **k):
            return self.status

        monkeypatch.setattr(chat_router, "describe_image", describe)
        monkeypatch.setattr(chat_router, "wait_until_ready", ready)


@pytest.fixture
def no_tools(monkeypatch):
    """Any document / database tool call or text-model call is recorded."""
    calls = []

    @asynccontextmanager
    async def session(token):
        yield object()

    async def call_tool(_session, name, arguments):
        calls.append((name, arguments))
        if name in ("search_documents", "search_database"):
            return {"results": []}
        return {"error": "document introuvable", "code": "NOT_FOUND"}

    async def chat_completion(messages, tools=None, *, max_tokens=None):
        calls.append(("chat_completion", messages))
        return {"role": "assistant", "content": "Je ne trouve pas ce matériel dans vos documents."}

    monkeypatch.setattr(orchestrator, "open_mcp_session", session)
    monkeypatch.setattr(orchestrator, "call_tool", call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", chat_completion)
    return calls


def _send(db, user, message, image_b64=None, mime="image/jpeg"):
    payload = {"message": message}
    if image_b64 is not None:
        payload["image"] = {"mime_type": mime, "data_base64": image_b64}
    return asyncio.run(chat_router.chat(ChatRequest(**payload), db=db, user=user))


# 1. JPEG + "Analyse l'image jointe" -> the vision model's answer, no document search
def test_jpeg_analysis_is_answered_by_the_vision_model(db, make_test_user, monkeypatch, no_tools):
    user = make_test_user(username="img_jpeg", role="DIRECTOR")
    vision = Vision(monkeypatch)
    response = _send(db, user, "Analyse l’image jointe.", _image("JPEG"))
    answer = response.message.content
    assert answer.startswith("**Analyse de l’image** — modèle vision Qwen3-VL-8B")
    assert "zone industrielle" in answer and "silos" in answer
    assert "0.95" not in answer and "introuvable" not in answer
    assert no_tools == []  # no document tool, no text model
    assert vision.calls[0]["prompt"] == "Analyse l’image jointe." and vision.calls[0]["mime_type"] == "image/jpeg"
    assert [t.tool for t in response.message.tool_trace] == ["analyze_image"]
    assert response.message.tool_trace[0].arguments["model"] == get_settings().vision_model_name


# 2. PNG + a question about its content: the question is what the vision model is asked
def test_png_question_is_sent_to_the_vision_model(db, make_test_user, monkeypatch, no_tools):
    user = make_test_user(username="img_png", role="DIRECTOR")
    vision = Vision(monkeypatch, {"description": "Plaque signalétique d'un transformateur.", "text": ["400 kVA", "TUNISIE TRANSFO"]})
    answer = _send(db, user, "Quelle est la puissance indiquée sur la plaque ?", _image("PNG"), mime="image/png").message.content
    assert vision.calls[0]["prompt"] == "Quelle est la puissance indiquée sur la plaque ?"
    assert vision.calls[0]["mime_type"] == "image/png"
    assert "« 400 kVA »" in answer and no_tools == []


# 3. the image shows "0.95": still an image analysis, never a document named "0.95"
def test_text_0_95_on_the_image_never_becomes_a_document_search(db, make_test_user, monkeypatch, no_tools):
    user = make_test_user(username="img_095", role="DIRECTOR")
    Vision(monkeypatch, {"description": "Afficheur d'un analyseur de réseau.", "text": ["cos φ 0.95"], "confidence": 0.95})
    answer = _send(db, user, "Analyse l’image jointe.", _image("JPEG")).message.content
    assert "« cos φ 0.95 »" in answer
    assert not any(name in ("get_document_section", "get_document", "list_documents") for name, _ in no_tools)


# 4. a REAL request for a document named "0.95..." still is one (no image)
def test_explicit_request_for_a_document_named_0_95_still_searches_it(no_tools):
    grant = Grant(role="DIRECTOR", departments=frozenset({"GENERAL"}), max_level="SECRET", max_level_rank=3,
                  tools=frozenset({"get_document_section", "get_document", "search_documents"}))
    asyncio.run(orchestrator.run_agent_turn(
        user_id="u1", grant=grant, message="Analyse le fichier 0.95.pdf", history=[], workspace_name="projets",
    ))
    looked_up = [args for name, args in no_tools if name == "get_document_section"]
    assert looked_up and looked_up[0]["document_name"] in ("0.95", "0.95.pdf")  # looked up by name, extension optional


# 5. missing / unreadable attachment: an attachment error, nothing pretended
def test_unreadable_attachments_are_attachment_errors(db, make_test_user, monkeypatch, no_tools):
    user = make_test_user(username="img_bad", role="DIRECTOR")
    monkeypatch.setattr(chat_router, "wait_until_ready", lambda *a, **k: asyncio.sleep(0, result="ready"))
    monkeypatch.setattr(get_settings(), "vision_enabled", True)
    with pytest.raises(HTTPException) as error:
        _send(db, user, "Analyse l’image jointe.", "ceci n'est pas du base64 !!")
    assert error.value.status_code == 422 and "Pièce jointe illisible" in error.value.detail
    with pytest.raises(HTTPException) as error:  # real decoder: valid base64, not an image
        _send(db, user, "Analyse l’image jointe.", base64.b64encode(b"bonjour").decode())
    assert error.value.status_code == 422 and "n’est pas une image lisible" in error.value.detail
    assert no_tools == []


# 6. a sleeping / loading model: waited for, then the image is sent
def test_loading_model_is_waited_for(monkeypatch):
    statuses = iter(["loading", "loading", "ready"])

    async def status():
        return {"status": next(statuses)}

    async def no_wait(_):
        return None

    monkeypatch.setattr(vision_client, "get_vision_status", status)
    monkeypatch.setattr("asyncio.sleep", no_wait)
    assert asyncio.run(vision_client.wait_until_ready(timeout_seconds=60)) == "ready"


def test_model_still_loading_or_stopped_is_said_and_nothing_is_sent(db, make_test_user, monkeypatch, no_tools):
    user = make_test_user(username="img_loading", role="DIRECTOR")
    vision = Vision(monkeypatch, status="loading")
    with pytest.raises(HTTPException) as error:
        _send(db, user, "Analyse l’image jointe.", _image("JPEG"))
    assert error.value.status_code == 503 and "en cours de chargement" in error.value.detail
    vision.status = "unavailable"
    with pytest.raises(HTTPException) as error:
        _send(db, user, "Analyse l’image jointe.", _image("JPEG"))
    assert "n’a pas été analysée" in error.value.detail
    assert vision.calls == []


# 7. real errors of the vision server, told apart
@pytest.mark.parametrize("error, status", [
    (VisionUnavailableError("serveur arrêté"), 503),
    (LLMServiceError("Le modèle vision a renvoyé une erreur (HTTP 500)."), 502),
    (InvalidImageError("format réel GIF non pris en charge (JPEG, PNG ou WebP)."), 422),
])
def test_vision_errors_are_told_apart(db, make_test_user, monkeypatch, no_tools, error, status):
    user = make_test_user(username=f"img_err_{status}", role="DIRECTOR")
    Vision(monkeypatch, error=error)
    with pytest.raises(HTTPException) as raised:
        _send(db, user, "Analyse l’image jointe.", _image("JPEG"))
    assert raised.value.status_code == status
    assert no_tools == []  # never turned into a document search


# 8. a text conversation without image is unchanged
def test_text_message_without_image_never_calls_the_vision_model(db, make_test_user, monkeypatch):
    user = make_test_user(username="img_none", role="DIRECTOR")
    vision = Vision(monkeypatch)
    seen = {}

    async def fake_turn(**kwargs):
        seen.update(kwargs)
        return orchestrator.AgentTurnResult(answer="ok")

    monkeypatch.setattr(chat_router, "run_agent_turn", fake_turn)
    _send(db, user, "Résume cette consultation")
    assert vision.calls == [] and seen["image_observation"] is None and seen["message"] == "Résume cette consultation"


# vision + documents when the question really needs them: routed on the user's words only
def test_image_with_a_documents_question_uses_the_observation_only_as_context(db, make_test_user, monkeypatch, no_tools):
    user = make_test_user(username="img_docs", role="DIRECTOR")
    monkeypatch.setattr(orchestrator, "load_web_config", lambda: type("C", (), {"enabled": False, "trigger": "auto"})())
    Vision(monkeypatch)
    _send(db, user, "Ce matériel figure-t-il dans mes documents ?", _image("JPEG"))
    searched = [args["query"] for name, args in no_tools if name == "search_documents"]
    assert searched == ["Ce matériel figure-t-il dans mes documents ?"]  # the user's words, not the JSON
    prompt = next(messages for name, messages in no_tools if name == "chat_completion")
    user_turn = [m for m in prompt if m["role"] == "user"][-1]["content"]
    assert user_turn.startswith("Ce matériel figure-t-il dans mes documents ?")
    assert "[CONTEXTE VISUEL NON VÉRIFIÉ" in user_turn and "zone industrielle" in user_turn
    assert not any(name == "get_document_section" for name, _ in no_tools)


def test_what_an_image_question_needs():
    assert not needs_more_than_the_image("Analyse l’image jointe.")
    assert not needs_more_than_the_image("Quelle est la puissance indiquée sur la plaque ?")
    assert needs_more_than_the_image("Ce matériel figure-t-il dans mes documents ?")
    assert needs_more_than_the_image("Compare cette photo avec le CCTP")
