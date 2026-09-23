from contextlib import asynccontextmanager

import app.agent.orchestrator as orchestrator
from app.agent.intent import SectionRequest
from app.policy.rules import Grant


def _grant(tools=("get_document_section", "search_documents", "get_document", "get_company_information")):
    return Grant(role="EMPLOYEE", departments=frozenset({"GENERAL"}), max_level="INTERNAL", max_level_rank=1, tools=frozenset(tools))


# --- formatters --------------------------------------------------------


def test_format_document_section_header_includes_title_location_confidentiality():
    result = {
        "title": "Procédure qualité", "page": 2, "section": None,
        "confidentiality": "INTERNAL", "text": "Contenu exact.", "truncated": False, "offset": 0, "total_length": 14,
    }
    text = orchestrator._format_document_section_result(result)
    assert text.startswith("Procédure qualité — Page 2 — Confidentialité : INTERNAL")
    assert "Contenu exact." in text


def test_format_document_section_by_section_name():
    result = {
        "title": "Manuel", "page": None, "section": "4.2",
        "confidentiality": "CONFIDENTIAL", "text": "texte", "truncated": False, "offset": 0, "total_length": 5,
    }
    text = orchestrator._format_document_section_result(result)
    assert "Section « 4.2 »" in text


def test_format_document_section_truncated_shows_continuation_hint():
    result = {
        "title": "Long", "page": 1, "section": None, "confidentiality": "INTERNAL",
        "text": "a" * 2000, "truncated": True, "offset": 0, "total_length": 5000,
    }
    text = orchestrator._format_document_section_result(result)
    assert "caractères 0 à 2000 sur 5000" in text
    assert "suite" in text.lower()


def test_format_document_choices():
    text = orchestrator._format_document_choices([{"document_id": "1", "title": "A"}, {"document_id": "2", "title": "B"}])
    assert "1. A" in text
    assert "2. B" in text


# --- resolution ----------------------------------------------------------


def test_resolve_explicit_document_name():
    req = SectionRequest(page=2, document_name="Charte")
    resolved = orchestrator._resolve_section_request(req, None)
    assert resolved == (None, "Charte", 2, None, 0)


def test_resolve_no_document_reference_and_no_context_asks_which_document():
    req = SectionRequest(page=2)
    resolved = orchestrator._resolve_section_request(req, None)
    assert isinstance(resolved, orchestrator.AgentTurnResult)
    assert "quel document" in resolved.answer.lower()


def test_resolve_context_document_from_prior_section():
    ctx = {"kind": "document_section", "document_id": "doc-1", "page": 1}
    req = SectionRequest(page=2, use_context_document=True)
    resolved = orchestrator._resolve_section_request(req, ctx)
    assert resolved == ("doc-1", None, 2, None, 0)


def test_resolve_implicit_context_document_without_explicit_ce_document():
    ctx = {"kind": "document_section", "document_id": "doc-1"}
    req = SectionRequest(section="4.2")
    resolved = orchestrator._resolve_section_request(req, ctx)
    assert resolved == ("doc-1", None, None, "4.2", 0)


def test_resolve_first_occurrence():
    ctx = {
        "kind": "keyword_search",
        "documents": [{"document_id": "doc-1", "title": "CV", "locations": [{"page": 1, "section": None}]}],
    }
    req = SectionRequest(first_occurrence=True)
    resolved = orchestrator._resolve_section_request(req, ctx)
    assert resolved == ("doc-1", None, 1, None, 0)


def test_resolve_first_occurrence_without_context_is_clarification():
    req = SectionRequest(first_occurrence=True)
    resolved = orchestrator._resolve_section_request(req, None)
    assert isinstance(resolved, orchestrator.AgentTurnResult)


def test_resolve_nth_choice_from_document_choice():
    ctx = {
        "kind": "document_choice",
        "candidates": [{"document_id": "a", "title": "A"}, {"document_id": "b", "title": "B"}],
        "page": 3,
        "section": None,
    }
    req = SectionRequest(nth_choice=2)
    resolved = orchestrator._resolve_section_request(req, ctx)
    assert resolved == ("b", None, 3, None, 0)


def test_resolve_nth_choice_out_of_range():
    ctx = {"kind": "document_choice", "candidates": [{"document_id": "a", "title": "A"}], "page": 1, "section": None}
    req = SectionRequest(nth_choice=5)
    resolved = orchestrator._resolve_section_request(req, ctx)
    assert isinstance(resolved, orchestrator.AgentTurnResult)


def test_resolve_nth_choice_from_keyword_search():
    ctx = {
        "kind": "keyword_search",
        "documents": [
            {"document_id": "a", "title": "A", "locations": [{"page": 1, "section": None}]},
            {"document_id": "b", "title": "B", "locations": [{"page": 5, "section": None}]},
        ],
    }
    req = SectionRequest(nth_choice=2)
    resolved = orchestrator._resolve_section_request(req, ctx)
    assert resolved == ("b", None, 5, None, 0)


