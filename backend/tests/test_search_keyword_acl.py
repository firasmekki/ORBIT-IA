"""Integration tests for the search_keyword MCP tool's ACL enforcement and
silent-leak detection - the part that only a real database can prove,
since it depends on the actual SQL filter in app.mcp.server."""

import app.mcp.server as mcp_server


def test_hr_does_not_count_finance_only_documents(make_doc, make_test_user):
    make_doc(
        title="Grille des salaires",
        department="FINANCE",
        confidentiality="SECRET",
        pages=[(None, None, 1, "Le mot cle confidentiel-paie-2025 figure ici trois fois: confidentiel-paie-2025.")],
    )
    hr = make_test_user(username="rh_test", role="HR")

    result = mcp_server._search_keyword_impl(str(hr.id), "confidentiel-paie-2025", True)

    assert result["total_count"] == 0
    assert result["documents"] == []
    # the tool itself still surfaces the metadata-only signal - it's the
    # orchestrator layer's job (tested separately) to keep it out of the
    # user-visible answer
    assert result["restricted_match"] == {"department": "FINANCE", "confidentiality": "SECRET"}


def test_hr_counts_hr_department_documents(make_doc, make_test_user):
    make_doc(
        title="Politique de congés",
        department="HR",
        confidentiality="CONFIDENTIAL",
        pages=[(None, None, 1, "La politique de conges-unique-token s'applique à tous.")],
    )
    hr = make_test_user(username="rh_test2", role="HR")

    result = mcp_server._search_keyword_impl(str(hr.id), "conges-unique-token", True)

    assert result["total_count"] == 1
    assert result["documents"][0]["title"] == "Politique de congés"
    assert "restricted_match" not in result


def test_director_sees_everything(make_doc, make_test_user):
    make_doc(
        title="Grille des salaires",
        department="FINANCE",
        confidentiality="SECRET",
        pages=[(None, None, 1, "montant-director-visible-xyz apparaît une fois.")],
    )
    director = make_test_user(username="dir_test", role="DIRECTOR")

    result = mcp_server._search_keyword_impl(str(director.id), "montant-director-visible-xyz", True)

    assert result["total_count"] == 1
    assert "restricted_match" not in result


def test_employee_role_has_no_search_keyword_tool_removed_still_works_for_default_role(make_doc, make_test_user):
    # sanity check: EMPLOYEE is in ROLE_TOOLS for search_keyword by default
    # (see app/policy/rules.py) and is scoped to GENERAL only.
    make_doc(
        title="Note générale",
        department="GENERAL",
        confidentiality="INTERNAL",
        pages=[(None, None, 1, "terme-general-partage-abc mentionné ici.")],
    )
    employee = make_test_user(username="emp_test", role="EMPLOYEE")

    result = mcp_server._search_keyword_impl(str(employee.id), "terme-general-partage-abc", True)
    assert result["total_count"] == 1


def test_genuinely_absent_keyword_returns_zero_with_no_restricted_match(make_doc, make_test_user):
    make_doc(
        title="Note générale",
        department="GENERAL",
        confidentiality="INTERNAL",
        pages=[(None, None, 1, "rien à voir ici.")],
    )
    employee = make_test_user(username="emp_test2", role="EMPLOYEE")

    result = mcp_server._search_keyword_impl(str(employee.id), "mot-completement-absent-partout", True)
    assert result["total_count"] == 0
    assert "restricted_match" not in result


def test_whole_word_vs_substring_via_full_pipeline(make_doc, make_test_user):
    make_doc(
        title="Doc",
        department="GENERAL",
        confidentiality="INTERNAL",
        pages=[(None, None, 1, "firasment n'est pas firas ni firasco.")],
    )
    employee = make_test_user(username="emp_test3", role="EMPLOYEE")

    whole_word_result = mcp_server._search_keyword_impl(str(employee.id), "firas", True)
    substring_result = mcp_server._search_keyword_impl(str(employee.id), "firas", False)

    assert whole_word_result["total_count"] == 1
    assert substring_result["total_count"] == 3
