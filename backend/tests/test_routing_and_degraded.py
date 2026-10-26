"""Routing of questions (greeting / general / documents / web / hybrid), the
local model being down, and credentials never leaking - the incident of
2026-10-14: "bonjour" with the model stopped ran a document search and
showed the raw excerpts of an "acces.txt" (login + password).

All secrets below are synthetic. No real model or network is reached."""

import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
import pytest

import app.agent.orchestrator as orchestrator
import app.routers.chat as chat_router
from app.agent import intent_router, llm_client
from app.agent.intent_router import DIRECT_LOCAL, GENERAL_KNOWLEDGE, HYBRID, LOCAL_DOCUMENTS, WEB_REQUIRED
from app.agent.llm_client import LLMServiceError
from app.agent.orchestrator import AgentTurnResult
from app.core.secrets_filter import MASK, contains_secret, redact_payload, redact_secrets
from app.models.chat import Message
from app.policy.rules import Grant
from app.schemas.chat import ChatRequest
from app.websearch.config import WebSearchConfig
from app.websearch.intent import decide_search_mode

SECRET_DOC = (
    "Accès supervision site B\nLogin : technicien.test\nMot de passe : Synth3tique!Pass42\n"
    "Clé API : sk-SYNTHETICKEY0000000000000000\nAdresse : 10.0.0.12"
)
SECRET_VALUES = ("technicien.test", "Synth3tique!Pass42", "sk-SYNTHETICKEY0000000000000000")


def _grant(role="DIRECTOR", tools=("search_documents", "get_document", "search_database", "search_keyword",
                                     "get_document_section", "list_documents", "generate_chart")):
    return Grant(role=role, departments=frozenset({"GENERAL", "FINANCE"}), max_level="SECRET", max_level_rank=3,
                 tools=frozenset(tools))


class Agent:
    """Runs real turns of the orchestrator; records every tool and model call."""

    def __init__(self, monkeypatch, *, llm="ok", classify=None, web_enabled=False, documents=None):
        self.tools: list[str] = []
        self.prompts: list[list[dict]] = []
        self.classifier_calls = 0
        self.llm = llm

        @asynccontextmanager
        async def session(token):
            yield object()

        async def call_tool(_session, name, arguments):
            self.tools.append(name)
            if name == "search_documents":
                return {"results": documents if documents is not None else [
                    {"document_id": "d1", "title": "acces.txt", "relative_path": "Delice/acces.txt",
                     "department": "GENERAL", "confidentiality": "INTERNAL", "excerpt": SECRET_DOC, "score": 0.9}]}
            if name == "search_database":
                return {"results": []}
            if name == "get_document":  # the full text of the file, credentials included
                return {"document_id": arguments.get("document_id", "d1"), "title": "acces.txt",
                        "relative_path": "Delice/acces.txt", "department": "GENERAL", "confidentiality": "INTERNAL",
                        "content": SECRET_DOC, "pages": [{"page": 1, "section": None, "text": SECRET_DOC}]}
            if name == "get_document_section":  # lookup by name: one match
                return {"document_id": "d1", "title": "acces.txt", "relative_path": "Delice/acces.txt", "content": SECRET_DOC}
            if name in ("list_documents", "list_folders"):
                return {"results": [{"document_id": "d1", "title": "acces.txt", "relative_path": "Delice/acces.txt",
                                     "department": "GENERAL", "confidentiality": "INTERNAL"}], "folders": []}
            return {"error": "introuvable", "code": "NOT_FOUND"}

        async def chat_completion(messages, tools=None, *, max_tokens=None):
            self.prompts.append(messages)
            if self.llm != "ok":
                raise self.llm
            return {"role": "assistant", "content": "Réponse du modèle local.", "tool_calls": None}

        async def complete_json(messages, schema, **kwargs):
            self.classifier_calls += 1
            self.classifier_messages = messages
            if classify is None:
                raise LLMServiceError("indisponible")
            return classify

        config = WebSearchConfig(
            enabled=web_enabled, provider="brave", trigger="auto", max_results=3, fetch_pages=0, timeout_seconds=5,
            language="fr", country="", cache_ttl_seconds=0, max_per_hour=10, fetch_max_bytes=1000, google_engine_id="",
        )
        monkeypatch.setattr(orchestrator, "open_mcp_session", session)
        monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kw: "token")
        monkeypatch.setattr(orchestrator, "call_tool", call_tool)
        monkeypatch.setattr(orchestrator, "chat_completion", chat_completion)
        monkeypatch.setattr(orchestrator, "complete_json", complete_json)
        monkeypatch.setattr(orchestrator, "load_web_config", lambda: config)

    def say(self, message, grant=None, **kwargs):
        return asyncio.run(orchestrator.run_agent_turn(
            user_id="u1", grant=grant or _grant(), message=message, history=kwargs.pop("history", []), **kwargs
        ))


