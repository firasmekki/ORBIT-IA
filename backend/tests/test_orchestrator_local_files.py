"""Orchestrator-level tests for the Local File Agent's two-pass turn
(app/agent/orchestrator.py::_run_local_file_turn, wired into
run_agent_turn). First pass resolves a filename against the client-
supplied workspace index and returns a pending_client_action - this
never touches MCP or the LLM, since the backend has no disk access at
all (the whole point of this feature). Second pass (resume) is given the
file's content directly and grounds the LLM with it. call_tool/
chat_completion/open_mcp_session are monkeypatched - consistent with
every other orchestrator test in this suite.
"""

from dataclasses import dataclass

import app.agent.orchestrator as orchestrator
from app.agent.llm_client import LLMServiceError
from app.policy.rules import Grant


def _grant(tools=("search_documents", "get_document_section")):
    return Grant(role="EMPLOYEE", departments=frozenset({"GENERAL"}), max_level="INTERNAL", max_level_rank=1, tools=frozenset(tools))


@dataclass
class _Entry:
    name: str
    relative_path: str
    extension: str = "pdf"
    size: int = 1024
    modified_at: str | None = None
    parent_folder: str | None = None


async def _fail_mcp(*a, **k):
    raise AssertionError("local-file turn must never open an MCP session - the backend has no disk access to delegate")


async def _fail_llm_first_pass(*a, **k):
    raise AssertionError("first pass (resolving which file) must never call the LLM")


# --- first pass: resolve + pending_client_action ----------------------------


async def test_matching_file_returns_pending_client_action(monkeypatch):
    monkeypatch.setattr(orchestrator, "open_mcp_session", _fail_mcp)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm_first_pass)
    index = [_Entry(name="rapport.pdf", relative_path="Projet/rapport.pdf")]

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(), message="Lis rapport.pdf", history=[], workspace_index=index
    )

    assert result.pending_client_action == {
        "tool": "read_local_file",
        "relative_path": "Projet/rapport.pdf",
        "name": "rapport.pdf",
    }
    assert result.answer == ""


async def test_unmatched_filename_returns_not_found_answer(monkeypatch):
    monkeypatch.setattr(orchestrator, "open_mcp_session", _fail_mcp)
    index = [_Entry(name="budget.xlsx", relative_path="budget.xlsx")]

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(), message="Lis rapport.pdf", history=[], workspace_index=index
    )

    assert result.pending_client_action is None
    assert "rapport.pdf" in result.answer
    assert "workspace actif" in result.answer


async def test_ambiguous_filename_asks_for_clarification(monkeypatch):
    monkeypatch.setattr(orchestrator, "open_mcp_session", _fail_mcp)
    index = [
        _Entry(name="rapport.pdf", relative_path="Projet/rapport.pdf"),
        _Entry(name="rapport.pdf", relative_path="RH/rapport.pdf"),
    ]

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(), message="Lis rapport.pdf", history=[], workspace_index=index
    )

    assert result.pending_client_action is None
    assert "Projet/rapport.pdf" in result.answer
    assert "RH/rapport.pdf" in result.answer


async def test_no_workspace_index_falls_through_to_normal_rag_path(monkeypatch):
    """Regression guard: without an active workspace, "Lis rapport.pdf"
    must behave exactly as before this feature existed - routed into the
    normal search_documents grounding, never treated as a local-file
    request. Proves the gate (`if workspace_index or client_file_content`)
    actually guards the feature rather than matching unconditionally."""

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session(token):
        yield object()

    called = {"search_documents": False}

    async def fake_call_tool(session, name, arguments):
        if name == "search_documents":
            called["search_documents"] = True
        return {"results": []}

    async def fake_chat_completion(messages, tools=None):
        return {"content": "Réponse normale.", "tool_calls": None}

    monkeypatch.setattr(orchestrator, "open_mcp_session", fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", fake_chat_completion)

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(), message="Lis rapport.pdf", history=[], workspace_index=None
    )

    assert result.pending_client_action is None
    assert called["search_documents"] is True


# --- second pass: resume with content ---------------------------------------


async def test_resume_with_content_grounds_llm_and_returns_answer(monkeypatch):
    monkeypatch.setattr(orchestrator, "open_mcp_session", _fail_mcp)

    captured = {}

    async def fake_chat_completion(messages, tools=None):
        captured["messages"] = messages
        return {"content": "Ce rapport parle du budget 2025.", "tool_calls": None}

    monkeypatch.setattr(orchestrator, "chat_completion", fake_chat_completion)

    result = await orchestrator.run_agent_turn(
        user_id="u1",
        grant=_grant(),
        # Realistic: resume always replays the exact original message that
        # triggered the pending action in the first place (recovered from
        # the conversation's last user Message row) - it necessarily
        # already contains a filename, or there'd have been no pending
        # action to resume.
        message="Résume rapport.txt",
        history=[],
        client_file_content=b"Contenu brut du rapport, budget 2025.",
        client_file_name="rapport.txt",
    )

    assert result.answer == "Ce rapport parle du budget 2025."
    assert any("rapport.txt" in m.get("content", "") for m in captured["messages"])
    assert any("budget 2025" in m.get("content", "") for m in captured["messages"])


async def test_resume_unsupported_file_type_returns_clean_error(monkeypatch):
    monkeypatch.setattr(orchestrator, "open_mcp_session", _fail_mcp)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm_first_pass)

    result = await orchestrator.run_agent_turn(
        user_id="u1",
        grant=_grant(),
        message="Lis archive.zip",
        history=[],
        client_file_content=b"PK\x03\x04fake-zip-bytes",
        client_file_name="archive.zip",
    )

    assert "pas encore pris en charge" in result.answer


async def test_resume_llm_unavailable_falls_back_to_raw_excerpt(monkeypatch):
    monkeypatch.setattr(orchestrator, "open_mcp_session", _fail_mcp)

    async def failing_llm(messages, tools=None):
        raise LLMServiceError("Ollama indisponible")

    monkeypatch.setattr(orchestrator, "chat_completion", failing_llm)

    result = await orchestrator.run_agent_turn(
        user_id="u1",
        grant=_grant(),
        message="Lis notes.txt",
        history=[],
        client_file_content=b"Contenu important des notes.",
        client_file_name="notes.txt",
    )

    assert result.degraded is True
    assert "Contenu important des notes." in result.answer
