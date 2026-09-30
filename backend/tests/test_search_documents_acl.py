"""Integration tests for the search_documents MCP tool: score, ordering,
page/section, ACL, restricted_match - against a real database.
app.rag.retriever.embed_text is monkeypatched to a deterministic
hot_vector() so cosine-distance ordering is fully controllable without
Ollama (see tests/conftest.py::hot_vector)."""

import app.mcp.server as mcp_server
from tests.conftest import hot_vector


def _patch_query_embed(monkeypatch, vector):
    monkeypatch.setattr("app.rag.retriever.embed_text", lambda query: vector)


def test_score_present_and_rounded(make_doc, make_test_user, make_test_chunk, monkeypatch):
    doc = make_doc(title="Doc", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, "texte")])
    make_test_chunk(document_id=doc.id, chunk_text="texte", embedding=hot_vector(0))
    employee = make_test_user(username="sd_emp1", role="EMPLOYEE")
    _patch_query_embed(monkeypatch, hot_vector(0))

    result = mcp_server._search_documents_impl(str(employee.id), "peu importe", 5)

    assert len(result["results"]) == 1
    score = result["results"][0]["score"]
    assert isinstance(score, float)
    assert score == round(score, 3)
    assert score == 1.0  # identical vector -> cosine distance 0 -> score 1.0


def test_results_ordered_by_relevance(make_doc, make_test_user, make_test_chunk, monkeypatch):
    doc_a = make_doc(title="Proche", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, "a")])
    doc_b = make_doc(title="Loin", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, "b")])
    make_test_chunk(document_id=doc_a.id, chunk_text="a", embedding=hot_vector(0))
    make_test_chunk(document_id=doc_b.id, chunk_text="b", embedding=hot_vector(1))  # orthogonal to query
    employee = make_test_user(username="sd_emp2", role="EMPLOYEE")
    _patch_query_embed(monkeypatch, hot_vector(0))

    result = mcp_server._search_documents_impl(str(employee.id), "q", 5)

    titles = [r["title"] for r in result["results"]]
    assert titles[0] == "Proche"
    scores = [r["score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True)


def test_page_and_section_reported_when_linked(make_doc, make_test_user, make_test_chunk, monkeypatch, pages_of):
    doc = make_doc(title="Doc", department="GENERAL", confidentiality="INTERNAL", pages=[(3, None, 1, "texte page 3")])
    page = pages_of(doc.id)[0]
    make_test_chunk(document_id=doc.id, chunk_text="texte page 3", embedding=hot_vector(0), document_page_id=page.id)
    employee = make_test_user(username="sd_emp3", role="EMPLOYEE")
    _patch_query_embed(monkeypatch, hot_vector(0))

    result = mcp_server._search_documents_impl(str(employee.id), "q", 5)
    assert result["results"][0]["page"] == 3
    assert result["results"][0]["section"] is None


def test_section_reported_when_linked(make_doc, make_test_user, make_test_chunk, monkeypatch, pages_of):
    doc = make_doc(title="Doc", department="GENERAL", confidentiality="INTERNAL", pages=[(None, "Introduction", 1, "texte")])
    page = pages_of(doc.id)[0]
    make_test_chunk(document_id=doc.id, chunk_text="texte", embedding=hot_vector(0), document_page_id=page.id)
    employee = make_test_user(username="sd_emp4", role="EMPLOYEE")
    _patch_query_embed(monkeypatch, hot_vector(0))

    result = mcp_server._search_documents_impl(str(employee.id), "q", 5)
    assert result["results"][0]["section"] == "Introduction"


def test_chunk_without_linked_page_reports_null_never_invented(make_doc, make_test_user, make_test_chunk, monkeypatch):
    doc = make_doc(title="Doc", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, "texte")])
    make_test_chunk(document_id=doc.id, chunk_text="texte", embedding=hot_vector(0), document_page_id=None)
    employee = make_test_user(username="sd_emp5", role="EMPLOYEE")
    _patch_query_embed(monkeypatch, hot_vector(0))

    result = mcp_server._search_documents_impl(str(employee.id), "q", 5)
    assert result["results"][0]["page"] is None
    assert result["results"][0]["section"] is None


def test_acl_excludes_unauthorized_department(make_doc, make_test_user, make_test_chunk, monkeypatch):
    finance_doc = make_doc(title="Finance", department="FINANCE", confidentiality="SECRET", pages=[(1, None, 1, "argent")])
    make_test_chunk(document_id=finance_doc.id, chunk_text="argent", embedding=hot_vector(0))
    employee = make_test_user(username="sd_emp6", role="EMPLOYEE")
    _patch_query_embed(monkeypatch, hot_vector(0))

    result = mcp_server._search_documents_impl(str(employee.id), "q", 5)
    assert result["results"] == []


def test_restricted_match_signals_closer_out_of_scope_result(make_doc, make_test_user, make_test_chunk, monkeypatch):
    finance_doc = make_doc(title="Finance", department="FINANCE", confidentiality="SECRET", pages=[(1, None, 1, "argent")])
    make_test_chunk(document_id=finance_doc.id, chunk_text="argent", embedding=hot_vector(0))
    employee = make_test_user(username="sd_emp7", role="EMPLOYEE")
    _patch_query_embed(monkeypatch, hot_vector(0))

    result = mcp_server._search_documents_impl(str(employee.id), "q", 5)
    assert result["results"] == []
    assert result["restricted_match"] == {"department": "FINANCE", "confidentiality": "SECRET"}


def test_no_results_when_nothing_indexed(make_test_user, monkeypatch):
    employee = make_test_user(username="sd_emp8", role="EMPLOYEE")
    _patch_query_embed(monkeypatch, hot_vector(0))

    result = mcp_server._search_documents_impl(str(employee.id), "rien ici", 5)
    assert result["results"] == []
    assert "restricted_match" not in result
