"""Orchestrator-level tests for the spreadsheet-file chart priority source
(_run_chart_from_spreadsheet), reproducing the exact bug report: a user
naming a specific file+sheet must always get that sheet's real data, never
a silent fallback to search_database's generic seeded rows. call_tool is
monkeypatched to a fake read_spreadsheet_data/generate_chart pair backed
by the REAL Orbitia_Test_Entreprise_Complet.xlsx headers/values (verified
against the actual ingested file, not invented)."""

import logging
from contextlib import asynccontextmanager

import app.agent.orchestrator as orchestrator
from app.policy.rules import Grant

CA_MENSUEL = {
    "document_id": "doc-1", "title": "Orbitia_Test_Entreprise_Complet",
    "source_filename": "Orbitia_Test_Entreprise_Complet.xlsx", "department": "GENERAL", "confidentiality": "INTERNAL",
    "sheet": "CA mensuel",
    "columns": ["Mois", "Chiffre d'affaires (TND)", "Commandes", "Clients actifs"],
    "rows": [
        {"Mois": "Janvier", "Chiffre d'affaires (TND)": 420000, "Commandes": 820, "Clients actifs": 610},
        {"Mois": "Décembre", "Chiffre d'affaires (TND)": 610000, "Commandes": 1170, "Clients actifs": 812},
    ],
    "row_count": 2,
}

COMPARAISON = {
    "document_id": "doc-1", "title": "Orbitia_Test_Entreprise_Complet",
    "source_filename": "Orbitia_Test_Entreprise_Complet.xlsx", "department": "GENERAL", "confidentiality": "INTERNAL",
    "sheet": "Comparaison annuelle",
    "columns": ["Mois", "CA 2025 (TND)", "CA 2026 (TND)", "Commandes 2025", "Commandes 2026"],
    "rows": [{"Mois": "Janvier", "CA 2025 (TND)": 390000, "CA 2026 (TND)": 420000, "Commandes 2025": 760, "Commandes 2026": 820}],
    "row_count": 1,
}

VENTES = {
    "document_id": "doc-1", "title": "Orbitia_Test_Entreprise_Complet",
    "source_filename": "Orbitia_Test_Entreprise_Complet.xlsx", "department": "GENERAL", "confidentiality": "INTERNAL",
    "sheet": "Ventes par produit",
    "columns": ["Produit", "Ventes (TND)", "Unités vendues", "Marge (%)"],
    "rows": [{"Produit": "Produit A", "Ventes (TND)": 185000, "Unités vendues": 1240, "Marge (%)": 24.5}],
    "row_count": 1,
}

MARKETING = {
    "document_id": "doc-1", "title": "Orbitia_Test_Entreprise_Complet",
    "source_filename": "Orbitia_Test_Entreprise_Complet.xlsx", "department": "GENERAL", "confidentiality": "INTERNAL",
    "sheet": "Marketing",
    "columns": ["Mois", "Dépenses marketing (TND)", "Leads", "CA attribué (TND)"],
    "rows": [{"Mois": "Janvier", "Dépenses marketing (TND)": 18000, "Leads": 420, "CA attribué (TND)": 82000}],
    "row_count": 1,
}


def _grant():
    return Grant(
        role="DIRECTOR", departments=frozenset({"GENERAL", "FINANCE"}), max_level="SECRET", max_level_rank=4,
        tools=frozenset({"generate_chart", "read_spreadsheet_data", "search_database", "search_documents", "get_document_section"}),
    )


@asynccontextmanager
async def _fake_session(token):
    yield object()