DOWN = LLMServiceError(llm_client.LLM_UNAVAILABLE_MESSAGE, "unreachable")


def _no_secret(text: str):
    for value in SECRET_VALUES:
        assert value not in text, f"secret leaked: {value!r}"


# --- P0: greetings ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("message,expected", [
    ("bonjour", "Bonjour ! Comment puis-je vous aider ?"),
    ("Bonjour, comment vas-tu ?", "Bonjour ! Je vais bien, merci. Comment puis-je vous aider ?"),
    ("Salut !", "Bonjour ! Comment puis-je vous aider ?"),
    ("aslema", "Bonjour ! Comment puis-je vous aider ?"),
    ("Hello", "Hello! How can I help you?"),
    ("مرحبا", "مرحبًا! كيف يمكنني مساعدتك؟"),
    ("merci beaucoup", "Avec plaisir ! N'hésitez pas si vous avez une autre question."),
])
def test_greeting_uses_no_tool_no_model_no_web(monkeypatch, message, expected):
    agent = Agent(monkeypatch, web_enabled=True)
    result = agent.say(message)
    assert result.answer == expected and result.answer_mode == "direct"
    assert agent.tools == [] and agent.prompts == [] and agent.classifier_calls == 0


def test_greeting_works_with_the_model_down(monkeypatch):
    agent = Agent(monkeypatch, llm=DOWN)
    result = agent.say("bonjour")
    assert result.answer == "Bonjour ! Comment puis-je vous aider ?" and not result.degraded
    assert agent.tools == [] and agent.prompts == []


def test_greeting_with_an_image_still_analyses_the_image(monkeypatch):
    agent = Agent(monkeypatch)
    observation = {"description": "Armoire électrique.", "objects": [], "measurements": [], "text": [], "warnings": [], "confidence": 0.9}
    result = agent.say("bonjour", image_observation=observation)
    assert "Armoire électrique." in result.answer and agent.tools == []


# --- P0: the model down never leaks anything ------------------------------------------------------------------

def test_general_question_with_the_model_down_reveals_no_document(monkeypatch):
    agent = Agent(monkeypatch, llm=DOWN)
    result = agent.say("Explique la différence entre courant alternatif et continu")
    assert result.degraded and result.answer_mode == "unavailable"
    assert agent.tools == []  # an LLM error never turns into a document search
    assert "acces.txt" not in result.answer and result.sources == []
    _no_secret(result.answer)


@pytest.mark.parametrize("question", [
    "Quel est le délai de maintenance prévu dans le contrat ?",  # RAG answer
    "Résume acces.txt",  # one-document report (used to print its first 1500 characters)
    "Fais une synthèse des documents du dossier Delice",  # multi-document synthesis
])
def test_document_question_with_the_model_down_shows_no_raw_excerpt(monkeypatch, question):
    agent = Agent(monkeypatch, llm=DOWN)
    result = agent.say(question)
    assert result.degraded and result.answer_mode == "unavailable"
    assert "Aucun contenu de document n'est affiché" in result.answer
    assert result.sources == [] and "Accès supervision" not in result.answer
    _no_secret(result.answer)


