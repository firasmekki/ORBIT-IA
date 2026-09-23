"""Integration tests for the list_documents MCP tool: ACL scoping, and the
type/department/date/author filters, against a real database."""

from datetime import datetime, timezone

import app.mcp.server as mcp_server


def test_hr_does_not_see_finance_only_documents(make_doc, make_test_user):
    make_doc(title="Grille des salaires", department="FINANCE", confidentiality="SECRET", pages=[(None, None, 1, "x")])
    make_doc(title="Politique de congés", department="HR", confidentiality="CONFIDENTIAL", pages=[(None, None, 1, "x")])
    hr = make_test_user(username="rh_list", role="HR")

    result = mcp_server._list_documents_impl(str(hr.id), None, None, None, None, None)

    titles = {d["title"] for d in result["results"]}
    assert "Politique de congés" in titles
    assert "Grille des salaires" not in titles


def test_department_filter_outside_scope_returns_empty_not_error(make_doc, make_test_user):
    make_doc(title="Grille des salaires", department="FINANCE", confidentiality="SECRET", pages=[(None, None, 1, "x")])
    hr = make_test_user(username="rh_list2", role="HR")

    result = mcp_server._list_documents_impl(str(hr.id), None, "FINANCE", None, None, None)

    assert result == {"results": []}
    assert "error" not in result


def test_document_type_filter(make_doc, make_test_user):
    make_doc(
        title="Rapport PDF",
        department="GENERAL",
        confidentiality="INTERNAL",
        pages=[(1, None, 1, "x")],
        source_filename="rapport.pdf",
    )
    make_doc(
        title="Tableau Excel",
        department="GENERAL",
        confidentiality="INTERNAL",
        pages=[(None, "Feuille1", 1, "x")],
        source_filename="tableau.xlsx",
    )
    employee = make_test_user(username="emp_list", role="EMPLOYEE")

    result = mcp_server._list_documents_impl(str(employee.id), "pdf", None, None, None, None)

    titles = {d["title"] for d in result["results"]}
    assert titles == {"Rapport PDF"}


def test_author_filter(make_doc, make_test_user):
    author = make_test_user(username="auteur1", role="EMPLOYEE")
    other = make_test_user(username="auteur2", role="EMPLOYEE")
    make_doc(title="Doc de auteur1", department="GENERAL", confidentiality="INTERNAL", pages=[(None, None, 1, "x")], owner_id=author.id)
    make_doc(title="Doc de auteur2", department="GENERAL", confidentiality="INTERNAL", pages=[(None, None, 1, "x")], owner_id=other.id)
    reader = make_test_user(username="reader", role="EMPLOYEE")

    result = mcp_server._list_documents_impl(str(reader.id), None, None, None, None, author.full_name)

    titles = {d["title"] for d in result["results"]}
    assert titles == {"Doc de auteur1"}


def test_date_range_filter(make_doc, make_test_user):
    old_doc = make_doc(
        title="Ancien", department="GENERAL", confidentiality="INTERNAL", pages=[(None, None, 1, "x")],
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    recent_doc = make_doc(
        title="Récent", department="GENERAL", confidentiality="INTERNAL", pages=[(None, None, 1, "x")],
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    employee = make_test_user(username="emp_dates", role="EMPLOYEE")

    result = mcp_server._list_documents_impl(str(employee.id), None, None, "2026-01-01", None, None)
    titles = {d["title"] for d in result["results"]}
    assert titles == {"Récent"}

    result2 = mcp_server._list_documents_impl(str(employee.id), None, None, None, "2025-01-01", None)
    titles2 = {d["title"] for d in result2["results"]}
    assert titles2 == {"Ancien"}


def test_invalid_date_format_returns_error(make_doc, make_test_user):
    employee = make_test_user(username="emp_baddate", role="EMPLOYEE")
    result = mcp_server._list_documents_impl(str(employee.id), None, None, "not-a-date", None, None)
    assert "error" in result