def _install_session(monkeypatch):
    monkeypatch.setattr(orchestrator, "open_mcp_session", _fake_session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")


async def _fail_llm(*a, **k):
    raise AssertionError("spreadsheet chart requests must never reach the LLM (Voie A)")


async def test_cas1_ca_mensuel_never_mixes_generic_budget_data(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)
    captured = {}

    async def fake_call_tool(session, name, arguments):
        if name == "read_spreadsheet_data":
            assert arguments["document_name"] == "Orbitia_Test_Entreprise_Complet.xlsx"
            assert arguments["sheet_name"] == "CA mensuel"
            return CA_MENSUEL
        if name == "search_database":
            raise AssertionError("must never fall back to search_database when a file/sheet was explicitly named")
        assert name == "generate_chart"
        captured["arguments"] = arguments
        return {"chart": {**arguments}}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(),
        message=(
            "À partir du fichier Orbitia_Test_Entreprise_Complet.xlsx, feuille CA mensuel, affiche une vraie "
            "courbe interactive du chiffre d'affaires de janvier à décembre. Utilise uniquement la colonne Mois "
            "pour l'axe X et Chiffre d'affaires (TND) pour l'axe Y."
        ),
        history=[],
    )

    assert captured["arguments"]["category_field"] == "Mois"
    assert captured["arguments"]["value_fields"] == [{"field": "Chiffre d'affaires (TND)", "label": "Chiffre d'affaires", "unit": "TND"}]
    assert captured["arguments"]["data"] == [
        {"Mois": "Janvier", "Chiffre d'affaires (TND)": 420000},
        {"Mois": "Décembre", "Chiffre d'affaires (TND)": 610000},
    ]
    assert captured["arguments"]["chart_type"] == "line"
    # never any trace of the generic FinancialRecord seed data
    for row in captured["arguments"]["data"]:
        assert "Budget RH" not in str(row)
        assert "Résultat net" not in str(row)
    assert result.chart is not None


async def test_cas2_comparaison_annuelle_two_series(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)
    captured = {}

    async def fake_call_tool(session, name, arguments):
        if name == "read_spreadsheet_data":
            assert arguments["sheet_name"] == "Comparaison annuelle"
            return COMPARAISON
        assert name == "generate_chart"
        captured["arguments"] = arguments
        return {"chart": {**arguments}}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(),
        message="À partir de Orbitia_Test_Entreprise_Complet.xlsx, feuille Comparaison annuelle, compare CA 2025 et CA 2026.",
        history=[],
    )

    field_names = [vf["field"] for vf in captured["arguments"]["value_fields"]]
    assert field_names == ["CA 2025 (TND)", "CA 2026 (TND)"]
    assert len(captured["arguments"]["value_fields"]) == 2


async def test_cas3_ventes_par_produit_isolated(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)
    captured = {}

    async def fake_call_tool(session, name, arguments):
        if name == "read_spreadsheet_data":
            assert arguments["sheet_name"] == "Ventes par produit"
            return VENTES
        assert name == "generate_chart"
        captured["arguments"] = arguments
        return {"chart": {**arguments}}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(),
        message="À partir de Orbitia_Test_Entreprise_Complet.xlsx, feuille Ventes par produit, affiche les ventes par produit.",
        history=[],
    )

    assert captured["arguments"]["category_field"] == "Produit"
    assert [vf["field"] for vf in captured["arguments"]["value_fields"]] == ["Ventes (TND)"]
    for row in captured["arguments"]["data"]:
        assert "Mois" not in row  # no CA mensuel data mixed in


async def test_cas4_marketing_leads_par_mois(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)
    captured = {}

    async def fake_call_tool(session, name, arguments):
        if name == "read_spreadsheet_data":
            assert arguments["sheet_name"] == "Marketing"
            return MARKETING
        assert name == "generate_chart"
        captured["arguments"] = arguments
        return {"chart": {**arguments}}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(),
        message="À partir de Orbitia_Test_Entreprise_Complet.xlsx, feuille Marketing, affiche les leads par mois.",
        history=[],
    )

    assert captured["arguments"]["category_field"] == "Mois"
    assert [vf["field"] for vf in captured["arguments"]["value_fields"]] == ["Leads"]
    for row in captured["arguments"]["data"]:
        assert "Dépenses marketing (TND)" not in row


async def test_file_not_found_no_fallback_to_search_database(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    async def fake_call_tool(session, name, arguments):
        if name == "read_spreadsheet_data":
            return {"error": "document introuvable ou accès non autorisé à cette section", "error_kind": "file_not_found"}
        raise AssertionError(f"must not call {name} after a file-not-found - no fallback allowed")

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(),
        message="Utilise FichierInexistant.xlsx, feuille CA mensuel, affiche un graphique.",
        history=[],
    )

    assert result.chart is None
    assert "FichierInexistant.xlsx" in result.answer
    assert "n'a pas été trouvé" in result.answer


