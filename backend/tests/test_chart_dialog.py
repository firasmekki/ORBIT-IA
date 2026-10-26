"""app/agent/chart_dialog.py - deterministic chart planning / clarification,
on the demo's real financial records (app/seed.py FINANCIAL_RECORDS)."""

from app.agent.chart_dialog import (
    ALL_PERIODS_VALUE,
    FINANCIAL_SOURCE,
    OTHER_VALUE,
    ChartPlan,
    Clarification,
    KpiPlan,
    analyze_variation,
    build_catalog,
    is_variation_request,
    has_chart_subject,
    infer_chart_type,
    is_chart_followup,
    match_metrics,
    plan_chart,
)

SEED_RECORDS = [
    {"label": "Budget IT / Infrastructure 2025", "amount": 310000.0, "year": 2025},
    {"label": "Budget Marketing 2025", "amount": 95000.0, "year": 2025},
    {"label": "Budget RH 2025", "amount": 180000.0, "year": 2025},
    {"label": "Masse salariale totale 2025", "amount": 4100000.0, "year": 2025},
    {"label": "Résultat net 2025 (prévisionnel)", "amount": 620000.0, "year": 2025},
]
MULTI_YEAR = SEED_RECORDS + [
    {"label": "Budget IT / Infrastructure 2023", "amount": 180000.0, "year": 2023},
    {"label": "Budget IT / Infrastructure 2024", "amount": 240000.0, "year": 2024},
    {"label": "Budget Marketing 2024", "amount": 90000.0, "year": 2024},
]
CATALOG = build_catalog(SEED_RECORDS)


def _plan(text, explicit=None, catalog=CATALOG, state=None, subject=None):
    return plan_chart(text=text, explicit_type=explicit, catalog=catalog, state=state, subject=subject)


# --- catalog -------------------------------------------------------------------------------


def test_catalog_groups_records_by_series_without_the_year():
    labels = {m.label: m for m in CATALOG}
    assert set(labels) == {
        "Budget IT / Infrastructure", "Budget Marketing", "Budget RH", "Masse salariale totale",
        "Résultat net (prévisionnel)",
    }
    assert labels["Budget IT / Infrastructure"].points == [("2025", 310000.0)]
    assert labels["Budget IT / Infrastructure"].id == "budget_it_infrastructure"


def test_catalog_keeps_every_period_sorted():
    metric = next(m for m in build_catalog(MULTI_YEAR) if m.id == "budget_it_infrastructure")
    assert metric.points == [("2023", 180000.0), ("2024", 240000.0), ("2025", 310000.0)]


# --- matching ------------------------------------------------------------------------------


def test_specific_word_selects_one_metric():
    assert [m.label for m in match_metrics("Infrastructure", CATALOG).selected] == ["Budget IT / Infrastructure"]
    assert [m.label for m in match_metrics("le résultat net", CATALOG).selected] == ["Résultat net (prévisionnel)"]


def test_two_specific_words_select_two_metrics():
    selected = match_metrics("Compare le budget IT et Marketing", CATALOG).selected
    assert {m.label for m in selected} == {"Budget IT / Infrastructure", "Budget Marketing"}


def test_shared_word_is_ambiguous_but_its_plural_means_all():
    single = match_metrics("le budget", CATALOG)
    assert single.selected == [] and {m.label for m in single.candidates} == {
        "Budget IT / Infrastructure", "Budget Marketing", "Budget RH",
    }
    assert len(match_metrics("les budgets", CATALOG).selected) == 3


def test_unknown_subject_is_reported_not_guessed():
    match = match_metrics("la courbe du chiffre d'affaires", CATALOG)
    assert match.selected == [] and match.candidates == []
    assert match.unmatched == ["affaire"]


def test_chart_vocabulary_alone_has_no_subject():
    assert not has_chart_subject("Donne-moi une courbe")
    assert not has_chart_subject("Fais-moi un graphique")
    assert has_chart_subject("budget IT")


# --- TEST 1: incomplete request -> one clarification -------------------------------------------


