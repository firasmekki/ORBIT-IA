"""run_agent_turn for search requests: the search always runs (the LLM is
never asked), the answer is built from the server's results only, the
Workspace is searched through the browser when one is selected, and the
other routes (semantic RAG, chart dialog) are untouched."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import app.agent.orchestrator as orchestrator
from app.policy.rules import Grant

TAREK_RESULT = {
    "keyword": "Tarek", "whole_word": True, "total_count": 2,
    "documents": [
        {"document_id": "doc-1", "title": "Compte rendu équipe", "department": "GENERAL", "confidentiality": "INTERNAL",
         "count": 2, "locations": [{"page": 1, "section": None, "line": 3, "excerpt": "équipe : Tarek Ben Ali présente"}]},
    ],
}
EMPTY = {"keyword": "Tarak", "whole_word": True, "total_count": 0, "documents": []}


def _grant(role="EMPLOYEE", extra=()):
    return Grant(role=role, departments=frozenset({"GENERAL"}), max_level="INTERNAL", max_level_rank=1,
                 tools=frozenset({"search_documents", "search_keyword", "get_document", "list_documents",
                                  "get_document_section", "generate_chart", *extra}))


def _install(monkeypatch, *, keyword_result=TAREK_RESULT, calls=None, llm_answer=None):
    @asynccontextmanager
    async def session(token):
        yield object()

    async def call_tool(_session, name, arguments):
        if calls is not None:
            calls.append((name, arguments))
        if name == "search_keyword":
            return {**keyword_result, "keyword": arguments["keyword"]}
        if name == "search_documents":
            return {"results": [{"document_id": "doc-c", "title": "Politique de congés", "department": "GENERAL",
                                 "confidentiality": "INTERNAL", "excerpt": "25 jours de congés payés par an.",
                                 "score": 0.8, "page": None, "section": None}]}
        raise AssertionError(f"unexpected tool {name}")

    async def llm(messages, tools=None):
        if llm_answer is None:
            raise AssertionError("a search must never be answered by the LLM")
        return {"content": llm_answer, "tool_calls": None}

    monkeypatch.setattr(orchestrator, "open_mcp_session", session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", llm)


async def _turn(message, grant=None, **kwargs):
    return await orchestrator.run_agent_turn(user_id="u1", grant=grant or _grant(), message=message, history=[], **kwargs)


# Scenarios 1 / 2 / 3 ---------------------------------------------------------------------------


async def test_recherche_tarek_runs_the_exact_search_and_answers_from_its_results(monkeypatch):
    calls = []
    _install(monkeypatch, calls=calls)

    result = await _turn("recherche Tarek")

    assert calls == [("search_keyword", {"keyword": "Tarek", "whole_word": True})]
    assert result.answer == "« Tarek » apparaît 2 fois dans 1 document.\nFichier : Compte rendu équipe."
    assert result.search_results["term"] == "Tarek" and result.search_results["total"] == 2
    occurrence = result.search_results["documents"][0]["occurrences"][0]
    assert occurrence == {"page": 1, "section": None, "line": 3, "excerpt": "équipe : Tarek Ben Ali présente"}
    assert [s["title"] for s in result.sources] == ["Compte rendu équipe"]


async def test_every_reproduced_phrasing_reaches_the_search(monkeypatch):
    for message in ("Tarek", "rechercher tarek", "trouve Tarek dans le workspace", "où apparaît Tarek ?",
                    "donne-moi les documents qui mentionnent Tarek", "rechercher firas", "Firas"):
        calls = []
        _install(monkeypatch, calls=calls)
        await _turn(message)
        assert calls and calls[0][0] == "search_keyword", message


async def test_tarak_is_searched_as_typed(monkeypatch):
    calls = []
    _install(monkeypatch, keyword_result=EMPTY, calls=calls)

    result = await _turn("recherche Tarak")

    assert calls[0][1]["keyword"] == "Tarak"
    assert result.answer == "Aucune occurrence de « Tarak » trouvée dans les documents auxquels vous avez accès."


async def test_workspace_scope_searches_the_conversations_workspace_server_side(monkeypatch):
    # "au niveau de workspace" = the conversation's workspace, whose synced
    # files are indexed documents: searched server-side (search_keyword is
    # scoped to that workspace by the signed token), nothing read by the
    # browser.
    calls = []
    _install(monkeypatch, calls=calls)
    result = await _turn("rechercher tarek au niveau de workspace")
    assert [name for name, _ in calls] == ["search_keyword"]
    assert result.pending_client_action is None
    assert result.answer.startswith("« tarek » apparaît 2 fois")
    assert "Compte rendu équipe" in result.answer


# Workspace selected: browser reads the files, both sources are searched ---------------------------


def _index():
    entry = lambda name, ext, size=1000: SimpleNamespace(name=name, relative_path=name, extension=ext, size=size)  # noqa: E731
    return [entry("rapportpython.docx", "docx"), entry("Firas_Mekki_CV_2026.docx", "docx"), entry("logo.png", "png")]


async def test_with_a_workspace_the_browser_is_asked_to_read_the_searchable_files(monkeypatch):
    calls = []
    _install(monkeypatch, calls=calls)

    result = await _turn("rechercher tarek au niveau de workspace", workspace_index=_index())

    assert calls == []  # nothing searched yet - the content is still in the browser
    assert result.pending_client_action["tool"] == "search_local_files"
    assert result.pending_client_action["query"] == "tarek"
    assert [f["name"] for f in result.pending_client_action["files"]] == ["rapportpython.docx", "Firas_Mekki_CV_2026.docx"]


async def test_resumed_search_combines_workspace_and_company_documents(monkeypatch):
    calls = []
    _install(monkeypatch, calls=calls)
    # Same extraction + matcher path for every format (see
    # test_search_indexing_and_workspace.py for real docx/pdf/xlsx files).
    files = [
        ("rapportpython.txt", "rapportpython.txt", "Simulation – système réel\nTarek = 5\nFin".encode()),
        ("notes.txt", "notes.txt", b"Rien a signaler."),
    ]

    result = await _turn(
        "rechercher tarek au niveau de workspace",
        client_files=files,
        client_skipped=[{"name": "enorme.pdf", "reason": "fichier trop volumineux"}],
    )

    assert calls[0][0] == "search_keyword"
    assert result.answer.startswith("J'ai trouvé 3 occurrence(s) de « tarek » dans 2 source(s) accessible(s).")
    assert "Documents Orbitia :" in result.answer and "Compte rendu équipe" in result.answer
    assert "Workspace local :" in result.answer and "rapportpython.txt — 1 occurrence(s)" in result.answer
    assert "Tarek = 5" in result.answer
    assert "enorme.pdf (fichier trop volumineux)" in result.answer
    assert any(t["tool"] == "search_local_files" for t in result.tool_trace)


async def test_nothing_anywhere_is_a_clear_no_result(monkeypatch):
    _install(monkeypatch, keyword_result=EMPTY)
    result = await _turn("recherche Tarak", client_files=[("a.txt", "a.txt", b"Tarek seulement")], client_skipped=[])
    assert result.answer.startswith(
        "Aucune occurrence de « Tarak » n'a été trouvée dans les données accessibles à votre compte "
        "(documents Orbitia et Workspace local)."
    )


# Other routes untouched ---------------------------------------------------------------------------


async def test_semantic_question_still_goes_through_rag_and_the_llm(monkeypatch):
    calls = []
    _install(monkeypatch, calls=calls, llm_answer="Vous disposez de 25 jours de congés payés par an (Politique de congés).")

    result = await _turn("Quelle est la politique de congés ?")

    assert [c[0] for c in calls] == ["search_documents"]
    assert "25 jours" in result.answer


async def test_bare_answer_to_a_pending_chart_question_is_not_a_search(monkeypatch):
    calls = []
    _install(monkeypatch, calls=calls)

    async def call_tool(_session, name, arguments):
        calls.append((name, arguments))
        if name == "search_database":
            return {"results": [{"label": "Budget IT / Infrastructure 2025", "amount": 310000.0, "year": 2025}]}
        if name == "generate_chart":
            return {"chart": {**arguments}}
        raise AssertionError(name)

    monkeypatch.setattr(orchestrator, "call_tool", call_tool)
    pending = {"kind": "chart_clarification", "asked": "metric", "request": "Donne-moi une courbe", "answers": [],
               "option_values": ["Budget IT / Infrastructure"]}
    result = await _turn("Infrastructure", grant=_grant(extra=("search_database",)), last_reference_context=pending)

    assert "search_keyword" not in [c[0] for c in calls]
    assert result.chart is not None


def test_occurrence_lines_are_single_line_and_have_no_dangling_comma():
    assert orchestrator._occurrence_line({"page": None, "section": None, "line": 3, "excerpt": "a\n\nTarek =  5\n"}) == (
        "• l.3 : …a Tarek = 5…"
    )
    assert orchestrator._occurrence_line({"page": 2, "section": None, "line": 14, "excerpt": "x"}) == "• p.2, l.14 : …x…"


async def test_model_down_never_falls_back_to_a_document_search(monkeypatch):
    # Before: LLM down -> search_documents -> raw excerpts shown as the answer
    # (seen live: "bonjour" displayed an "acces.txt" with credentials).
    calls = []

    @asynccontextmanager
    async def session(token):
        yield object()

    async def call_tool(_session, name, arguments):
        calls.append(name)
        excerpt = {"document_id": "doc-csv", "title": "export-discussion.csv", "relative_path": "dataexemple/export-discussion.csv",
                   "department": "GENERAL", "confidentiality": "INTERNAL"}
        return {"results": [{**excerpt, "excerpt": f"extrait {i}"} for i in range(3)]}

    async def down(*args, **kwargs):
        raise LLMServiceError("Le modèle IA local n'est pas disponible pour le moment.")

    monkeypatch.setattr(orchestrator, "open_mcp_session", session)
    monkeypatch.setattr(orchestrator, "call_tool", call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", down)
    result = await _turn("bacs de traversée dans les documents", grant=_grant(extra=("search_database",)))
    assert result.degraded and result.answer_mode == "unavailable"
    assert "extrait" not in result.answer and "export-discussion" not in result.answer
    assert result.sources == []
    assert not hasattr(orchestrator, "_degraded_fallback")
