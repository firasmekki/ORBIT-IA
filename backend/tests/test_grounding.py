"""Unit tests for app/agent/grounding.py - the deterministic check that
every number in an LLM-composed answer traces back to what the model was
actually shown. Pure functions, no database, no LLM.

The fixtures reproduce the real incident: asked "donner une comparaison
entre l'année 2025 ET 2026", the model was given a 2025 payroll synthesis
and the 2026 strategic plan, and answered with a full 2025/2026 table of
budget / staff costs / production figures, almost all of them invented.
"""

from app.agent.grounding import (
    NO_DATA_ANSWER,
    REDACTION_NOTE,
    UNAVAILABLE,
    Evidence,
    is_comparison_request,
    requested_years,
    verify_answer,
)

QUESTION = "donner une comparaison entre l'année 2025 ET 2026"

PAYROLL_TEXT = (
    "Synthèse budgétaire - masse salariale 2025\n\n"
    "Le budget global de la masse salariale pour 2025 s'élève à 4,1 millions d'euros, en hausse "
    "de 6% par rapport à 2024, principalement porté par le recrutement de l'équipe technique."
)
PLAN_TEXT = (
    "Plan stratégique 2026 - document de direction\n\n"
    "Objectifs : ouverture d'un bureau à Lyon, lancement d'une offre IA d'entreprise, croissance "
    "du chiffre d'affaires de 20% dans les 5 prochaines années."
)
LEAVE_TEXT = "Les congés payés sont de 25 jours ouvrés par an, à poser avant le 31 mai."


def _evidence() -> Evidence:
    ev = Evidence()
    ev.add_user_message(QUESTION)
    ev.add(PAYROLL_TEXT, document_id="doc-payroll", title="Budget masse salariale 2025 (synthèse)")
    ev.add(PLAN_TEXT, document_id="doc-plan", title="Plan stratégique 2026")
    ev.add(LEAVE_TEXT, document_id="doc-leave", title="Politique de congés")
    return ev


def _candidates() -> dict[str, dict]:
    return {
        doc_id: {"document_id": doc_id, "title": title, "department": "X", "confidentiality": "X", "excerpt": ""}
        for doc_id, title in (
            ("doc-payroll", "Budget masse salariale 2025 (synthèse)"),
            ("doc-plan", "Plan stratégique 2026"),
            ("doc-leave", "Politique de congés"),
        )
    }


HALLUCINATED_ANSWER = """Voici la comparaison :

2025 :
- Budget budgétaire : 4,1 M€
- Dépenses totales de personnel : 800 000 €
- Production : 1,2 million de tonnes

2026 :
- Budget budgétaire : 4,3 M€
- Dépenses totales de personnel : 1 000 000 €
- Production : 1,5 million de tonnes"""


# --- impossibility to invent a value -------------------------------------------


def test_incident_answer_has_every_invented_value_removed():
    report = verify_answer(HALLUCINATED_ANSWER, _evidence(), _candidates(), question=QUESTION)

    for invented in ("800 000", "1,2 million", "4,3 M", "1 000 000", "1,5 million"):
        assert invented not in report.answer
    assert set(report.unsupported) >= {"800 000", "1,2 million", "4,3 M", "1 000 000", "1,5 million"}
    assert report.answer.count(UNAVAILABLE) == 6
    assert REDACTION_NOTE in report.answer


def test_value_absent_from_every_source_is_removed_even_in_prose():
    answer = "La production 2026 atteindra 1,5 million de tonnes. Le reste n'est pas documenté."
    report = verify_answer(answer, _evidence(), _candidates(), question=QUESTION)

    assert "1,5" not in report.answer
    assert "Le reste n'est pas documenté." in report.answer
    assert report.unsupported == ["1,5 million"]


def test_every_value_removed_leaves_the_no_data_answer():
    report = verify_answer("850 000 €", _evidence(), _candidates(), question=QUESTION)
    assert report.answer == NO_DATA_ANSWER
    assert report.sources == []


def test_computed_value_not_in_sources_is_rejected():
    # 4,1 M€ * 1,2 = 4,92 M€ - a derived figure, never written in any source.
    answer = "Masse salariale 2026 estimée : 4,92 M€"
    report = verify_answer(answer, _evidence(), _candidates(), question=QUESTION)
    assert "4,92" not in report.answer


def test_value_typed_by_the_user_is_accepted():
    ev = _evidence()
    ev.add_user_message("Mon budget est de 730 000 €, compare avec la masse salariale 2025")
    report = verify_answer("Votre budget : 730 000 €", ev, _candidates(), question=QUESTION)
    assert "730 000 €" in report.answer
    assert report.unsupported == []