def test_bare_chart_request_asks_a_short_question_without_a_generic_list():
    # 6. a vague request never lists every metric of the database
    result = _plan("Donne-moi une courbe", explicit="line")
    assert isinstance(result, Clarification)
    assert result.message == "Quelle donnée souhaitez-vous représenter, et sur quelle période ?"
    assert result.options == []
    payload = result.to_dict()
    assert payload["type"] == "clarification" and payload["status"] == "need_more_information"


def test_vague_request_offers_only_metrics_from_the_conversation():
    result = plan_chart(
        text="Donne-moi une courbe", explicit_type="line", catalog=build_catalog(MULTI_YEAR),
        context="On parlait du budget marketing et du budget RH.",
    )
    assert isinstance(result, Clarification)
    # a curve needs 2 periods: Marketing has 2024+2025, RH only 2025
    assert [o["label"] for o in result.options] == ["Budget Marketing", OTHER_VALUE]
    assert "une seule période pour « Budget RH » (2025)" in result.message


def test_single_value_never_becomes_a_curve():
    # 1. one yearly value: a value card and the reason, no artificial curve
    plan = _plan("Donne-moi une courbe de variation du budget IT", explicit="line")
    assert isinstance(plan, KpiPlan)
    assert [(i["label"], i["period"], i["value"], i["unit"]) for i in plan.items] == [
        ("Budget IT / Infrastructure", "2025", 310000.0, "€")
    ]
    assert plan.message.startswith(
        "Je dispose uniquement de la valeur de « Budget IT / Infrastructure » en 2025 (310 000 €). "
        "Il me faut au moins une autre période comparable pour tracer son évolution"
    )


def test_single_value_without_trend_is_a_value_card_not_a_bar():
    plan = _plan("Fais un graphique du budget RH")
    assert isinstance(plan, KpiPlan) and plan.items[0]["value"] == 180000.0


def test_series_with_enough_points_is_a_line_with_its_rationale():
    # 4. enough time points -> the right chart, with why it fits
    plan = _plan("Donne-moi une courbe du budget IT", explicit="line", catalog=build_catalog(MULTI_YEAR))
    assert isinstance(plan, ChartPlan) and plan.chart_type == "line"
    assert [row["Période"] for row in plan.data] == ["2023", "2024", "2025"]
    assert "3 périodes comparables" in plan.rationale and plan.periods == ["2023", "2024", "2025"]


def test_metrics_in_different_units_are_never_mixed():
    # 5. € and % never share an axis
    records = SEED_RECORDS + [{"label": "Taux de marge 2025", "amount": 12.5, "year": 2025}]
    catalog = build_catalog(records)
    next(m for m in catalog if m.label == "Taux de marge").unit = "%"
    result = _plan("Compare le budget RH et le taux de marge", explicit="bar", catalog=catalog)
    assert isinstance(result, Clarification)
    assert "n'ont pas la même unité (%, €)" in result.message


def test_curve_over_two_metrics_drops_the_single_period_one_and_says_so():
    plan = _plan("Montre l'évolution du budget IT et du budget RH", catalog=build_catalog(MULTI_YEAR))
    assert isinstance(plan, ChartPlan) and plan.chart_type == "line"
    assert [vf["id"] for vf in plan.value_fields] == ["budget_it_infrastructure"]
    assert "une seule période pour « Budget RH » (2025)" in plan.note


# --- variation (no chart) -------------------------------------------------------------------


def test_variation_with_two_comparable_values_is_computed():
    # 2. two comparable values -> variation computed from them
    catalog = build_catalog(MULTI_YEAR)
    answer = analyze_variation("Analyse la variation du budget marketing", catalog)
    assert "« Budget Marketing » augmente de +5 000 € (+5,6 %) entre 2024 et 2025 : 90 000 € → 95 000 €." in answer
    assert FINANCIAL_SOURCE in answer


def test_variation_with_one_period_is_not_calculated_and_says_why():
    answer = analyze_variation("Analyse la variation du Budget IT / Infrastructure", CATALOG)
    assert answer.startswith(
        "La valeur disponible pour « Budget IT / Infrastructure » est de 310 000 € pour 2025. "
        "Aucune valeur comparable pour une autre année n'a été trouvée dans les sources consultées. "
        "La variation annuelle ne peut donc pas être calculée"
    )
    assert "variation de 0" not in answer and "reste à" not in answer


