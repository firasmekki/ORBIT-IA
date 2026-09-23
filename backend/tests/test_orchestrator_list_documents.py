from contextlib import asynccontextmanager

import app.agent.orchestrator as orchestrator
from app.agent.intent import ListDocumentsIntent
from app.policy.rules import Grant


def _grant(tools=("list_documents", "search_documents", "get_document", "get_company_information")):
    return Grant(role="EMPLOYEE", departments=frozenset({"GENERAL"}), max_level="INTERNAL", max_level_rank=1, tools=frozenset(tools))


def test_format_empty_result():
    assert orchestrator._format_list_documents_result({"results": []}) == (
        "Aucun document trouvé pour ces critères, dans les documents auxquels vous avez accès."
    )


def test_format_with_results():
    result = {
        "results": [
            {
                "document_id": "1",
                "title": "CV",
                "department": "GENERAL",
                "confidentiality": "INTERNAL",
                "type": "docx",
                "author": "Sophie Martin",
                "created_at": "2026-01-15T10:00:00+00:00",
            }
        ]
    }
    text = orchestrator._format_list_documents_result(result)
    assert "CV" in text
    assert "docx" in text
    assert "Sophie Martin" in text
    assert "2026-01-15" in text


async def test_run_list_documents_never_calls_llm(monkeypatch):
    async def fake_call_tool(session, name, arguments):
        return {"results": []}

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("chat_completion must never be called on the deterministic list_documents path")

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", fail_if_called)

    result = await orchestrator._run_list_documents(None, ListDocumentsIntent(), [])
    assert "Aucun document" in result.answer


async def test_run_agent_turn_routes_list_intent_deterministically(monkeypatch):
    @asynccontextmanager
    async def fake_session(token):
        yield object()

    async def fake_call_tool(session, name, arguments):
        assert name == "list_documents"
        return {"results": []}

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("the LLM must never be called for a deterministic list_documents question")

    monkeypatch.setattr(orchestrator, "open_mcp_session", fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", fail_if_called)

    result = await orchestrator.run_agent_turn(
        user_id="00000000-0000-0000-0000-000000000000",
        grant=_grant(),
        message="liste les documents",
        history=[],
    )
    assert "Aucun document" in result.answer
