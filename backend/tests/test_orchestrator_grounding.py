"""Orchestrator-level grounding tests: the real incident replayed end to end
through run_agent_turn ("donner une comparaison entre l'année 2025 ET
2026" -> invented 2025/2026 figures), plus the chart and tool-catalog
guarantees. call_tool / chat_completion are monkeypatched, same pattern as
test_orchestrator_search_documents.py - no database, no LLM."""

from contextlib import asynccontextmanager

import app.agent.orchestrator as orchestrator
from app.agent.grounding import NO_DATA_ANSWER, UNAVAILABLE
from app.policy.rules import Grant

QUESTION = "donner une comparaison entre l'année 2025 ET 2026"

SEARCH_RESULT = {
    "results": [
        {
            "document_id": "doc-payroll", "title": "Budget masse salariale 2025 (synthèse)", "department": "FINANCE",
            "confidentiality": "CONFIDENTIAL", "score": 0.71, "page": None, "section": None,
            "excerpt": (
                "Synthèse budgétaire - masse salariale 2025\n\nLe budget global de la masse salariale pour 2025 "
                "s'élève à 4,1 millions d'euros, en hausse de 6% par rapport à 2024."
            ),
        },
        {
            "document_id": "doc-plan", "title": "Plan stratégique 2026", "department": "EXEC",
            "confidentiality": "SECRET", "score": 0.66, "page": None, "section": None,
            "excerpt": (
                "Plan stratégique 2026 - document de direction\n\nObjectifs : ouverture d'un bureau à Lyon, "
                "croissance du chiffre d'affaires de 20% dans les 5 prochaines années."
            ),
        },
        {
            "document_id": "doc-leave", "title": "Politique de congés", "department": "GENERAL",
            "confidentiality": "INTERNAL", "score": 0.31, "page": None, "section": None,
            "excerpt": "Les congés payés sont de 25 jours ouvrés par an.",
        },
    ]
}
DATABASE_RESULT = {
    "results": [{"label": "Masse salariale totale 2025", "amount": 4100000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"}]
}

HALLUCINATED_ANSWER = """2025 :
- Budget budgétaire : 4,1 M€
- Dépenses totales de personnel : 800 000 €
- Production : 1,2 million de tonnes

2026 :
- Budget budgétaire : 4,3 M€
- Dépenses totales de personnel : 1 000 000 €
- Production : 1,5 million de tonnes"""


def _director(tools=("search_documents", "get_document", "search_database", "generate_chart", "get_company_information")):
    return Grant(
        role="DIRECTOR",
        departments=frozenset({"HR", "FINANCE", "TECH", "GENERAL", "EXEC"}),
        max_level="SECRET",
        max_level_rank=3,
        tools=frozenset(tools),
    )


def _install(monkeypatch, *, llm_replies, tool_results=None, calls=None, captured=None):
    @asynccontextmanager
    async def fake_session(token):
        yield object()

    async def fake_call_tool(session, name, arguments):
        if calls is not None:
            calls.append((name, arguments))
        result = (tool_results or {})[name]
        return result(arguments) if callable(result) else result

    replies = iter(llm_replies)

    async def fake_llm(messages, tools=None):
        if captured is not None:
            captured.append({"messages": list(messages), "tools": tools})
        return next(replies)

    monkeypatch.setattr(orchestrator, "open_mcp_session", fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", fake_llm)


# --- the incident, end to end ---------------------------------------------------


async def test_incident_replay_invented_values_never_reach_the_user(monkeypatch):
    captured: list[dict] = []
    _install(
        monkeypatch,
        llm_replies=[{"content": HALLUCINATED_ANSWER, "tool_calls": None}],
        tool_results={"search_documents": SEARCH_RESULT, "search_database": DATABASE_RESULT},
        captured=captured,
    )

    result = await orchestrator.run_agent_turn(user_id="u1", grant=_director(), message=QUESTION, history=[])

    for invented in ("800 000", "1,2 million", "4,3 M", "1 000 000", "1,5 million"):
        assert invented not in result.answer
    # The real 4,1 M€ was presented as "Budget budgétaire" - a payroll
    # figure relabelled as something else is removed too.
    assert "4,1" not in result.answer
    assert UNAVAILABLE in result.answer
    # Nothing in the final answer came from any retrieved document.
    assert result.sources == []


async def test_comparison_request_injects_per_period_instructions(monkeypatch):
    captured: list[dict] = []
    _install(
        monkeypatch,
        llm_replies=[{"content": NO_DATA_ANSWER, "tool_calls": None}],
        tool_results={"search_documents": SEARCH_RESULT, "search_database": DATABASE_RESULT},
        captured=captured,
    )

    await orchestrator.run_agent_turn(user_id="u1", grant=_director(), message=QUESTION, history=[])

    system_texts = [m["content"] for m in captured[0]["messages"] if m["role"] == "system"]
    assert any("Indicateur | 2025 | 2026" in t and UNAVAILABLE in t for t in system_texts)
    assert "Do not invent, estimate, infer or fabricate numerical values" in system_texts[0]
    assert "Every numerical value in the answer must be traceable to a retrieved source" in system_texts[0]
    assert "Every plotted value must come from an authorized source" in system_texts[0]


async def test_faithful_comparison_is_kept_with_only_used_sources(monkeypatch):
    faithful = (
        "| Indicateur | 2025 | 2026 |\n"
        "|---|---|---|\n"
        f"| Masse salariale | 4,1 M€ | {UNAVAILABLE} |\n"
        f"| Chiffre d'affaires | {UNAVAILABLE} | objectif de croissance de 20 % sur 5 ans |\n\n"
        "Sources : Budget masse salariale 2025, Plan stratégique 2026."
    )
    _install(
        monkeypatch,
        llm_replies=[{"content": faithful, "tool_calls": None}],
        tool_results={"search_documents": SEARCH_RESULT, "search_database": DATABASE_RESULT},
    )

    result = await orchestrator.run_agent_turn(user_id="u1", grant=_director(), message=QUESTION, history=[])

    assert result.answer == faithful
    # Retrieved but unused ("Politique de congés") is not listed.
    assert sorted(s["document_id"] for s in result.sources) == ["doc-payroll", "doc-plan"]


async def test_no_authorized_excerpt_tells_model_to_answer_no_data(monkeypatch):
    captured: list[dict] = []
    _install(
        monkeypatch,
        llm_replies=[{"content": NO_DATA_ANSWER, "tool_calls": None}],
        tool_results={"search_documents": {"results": []}},
        captured=captured,
    )
    grant = _director(tools=("search_documents",))

    result = await orchestrator.run_agent_turn(user_id="u1", grant=grant, message="Quelle est la production 2026 ?", history=[])

    assert any(NO_DATA_ANSWER in m["content"] for m in captured[0]["messages"] if m["role"] == "system")
    assert result.answer == NO_DATA_ANSWER
    assert result.sources == []


async def test_document_fetched_by_the_model_but_unused_is_not_a_source(monkeypatch):
    get_document_result = {
        "document_id": "doc-leave", "title": "Politique de congés", "department": "GENERAL",
        "confidentiality": "INTERNAL", "content": "Les congés payés sont de 25 jours ouvrés par an.",
    }
    _install(
        monkeypatch,
        llm_replies=[
            {"content": "", "tool_calls": [{"function": {"name": "get_document", "arguments": {"document_id": "doc-leave"}}}]},
            {"content": "La masse salariale 2025 s'élève à 4,1 millions d'euros.", "tool_calls": None},
        ],
        tool_results={"search_documents": SEARCH_RESULT, "search_database": DATABASE_RESULT, "get_document": get_document_result},
    )

    result = await orchestrator.run_agent_turn(user_id="u1", grant=_director(), message="masse salariale 2025 ?", history=[])

    assert "4,1 millions" in result.answer
    assert [s["document_id"] for s in result.sources] == ["doc-payroll"]


# --- charts ---------------------------------------------------------------------


async def test_generate_chart_is_never_offered_to_the_free_llm_loop(monkeypatch):
    captured: list[dict] = []
    _install(
        monkeypatch,
        llm_replies=[{"content": NO_DATA_ANSWER, "tool_calls": None}],
        tool_results={"search_documents": SEARCH_RESULT, "search_database": DATABASE_RESULT},
        captured=captured,
    )

    await orchestrator.run_agent_turn(user_id="u1", grant=_director(), message=QUESTION, history=[])

    offered = {t["function"]["name"] for t in captured[0]["tools"]}
    assert "generate_chart" not in offered
    assert "search_documents" in offered


async def test_llm_generate_chart_call_with_synthetic_data_is_refused(monkeypatch):
    calls: list = []
    fabricated = {"chart_type": "bar", "title": "CA", "value_fields": [{"field": "ca"}], "data": [{"ca": 987654}]}
    _install(
        monkeypatch,
        llm_replies=[
            {"content": "", "tool_calls": [{"function": {"name": "generate_chart", "arguments": fabricated}}]},
            {"content": "Chiffre d'affaires 2026 : 987 654 €", "tool_calls": None},
        ],
        tool_results={"search_documents": SEARCH_RESULT, "search_database": DATABASE_RESULT},
        calls=calls,
    )

    result = await orchestrator.run_agent_turn(user_id="u1", grant=_director(), message=QUESTION, history=[])

    assert "generate_chart" not in [name for name, _ in calls]
    # Refused, but not a security event: a validation error, never an alert.
    assert any(t["tool"] == "generate_chart" and t["decision"] == "ERROR" and t["code"] == "VALIDATION_ERROR" for t in result.tool_trace)
    assert result.chart is None
    # The model's own fabricated data never becomes evidence.
    assert "987 654" not in result.answer


async def test_chart_with_incomplete_years_is_not_generated(monkeypatch):
    calls: list = []
    _install(
        monkeypatch,
        llm_replies=[],
        tool_results={"search_database": {"results": [
            {"label": "Budget RH 2025", "amount": 180000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"},
            {"label": "Budget IT 2025", "amount": 310000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"},
        ]}},
        calls=calls,
    )

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_director(), message="Fais un graphique comparant les budgets 2025 et 2026", history=[]
    )

    assert result.chart is None
    assert "generate_chart" not in [name for name, _ in calls]
    assert result.answer.startswith(
        "Je peux générer le graphique, mais les documents accessibles ne contiennent pas suffisamment de "
        "données 2025/2026 comparables."
    )
    assert "Années disponibles : 2025" in result.answer


async def test_chart_with_every_requested_year_plots_only_real_rows(monkeypatch):
    _install(
        monkeypatch,
        llm_replies=[],
        tool_results={
            "search_database": {"results": [
                {"label": "Budget RH 2024", "amount": 170000.0, "year": 2024, "confidentiality": "CONFIDENTIAL"},
                {"label": "Budget RH 2025", "amount": 180000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"},
                {"label": "Budget RH 2026", "amount": 195000.0, "year": 2026, "confidentiality": "CONFIDENTIAL"},
            ]},
            "generate_chart": lambda arguments: {"chart": {**arguments}},
        },
    )

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_director(), message="Fais un graphique comparant les budgets 2025 et 2026", history=[]
    )

    assert result.chart is not None
    # Only the requested periods (2024 is left out), one bar per period,
    # each row carrying the metric's id for drill-down.
    assert result.chart["data"] == [
        {"label": "2025", "amount": 180000.0, "id": "budget_rh"},
        {"label": "2026", "amount": 195000.0, "id": "budget_rh"},
    ]
    assert result.chart["drilldown"]["budget_rh"]["points"] == [
        {"period": "2024", "value": 170000.0},
        {"period": "2025", "value": 180000.0},
        {"period": "2026", "value": 195000.0},
    ]
    # Every plotted value is one of the authorized records, unchanged.
    plotted = {row["amount"] for row in result.chart["data"]}
    assert plotted <= {170000.0, 180000.0, 195000.0}


async def test_chart_for_a_single_missing_year_says_no_data(monkeypatch):
    _install(
        monkeypatch,
        llm_replies=[],
        tool_results={"search_database": {"results": [
            {"label": "Budget RH 2025", "amount": 180000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"},
        ]}},
    )

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_director(), message="Fais un graphique des budgets 2026", history=[]
    )

    assert result.chart is None
    assert "Je ne dispose pas de données 2026" in result.answer


async def test_python_plotting_code_from_the_llm_never_reaches_the_user(monkeypatch):
    python_answer = (
        "Voici le code :\n```python\nimport matplotlib.pyplot as plt\n"
        "plt.plot(['Janvier', 'Février'], [120, 135])\nplt.show()\n```"
    )
    _install(
        monkeypatch,
        llm_replies=[{"content": python_answer, "tool_calls": None}],
        tool_results={"search_documents": {"results": []}},
    )
    grant = _director(tools=("search_documents",))

    result = await orchestrator.run_agent_turn(user_id="u1", grant=grant, message="test avec deux fichiers", history=[])

    assert "```" not in result.answer
    assert "matplotlib" not in result.answer and "plt." not in result.answer
    assert "s'affichent directement dans le chat" in result.answer
