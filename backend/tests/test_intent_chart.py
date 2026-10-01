from app.agent.intent import detect_chart_intent


def test_no_trigger_for_bare_data_question():
    assert detect_chart_intent("Quel est le chiffre d'affaires ?") is None
    assert detect_chart_intent("Quelle est la politique de congés ?") is None


def test_line_chart_courbe():
    r = detect_chart_intent("Fais-moi une courbe du chiffre d'affaires de 2025.")
    assert r.chart_type == "line"


def test_line_chart_evolution():
    r = detect_chart_intent("Affiche l'évolution des ventes par mois.")
    assert r.chart_type == "line"


def test_bar_chart_histogramme():
    r = detect_chart_intent("Fais un histogramme des ventes.")
    assert r.chart_type == "bar"


def test_pie_chart_camembert():
    r = detect_chart_intent("Fais un camembert des dépenses.")
    assert r.chart_type == "pie"


def test_pie_chart_repartition():
    r = detect_chart_intent("Montre la répartition des dépenses.")
    assert r.chart_type == "pie"


def test_scatter_relation_entre():
    r = detect_chart_intent("Montre la relation entre les dépenses marketing et les ventes.")
    assert r.chart_type == "scatter"


def test_compare_data_defaults_to_bar():
    r = detect_chart_intent("Compare les ventes des différents départements.")
    assert r.chart_type == "bar"


def test_generic_graphique_with_verb_has_no_explicit_type():
    r = detect_chart_intent("Fais un graphique des ventes 2025.")
    assert r is not None
    assert r.chart_type is None


def test_visualise_trigger():
    r = detect_chart_intent("Visualise ces données.")
    assert r is not None


def test_subject_extraction_strips_trigger_words():
    r = detect_chart_intent("Fais un histogramme des ventes par produit.")
    assert "histogramme" not in r.subject.lower()
    assert "ventes" in r.subject.lower()