def test_resolve_continuation_requires_truncated_context():
    ctx = {"kind": "document_section", "document_id": "doc-1", "truncated": False}
    req = SectionRequest(continuation=True)
    resolved = orchestrator._resolve_section_request(req, ctx)
    assert isinstance(resolved, orchestrator.AgentTurnResult)


def test_resolve_continuation_uses_next_offset():
    ctx = {
        "kind": "document_section", "document_id": "doc-1", "page": 1, "section": None,
        "truncated": True, "next_offset": 2000,
    }
    req = SectionRequest(continuation=True)
    resolved = orchestrator._resolve_section_request(req, ctx)
    assert resolved == ("doc-1", None, 1, None, 2000)


def test_resolve_bare_request_without_location_asks_to_precise():
    req = SectionRequest()
    resolved = orchestrator._resolve_section_request(req, None)
    assert isinstance(resolved, orchestrator.AgentTurnResult)


# --- Voie A: never touches the LLM ----------------------------------------


async def test_run_get_document_section_never_calls_llm(monkeypatch):
    async def fake_call_tool(session, name, arguments):
        return {
            "document_id": "doc-1", "title": "CV", "department": "GENERAL", "confidentiality": "INTERNAL",
            "page": 1, "section": None, "text": "contenu exact", "offset": 0, "total_length": 13, "truncated": False,
        }

    async def fail_if_called(*a, **k):
        raise AssertionError("chat_completion must never be called on the get_document_section path")

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", fail_if_called)

    request = SectionRequest(page=1, document_name="CV")
    result = await orchestrator._run_get_document_section(None, request, None, [])
    assert "contenu exact" in result.answer
    assert result.answer.startswith("CV")


async def test_run_get_document_section_populates_reference_context_for_next_turn(monkeypatch):
    async def fake_call_tool(session, name, arguments):
        return {
            "document_id": "doc-1", "title": "Long", "department": "GENERAL", "confidentiality": "INTERNAL",
            "page": 1, "section": None, "text": "a" * 2000, "offset": 0, "total_length": 5000, "truncated": True,
        }

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    request = SectionRequest(page=1, document_name="Long")
    result = await orchestrator._run_get_document_section(None, request, None, [])
    assert result.reference_context["kind"] == "document_section"
    assert result.reference_context["truncated"] is True
    assert result.reference_context["next_offset"] == 2000


# --- end-to-end routing through run_agent_turn -----------------------------


async def test_run_agent_turn_routes_explicit_page_and_document_name(monkeypatch):
    @asynccontextmanager
    async def fake_session(token):
        yield object()

    async def fake_call_tool(session, name, arguments):
        assert name == "get_document_section"
        assert arguments["document_name"] == "Charte"
        assert arguments["page"] == 2
        return {
            "document_id": "doc-1", "title": "Charte de l'entreprise", "department": "GENERAL",
            "confidentiality": "PUBLIC", "page": 2, "section": None, "text": "texte", "offset": 0,
            "total_length": 5, "truncated": False,
        }

    async def fail_llm(*a, **k):
        raise AssertionError("must not reach the LLM")

    monkeypatch.setattr(orchestrator, "open_mcp_session", fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", fail_llm)

    result = await orchestrator.run_agent_turn(
        user_id="00000000-0000-0000-0000-000000000000",
        grant=_grant(),
        message="ouvre la page 2 de Charte",
        history=[],
    )
    assert "Charte de l'entreprise" in result.answer


async def test_run_agent_turn_followup_first_occurrence_uses_last_reference_context(monkeypatch):
    @asynccontextmanager
    async def fake_session(token):
        yield object()

    async def fake_call_tool(session, name, arguments):
        assert name == "get_document_section"
        assert arguments["document_id"] == "doc-42"
        assert arguments["page"] == 3
        return {
            "document_id": "doc-42", "title": "CV", "department": "GENERAL", "confidentiality": "INTERNAL",
            "page": 3, "section": None, "text": "Firas Mekki", "offset": 0, "total_length": 11, "truncated": False,
        }

    monkeypatch.setattr(orchestrator, "open_mcp_session", fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    last_ctx = {
        "kind": "keyword_search",
        "documents": [{"document_id": "doc-42", "title": "CV", "locations": [{"page": 3, "section": None}]}],
    }
    result = await orchestrator.run_agent_turn(
        user_id="00000000-0000-0000-0000-000000000000",
        grant=_grant(),
        message="ouvre la première occurrence",
        history=[],
        last_reference_context=last_ctx,
    )
    assert "Firas Mekki" in result.answer