# --- comparisons with missing data ---------------------------------------------


def test_faithful_comparison_table_passes_untouched():
    answer = (
        "| Indicateur | 2025 | 2026 |\n"
        "|---|---|---|\n"
        f"| Masse salariale | 4,1 M€ | {UNAVAILABLE} |\n"
        f"| Chiffre d'affaires | {UNAVAILABLE} | objectif de croissance de 20 % sur 5 ans |\n"
        f"| Production | {UNAVAILABLE} | {UNAVAILABLE} |"
    )
    report = verify_answer(answer, _evidence(), _candidates(), question=QUESTION)

    assert report.answer == answer
    assert report.unsupported == []


def test_invented_cell_in_comparison_table_is_replaced_not_the_whole_row():
    answer = (
        "| Indicateur | 2025 | 2026 |\n"
        "|---|---|---|\n"
        "| Masse salariale | 4,1 M€ | 4,3 M€ |"
    )
    report = verify_answer(answer, _evidence(), _candidates(), question=QUESTION)

    row = report.answer.split("\n")[2]
    assert row == f"| Masse salariale | 4,1 M€ | {UNAVAILABLE} |"


def test_year_not_requested_nor_in_sources_is_flagged():
    report = verify_answer("| Indicateur | 2025 | 2027 |", _evidence(), _candidates(), question=QUESTION)
    assert "2027" not in report.answer


def test_requested_years_and_comparison_detection():
    assert requested_years(QUESTION) == [2025, 2026]
    assert requested_years("budget 2025, encore 2025") == [2025]
    assert is_comparison_request(QUESTION)
    assert is_comparison_request("compare le budget RH et le budget IT")
    assert not is_comparison_request("quel est le budget 2025 ?")


# --- budget vs masse salariale --------------------------------------------------


def test_payroll_figure_relabelled_as_company_budget_is_rejected():
    # The source says "budget ... de la masse salariale" - not the company's budget.
    for answer in (
        "- Budget budgétaire : 4,1 M€",
        "- Budget de l'entreprise : 4,1 M€",
        "| Budget total de l'entreprise | 4,1 M€ |",
    ):
        report = verify_answer(answer, _evidence(), _candidates(), question=QUESTION)
        assert "4,1" not in report.answer, answer
        assert UNAVAILABLE in report.answer


def test_payroll_figure_under_its_real_label_is_accepted():
    for answer in (
        "- Masse salariale 2025 : 4,1 M€",
        "- Budget de la masse salariale : 4,1 millions d'euros",
        "| Masse salariale | 4 100 000 € |",
    ):
        report = verify_answer(answer, _evidence(), _candidates(), question=QUESTION)
        assert report.unsupported == [], answer


def test_multi_year_growth_target_is_not_an_annual_revenue_figure():
    # "croissance de 20% dans les 5 prochaines années" backs "20 %", but a
    # revenue amount for 2026 does not exist anywhere.
    report = verify_answer("- Chiffre d'affaires 2026 : 1 200 000 €", _evidence(), _candidates(), question=QUESTION)
    assert "1 200 000" not in report.answer


# --- traceability of every numerical value -------------------------------------


def test_number_formats_trace_to_the_same_source_value():
    ev = Evidence()
    ev.add("Masse salariale totale 2025 (2025) : 4100000.0")  # search_database record, raw float
    for shown in ("4,1 M€", "4 100 000 €", "4,100,000 €", "4.1 millions", "4 100 000,00 €"):
        report = verify_answer(f"Masse salariale totale : {shown}", ev, {}, question="")
        assert report.unsupported == [], shown


def test_rounding_is_tolerated_but_a_different_magnitude_is_not():
    ev = Evidence()
    ev.add("Résultat net 2025 (2025) : 620000.0")
    assert verify_answer("Résultat net : 0,6 M€", ev, {}).unsupported == []
    assert verify_answer("Résultat net : 1 M€", ev, {}).unsupported == ["1 M"]
    assert verify_answer("Résultat net : 6,2 M€", ev, {}).unsupported == ["6,2 M"]


def test_percentages_must_be_in_sources_too():
    report = verify_answer("Hausse de la masse salariale : 6 %. Hausse prévue en 2026 : 8 %.", _evidence(), _candidates())
    assert "6 %" in report.answer
    assert "8 %" not in report.answer