def test_missing_value_is_never_replaced_by_zero():
    # 3. 2024 is missing for RH: said missing, never computed as 0
    catalog = build_catalog(MULTI_YEAR)
    answer = analyze_variation("Variation du budget RH entre 2024 et 2025", catalog)
    assert "Aucune valeur pour 2024 n'a été trouvée" in answer and "ne peut donc pas être calculée" in answer
    assert "augmente" not in answer and "diminue" not in answer and "+" not in answer


def test_variation_without_a_named_metric_falls_back_to_documents():
    # 7. nothing relevant: not answered from the database at all
    assert analyze_variation("Analyse la variation de la consommation électrique du site", CATALOG) is None
    assert is_variation_request("Analyse la variation du budget") and not is_variation_request("Résume le rapport")


def test_no_accessible_data_explains_clearly():
    result = _plan("Donne-moi une courbe du budget IT", explicit="line", catalog=[])
    assert isinstance(result, Clarification) and "pas de données chiffrées accessibles" in result.message


def test_family_answer_narrows_with_a_targeted_question():
    result = _plan("Donne-moi une courbe Le budget", explicit="line")
    assert isinstance(result, Clarification)
    assert result.message == (
        "Quel budget souhaitez-vous analyser : Budget IT / Infrastructure, Budget Marketing ou Budget RH ?"
    )
    assert result.state["candidate_ids"] == ["budget_it_infrastructure", "budget_marketing", "budget_rh"]


def test_answers_accumulate_until_the_chart_is_ready():
    first = _plan("Je veux une courbe", explicit="line")
    second = _plan("Je veux une courbe Le budget", explicit="line", state=first.state)
    third = _plan("Je veux une courbe Le budget Infrastructure", explicit="line", state=second.state)
    assert isinstance(first, Clarification) and isinstance(second, Clarification)
    # everything said so far is kept; a single period gives a value card
    assert isinstance(third, KpiPlan)
    assert third.items[0]["metric_id"] == "budget_it_infrastructure" and third.items[0]["value"] == 310000.0


# --- TEST 2 / TEST 3: complete requests -> chart directly ---------------------------------------


def test_complete_request_needs_no_question():
    plan = _plan("Donne-moi une courbe du budget Infrastructure 2025", explicit="line")
    assert isinstance(plan, KpiPlan)  # one period: no line, no one-bar chart
    assert plan.items[0]["period"] == "2025"


def test_comparison_is_a_bar_chart_of_real_values():
    plan = _plan("Compare le budget IT et Marketing", explicit="bar")
    assert isinstance(plan, ChartPlan) and plan.chart_type == "bar"
    assert {(r["label"], r["amount"]) for r in plan.data} == {
        ("Budget IT / Infrastructure", 310000.0),
        ("Budget Marketing", 95000.0),
    }
    assert plan.title.startswith("Comparaison 2025")


def test_evolution_over_several_years_is_a_line():
    catalog = build_catalog(MULTI_YEAR)
    plan = _plan("Je veux voir l'évolution du budget infrastructure entre 2023 et 2025", catalog=catalog)
    assert isinstance(plan, ChartPlan) and plan.chart_type == "line"
    assert [row["Période"] for row in plan.data] == ["2023", "2025"]
    assert plan.value_fields[0]["id"] == "budget_it_infrastructure"


def test_multi_metric_time_series_is_a_multi_line():
    catalog = build_catalog(MULTI_YEAR)
    plan = _plan("Montre l'évolution du budget IT et du budget Marketing", catalog=catalog)
    assert isinstance(plan, ChartPlan) and plan.chart_type == "line"
    assert len(plan.value_fields) == 2
    marketing_2023 = next(r for r in plan.data if r["Période"] == "2023")["Budget Marketing"]
    assert marketing_2023 is None  # a missing value stays missing, never interpolated


# --- TEST 5: data that does not exist -> ask for it, never invent ------------------------------------


def test_unknown_metric_asks_for_values_or_a_file():
    result = _plan("Fais-moi la courbe du chiffre d'affaires", explicit="line", subject="chiffre d'affaires")
    assert isinstance(result, Clarification)
    assert result.message.startswith("Je peux créer ce graphique, mais je n'ai pas encore les données de « chiffre d'affaires »")
    assert "fournir les valeurs" in result.message
    assert result.state["asked"] == "data"
    assert "Aucune donnée autorisée" not in result.message


