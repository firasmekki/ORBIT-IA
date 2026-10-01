"""Orchestrator-level tests for the generate_chart deterministic path
(_run_generate_chart / detect_chart_intent routing in run_agent_turn).
call_tool/chat_completion are monkeypatched - these never touch a real
database or Ollama, same pattern as test_orchestrator_search_documents.py.

Covers: per-type chart generation, the two ACL-safe data sources (inline
user-typed numbers vs. search_database), the "no data available" and
"no authorized rows" anti-hallucination paths, the honest failure mode
for a chart type the current data model can't support (scatter - only
one numeric metric exists today), and that a bare data question never
triggers this path at all.
"""

from contextlib import asynccontextmanager

import app.agent.orchestrator as orchestrator
from app.policy.rules import Grant


def _grant(role="DIRECTOR", tools=("generate_chart", "search_database", "search_documents", "get_document_section")):
    return Grant(role=role, departments=frozenset({"FINANCE", "GENERAL"}), max_level="SECRET", max_level_rank=4, tools=frozenset(tools))


def _employee_grant():
    # generate_chart is in every role's _BASE_TOOLS - EMPLOYEE never gets
    # search_database, so this is the "no data source at all" case.
    return Grant(role="EMPLOYEE", departments=frozenset({"GENERAL"}), max_level="INTERNAL", max_level_rank=1, tools=frozenset({"generate_chart"}))


@asynccontextmanager
async def _fake_session(token):
    yield object()