def test_list_markers_and_small_counts_are_not_treated_as_data():
    answer = "1. Masse salariale 2025 : 4,1 M€\n2. Les 3 documents consultés sont cités."
    report = verify_answer(answer, _evidence(), _candidates(), question=QUESTION)
    assert report.unsupported == []
    assert report.answer == answer


# --- only the sources actually used ----------------------------------------------


def test_sources_are_only_the_documents_that_backed_the_answer():
    answer = f"| Masse salariale | 4,1 M€ | {UNAVAILABLE} |"
    report = verify_answer(answer, _evidence(), _candidates(), question=QUESTION)
    assert [s["document_id"] for s in report.sources] == ["doc-payroll"]


def test_cited_title_counts_as_used_source():
    answer = "Selon le Plan stratégique 2026, l'objectif est une croissance du chiffre d'affaires de 20 %."
    report = verify_answer(answer, _evidence(), _candidates(), question=QUESTION)
    assert [s["document_id"] for s in report.sources] == ["doc-plan"]


def test_retrieved_but_unused_document_is_not_a_source():
    report = verify_answer(HALLUCINATED_ANSWER, _evidence(), _candidates(), question=QUESTION)
    # Every value was invented - nothing was actually used, not even the
    # payroll synthesis whose real 4,1 M€ was mislabelled, nor the leave
    # policy that retrieval also surfaced.
    assert report.sources == []


def test_no_data_answer_has_no_sources():
    report = verify_answer(NO_DATA_ANSWER, _evidence(), _candidates(), question=QUESTION)
    assert report.answer == NO_DATA_ANSWER
    assert report.sources == []


def test_tool_results_are_evidence_but_tool_errors_are_not():
    ev = Evidence()
    ev.add_tool_result("get_document", {"document_id": "d1", "title": "Budget RH", "content": "Budget RH : 180 000 €."})
    ev.add_tool_result("search_documents", {"error": "refus", "results": [{"excerpt": "Budget secret : 999 999 €"}]})
    assert verify_answer("Budget RH : 180 000 €", ev, {}).unsupported == []
    assert verify_answer("Budget secret : 999 999 €", ev, {}).unsupported == ["999 999"]


# --- no plotting code in an answer ---------------------------------------------

from app.agent.grounding import CHART_CODE_NOTICE, strip_chart_code  # noqa: E402

PYTHON_ANSWER = """D'après les extraits, voici le code Python qui génère le graphique demandé :

```python
import pandas as pd
import matplotlib.pyplot as plt
df = pd.read_excel("Orbitia_Test_Entreprise_Complet.xlsx", sheet_name="CA mensuel")
plt.plot(df["Mois"], df["Chiffre d'affaires (TND)"])
plt.show()
```"""


def test_matplotlib_block_is_replaced_by_how_to_get_the_real_chart():
    cleaned, removed = strip_chart_code(PYTHON_ANSWER)
    assert removed
    assert "```" not in cleaned
    assert "matplotlib" not in cleaned and "plt." not in cleaned
    assert cleaned.endswith(CHART_CODE_NOTICE)


def test_unfenced_plotting_lines_are_removed_too():
    cleaned, removed = strip_chart_code("Voici :\nimport matplotlib.pyplot as plt\nplt.bar(x, y)\nFin.")
    assert removed
    assert "plt" not in cleaned
    assert "Voici :" in cleaned and "Fin." in cleaned


def test_unrelated_code_is_kept():
    answer = "La procédure indique :\n```bash\nssh -J bastion prod-01\n```"
    assert strip_chart_code(answer) == (answer, False)


# --- a real value under the wrong year column -----------------------------------

def test_value_copied_under_another_years_column_is_removed():
    # Real Mistral output: the 2025 Budget RH amount repeated in a 2024 column.
    ev = Evidence()
    ev.add_user_message("2024: 120000, 2025: 135000")
    ev.add_tool_result("search_database", {"results": [{"label": "Budget RH 2025", "year": 2025, "amount": 180000.0}]})
    answer = "| Indicateur | 2024 | 2025 |\n|---|---|---|\n| Budget RH | 180,000 € | 180,000 € |"

    report = verify_answer(answer, ev, {})

    assert report.answer.split("\n")[2] == f"| Budget RH | {UNAVAILABLE} | 180,000 € |"


def test_document_title_year_backs_its_sentences():
    report = verify_answer(
        f"| Indicateur | 2025 | 2026 |\n|---|---|---|\n| Chiffre d'affaires | {UNAVAILABLE} | objectif de croissance de 20 % |",
        _evidence(), _candidates(), question=QUESTION,
    )
    assert report.unsupported == []  # the 20 % comes from "Plan stratégique 2026"
