import pytest

from app.agent.chart import ChartValidationError, parse_inline_data, summarize_chart, validate_chart_spec


# --- parse_inline_data -----------------------------------------------------


def test_parse_inline_simple():
    rows = parse_inline_data("Janvier: 120000, Février: 135000, Mars: 128000")
    assert rows == [("Janvier", 120000.0), ("Février", 135000.0), ("Mars", 128000.0)]


def test_parse_inline_unknown_becomes_none_not_zero():
    rows = parse_inline_data("Janvier: 120000, Février: inconnu, Mars: 128000")
    assert rows[1] == ("Février", None)


def test_parse_inline_requires_at_least_two_pairs():
    assert parse_inline_data("Le total est de 120000.") is None


def test_parse_inline_no_clean_data_returns_none():
    assert parse_inline_data("Quel est le chiffre d'affaires ?") is None


def test_parse_inline_ambiguous_decimal_rejected():
    # more than one "." after normalization is ambiguous - reject, don't guess
    assert parse_inline_data("A: 1.234.567, B: 2.000.000") is None


def test_parse_inline_equals_sign():
    rows = parse_inline_data("RH = 180000, Marketing = 95000")
    assert rows == [("RH", 180000.0), ("Marketing", 95000.0)]


# --- validate_chart_spec ----------------------------------------------------


def test_validate_bar_requires_category_field():
    with pytest.raises(ChartValidationError, match="catégorie"):
        validate_chart_spec(chart_type="bar", title="T", category_field=None, value_fields=[{"field": "v", "label": "V"}], data=[{"v": 1}])


def test_validate_pie_requires_exactly_one_value_field():
    with pytest.raises(ChartValidationError, match="circulaire"):
        validate_chart_spec(
            chart_type="pie", title="T", category_field="label",
            value_fields=[{"field": "a", "label": "A"}, {"field": "b", "label": "B"}],
            data=[{"label": "x", "a": 1, "b": 2}],
        )


def test_validate_scatter_requires_exactly_two_value_fields():
    with pytest.raises(ChartValidationError, match="nuage de points"):
        validate_chart_spec(chart_type="scatter", title="T", category_field=None, value_fields=[{"field": "x", "label": "X"}], data=[{"x": 1}])


def test_validate_scatter_rejects_category_field():
    with pytest.raises(ChartValidationError, match="catégorie"):
        validate_chart_spec(
            chart_type="scatter", title="T", category_field="label",
            value_fields=[{"field": "x", "label": "X"}, {"field": "y", "label": "Y"}],
            data=[{"label": "a", "x": 1, "y": 2}],
        )


def test_validate_rejects_empty_data():
    with pytest.raises(ChartValidationError, match="aucune donnée"):
        validate_chart_spec(chart_type="bar", title="T", category_field="label", value_fields=[{"field": "v", "label": "V"}], data=[])


def test_validate_rejects_non_numeric_value_never_coerces():
    with pytest.raises(ChartValidationError, match="non numérique"):
        validate_chart_spec(
            chart_type="bar", title="T", category_field="label",
            value_fields=[{"field": "v", "label": "V"}],
            data=[{"label": "Mars", "v": "inconnu"}],
        )


def test_validate_allows_none_as_explicit_missing_value():
    validate_chart_spec(
        chart_type="bar", title="T", category_field="label",
        value_fields=[{"field": "v", "label": "V"}],
        data=[{"label": "Janvier", "v": 100}, {"label": "Février", "v": None}],
    )  # must not raise


def test_validate_unknown_chart_type_rejected():
    with pytest.raises(ChartValidationError, match="inconnu"):
        validate_chart_spec(chart_type="pyramid", title="T", category_field="label", value_fields=[{"field": "v", "label": "V"}], data=[{"label": "a", "v": 1}])


# --- summarize_chart ---------------------------------------------------------


def test_summarize_bar_reports_max_and_min():
    spec = {
        "chart_type": "bar", "category_field": "label",
        "value_fields": [{"field": "amount", "label": "Montant", "unit": "TND"}],
        "data": [{"label": "RH", "amount": 180000}, {"label": "Marketing", "amount": 95000}],
    }
    text = summarize_chart(spec)
    assert "RH" in text
    assert "Marketing" in text
    assert "180 000" in text or "180000" in text


def test_summarize_line_reports_direction():
    spec = {
        "chart_type": "line", "category_field": "month",
        "value_fields": [{"field": "sales", "label": "Ventes", "unit": None}],
        "data": [{"month": "Jan", "sales": 100}, {"month": "Fév", "sales": 150}],
    }
    text = summarize_chart(spec)
    assert "hausse" in text


def test_summarize_never_invents_missing_values():
    spec = {
        "chart_type": "bar", "category_field": "label",
        "value_fields": [{"field": "v", "label": "V", "unit": None}],
        "data": [{"label": "A", "v": None}],
    }
    text = summarize_chart(spec)
    assert "Aucune valeur" in text
