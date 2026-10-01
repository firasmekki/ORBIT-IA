"""MCP-level tests for _generate_chart_impl: this tool never fetches data
itself (see app/mcp/server.py's docstring) - its only job is the final
structural check (validate_chart_spec) plus an audit entry, so these tests
focus on that contract: valid specs are echoed back unchanged (no number
silently altered), invalid specs come back as a clean {"error": ...} - not
an exception - and a validation failure is still an audited ALLOW (the
role was allowed to call the tool; it's the caller-supplied shape that was
rejected, not a policy decision). ACL for chart *data* is enforced
upstream by whichever tool produced it (search_database) - see
test_orchestrator_chart.py for that path."""

import app.mcp.server as mcp_server


def test_valid_bar_spec_returns_chart_unchanged(make_test_user):
    user = make_test_user(username="chart_u1", role="DIRECTOR")

    result = mcp_server._generate_chart_impl(
        str(user.id),
        chart_type="bar",
        title="Répartition des budgets",
        category_field="label",
        category_label="Département",
        value_fields=[{"field": "amount", "label": "Montant", "unit": "€"}],
        data=[{"label": "RH", "amount": 180000}, {"label": "Marketing", "amount": 95000}],
        sources=None,
    )

    assert "error" not in result
    chart = result["chart"]
    assert chart["chart_type"] == "bar"
    assert chart["data"] == [{"label": "RH", "amount": 180000}, {"label": "Marketing", "amount": 95000}]
    assert chart["sources"] == []  # None normalized to [], never fabricated


def test_invalid_scatter_spec_returns_clean_error_not_exception(make_test_user):
    user = make_test_user(username="chart_u2", role="DIRECTOR")

    result = mcp_server._generate_chart_impl(
        str(user.id),
        chart_type="scatter",
        title="Corrélation",
        category_field=None,
        category_label=None,
        value_fields=[{"field": "x", "label": "X"}],  # scatter needs exactly two
        data=[{"x": 1}],
        sources=None,
    )

    assert "chart" not in result
    assert "nuage de points" in result["error"]


def test_none_value_never_coerced_to_zero(make_test_user):
    user = make_test_user(username="chart_u3", role="DIRECTOR")

    result = mcp_server._generate_chart_impl(
        str(user.id),
        chart_type="bar",
        title="Ventes",
        category_field="label",
        category_label=None,
        value_fields=[{"field": "amount", "label": "Montant"}],
        data=[{"label": "Janvier", "amount": 100}, {"label": "Février", "amount": None}],
        sources=None,
    )

    row = result["chart"]["data"][1]
    assert row["amount"] is None


def test_sources_echoed_exactly_as_given(make_test_user):
    user = make_test_user(username="chart_u4", role="DIRECTOR")
    sources = [{"document_id": "doc-1", "title": "Rapport annuel", "page": 3, "section": None}]

    result = mcp_server._generate_chart_impl(
        str(user.id),
        chart_type="pie",
        title="Répartition",
        category_field="label",
        category_label=None,
        value_fields=[{"field": "amount", "label": "Montant"}],
        data=[{"label": "RH", "amount": 1}],
        sources=sources,
    )

    assert result["chart"]["sources"] == sources


def test_unknown_user_denied(make_test_user):
    result = mcp_server._generate_chart_impl(
        "00000000-0000-0000-0000-000000000000",
        chart_type="bar",
        title="T",
        category_field="label",
        category_label=None,
        value_fields=[{"field": "v", "label": "V"}],
        data=[{"label": "a", "v": 1}],
        sources=None,
    )
    assert "error" in result
    assert "chart" not in result
