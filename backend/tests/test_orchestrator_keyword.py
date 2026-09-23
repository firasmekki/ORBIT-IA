"""Tests for the orchestrator's deterministic keyword-search path: the
silent-leak message contract, the DENY-trace shape that feeds
routers/chat.py's alert trigger, and independence from the LLM (degraded
mode). No live MCP/LLM server needed - app.agent.orchestrator.call_tool /
chat_completion / open_mcp_session are monkeypatched.
"""

from contextlib import asynccontextmanager

import app.agent.orchestrator as orchestrator
from app.policy.rules import Grant


def _grant(tools=("search_keyword", "search_documents", "get_document", "get_company_information")):
    return Grant(
        role="EMPLOYEE",
        departments=frozenset({"GENERAL"}),
        max_level="INTERNAL",
        max_level_rank=1,
        tools=frozenset(tools),
    )


def test_format_keyword_result_identical_message_with_or_without_restricted_match():
    no_leak = {"keyword": "test", "total_count": 0, "documents": []}
    with_leak = {
        "keyword": "test",
        "total_count": 0,
        "documents": [],
        "restricted_match": {"department": "FINANCE", "confidentiality": "SECRET"},
    }
    assert orchestrator._format_keyword_result(no_leak) == orchestrator._format_keyword_result(with_leak)


def test_format_keyword_result_with_matches():
    result = {
        "keyword": "firas",
        "total_count": 2,
        "documents": [
            {
                "title": "CV",
                "count": 2,
                "locations": [
                    {"page": 1, "section": None, "line": 3, "excerpt": "...Firas Mekki..."},
                    {"page": None, "section": "Bloc 2", "line": 14, "excerpt": "...Firas encore..."},
                ],
            }
        ],
    }
    text = orchestrator._format_keyword_result(result)
    assert "firas" in text
    assert "2 fois" in text
    assert "p.1, l.3" in text
    assert "Bloc 2, l.14" in text


async def test_run_search_keyword_appends_deny_trace_and_hides_department(monkeypatch):
    async def fake_call_tool(session, name, arguments):
        return {
            "keyword": "salaire",
            "whole_word": True,
            "total_count": 0,
            "documents": [],
            "restricted_match": {"department": "FINANCE", "confidentiality": "SECRET"},
        }

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    tool_trace: list[dict] = []
    result = await orchestrator._run_search_keyword(None, "salaire", True, tool_trace)

    assert "FINANCE" not in result.answer
    assert "SECRET" not in result.answer
    assert result.answer == "Aucune occurrence de « salaire » trouvée dans les documents auxquels vous avez accès."

    assert len(tool_trace) == 2
    assert tool_trace[0]["decision"] == "ALLOW"
    assert tool_trace[1]["decision"] == "DENY"
    assert "FINANCE" in tool_trace[1]["reason"]


async def test_run_search_keyword_never_calls_llm(monkeypatch):
    async def fake_call_tool(session, name, arguments):
        return {
            "keyword": "firas",
            "whole_word": True,
            "total_count": 1,
            "documents": [{"document_id": "x", "title": "CV", "department": "GENERAL", "confidentiality": "INTERNAL", "count": 1, "locations": []}],
        }

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("chat_completion must never be called on the deterministic keyword path")

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", fail_if_called)

    result = await orchestrator._run_search_keyword(None, "firas", True, [])
    assert result.answer.startswith("« firas »")


async def test_run_search_keyword_service_unavailable_is_not_phrased_as_denial(monkeypatch):
    async def fake_call_tool(session, name, arguments):
        return {"error": "db exploded", "service_unavailable": True}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator._run_search_keyword(None, "firas", True, [])
    assert "indisponible" in result.answer
    assert "Accès refusé" not in result.answer
    assert result.degraded is True


async def test_run_search_keyword_policy_denial_is_phrased_as_refusal(monkeypatch):
    async def fake_call_tool(session, name, arguments):
        return {"error": "role EMPLOYEE is not permitted to use tool 'search_keyword'"}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator._run_search_keyword(None, "firas", True, [])
    assert result.answer.startswith("Accès refusé")


async def test_run_agent_turn_routes_keyword_question_deterministically(monkeypatch):
    """End-to-end within the orchestrator: a keyword-counting message must
    never reach the LLM tool-calling loop at all."""

    @asynccontextmanager
    async def fake_session(token):
        yield object()

    async def fake_call_tool(session, name, arguments):
        assert name == "search_keyword"
        assert arguments["keyword"] == "firas"
        return {"keyword": "firas", "whole_word": True, "total_count": 1, "documents": [{"document_id": "x", "title": "CV", "department": "GENERAL", "confidentiality": "INTERNAL", "count": 1, "locations": []}]}

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("the LLM must never be called for a deterministic keyword question")

    monkeypatch.setattr(orchestrator, "open_mcp_session", fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", fail_if_called)

    result = await orchestrator.run_agent_turn(
        user_id="00000000-0000-0000-0000-000000000000",
        grant=_grant(),
        message='cherche le mot "firas"',
        history=[],
    )
    assert result.answer.startswith("« firas »")


async def test_run_agent_turn_falls_back_to_grounding_when_tool_not_granted(monkeypatch):
    """A role without search_keyword in its grant must fall through to the
    normal grounding path instead - the deterministic router respects the
    role filter, it doesn't bypass it."""

    @asynccontextmanager
    async def fake_session(token):
        yield object()

    calls = []

    async def fake_call_tool(session, name, arguments):
        calls.append(name)
        return {"results": []}

    monkeypatch.setattr(orchestrator, "open_mcp_session", fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    grant = _grant(tools=("search_documents", "get_document", "get_company_information"))

    llm_was_called = False

    async def spy_chat_completion(messages, tools):
        nonlocal llm_was_called
        llm_was_called = True
        return {"content": "réponse du modèle", "tool_calls": None}

    monkeypatch.setattr(orchestrator, "chat_completion", spy_chat_completion)

    await orchestrator.run_agent_turn(
        user_id="00000000-0000-0000-0000-000000000000",
        grant=grant,
        message='cherche le mot "firas"',
        history=[],
    )
    # search_keyword was never attempted (not in this grant) - only the
    # normal grounding search ran, and execution proceeded to the LLM loop
    # exactly as before this feature existed (no regression).
    assert calls == ["search_documents"]
    assert llm_was_called is True
