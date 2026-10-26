"""Term search ("recherche Tarek") against a real database: exact lexical
matching, the RBAC/ABAC filter applied in SQL before anything is
returned, documents indexed without pages, case B (term only in a
forbidden document) invisible to the user but alerted and audited, the
hybrid ranking of search_documents, and the four original MCP tools
exercised directly with different users."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.mcp.server as mcp_server
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.document import CONFIDENTIALITY_RANK, Document
from app.models.finance import FinancialRecord
from app.policy.engine import public_tool_result
from app.routers.documents import router as documents_router
from tests.conftest import hot_vector

GENERAL_TEXT = "Réunion d'équipe : Tarek Ben Ali présente le planning avec Firas Trabelsi."
LEGACY_TEXT = "Rapport historique rédigé par Tarek."  # indexed before document_pages existed
HR_TEXT = "Entretien annuel de Tarek : objectifs atteints."
SECRET_TEXT = "Prime exceptionnelle de Tarek : 5000 euros. Mutation confidentielle de Karim."


def _seed(make_doc, db):
    docs = {
        "general": make_doc(title="Compte rendu équipe", department="GENERAL", confidentiality="INTERNAL",
                            pages=[(1, None, 1, GENERAL_TEXT)]),
        "hr": make_doc(title="Entretiens RH 2025", department="HR", confidentiality="CONFIDENTIAL",
                       pages=[(1, None, 1, HR_TEXT)]),
        "secret": make_doc(title="Primes direction", department="FINANCE", confidentiality="SECRET",
                           pages=[(1, None, 1, SECRET_TEXT)]),
    }
    legacy = Document(title="Rapport historique", department="GENERAL", confidentiality="INTERNAL",
                      confidentiality_rank=CONFIDENTIALITY_RANK["INTERNAL"], content=LEGACY_TEXT)
    db.add(legacy)
    db.commit()
    docs["legacy"] = legacy
    return docs


def _search(user, term):
    return mcp_server._search_keyword_impl(str(user.id), term, True)


def _titles(result):
    return {d["title"] for d in result["documents"]}


# --- exact search, case, other names ---------------------------------------------------------


def test_exact_search_finds_tarek_whatever_the_case(make_doc, make_test_user, db):
    _seed(make_doc, db)
    employee = make_test_user(username="ts_emp", role="EMPLOYEE")
    for term in ("Tarek", "tarek", "TAREK"):
        result = _search(employee, term)
        assert _titles(result) == {"Compte rendu équipe", "Rapport historique"}, term
        assert result["total_count"] == 2


def test_other_first_name(make_doc, make_test_user, db):
    _seed(make_doc, db)
    employee = make_test_user(username="ts_emp2", role="EMPLOYEE")
    assert _titles(_search(employee, "Firas")) == {"Compte rendu équipe"}


def test_tarak_is_searched_exactly_never_replaced_by_tarek(make_doc, make_test_user, db):
    _seed(make_doc, db)
    employee = make_test_user(username="ts_emp3", role="EMPLOYEE")
    result = _search(employee, "Tarak")
    assert result["total_count"] == 0 and result["documents"] == []


def test_document_indexed_without_pages_is_still_searched(make_doc, make_test_user, db):
    # Root cause #2: every document of the demo database had 0
    # document_pages rows, so exact search returned 0 for all of them.
    _seed(make_doc, db)
    employee = make_test_user(username="ts_emp4", role="EMPLOYEE")
    legacy = next(d for d in _search(employee, "Tarek")["documents"] if d["title"] == "Rapport historique")
    assert legacy["count"] == 1
    assert "Tarek" in legacy["locations"][0]["excerpt"]


# --- RBAC / ABAC ------------------------------------------------------------------------------


def test_hr_never_receives_finance_secret_even_if_tarek_is_in_it(make_doc, make_test_user, db):
    _seed(make_doc, db)
    hr = make_test_user(username="ts_hr", role="HR")
    result = _search(hr, "Tarek")
    assert _titles(result) == {"Compte rendu équipe", "Rapport historique", "Entretiens RH 2025"}
    assert "5000" not in str(result) and "Primes direction" not in str(result)


def test_director_gets_every_occurrence_his_rights_cover(make_doc, make_test_user, db):
    _seed(make_doc, db)
    director = make_test_user(username="ts_dir", role="DIRECTOR")
    assert _titles(_search(director, "Tarek")) == {
        "Compte rendu équipe", "Rapport historique", "Entretiens RH 2025", "Primes direction",
    }


def test_override_widens_level_but_not_department(make_doc, make_test_user, db):
    _seed(make_doc, db)
    accountant = make_test_user(username="ts_acc", role="ACCOUNTANT", confidentiality_override="SECRET")
    titles = _titles(_search(accountant, "Tarek"))
    assert "Primes direction" in titles  # FINANCE/SECRET now allowed
    assert "Entretiens RH 2025" not in titles  # HR is still not one of its departments


# --- case B: the term exists only where the user may not read --------------------------------


def test_term_only_in_forbidden_document_is_invisible_but_alerted_and_audited(make_doc, make_test_user, db):
    _seed(make_doc, db)
    employee = make_test_user(username="ts_emp_b", role="EMPLOYEE")

    result = _search(employee, "Karim")

    # Nothing about the document reaches the caller...
    assert result["total_count"] == 0 and result["documents"] == []
    assert "Primes direction" not in str(result) and "Mutation" not in str(result)
    assert result["alert_raised"] is True
    # ...the Director is alerted, once even if the search is retried...
    _search(employee, "Karim")
    _search(employee, "karim")
    db.expire_all()
    alerts = db.query(Alert).filter(Alert.user_id == employee.id).all()
    assert len(alerts) == 1
    assert "Karim" in alerts[0].description and "Mutation" not in alerts[0].description
    # ...and every attempt is in the audit log.
    audits = db.query(AuditLog).filter(AuditLog.user_id == employee.id, AuditLog.action == "MCP_SEARCH_KEYWORD").all()
    assert len(audits) == 3
    assert audits[0].extra["out_of_scope_hint"] == {"department": "FINANCE", "confidentiality": "SECRET"}


def test_no_alert_when_the_term_exists_nowhere(make_doc, make_test_user, db):
    _seed(make_doc, db)
    employee = make_test_user(username="ts_emp_a", role="EMPLOYEE")
    result = _search(employee, "Zorglub")
    assert result["total_count"] == 0 and "alert_raised" not in result
    db.expire_all()
    assert db.query(Alert).filter(Alert.user_id == employee.id).count() == 0


# --- hybrid search_documents (lexical + vector) ----------------------------------------------------


def test_chunk_containing_the_name_outranks_a_closer_vector(make_doc, make_test_user, make_test_chunk, monkeypatch):
    named = make_doc(title="Annuaire", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, "x")])
    other = make_doc(title="Charte", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, "y")])
    secret = make_doc(title="Primes", department="FINANCE", confidentiality="SECRET", pages=[(1, None, 1, "z")])
    make_test_chunk(document_id=named.id, chunk_text="Contact : Tarek Ben Ali, service RH.", embedding=hot_vector(1))
    make_test_chunk(document_id=other.id, chunk_text="Valeurs et engagements de l'entreprise.", embedding=hot_vector(0))
    make_test_chunk(document_id=secret.id, chunk_text="Prime de Tarek.", embedding=hot_vector(0))
    employee = make_test_user(username="ts_hyb", role="EMPLOYEE")
    # The query embedding is identical to the chunk WITHOUT the name.
    monkeypatch.setattr("app.rag.retriever.embed_text", lambda query: hot_vector(0))

    result = mcp_server._search_documents_impl(str(employee.id), "Tarek", 5)

    titles = [r["title"] for r in result["results"]]
    assert titles[0] == "Annuaire"
    assert "Primes" not in titles  # lexical candidates are ACL-filtered in SQL too


def test_conceptual_question_keeps_the_vector_ranking(make_doc, make_test_user, make_test_chunk, monkeypatch):
    conges = make_doc(title="Politique de congés", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, "a")])
    other = make_doc(title="Charte", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, "b")])
    make_test_chunk(document_id=conges.id, chunk_text="Chaque salarié dispose de 25 jours de repos annuels.", embedding=hot_vector(0))
    make_test_chunk(document_id=other.id, chunk_text="Valeurs de l'entreprise.", embedding=hot_vector(1))
    employee = make_test_user(username="ts_sem", role="EMPLOYEE")
    monkeypatch.setattr("app.rag.retriever.embed_text", lambda query: hot_vector(0))

    result = mcp_server._search_documents_impl(str(employee.id), "Quelle est la politique de congés ?", 5)

    assert result["results"][0]["title"] == "Politique de congés"
    assert result["results"][0]["score"] == 1.0  # no query word in any chunk: pure vector score


# --- the four original MCP tools, called directly as different users ---------------------------


def test_get_document_refuses_a_forbidden_document_like_a_missing_one(make_doc, make_test_user, db):
    docs = _seed(make_doc, db)
    hr = make_test_user(username="ts_get_hr", role="HR")
    director = make_test_user(username="ts_get_dir", role="DIRECTOR")

    refused = mcp_server._get_document_impl(str(hr.id), str(docs["secret"].id))
    assert public_tool_result(refused) == {"error": mcp_server.DOCUMENT_NOT_FOUND_OR_DENIED}
    assert refused["code"] == "ACCESS_DENIED"
    assert "5000" in mcp_server._get_document_impl(str(director.id), str(docs["secret"].id))["content"]
    db.expire_all()
    denied = db.query(AuditLog).filter(AuditLog.user_id == hr.id, AuditLog.action == "MCP_GET_DOCUMENT").one()
    assert denied.decision == "DENY"


def test_search_database_is_role_gated_and_row_filtered(make_test_user, db):
    db.add_all(
        [
            FinancialRecord(label="Budget RH 2025", department="FINANCE", confidentiality="CONFIDENTIAL",
                            confidentiality_rank=CONFIDENTIALITY_RANK["CONFIDENTIAL"], amount=180000, year=2025),
            FinancialRecord(label="Salaire direction 2025", department="FINANCE", confidentiality="SECRET",
                            confidentiality_rank=CONFIDENTIALITY_RANK["SECRET"], amount=145000, year=2025),
        ]
    )
    db.commit()
    try:
        hr = make_test_user(username="ts_db_hr", role="HR")
        accountant = make_test_user(username="ts_db_acc", role="ACCOUNTANT")
        director = make_test_user(username="ts_db_dir", role="DIRECTOR")

        assert mcp_server._search_database_impl(str(hr.id), "budget")["code"] == "ACCESS_DENIED"
        assert {r["label"] for r in mcp_server._search_database_impl(str(accountant.id), "")["results"]} == {"Budget RH 2025"}
        assert {r["label"] for r in mcp_server._search_database_impl(str(director.id), "")["results"]} == {
            "Budget RH 2025", "Salaire direction 2025",
        }
    finally:
        db.query(FinancialRecord).delete()  # not covered by the conftest TRUNCATE
        db.commit()


def test_get_company_information_only_returns_general_documents(make_doc, make_test_user, db):
    _seed(make_doc, db)
    director = make_test_user(username="ts_ci_dir", role="DIRECTOR")
    result = mcp_server._get_company_information_impl(str(director.id), "Tarek")
    assert {r["title"] for r in result["results"]} == {"Compte rendu équipe", "Rapport historique"}


def test_mcp_tools_are_registered_with_fastmcp():
    import anyio

    names = {tool.name for tool in anyio.run(mcp_server.mcp_app.list_tools)}
    assert {"search_documents", "get_document", "search_database", "get_company_information", "search_keyword"} <= names


# --- direct access by UUID -------------------------------------------------------------------------


def test_direct_access_to_a_forbidden_document_is_403(make_doc, make_test_user, db):
    docs = _seed(make_doc, db)
    hr = make_test_user(username="ts_rest_hr", role="HR")
    test_app = FastAPI()
    test_app.include_router(documents_router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: hr
    client = TestClient(test_app)

    assert client.get(f"/api/documents/{docs['secret'].id}").status_code == 403
    assert client.get(f"/api/documents/{docs['general'].id}").status_code == 200