def test_requested_year_absent_offers_the_real_periods():
    result = _plan("courbe du budget RH 2023", explicit="line")
    assert isinstance(result, Clarification)
    assert "Je ne dispose pas de données 2023" in result.message
    assert [o["value"] for o in result.options] == ["2025"]


def test_no_accessible_data_at_all():
    result = _plan("Donne-moi une courbe", explicit="line", catalog=[])
    assert isinstance(result, Clarification)
    assert "pas de données chiffrées accessibles" in result.message


def test_other_answer_asks_for_the_users_own_values():
    first = _plan("Donne-moi une courbe", explicit="line")
    result = _plan("Donne-moi une courbe Autre", explicit="line", state=first.state)
    assert isinstance(result, Clarification)
    assert result.message.startswith("D'accord. Donnez-moi les valeurs")


# --- periods -------------------------------------------------------------------------------------


def test_bar_comparison_over_several_periods_asks_which_period():
    catalog = build_catalog(MULTI_YEAR)
    result = _plan("Compare le budget IT et Marketing", explicit="bar", catalog=catalog)
    assert isinstance(result, Clarification)
    assert result.message == "Quelle période souhaitez-vous comparer ?"
    assert [o["value"] for o in result.options] == ["2023", "2024", "2025", ALL_PERIODS_VALUE]
    answered = _plan("Compare le budget IT et Marketing 2024", explicit="bar", catalog=catalog, state=result.state)
    assert isinstance(answered, ChartPlan)
    assert {(r["label"], r["amount"]) for r in answered.data} == {
        ("Budget IT / Infrastructure", 240000.0), ("Budget Marketing", 90000.0),
    }


# --- drill-down metadata (clickable bars) ------------------------------------------------------


def test_every_plotted_item_carries_its_id_and_full_history():
    catalog = build_catalog(MULTI_YEAR)
    plan = _plan("Compare le budget IT et Marketing 2025", explicit="bar", catalog=catalog)
    for row in plan.data:
        detail = plan.drilldown[row["id"]]
        assert detail["label"] == row["label"]
        assert detail["points"]  # every authorized period of that metric
        assert row["label"] not in detail["compare_with"]
    assert plan.drilldown["budget_it_infrastructure"]["points"] == [
        {"period": "2023", "value": 180000.0},
        {"period": "2024", "value": 240000.0},
        {"period": "2025", "value": 310000.0},
    ]


# --- chart type inference & follow-up detection -----------------------------------------------------


def test_chart_type_inference():
    assert infer_chart_type(None, "évolution", 1, 3) == "line"
    assert infer_chart_type(None, "compare", 2, 1) == "bar"
    assert infer_chart_type(None, "répartition des budgets", 3, 1) == "pie"
    assert infer_chart_type("line", "courbe", 1, 1) == "bar"  # nothing to draw a line through
    assert infer_chart_type("pie", "", 3, 1) == "pie"


def test_followup_detection():
    state = {"kind": "chart_clarification", "option_values": ["Budget Marketing"]}
    assert is_chart_followup("Budget Marketing", state)
    assert is_chart_followup("Infrastructure", state)
    assert is_chart_followup("2025", state)
    assert is_chart_followup("2024: 120000, 2025: 135000", state)
    assert not is_chart_followup("Quelle est la politique de congés pour les nouveaux arrivants ?", state)
    assert not is_chart_followup("Infrastructure", None)
    assert not is_chart_followup("Infrastructure", {"kind": "keyword_search"})


def test_plural_family_adds_to_specifically_named_metrics():
    selected = match_metrics("Compare les budgets et le résultat net 2025", CATALOG).selected
    assert {m.label for m in selected} == {
        "Budget IT / Infrastructure", "Budget Marketing", "Budget RH", "Résultat net (prévisionnel)",
    }
    # ...while a singular family word next to a specific one stays specific.
    assert {m.label for m in match_metrics("Compare le budget IT et Marketing", CATALOG).selected} == {
        "Budget IT / Infrastructure", "Budget Marketing",
    }