@pytest.mark.parametrize("error,needle", [
    (LLMServiceError(llm_client.LLM_UNAVAILABLE_MESSAGE, "unreachable"), "n'est pas disponible"),
    (LLMServiceError(llm_client.LLM_LOADING_MESSAGE, "loading"), "Chargement du modèle"),
    (LLMServiceError(llm_client.LLM_TIMEOUT_MESSAGE, "timeout"), "délai prévu"),
    (LLMServiceError(llm_client.LLM_ERROR_MESSAGE, "server_error"), "rencontré une erreur"),
])
def test_each_model_failure_has_its_own_message_and_no_rag_fallback(monkeypatch, error, needle):
    agent = Agent(monkeypatch, llm=error)
    result = agent.say("Quel est le délai de maintenance prévu dans le contrat ?")
    assert needle in result.answer and result.answer_mode == "unavailable"
    assert agent.tools.count("search_documents") <= 1  # the normal search only, never a second "fallback" one
    _no_secret(result.answer)
    assert "Accès supervision" not in result.answer


# --- P0: credentials masked everywhere -------------------------------------------------------------------------

def test_secret_filter_masks_credentials_and_keeps_ordinary_text():
    masked = redact_secrets(SECRET_DOC)
    _no_secret(masked)
    assert "Login : " + MASK in masked and "Mot de passe : " + MASK in masked and "Adresse : 10.0.0.12" in masked
    for text in ("Le budget IT 2025 est de 310 000 €.", "mot de passe oublié ?", "Compte : 401000 Fournisseurs",
                 "Le passage du câble", "le token est expiré"):
        assert redact_secrets(text) == text
    assert redact_secrets("ftp://admin:S3cret@10.0.0.1/") == f"ftp://admin:{MASK}@10.0.0.1/"
    # formats seen in real project files (values synthetic): suffixed labels,
    # JSON keys (plain and escaped), the short "pass :", a one-character value
    assert redact_secrets("Login-Adm :    ZZTEST-Adm") == f"Login-Adm :    {MASK}"
    assert redact_secrets('{"password": "123456789++", "username": "admin"}') == f'{{"password": {MASK}, "username": {MASK}}}'
    assert MASK in redact_secrets('curl -d "{\\"password\\":\\"S3cr3t\\"}"') and "S3cr3t" not in redact_secrets('{\\"password\\":\\"S3cr3t\\"}')
    assert redact_secrets("pass : secret9.9") == f"pass : {MASK}" and redact_secrets("mot de passe : 0") == f"mot de passe : {MASK}"
    assert redact_secrets("pass du métro") == "pass du métro"
    assert contains_secret("password=hunter2!") and not contains_secret("Rapport annuel 2025")
    payload = redact_payload({"document_id": "d1", "relative_path": "x/acces.txt", "excerpt": SECRET_DOC})
    assert payload["relative_path"] == "x/acces.txt" and payload["document_id"] == "d1"
    _no_secret(payload["excerpt"])


def test_tool_results_are_masked_before_the_model_or_the_user_sees_them():
    from app.agent import mcp_client

    class Block:
        type = "text"
        text = '{"results": [{"document_id": "d1", "title": "acces.txt", "excerpt": "Mot de passe : Synth3tique!Pass42"}]}'

    class Result:
        content = [Block()]
        isError = False

    class Session:
        async def call_tool(self, name, arguments):
            return Result()

    parsed = asyncio.run(mcp_client.call_tool(Session(), "search_documents", {"query": "x"}))
    assert parsed["results"][0]["excerpt"] == f"Mot de passe : {MASK}"


def test_document_with_a_password_is_never_revealed_by_a_chat_answer(db, make_test_user, monkeypatch):
    # whatever produced it (old path, model echo...), the last guard masks it
    user = make_test_user(username="leak_guard", role="DIRECTOR")

    async def turn(**kwargs):
        return AgentTurnResult(
            answer=f"Voici les accès : {SECRET_DOC}",
            sources=[{"document_id": "00000000-0000-0000-0000-000000000001", "title": "acces.txt",
                      "department": "GENERAL", "confidentiality": "INTERNAL", "excerpt": SECRET_DOC}],
            degraded=True,
        )

    monkeypatch.setattr(chat_router, "run_agent_turn", turn)
    response = asyncio.run(chat_router.chat(ChatRequest(message="accès du site B"), db=db, user=user))
    _no_secret(response.message.content)
    _no_secret(response.message.sources[0].excerpt)
    stored = db.query(Message).filter(Message.role == "assistant").one()
    _no_secret(stored.content)
    _no_secret(str(stored.sources))


