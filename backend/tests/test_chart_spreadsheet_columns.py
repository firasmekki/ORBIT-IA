"""Unit tests for resolve_spreadsheet_columns - the grounded column
resolver behind _run_chart_from_spreadsheet (app/agent/orchestrator.py).

Headers/rows below are exactly what the real
Orbitia_Test_Entreprise_Complet.xlsx contains (verified directly against
the ingested file via openpyxl, not guessed) - these are the CAS 1-4
scenarios from the bug report, at the pure-function level (no DB/MCP).
"""

import pytest

from app.agent.chart import ChartValidationError, extract_unit, resolve_spreadsheet_columns, strip_parenthetical

CA_MENSUEL_HEADERS = ["Mois", "Chiffre d'affaires (TND)", "Commandes", "Clients actifs"]
CA_MENSUEL_ROWS = [
    {"Mois": "Janvier", "Chiffre d'affaires (TND)": 420000, "Commandes": 820, "Clients actifs": 610},
    {"Mois": "Décembre", "Chiffre d'affaires (TND)": 610000, "Commandes": 1170, "Clients actifs": 812},
]

COMPARAISON_HEADERS = ["Mois", "CA 2025 (TND)", "CA 2026 (TND)", "Commandes 2025", "Commandes 2026"]
COMPARAISON_ROWS = [
    {"Mois": "Janvier", "CA 2025 (TND)": 390000, "CA 2026 (TND)": 420000, "Commandes 2025": 760, "Commandes 2026": 820},
]

VENTES_HEADERS = ["Produit", "Ventes (TND)", "Unités vendues", "Marge (%)"]
VENTES_ROWS = [{"Produit": "Produit A", "Ventes (TND)": 185000, "Unités vendues": 1240, "Marge (%)": 24.5}]

MARKETING_HEADERS = ["Mois", "Dépenses marketing (TND)", "Leads", "CA attribué (TND)"]
MARKETING_ROWS = [{"Mois": "Janvier", "Dépenses marketing (TND)": 18000, "Leads": 420, "CA attribué (TND)": 82000}]


def test_cas1_ca_mensuel_explicit_axes():
    message = (
        "À partir du fichier Orbitia_Test_Entreprise_Complet.xlsx, feuille CA mensuel, affiche une vraie courbe "
        "interactive du chiffre d'affaires de janvier à décembre. Utilise uniquement la colonne Mois pour l'axe X "
        "et Chiffre d'affaires (TND) pour l'axe Y."
    )
    category, values = resolve_spreadsheet_columns(CA_MENSUEL_HEADERS, CA_MENSUEL_ROWS, message)
    assert category == "Mois"
    assert values == ["Chiffre d'affaires (TND)"]


def test_cas2_comparaison_annuelle_two_series_stripped_unit_match():
    message = "À partir de Orbitia_Test_Entreprise_Complet.xlsx, feuille Comparaison annuelle, compare CA 2025 et CA 2026."
    category, values = resolve_spreadsheet_columns(COMPARAISON_HEADERS, COMPARAISON_ROWS, message)
    assert category == "Mois"  # not literally named, but the sheet's own natural x-axis
    assert values == ["CA 2025 (TND)", "CA 2026 (TND)"]


def test_cas3_ventes_par_produit_no_ca_mensuel_leak():
    message = "À partir de Orbitia_Test_Entreprise_Complet.xlsx, feuille Ventes par produit, affiche les ventes par produit."
    category, values = resolve_spreadsheet_columns(VENTES_HEADERS, VENTES_ROWS, message)
    assert category == "Produit"
    assert values == ["Ventes (TND)"]
    assert "Mois" not in (category, *values)
    assert "Chiffre d'affaires (TND)" not in values


def test_cas4_marketing_leads_par_mois():
    message = "À partir de Orbitia_Test_Entreprise_Complet.xlsx, feuille Marketing, affiche les leads par mois."
    category, values = resolve_spreadsheet_columns(MARKETING_HEADERS, MARKETING_ROWS, message)
    assert category == "Mois"
    assert values == ["Leads"]
    assert "Dépenses marketing (TND)" not in values  # mentioned sheet name "Marketing" must not bleed into this header


def test_explicit_colonne_not_found_is_a_hard_error():
    with pytest.raises(ChartValidationError, match="introuvable"):
        resolve_spreadsheet_columns(CA_MENSUEL_HEADERS, CA_MENSUEL_ROWS, "Utilise la colonne Bénéfice net pour l'axe Y.")


def test_no_columns_mentioned_defaults_to_first_column_and_numeric_rest():
    category, values = resolve_spreadsheet_columns(
        VENTES_HEADERS, VENTES_ROWS, "Affiche un graphique de cette feuille."
    )
    assert category == "Produit"
    assert set(values) == {"Ventes (TND)", "Unités vendues", "Marge (%)"}


def test_no_headers_raises():
    with pytest.raises(ChartValidationError, match="aucune colonne"):
        resolve_spreadsheet_columns([], [], "peu importe")


def test_no_numeric_column_raises():
    headers = ["Nom"]
    rows = [{"Nom": "a"}]
    with pytest.raises(ChartValidationError, match="aucune colonne de valeurs"):
        resolve_spreadsheet_columns(headers, rows, "Nom: valeur")


def test_anti_melange_three_sheets_yield_three_different_datasets():
    m1 = "feuille CA mensuel, affiche un graphique"
    m2 = "feuille Comparaison annuelle, affiche un graphique"
    m3 = "feuille Marketing, affiche un graphique"
    r1 = resolve_spreadsheet_columns(CA_MENSUEL_HEADERS, CA_MENSUEL_ROWS, m1)
    r2 = resolve_spreadsheet_columns(COMPARAISON_HEADERS, COMPARAISON_ROWS, m2)
    r3 = resolve_spreadsheet_columns(MARKETING_HEADERS, MARKETING_ROWS, m3)
    assert r1 != r2
    assert r2 != r3
    assert r1 != r3


def test_strip_parenthetical_and_extract_unit():
    assert strip_parenthetical("Chiffre d'affaires (TND)") == "Chiffre d'affaires"
    assert extract_unit("Chiffre d'affaires (TND)") == "TND"
    assert strip_parenthetical("Mois") == "Mois"
    assert extract_unit("Mois") is None
