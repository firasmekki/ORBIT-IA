"""Regression: "le budget du projet" answered with the company's
"Budget IT / Infrastructure 2025 = 310 000 €" (a line of financial_records,
found because "tu" matched "Infras-tu-cture"), and a Google request answered
as if something had been searched. Every plotted value must now carry a
verifiable source; project figures come from the documents only."""

import asyncio
from contextlib import asynccontextmanager

import pytest

import app.agent.orchestrator as orchestrator
from app.agent.document_metrics import (
    extract_amounts,
    is_document_metric_request,
    mentions_from_search_rows,
    is_web_request,
    plan_document_metric,
)
from app.mcp import server as mcp_server
from app.models.document import CONFIDENTIALITY_RANK
from app.models.finance import FinancialRecord
from app.policy.rules import Grant

ORIGINAL_QUESTION = (
    "Trace une courbe de l’évolution du budget du projet par année, à partir des données disponibles dans mes documents."
)
GOOGLE_QUESTION = "tu peut gérré des information dapres google ?"
COMPANY_RECORDS = [
    {"label": "Budget IT / Infrastructure 2025", "amount": 310000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"},
    {"label": "Budget Marketing 2025", "amount": 95000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"},
    {"label": "Budget RH 2025", "amount": 180000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"},
]


def _row(doc_id, path, page, excerpt):
    return {"document_id": doc_id, "title": path.rsplit("/", 1)[-1], "relative_path": path, "page": page, "excerpt": excerpt}


# --- detection ---------------------------------------------------------------------------------------------


def test_project_and_document_requests_are_recognised():
    assert is_document_metric_request(ORIGINAL_QUESTION)
    assert is_document_metric_request("Quel est le budget du projet ?")
    assert not is_document_metric_request("Donne-moi une courbe du budget IT")
    assert not is_document_metric_request("Quel est le budget IT 2025 ?")


def test_explicit_web_requests_are_recognised():
    assert is_web_request(GOOGLE_QUESTION)
    assert is_web_request("Cherche sur internet le montant du marché")
    assert is_web_request("Selon Google, quel est le budget ?")
    assert not is_web_request("Le CCTP prévoit-il un accès internet sur site ?")
    assert not is_web_request(ORIGINAL_QUESTION)


# --- extraction with provenance ----------------------------------------------------------------------------


def test_amounts_are_read_with_currency_year_and_their_sentence():
    found = extract_amounts(
        "Le budget prévisionnel 2024 est de 1.200.000 DT. Pour 2025, le montant est porté à 2,5 millions d’euros. "
        "La salle mesure 1200 m2."
    )
    assert [(m.value, m.currency, m.year) for m in found] == [(1_200_000.0, "DT", "2024"), (2_500_000.0, "€", "2025")]
    assert found[0].sentence.startswith("Le budget prévisionnel 2024")


def test_two_dated_values_of_one_document_make_a_sourced_curve():
    outcome = plan_document_metric(
        mentions_from_search_rows([_row("d1", "projets/SFBT/offre financière.pdf", 3,
              "Le budget du projet est de 400 000 DT en 2024. Le budget 2025 est de 520 000 DT.")]),
        "projets",
    )
    assert outcome.kind == "chart"
    assert [(p.period, p.value) for p in outcome.points] == [("2024", 400000.0), ("2025", 520000.0)]
    assert all(p.source["kind"] == "local" and p.source["path"] == "projets/SFBT/offre financière.pdf"
               and p.source["page"] == 3 and p.source["document_id"] == "d1" and p.source["excerpt"]
               for p in outcome.points)


def test_a_single_dated_value_is_a_value_card_not_an_evolution():
    outcome = plan_document_metric(mentions_from_search_rows([_row("d1", "BIC/devis.pdf", 2, "Montant total 2025 : 85 000 DT.")]), "projets")
    assert outcome.kind == "kpi" and len(outcome.points) == 1
    assert "au moins deux années comparables" in outcome.message


def test_values_from_another_project_are_never_put_on_the_same_curve():
    outcome = plan_document_metric(
        mentions_from_search_rows([
            _row("d1", "Attijari/offre.pdf", 1, "Le budget du projet 2024 est de 300 000 DT."),
            _row("d2", "KSA/offre.pdf", 4, "Le budget du projet 2025 est de 900 000 DT."),
        ]),
        "projets",
    )
    assert outcome.kind == "candidates" and outcome.points == []
    assert "documents différents" in outcome.message and "Attijari/offre.pdf" in outcome.message
    assert "KSA/offre.pdf" in outcome.message


def test_no_verifiable_amount_means_no_chart_and_no_company_figure():
    outcome = plan_document_metric(
        mentions_from_search_rows([_row("d1", "noveau consultation/CCTP.pdf", 5, "Le système de gestion de l'énergie couvre 12 sites.")]),
        "projets › noveau consultation", documents_in_scope=418, pages_read=0,
    )
    assert outcome.kind == "none" and outcome.points == []
    assert "J'ai examiné les 418 documents de « projets › noveau consultation »" in outcome.message
    assert "Aucun graphique fiable" in outcome.message and "310" not in outcome.message


def test_undated_amounts_are_listed_with_their_source_but_never_plotted():
    outcome = plan_document_metric(
        mentions_from_search_rows([_row("d2", "noveau consultation/DAO.pdf", 2, "Le montant de la caution est de 5 000 DT.")]),
        "projets › noveau consultation",
    )
    assert outcome.kind == "undated" and outcome.points == []
    assert "aucun n'est associé à une année" in outcome.message
    assert "| 5 000 DT | noveau consultation/DAO.pdf | 2 |" in outcome.message


# --- provenance contract -------------------------------------------------------------------------------------


def test_a_value_without_source_is_rejected():
    with pytest.raises(orchestrator.MissingProvenance):
        orchestrator._chart_contract(
            chart_type="line", metrics=[], periods=["2024", "2025"], categories=[], sources=[], rationale="",
            points=[{"period": "2024", "value": 1.0, "source": {"kind": "local", "path": "a.pdf"}},
                    {"period": "2025", "value": 2.0}],
        )
    with pytest.raises(orchestrator.MissingProvenance):  # a web value needs its URL and consultation date
        orchestrator._require_provenance([{"value": 1, "source": {"kind": "web", "url": "https://x.example"}}])


def test_local_and_web_sources_stay_distinct():
    local = orchestrator._source_text({"kind": "local", "path": "BIC/offre.pdf", "page": 3})
    web = orchestrator._source_text(
        {"kind": "web", "title": "Avis de marché", "url": "https://marches.example/123", "accessed_at": "2026-10-12"}
    )
    table = orchestrator._source_text({"kind": "financial_table", "record": "Budget IT / Infrastructure 2025"})
    assert local == "Document local : BIC/offre.pdf, page 3"
    assert web == "Web : Avis de marché (https://marches.example/123, consulté le 2026-10-12)"
    assert table.startswith("base de données financière") and "Budget IT / Infrastructure 2025" in table


# --- end to end through run_agent_turn (tools faked, same routing as the UI) -----------------------------------


def _director():
    return Grant(
        role="DIRECTOR", departments=frozenset({"FINANCE", "GENERAL"}), max_level="SECRET", max_level_rank=3,
        tools=frozenset({"generate_chart", "search_database", "search_documents"}),
    )


def _install(monkeypatch, *, documents):
    calls = []

    @asynccontextmanager
    async def session(token):
        yield object()

    async def call_tool(_session, name, arguments):
        calls.append((name, arguments))
        if name == "find_document_amounts":
            return {"mentions": mentions_from_search_rows(documents), "documents_in_scope": 7, "pages_read": len(documents)}
        if name == "search_database":
            return {"results": COMPANY_RECORDS}
        if name == "generate_chart":
            return {"chart": {**arguments}}
        raise AssertionError(name)

    async def no_llm(*a, **k):
        raise AssertionError("no model call is expected on this path")

    monkeypatch.setattr(orchestrator, "open_mcp_session", session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", no_llm)
    monkeypatch.setattr(orchestrator, "complete_json", no_llm)
    return calls


def _turn(message, **kwargs):
    return asyncio.run(orchestrator.run_agent_turn(
        user_id="u1", grant=_director(), message=message, history=[], workspace_name="projets", **kwargs
    ))


def test_project_budget_never_offers_or_uses_the_company_budgets(monkeypatch):
    calls = _install(monkeypatch, documents=[
        _row("d1", "noveau consultation/CCTP système de gestion de l'énergie.pdf", 4, "Le système couvre 12 sites."),
    ])
    result = _turn(ORIGINAL_QUESTION)
    assert [name for name, _ in calls] == ["find_document_amounts"]  # the company table is not consulted
    assert result.clarification is None and result.chart is None
    for forbidden in ("Budget IT", "Budget Marketing", "Budget RH", "310 000", "310000"):
        assert forbidden not in result.answer
    assert "Aucun graphique fiable" in result.answer


def test_project_budget_curve_from_one_document_keeps_every_source(monkeypatch):
    _install(monkeypatch, documents=[
        _row("d1", "SFBT SFAX/offre financière.pdf", 2, "Le budget du projet est de 400 000 DT en 2024."),
        _row("d1", "SFBT SFAX/offre financière.pdf", 3, "En 2025, le budget est porté à 520 000 DT."),
    ])
    result = _turn(ORIGINAL_QUESTION)
    assert result.chart["chart_type"] == "line"
    points = result.chart["contract"]["points"]
    assert [(p["period"], p["value"]) for p in points] == [("2024", 400000.0), ("2025", 520000.0)]
    assert [p["source"]["page"] for p in points] == [2, 3]
    assert all(p["source"]["kind"] == "local" for p in points)
    assert [s["relative_path"] for s in result.sources] == ["SFBT SFAX/offre financière.pdf"]


def test_google_request_runs_no_tool_and_says_so(monkeypatch):
    calls = _install(monkeypatch, documents=[])
    result = _turn(GOOGLE_QUESTION)
    assert calls == []  # nothing searched, nothing pretended
    assert result.tool_trace == []
    assert "aucune recherche sur Internet" in result.answer and "désactivée" in result.answer
    assert "310" not in result.answer


def test_explicit_company_financial_request_still_works(monkeypatch):
    calls = _install(monkeypatch, documents=[])
    result = _turn("Donne-moi une courbe du budget IT")
    assert [name for name, _ in calls] == ["search_database"]
    assert result.chart["chart_type"] == "kpi" and result.chart["data"][0]["value"] == 310000.0
    source = result.chart["contract"]["points"][0]["source"]
    assert source == {"kind": "financial_table", "title": "Table financière de l'entreprise",
                      "record": "Budget IT / Infrastructure 2025"}


def test_project_question_does_not_inject_the_company_table(monkeypatch):
    async def call_tool(*a, **k):
        raise AssertionError("search_database must not run for a project question")

    monkeypatch.setattr(orchestrator, "call_tool", call_tool)
    assert asyncio.run(orchestrator._ground_with_database(None, "Quel est le budget du projet ?", [], [])) is None


# --- search_database: whole words, no arbitrary record, permissions kept ----------------------------------------


@pytest.fixture
def company_records(db):
    db.add_all([
        FinancialRecord(label="Budget IT / Infrastructure 2025", department="FINANCE", confidentiality="CONFIDENTIAL",
                        confidentiality_rank=CONFIDENTIALITY_RANK["CONFIDENTIAL"], amount=310000, year=2025),
        FinancialRecord(label="Budget RH 2025", department="FINANCE", confidentiality="CONFIDENTIAL",
                        confidentiality_rank=CONFIDENTIALITY_RANK["CONFIDENTIAL"], amount=180000, year=2025),
        FinancialRecord(label="Résultat net 2025 (prévisionnel)", department="FINANCE", confidentiality="SECRET",
                        confidentiality_rank=CONFIDENTIALITY_RANK["SECRET"], amount=620000, year=2025),
    ])
    db.commit()
    yield
    db.query(FinancialRecord).delete()  # test database only; not covered by the conftest TRUNCATE
    db.commit()


def _labels(user, query):
    return [r["label"] for r in mcp_server._search_database_impl(str(user.id), query)["results"]]


def test_search_database_never_matches_inside_a_word(db, make_test_user, company_records):
    director = make_test_user(username="prov_dir", role="DIRECTOR")
    assert _labels(director, GOOGLE_QUESTION) == []  # "tu" is not "Infras-tu-cture"
    assert _labels(director, "le budget du projet") == []  # generic words only: no arbitrary record
    assert _labels(director, ORIGINAL_QUESTION) == []


def test_search_database_still_finds_explicit_financial_lines(db, make_test_user, company_records):
    director = make_test_user(username="prov_dir2", role="DIRECTOR")
    assert _labels(director, "Quel est le budget IT 2025 ?") == ["Budget IT / Infrastructure 2025"]
    assert _labels(director, "budget infrastructure") == ["Budget IT / Infrastructure 2025"]
    assert _labels(director, "resultat net") == ["Résultat net 2025 (prévisionnel)"]  # accents ignored
    assert _labels(director, "budget IT 2024") == []  # the year is a criterion, not ignored
    assert len(_labels(director, "")) == 3  # explicit empty query = the authorized catalog


def test_search_database_keeps_role_and_confidentiality_rules(db, make_test_user, company_records):
    accountant = make_test_user(username="prov_acc", role="ACCOUNTANT")
    hr = make_test_user(username="prov_hr", role="HR")
    assert _labels(accountant, "resultat net") == []  # SECRET: above the accountant's level
    assert sorted(_labels(accountant, "")) == ["Budget IT / Infrastructure 2025", "Budget RH 2025"]
    assert mcp_server._search_database_impl(str(hr.id), "budget IT")["code"] == "ACCESS_DENIED"


# --- find_document_amounts: every page of the authorized scope, sourced, no alert -------------------------


def test_find_document_amounts_reads_the_scope_only_with_sources_and_raises_no_alert(db, make_test_user, make_doc):
    from app.models.alert import Alert
    from app.models.workspace import Workspace, WorkspaceMember

    director = make_test_user(username="prov_amounts", role="DIRECTOR")
    employee = make_test_user(username="prov_amounts_emp", role="EMPLOYEE")
    ws = Workspace(name="projets", owner_id=director.id)
    db.add(ws)
    db.commit()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=employee.id))  # a member: allowed, filtered by role
    db.commit()
    page = lambda text: [(1, None, 1, "Introduction."), (2, "Offre", 1, text)]  # noqa: E731
    make_doc(title="offre.pdf", department="GENERAL", confidentiality="INTERNAL", workspace_id=ws.id,
             relative_path="SFBT/offre.pdf", pages=page("Le budget du projet est de 400 000 DT en 2024."))
    make_doc(title="offre.pdf", department="FINANCE", confidentiality="SECRET", workspace_id=ws.id,
             relative_path="KSA/offre.pdf", pages=page("Le budget du projet est de 900 000 DT en 2025."))
    make_doc(title="cctp.pdf", department="GENERAL", confidentiality="INTERNAL", workspace_id=ws.id,
             relative_path="SFBT/cctp.pdf", pages=page("Le système couvre 12 sites."))

    scoped = mcp_server._find_document_amounts_impl(str(director.id), workspace_id=str(ws.id), folder_scope=("SFBT/",))
    assert scoped["documents_in_scope"] == 2 and scoped["pages_read"] == 1
    assert [(m["relative_path"], m["page"], m["value"], m["year"]) for m in scoped["mentions"]] == [
        ("SFBT/offre.pdf", 2, 400000.0, "2024")
    ]
    assert scoped["mentions"][0]["sentence"].startswith("Le budget du projet")

    # the SECRET finance document is not readable by an employee: its amount never comes back
    hidden = mcp_server._find_document_amounts_impl(str(employee.id), workspace_id=str(ws.id))
    assert "error" not in hidden
    assert [m["relative_path"] for m in hidden["mentions"]] == ["SFBT/offre.pdf"]
    assert db.query(Alert).count() == 0  # not a user's term search: no security alert