async def test_sheet_not_found_no_fallback(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    async def fake_call_tool(session, name, arguments):
        if name == "read_spreadsheet_data":
            return {
                "error": "feuille « Feuille Fantome » introuvable dans « Orbitia_Test_Entreprise_Complet ». Feuilles disponibles : CA mensuel, Marketing",
                "error_kind": "sheet_not_found",
            }
        raise AssertionError(f"must not call {name} after a sheet-not-found - no fallback allowed")

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(),
        message="Utilise Orbitia_Test_Entreprise_Complet.xlsx, feuille Feuille Fantome, affiche un graphique.",
        history=[],
    )

    assert result.chart is None
    # the sheet-specific MCP error message, not the generic file-not-found wording
    assert "Feuille Fantome" in result.answer
    assert "introuvable" in result.answer


async def test_tool_returning_wrong_file_is_rejected_by_orchestrator_validation(monkeypatch):
    """Even if read_spreadsheet_data itself somehow resolved to the wrong
    document (bug, stale session, anything), the orchestrator's own
    requested-vs-resolved check must refuse to build a chart from it."""
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    wrong_file_result = {**CA_MENSUEL, "title": "Autre Fichier Sans Rapport", "source_filename": "autre.xlsx"}

    async def fake_call_tool(session, name, arguments):
        if name == "read_spreadsheet_data":
            return wrong_file_result
        raise AssertionError("generate_chart must never be called when file validation fails")

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(),
        message="Utilise Orbitia_Test_Entreprise_Complet.xlsx, feuille CA mensuel, affiche un graphique.",
        history=[],
    )

    assert result.chart is None
    assert "n'a pas été trouvé" in result.answer


async def test_explicit_unknown_column_is_explicit_error_not_generic_data(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    async def fake_call_tool(session, name, arguments):
        if name == "read_spreadsheet_data":
            return CA_MENSUEL
        raise AssertionError("generate_chart must never be called with a fabricated column")

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    result = await orchestrator.run_agent_turn(
        user_id="u1", grant=_grant(),
        message="Utilise Orbitia_Test_Entreprise_Complet.xlsx, feuille CA mensuel, colonne Bénéfice net, affiche un graphique.",
        history=[],
    )

    assert result.chart is None
    assert "Impossible de construire ce graphique" in result.answer


async def test_anti_melange_three_sequential_sheets_yield_three_different_charts(monkeypatch):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)
    responses = {"CA mensuel": CA_MENSUEL, "Comparaison annuelle": COMPARAISON, "Marketing": MARKETING}
    captured_data = []

    async def fake_call_tool(session, name, arguments):
        if name == "read_spreadsheet_data":
            return responses[arguments["sheet_name"]]
        assert name == "generate_chart"
        captured_data.append(arguments["data"])
        return {"chart": {**arguments}}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    for sheet in ("CA mensuel", "Comparaison annuelle", "Marketing"):
        await orchestrator.run_agent_turn(
            user_id="u1", grant=_grant(),
            message=f"Utilise Orbitia_Test_Entreprise_Complet.xlsx, feuille {sheet}, affiche un graphique.",
            history=[],
        )

    assert len(captured_data) == 3
    assert captured_data[0] != captured_data[1]
    assert captured_data[1] != captured_data[2]
    assert captured_data[0] != captured_data[2]


async def test_diagnostic_log_emitted_before_generate_chart(monkeypatch, caplog):
    _install_session(monkeypatch)
    monkeypatch.setattr(orchestrator, "chat_completion", _fail_llm)

    async def fake_call_tool(session, name, arguments):
        if name == "read_spreadsheet_data":
            return CA_MENSUEL
        return {"chart": {**arguments}}

    monkeypatch.setattr(orchestrator, "call_tool", fake_call_tool)

    with caplog.at_level(logging.INFO, logger="orbitia.chart"):
        await orchestrator.run_agent_turn(
            user_id="u1", grant=_grant(),
            message="Utilise Orbitia_Test_Entreprise_Complet.xlsx, feuille CA mensuel, affiche un graphique.",
            history=[],
        )

    messages = "\n".join(r.message for r in caplog.records)
    assert "REQUESTED FILE" in messages
    assert "RESOLVED SHEET: CA mensuel" in messages
    assert "ROWS FOUND: 2" in messages