def _install_session(monkeypatch):
    monkeypatch.setattr(orchestrator, "open_mcp_session", _fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")


async def _fail_llm(*a, **k):
    raise AssertionError("chart requests must never reach the LLM (Voie A)")


# --- inline user-provided data ------------------------------------------


async def test_bar_chart_from_inline_data(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    captured = {}

    async def fake_call_tool(session, name, arguments):
        assert name == "generate_chart"
        captured["arguments"] = arguments
        return {"chart": {**arguments}}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(
        user_id="u1",
        grant=_grant(),
        message="Fais un histogramme : RH: 180000, Marketing: 95000",
        history=[],
    )

    assert captured["arguments"]["chart_type"] == "bar"
    assert captured["arguments"]["data"] == [{"label": "RH", "amount": 180000.0}, {"label": "Marketing", "amount": 95000.0}]
    assert captured["arguments"]["sources"] == []
    assert result.chart is not None
    assert result.chart["chart_type"] == "bar"
    assert result.reference_context is None  # nothing to "open" after a chart


async def test_pie_chart_from_inline_data(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    captured = {}

    async def fake_call_tool(session, name, arguments):
        captured["arguments"] = arguments
        return {"chart": {**arguments}}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(), message="Fais un camembert : RH: 180000, Marketing: 95000", history=[]
    )

    assert captured["arguments"]["chart_type"] == "pie"
    assert len(captured["arguments"]["value_fields"]) == 1
    assert result.chart["chart_type"] == "pie"


async def test_line_chart_from_inline_data(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    async def fake_call_tool(session, name, arguments):
        return {"chart": {**arguments}}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(), message="Fais une courbe : Janvier: 120000, Février: 135000", history=[]
    )

    assert result.chart["chart_type"] == "line"


async def test_scatter_request_fails_honestly_no_second_metric_invented(monkeypatch):
    """Today's two data sources (inline pairs, search_database rows) only
    ever produce a single numeric metric ("amount") - a scatter chart
    needs two. generate_chart's own structural validation rejects this;
    the orchestrator must surface that as a clear message, never invent a
    second series to satisfy the request."""
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    async def fake_call_tool(session, name, arguments):
        assert name == "generate_chart"
        assert len(arguments["value_fields"]) == 1
        return {"error": "un nuage de points nécessite exactement deux séries (x et y)"}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(
        user_id="u1",
        grant=_grant(),
        message="Montre la relation entre les dépenses marketing et les ventes : RH: 180000, Marketing: 95000",
        history=[],
    )

    assert result.chart is None
    assert "Impossible de générer" in result.answer
    assert "nuage de points" in result.answer


async def test_inline_unknown_value_preserved_as_none_never_zero(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)
    captured = {}

    async def fake_call_tool(session, name, arguments):
        captured["arguments"] = arguments
        return {"chart": {**arguments}}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(), message="Histogramme : Janvier: 120000, Février: inconnu", history=[]
    )

    row = captured["arguments"]["data"][1]
    assert row["label"] == "Février"
    assert row["amount"] is None  # never coerced to 0


# --- search_database fallback --------------------------------------------


async def test_falls_back_to_search_database_when_no_inline_data(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)
    calls = []

    async def fake_call_tool(session, name, arguments):
        calls.append(name)
        if name == "search_database":
            return {"results": [{"label": "Budget IT 2025", "amount": 50000.0, "year": 2025, "confidentiality": "CONFIDENTIAL"}]}
        assert name == "generate_chart"
        return {"chart": {**arguments}}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(user_id="u1", grant=_grant(), message="Fais un histogramme des budgets.", history=[])

    assert calls == ["search_database", "generate_chart"]
    assert result.chart is not None


async def test_search_database_empty_results_no_chart_no_fabrication(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    async def fake_call_tool(session, name, arguments):
        assert name == "search_database"
        return {"results": []}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(user_id="u1", grant=_grant(), message="Fais un histogramme des budgets Mars.", history=[])

    assert result.chart is None
    assert "Aucune donnée autorisée" in result.answer


async def test_search_database_denied_surfaces_as_access_denied(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    async def fake_call_tool(session, name, arguments):
        assert name == "search_database"
        return {"error": "rôle non autorisé"}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(user_id="u1", grant=_grant(), message="Fais un histogramme des budgets.", history=[])

    assert result.chart is None
    assert "Accès refusé" in result.answer
    assert result.degraded is True


# --- no data source available at all --------------------------------------


async def test_no_search_database_access_and_no_inline_data_asks_for_data(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    async def fail_call_tool(*a, **k):
        raise AssertionError("no tool should be called when there is nothing to chart")

    monkeypatch.setattr(orchestrator, "call_tool", fail_call_tool)

    result = await orchestrator.run_agent_turn(user_id="u1", grant=_employee_grant(), message="Fais-moi un graphique des ventes.", history=[])

    assert result.chart is None
    assert "pas de données" in result.answer.lower()


# --- ambiguous type auto-defaults, doesn't block on clarification --------


async def test_ambiguous_type_defaults_to_bar_when_data_is_resolvable(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)
    captured = {}

    async def fake_call_tool(session, name, arguments):
        if name == "generate_chart":
            captured["arguments"] = arguments
        return {"chart": {**arguments}} if name == "generate_chart" else {"results": [{"label": "Budget IT", "amount": 1.0, "year": 2025, "confidentiality": "CONFIDENTIAL"}]}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(user_id="u1", grant=_grant(), message="Fais un graphique des budgets.", history=[])

    assert captured["arguments"]["chart_type"] == "bar"  # sensible default, no clarification round-trip
    assert result.chart is not None


# --- no intent, no chart (regression) --------------------------------------


async def test_bare_data_question_never_triggers_chart(monkeypatch):
    _install_session(monkeypatch)

    async def fake_call_tool(session, name, arguments):
        assert name != "generate_chart"
        if name == "search_documents":
            return {"results": []}
        if name == "search_database":
            return {"results": []}
        raise AssertionError(f"unexpected tool call: {name}")

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    async def llm_reply(messages, tools):
        return {"content": "Réponse.", "tool_calls": None}

    monkeypatch.setattr(orchestrator, "chat_completion", llm_reply)

    result = await orchestrator.run_agent_turn(user_id="u1", grant=_grant(), message="Quel est le chiffre d'affaires ?", history=[])

    assert result.chart is None
