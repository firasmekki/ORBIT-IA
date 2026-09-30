"""Orchestrator-level tests for search_documents' enriched grounding path:
reference_context construction (reusing search_keyword's exact shape) and
the two follow-up scenarios from the spec - "ouvre le deuxième résultat"
and "ouvre la section correspondante du premier résultat" - resolving
through get_document_section. call_tool/chat_completion are monkeypatched;
these don't need a real database."""

from contextlib import asynccontextmanager

import app.agent.orchestrator as orchestrator
from app.policy.rules import Grant


def _grant(tools=("search_documents", "get_document_section", "get_document", "get_company_information")):
    return Grant(role="EMPLOYEE", departments=frozenset({"GENERAL"}), max_level="INTERNAL", max_level_rank=1, tools=frozenset(tools))


_SEARCH_RESULT = {
    "results": [
        {
            "document_id": "doc-1", "title": "Politique de sécurité", "department": "GENERAL",
            "confidentiality": "INTERNAL", "excerpt": "...", "score": 0.82, "page": 2, "section": None,
        },
        {
            "document_id": "doc-2", "title": "Guide informatique", "department": "GENERAL",
            "confidentiality": "INTERNAL", "excerpt": "...", "score": 0.61, "page": None, "section": "Introduction",
        },
    ]
}


async def test_ground_with_search_populates_reference_context(monkeypatch):
    async def fake_call_tool(session, name, arguments):
        return _SEARCH_RESULT

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    tool_trace: list[dict] = []
    sources_by_doc: dict = {}
    messages: list[dict] = []
    holder: dict = {}

    result = await orchestrator._ground_with_search(None, "sécurité informatique", tool_trace, sources_by_doc, messages, holder)

    assert result is None  # grounding never short-circuits on a plain result set
    ctx = holder["value"]
    assert ctx["kind"] == "keyword_search"  # reused format, not a new one
    assert len(ctx["documents"]) == 2
    assert ctx["documents"][0]["document_id"] == "doc-1"
    assert ctx["documents"][0]["locations"] == [{"page": 2, "section": None}]
    assert ctx["documents"][1]["locations"] == [{"page": None, "section": "Introduction"}]


async def test_run_agent_turn_search_then_second_result_opens_correct_document(monkeypatch):
    """"Trouve les documents qui parlent de sécurité informatique." puis
    "Ouvre le deuxième résultat." - across two separate run_agent_turn
    calls, exactly like two chat turns, threading reference_context the
    same way routers/chat.py does."""

    @asynccontextmanager
    async def fake_session(token):
        yield object()

    async def fake_search(session, name, arguments):
        assert name == "search_documents"
        return _SEARCH_RESULT

    async def fail_llm(*a, **k):
        raise AssertionError("must not reach the LLM for a deterministic follow-up")

    monkeypatch.setattr(orchestrator, "open_mcp_session", fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", fake_search)

    async def llm_reply(messages, tools):
        return {"content": "Voici ce que j'ai trouvé.", "tool_calls": None}

    monkeypatch.setattr(orchestrator, "chat_completion", llm_reply)

    turn1 = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(), message="Trouve les documents qui parlent de sécurité informatique.", history=[]
    )
    assert turn1.reference_context["kind"] == "keyword_search"

    async def fake_get_section(session, name, arguments):
        assert name == "get_document_section"
        assert arguments["document_id"] == "doc-2"  # second result
        assert arguments["section"] == "Introduction"
        return {
            "document_id": "doc-2", "title": "Guide informatique", "department": "GENERAL",
            "confidentiality": "INTERNAL", "page": None, "section": "Introduction",
            "text": "Contenu exact.", "offset": 0, "total_length": 14, "truncated": False,
        }

    monkeypatch.setattr(orchestrator, "call_tool", fake_get_section)
    monkeypatch.setattr(orchestrator, "chat_completion", fail_llm)

    turn2 = await orchestrator.run_agent_turn(
        user_id="u1",
        grant=_grant(),
        message="Ouvre le deuxième résultat.",
        history=[{"role": "user", "content": "Trouve les documents qui parlent de sécurité informatique."}],
        last_reference_context=turn1.reference_context,
    )
    assert "Contenu exact." in turn2.answer


async def test_run_agent_turn_open_section_of_first_result(monkeypatch):
    """"Ouvre la section correspondante du premier résultat." must resolve
    to the FIRST document from the prior search, not get derailed by
    "section" appearing in the sentence (see app/agent/intent.py's
    _ANCHORED_NTH_RE / first_occurrence widening)."""

    @asynccontextmanager
    async def fake_session(token):
        yield object()

    async def fake_get_section(session, name, arguments):
        assert name == "get_document_section"
        assert arguments["document_id"] == "doc-1"  # first result
        assert arguments["page"] == 2
        return {
            "document_id": "doc-1", "title": "Politique de sécurité", "department": "GENERAL",
            "confidentiality": "INTERNAL", "page": 2, "section": None,
            "text": "Contenu de la page 2.", "offset": 0, "total_length": 22, "truncated": False,
        }

    monkeypatch.setattr(orchestrator, "open_mcp_session", fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", fake_get_section)

    prior_ctx = {
        "kind": "keyword_search",
        "keyword": "sécurité informatique",
        "documents": [
            {"document_id": "doc-1", "title": "Politique de sécurité", "locations": [{"page": 2, "section": None}]},
            {"document_id": "doc-2", "title": "Guide informatique", "locations": [{"page": None, "section": "Introduction"}]},
        ],
    }

    result = await orchestrator.run_agent_turn(
        user_id="u1",
        grant=_grant(),
        message="Ouvre la section correspondante du premier résultat.",
        history=[],
        last_reference_context=prior_ctx,
    )
    assert "Contenu de la page 2." in result.answer
