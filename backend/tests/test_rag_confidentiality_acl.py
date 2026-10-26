"""Confidentiality ACL of the RAG path, against a real database: SECRET EXEC,
SECRET FINANCE, CONFIDENTIAL HR and CONFIDENTIAL FINANCE documents are
only ever retrieved for a caller whose grant covers both the department
AND the level - and the filtering happens in the retrieval SQL itself,
before anything is handed to the LLM (the last test captures exactly
what the model receives).

Every chunk shares the query's embedding (hot_vector(0)), so all
documents are equally "relevant": whatever comes back is decided by the
ACL alone, never by similarity."""

from contextlib import asynccontextmanager

import app.agent.orchestrator as orchestrator
import app.mcp.server as mcp_server
from app.policy.rules import resolve_effective_grant
from tests.conftest import hot_vector

SENSITIVE = {
    "Plan stratégique 2026": ("EXEC", "SECRET", "Objectifs secrets : croissance du chiffre d'affaires de 20%."),
    "Grille des salaires individuels 2025": ("FINANCE", "SECRET", "Salaire individuel du directeur : 145 000 €."),
    "Rapport annuel RH - climat social": ("HR", "CONFIDENTIAL", "Taux de turnover RH : 12 %."),
    "Budget masse salariale 2025 (synthèse)": ("FINANCE", "CONFIDENTIAL", "Masse salariale 2025 : 4,1 millions d'euros."),
}
PUBLIC_TITLE = "Politique de congés"


def _seed(make_doc, make_test_chunk):
    for title, (department, level, text_) in {
        **SENSITIVE,
        PUBLIC_TITLE: ("GENERAL", "INTERNAL", "Les congés payés sont de 25 jours ouvrés par an."),
    }.items():
        doc = make_doc(title=title, department=department, confidentiality=level, pages=[(1, None, 1, text_)])
        make_test_chunk(document_id=doc.id, chunk_text=text_, embedding=hot_vector(0))


def _titles_for(user, monkeypatch) -> set[str]:
    monkeypatch.setattr("app.rag.retriever.embed_text", lambda query: hot_vector(0))
    result = mcp_server._search_documents_impl(str(user.id), "budget et salaires", 10)
    return {r["title"] for r in result["results"]}


def test_employee_never_retrieves_secret_or_confidential(make_doc, make_test_user, make_test_chunk, monkeypatch):
    _seed(make_doc, make_test_chunk)
    employee = make_test_user(username="acl_emp", role="EMPLOYEE")
    assert _titles_for(employee, monkeypatch) == {PUBLIC_TITLE}


def test_hr_sees_confidential_hr_but_not_finance_or_exec(make_doc, make_test_user, make_test_chunk, monkeypatch):
    _seed(make_doc, make_test_chunk)
    hr = make_test_user(username="acl_hr", role="HR")
    assert _titles_for(hr, monkeypatch) == {PUBLIC_TITLE, "Rapport annuel RH - climat social"}


def test_accountant_sees_confidential_finance_but_not_secret_finance(make_doc, make_test_user, make_test_chunk, monkeypatch):
    _seed(make_doc, make_test_chunk)
    accountant = make_test_user(username="acl_acc", role="ACCOUNTANT")
    titles = _titles_for(accountant, monkeypatch)
    assert titles == {PUBLIC_TITLE, "Budget masse salariale 2025 (synthèse)"}
    # Same department, level above the role's cap.
    assert "Grille des salaires individuels 2025" not in titles


def test_director_sees_every_level(make_doc, make_test_user, make_test_chunk, monkeypatch):
    _seed(make_doc, make_test_chunk)
    director = make_test_user(username="acl_dir", role="DIRECTOR")
    assert _titles_for(director, monkeypatch) == {PUBLIC_TITLE, *SENSITIVE}


def test_level_override_widens_level_but_not_department(make_doc, make_test_user, make_test_chunk, monkeypatch):
    _seed(make_doc, make_test_chunk)
    accountant = make_test_user(username="acl_acc_secret", role="ACCOUNTANT", confidentiality_override="SECRET")
    titles = _titles_for(accountant, monkeypatch)
    assert "Grille des salaires individuels 2025" in titles  # SECRET FINANCE now allowed
    assert "Plan stratégique 2026" not in titles  # SECRET, but EXEC is still not a granted department


def test_get_document_refuses_secret_document_to_unauthorized_role(make_doc, make_test_user, db):
    secret = make_doc(title="Plan stratégique 2026", department="EXEC", confidentiality="SECRET", pages=[(1, None, 1, "x")])
    accountant = make_test_user(username="acl_acc_get", role="ACCOUNTANT")

    result = mcp_server._get_document_impl(str(accountant.id), str(secret.id))

    assert "error" in result
    assert "content" not in result


async def test_unauthorized_chunks_never_reach_the_llm(make_doc, make_test_user, make_test_chunk, monkeypatch):
    _seed(make_doc, make_test_chunk)
    employee = make_test_user(username="acl_emp_llm", role="EMPLOYEE")
    grant = resolve_effective_grant(employee)
    monkeypatch.setattr("app.rag.retriever.embed_text", lambda query: hot_vector(0))

    @asynccontextmanager
    async def fake_session(token):
        yield object()

    async def real_tools(session, name, arguments):
        # The real MCP implementations, with the real ACL, against the real DB.
        if name == "search_documents":
            return mcp_server._search_documents_impl(str(employee.id), arguments["query"], arguments.get("top_k", 5))
        raise AssertionError(f"unexpected tool {name}")

    seen_by_llm: list[str] = []

    async def capturing_llm(messages, tools=None):
        seen_by_llm.extend(m.get("content") or "" for m in messages)
        return {"content": "Les congés payés sont de 25 jours ouvrés par an (Politique de congés).", "tool_calls": None}

    monkeypatch.setattr(orchestrator, "open_mcp_session", fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", real_tools)
    monkeypatch.setattr(orchestrator, "chat_completion", capturing_llm)

    result = await orchestrator.run_agent_turn(
        user_id=str(employee.id), grant=grant, message="budget et salaires 2025 et 2026", history=[]
    )

    everything_sent = "\n".join(seen_by_llm)
    for title, (_, _, text_) in SENSITIVE.items():
        assert title not in everything_sent
        assert text_ not in everything_sent
    assert "Les congés payés sont de 25 jours" in everything_sent
    assert [s["title"] for s in result.sources] == [PUBLIC_TITLE]