def test_an_old_message_holding_a_secret_is_masked_when_read_back(db, make_test_user):
    from app.models.chat import Conversation
    from app.schemas.chat import ConversationDetail

    user = make_test_user(username="leak_old", role="DIRECTOR")
    conv = Conversation(user_id=user.id, title="ancien")
    db.add(conv)
    db.commit()
    db.add(Message(conversation_id=conv.id, role="assistant", content=f"[Mode dégradé] {SECRET_DOC}", sources=[
        {"document_id": "00000000-0000-0000-0000-000000000001", "title": "acces.txt", "department": "GENERAL",
         "confidentiality": "INTERNAL", "excerpt": SECRET_DOC}]))
    db.commit()
    detail = ConversationDetail.model_validate(chat_router.get_conversation(conv.id, db=db, user=user), from_attributes=True)
    _no_secret(detail.messages[0].content)
    _no_secret(detail.messages[0].sources[0].excerpt)
    stored = db.query(Message).filter(Message.conversation_id == conv.id).one()
    assert "Synth3tique!Pass42" in stored.content  # the row itself is not modified


def test_an_unauthorized_user_gets_no_internal_data(db, make_test_user, make_doc):
    from app.mcp import server as mcp_server
    from app.models.workspace import Workspace, WorkspaceMember

    director = make_test_user(username="acl_dir", role="DIRECTOR")
    employee = make_test_user(username="acl_emp", role="EMPLOYEE")
    ws = Workspace(name="projets", owner_id=director.id)
    db.add(ws)
    db.commit()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=employee.id))
    db.commit()
    make_doc(title="acces.txt", department="FINANCE", confidentiality="SECRET", workspace_id=ws.id,
             relative_path="Delice/acces.txt", pages=[(1, None, 1, SECRET_DOC)])
    result = mcp_server._search_keyword_impl(str(employee.id), "supervision", False, workspace_id=str(ws.id))
    assert result.get("documents", []) == [] and "Synth3tique" not in str(result)


def test_no_secret_in_logs_or_errors_when_the_model_fails(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    agent = Agent(monkeypatch, llm=DOWN)
    result = agent.say("Quel est le délai de maintenance prévu dans le contrat ?")
    _no_secret(result.answer)
    _no_secret(caplog.text)


def test_model_error_body_is_never_logged(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="orbitia.llm")

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return httpx.Response(500, json={"error": f"bad prompt: {SECRET_DOC}"})

    monkeypatch.setattr(llm_client.httpx, "AsyncClient", Client)
    with pytest.raises(LLMServiceError) as exc:
        asyncio.run(llm_client.chat_completion([{"role": "user", "content": "x"}]))
    assert exc.value.kind == "server_error"
    _no_secret(caplog.text)
    _no_secret(str(exc.value))


# --- P0: model status ------------------------------------------------------------------------------------------

class _Server:
    """One fake model server: what /health, /v1/models, /chat answer; counts calls."""

    def __init__(self, health=200, chat=None, chat_exc=None, models=("Qwen3-30B-A3B-Instruct-2507",)):
        self.health, self.chat, self.chat_exc, self.models = health, chat, chat_exc, models
        self.posts = 0

    def install(self, monkeypatch):
        server = self

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                if isinstance(server.health, Exception):
                    raise server.health
                if url.endswith("/v1/models"):
                    return httpx.Response(200, json={"data": [{"id": m} for m in server.models]})
                return httpx.Response(server.health, json={})

            async def post(self, url, json=None):
                server.posts += 1
                if server.chat_exc is not None:
                    raise server.chat_exc
                return server.chat or httpx.Response(
                    200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        monkeypatch.setattr(llm_client.httpx, "AsyncClient", Client)
        monkeypatch.setattr(llm_client.settings, "llm_model_name", "Qwen3-30B-A3B-Instruct-2507")
        return self


def _status():
    return asyncio.run(llm_client.get_llm_status())


def test_status_loading_and_a_single_request_no_concurrent_reload(monkeypatch):
    server = _Server(health=503, chat=httpx.Response(503, json={"error": "Loading model"})).install(monkeypatch)
    assert _status()["status"] == "loading"
    with pytest.raises(LLMServiceError) as exc:
        asyncio.run(llm_client.chat_completion([{"role": "user", "content": "q"}]))
    assert exc.value.kind == "loading" and server.posts == 1  # no retry storm, nothing re-launched


def test_status_stopped_server(monkeypatch):
    _Server(health=httpx.ConnectError("refused"), chat_exc=httpx.ConnectError("refused")).install(monkeypatch)
    status = _status()
    assert status["status"] == "unavailable" and "arrêté ou injoignable" in status["detail"]
    with pytest.raises(LLMServiceError) as exc:
        asyncio.run(llm_client.chat_completion([{"role": "user", "content": "q"}]))
    assert exc.value.kind == "unreachable" and "refused" not in str(exc.value)


def test_inference_timeout_then_the_model_comes_back(monkeypatch):
    server = _Server(chat_exc=httpx.ReadTimeout("slow")).install(monkeypatch)
    with pytest.raises(LLMServiceError) as exc:
        asyncio.run(llm_client.chat_completion([{"role": "user", "content": "q"}]))
    assert exc.value.kind == "timeout"
    assert _status()["status"] == "error"  # up, but the last generation failed
    server.chat_exc = None  # model available again
    assert asyncio.run(llm_client.chat_completion([{"role": "user", "content": "q"}]))["content"] == "ok"
    assert _status()["status"] == "ready"


def test_status_misconfigured_model(monkeypatch):
    _Server(models=("another-model",)).install(monkeypatch)
    assert _status()["status"] == "misconfigured"


# --- routing (FR / EN / AR) ------------------------------------------------------------------------------------

def _route(message, *, enabled=True, classify=None):
    async def complete_json(messages, schema, **kw):
        if classify is None:
            raise LLMServiceError("indisponible")
        return classify

    return asyncio.run(intent_router.route_question(
        message, web_decision=decide_search_mode(message, enabled=enabled), complete_json=complete_json))


@pytest.mark.parametrize("message,intent", [
    ("Explique la différence entre courant alternatif et continu", GENERAL_KNOWLEDGE),
    ("What is a three-phase transformer?", GENERAL_KNOWLEDGE),
    ("اشرح الفرق بين التيار المتناوب والمستمر", GENERAL_KNOWLEDGE),
    ("Quelle est la dernière réglementation européenne sur le reporting carbone ?", WEB_REQUIRED),
    ("Cherche sur Internet les dernières nouveautés de Qwen", WEB_REQUIRED),
    ("Résume le contrat présent dans mon dossier", LOCAL_DOCUMENTS),
    ("Summarize the contract in my folder", LOCAL_DOCUMENTS),
    ("Compare les exigences du contrat local à la réglementation actuelle", HYBRID),
    ("Quel est le budget IT 2025 ?", LOCAL_DOCUMENTS),
    ("Résume Architecture.pdf", LOCAL_DOCUMENTS),
    ("Reformule ce texte : nous avons bien reçu votre offre", DIRECT_LOCAL),
    ("Combien font 12 x 7 ?", DIRECT_LOCAL),
    ("Liste les derniers documents ajoutés", LOCAL_DOCUMENTS),
])
def test_routing(message, intent):
    assert _route(message).intent == intent


def test_undecided_question_is_classified_by_the_local_model_with_a_validated_schema():
    route = _route("Quelle est la capitale du Japon ?", classify={
        "intent": "GENERAL_KNOWLEDGE", "needs_local_documents": False, "needs_web_search": False, "reason": "culture générale"})
    assert route.intent == GENERAL_KNOWLEDGE and route.decided_by == "model"
    invalid = _route("Quelle est la capitale du Japon ?", classify={"intent": "DELETE_ALL"})
    assert invalid.intent == LOCAL_DOCUMENTS and invalid.decided_by == "fallback"
    down = _route("Quelle est la capitale du Japon ?")
    assert down.intent == LOCAL_DOCUMENTS and down.decided_by == "fallback"


@pytest.mark.parametrize("message", [
    "Quelle est la dernière version stable de Python ?",
    "Quelles sont les dernières nouveautés de Qwen ?",
    "Quelle est la dernière mise à jour de la norme ISO 9001 ?",
    "Qui dirige actuellement le gouvernement japonais ?",
])
def test_recent_public_questions_across_domains_route_to_web(message):
    assert _route(message).intent == WEB_REQUIRED


def test_classifier_can_route_a_recent_question_without_a_recency_keyword():
    question = "La bibliothèque requests est-elle encore maintenue ?"
    answer = {
        "intent": WEB_REQUIRED,
        "needs_local_documents": False,
        "needs_web_search": True,
        "reason": "le statut de maintenance peut avoir changé",
        "search_query": "requests Python library maintenance status",
    }
    automatic = asyncio.run(intent_router.route_question(
        question,
        web_decision=decide_search_mode(question, enabled=True, trigger="auto"),
        complete_json=lambda *args, **kwargs: asyncio.sleep(0, result=answer),
        web_auto=True,
    ))
    assert automatic.intent == WEB_REQUIRED and automatic.needs_web_search
    assert automatic.decided_by == "model"

    manual = asyncio.run(intent_router.route_question(
        question,
        web_decision=decide_search_mode(question, enabled=True, trigger="manual"),
        complete_json=lambda *args, **kwargs: asyncio.sleep(0, result=answer),
    ))
    assert not manual.needs_web_search and manual.web_skipped


def test_classifier_receives_the_question_only(monkeypatch):
    agent = Agent(monkeypatch, classify={"intent": "GENERAL_KNOWLEDGE", "needs_local_documents": False,
                                         "needs_web_search": False, "reason": "x"})
    agent.say("Quelle est la capitale du Japon ?", history=[{"role": "assistant", "content": SECRET_DOC}])
    sent = str(agent.classifier_messages)
    assert "capitale du Japon" in sent
    _no_secret(sent)


def test_general_question_is_answered_by_the_model_without_documents(monkeypatch):
    agent = Agent(monkeypatch)
    result = agent.say("Explique la différence entre courant alternatif et continu")
    assert result.answer == "Réponse du modèle local." and result.answer_mode == "general"
    assert agent.tools == [] and len(agent.prompts) == 1
    assert "Tu n'as consulté ni les documents de l'entreprise ni Internet" in agent.prompts[0][0]["content"]
    assert "introuvable" not in result.answer and "Je ne dispose pas" not in result.answer


def test_general_question_with_the_web_off_is_answered_locally_and_said_unverified(monkeypatch):
    agent = Agent(monkeypatch, web_enabled=False)
    result = agent.say("Quelle est la dernière réglementation européenne sur le reporting carbone ?")
    assert result.answer.startswith("Réponse du modèle local.")
    assert "désactivée par l'administrateur" in result.answer and "n'est pas vérifiée sur le Web" in result.answer
    assert agent.tools == [] and result.answer_mode == "general"


def test_document_question_still_uses_the_documents_with_sources(monkeypatch):
    clean = [{"document_id": "d2", "title": "contrat.pdf", "relative_path": "BIC/contrat.pdf", "department": "GENERAL",
              "confidentiality": "INTERNAL", "excerpt": "Le contrat prévoit une maintenance trimestrielle.", "score": 0.9,
              "page": 2, "section": None}]
    agent = Agent(monkeypatch, documents=clean)
    result = agent.say("Quel est le délai de maintenance prévu dans le contrat ?",
                       grant=_grant(tools=("search_documents", "get_document")))
    assert agent.tools[0] == "search_documents" and len(agent.prompts) == 1
    assert "maintenance trimestrielle" in str(agent.prompts[0])  # the authorized excerpt reached the model
    assert not result.degraded


def test_answer_mode_is_stored_for_the_indicator(db, make_test_user, monkeypatch):
    user = make_test_user(username="mode_ind", role="DIRECTOR")

    async def turn(**kwargs):
        return AgentTurnResult(answer="Bonjour !", answer_mode="direct")

    monkeypatch.setattr(chat_router, "run_agent_turn", turn)
    response = asyncio.run(chat_router.chat(ChatRequest(message="bonjour"), db=db, user=user))
    assert response.message.answer_mode == "direct"
    assert chat_router.answer_mode(AgentTurnResult(answer="x", tool_trace=[{"tool": "web_search"}, {"tool": "search_documents"}])) == "hybrid"
    assert chat_router.answer_mode(AgentTurnResult(answer="x", degraded=True)) == "unavailable"
