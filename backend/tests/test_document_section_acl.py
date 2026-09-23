"""Integration tests for the get_document_section MCP tool: ACL enforcement,
the identical-message requirement for nonexistent vs unauthorized
documents, alert triggering, and title resolution - against a real
database."""

import uuid

import app.mcp.server as mcp_server


def test_authorized_read_by_page(make_doc, make_test_user):
    doc = make_doc(
        title="Procédure qualité",
        department="GENERAL",
        confidentiality="INTERNAL",
        pages=[(1, None, 1, "Contenu de la page 1"), (2, None, 1, "Contenu de la page 2")],
    )
    employee = make_test_user(username="emp_sec1", role="EMPLOYEE")

    result = mcp_server._get_document_section_impl(str(employee.id), str(doc.id), None, 2, None, 0)

    assert "error" not in result
    assert result["text"] == "Contenu de la page 2"
    assert result["page"] == 2
    assert result["title"] == "Procédure qualité"


def test_authorized_read_by_section(make_doc, make_test_user):
    doc = make_doc(
        title="Manuel",
        department="GENERAL",
        confidentiality="INTERNAL",
        pages=[(None, "Introduction", 1, "Texte intro"), (None, "Bloc 2", 20, "Texte bloc 2")],
    )
    employee = make_test_user(username="emp_sec2", role="EMPLOYEE")

    result = mcp_server._get_document_section_impl(str(employee.id), str(doc.id), None, None, "Bloc 2", 0)

    assert result["text"] == "Texte bloc 2"
    assert result["section"] == "Bloc 2"


def test_nonexistent_and_unauthorized_return_identical_response(make_doc, make_test_user):
    doc = make_doc(
        title="Grille des salaires",
        department="FINANCE",
        confidentiality="SECRET",
        pages=[(1, None, 1, "confidentiel")],
    )
    employee = make_test_user(username="emp_sec3", role="EMPLOYEE")

    nonexistent_result = mcp_server._get_document_section_impl(str(employee.id), str(uuid.uuid4()), None, 1, None, 0)
    unauthorized_result = mcp_server._get_document_section_impl(str(employee.id), str(doc.id), None, 1, None, 0)

    assert nonexistent_result == unauthorized_result
    assert nonexistent_result == {"error": mcp_server.DOCUMENT_NOT_FOUND_OR_DENIED}


def test_alert_only_fires_for_genuinely_existing_unauthorized_document(make_doc, make_test_user, db):
    from app.models.alert import Alert

    doc = make_doc(
        title="Grille des salaires 2",
        department="FINANCE",
        confidentiality="SECRET",
        pages=[(1, None, 1, "confidentiel")],
    )
    employee = make_test_user(username="emp_sec4", role="EMPLOYEE")

    assert db.query(Alert).count() == 0

    mcp_server._get_document_section_impl(str(employee.id), str(uuid.uuid4()), None, 1, None, 0)
    assert db.query(Alert).count() == 0, "a nonexistent document must never raise an alert"

    mcp_server._get_document_section_impl(str(employee.id), str(doc.id), None, 1, None, 0)
    assert db.query(Alert).count() == 1, "a real, unauthorized document must raise an alert"
    alert = db.query(Alert).first()
    assert alert.alert_type == "DOCUMENT_ACCESS_DENIED"
    assert doc.title in alert.description


def test_page_not_found_in_authorized_document_is_a_distinct_message(make_doc, make_test_user):
    doc = make_doc(
        title="Doc court",
        department="GENERAL",
        confidentiality="INTERNAL",
        pages=[(1, None, 1, "seule page")],
    )
    employee = make_test_user(username="emp_sec5", role="EMPLOYEE")

    result = mcp_server._get_document_section_impl(str(employee.id), str(doc.id), None, 99, None, 0)

    assert result["error"] != mcp_server.DOCUMENT_NOT_FOUND_OR_DENIED
    assert "99" in result["error"]
    assert doc.title in result["error"]


def test_section_not_found_in_authorized_document(make_doc, make_test_user):
    doc = make_doc(
        title="Doc sections",
        department="GENERAL",
        confidentiality="INTERNAL",
        pages=[(None, "Introduction", 1, "texte")],
    )
    employee = make_test_user(username="emp_sec6", role="EMPLOYEE")

    result = mcp_server._get_document_section_impl(str(employee.id), str(doc.id), None, None, "Conclusion", 0)

    assert result["error"] != mcp_server.DOCUMENT_NOT_FOUND_OR_DENIED
    assert "Conclusion" in result["error"]


def test_long_section_is_truncated_with_offset(make_doc, make_test_user):
    long_text = "x" * 3000
    doc = make_doc(title="Long doc", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, long_text)])
    employee = make_test_user(username="emp_sec7", role="EMPLOYEE")

    result = mcp_server._get_document_section_impl(str(employee.id), str(doc.id), None, 1, None, 0)
    assert result["truncated"] is True
    assert len(result["text"]) == mcp_server.MAX_SECTION_CHARS
    assert result["total_length"] == 3000

    continuation = mcp_server._get_document_section_impl(
        str(employee.id), str(doc.id), None, 1, None, mcp_server.MAX_SECTION_CHARS
    )
    assert continuation["offset"] == mcp_server.MAX_SECTION_CHARS
    assert continuation["truncated"] is False
    assert len(continuation["text"]) == 3000 - mcp_server.MAX_SECTION_CHARS


def test_title_resolution_single_match_opens_document(make_doc, make_test_user):
    doc = make_doc(
        title="Politique de congés",
        department="GENERAL",
        confidentiality="INTERNAL",
        pages=[(1, None, 1, "texte congés")],
    )
    employee = make_test_user(username="emp_sec8", role="EMPLOYEE")

    result = mcp_server._get_document_section_impl(str(employee.id), None, "politique de conges", 1, None, 0)
    assert result["title"] == doc.title
    assert result["text"] == "texte congés"


def test_title_resolution_multiple_matches_returns_choices(make_doc, make_test_user):
    make_doc(title="Procédure A", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, "a")])
    make_doc(title="Procédure B", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, "b")])
    employee = make_test_user(username="emp_sec9", role="EMPLOYEE")

    result = mcp_server._get_document_section_impl(str(employee.id), None, "Procédure", 1, None, 0)
    assert "choices" in result
    assert len(result["choices"]) == 2
    titles = {c["title"] for c in result["choices"]}
    assert titles == {"Procédure A", "Procédure B"}


def test_title_resolution_no_match_among_authorized_returns_identical_message(make_doc, make_test_user):
    make_doc(title="Grille des salaires confidentielle", department="FINANCE", confidentiality="SECRET", pages=[(1, None, 1, "x")])
    employee = make_test_user(username="emp_sec10", role="EMPLOYEE")

    result = mcp_server._get_document_section_impl(str(employee.id), None, "Grille des salaires confidentielle", 1, None, 0)
    assert result == {"error": mcp_server.DOCUMENT_NOT_FOUND_OR_DENIED}


def test_title_resolution_only_searches_authorized_documents(make_doc, make_test_user):
    # A FINANCE-only doc must never appear as a choice/candidate for an
    # EMPLOYEE (GENERAL-only) title search, even as a partial match.
    make_doc(title="Rapport Finance Confidentiel", department="FINANCE", confidentiality="SECRET", pages=[(1, None, 1, "x")])
    make_doc(title="Rapport Général", department="GENERAL", confidentiality="INTERNAL", pages=[(1, None, 1, "y")])
    employee = make_test_user(username="emp_sec11", role="EMPLOYEE")

    result = mcp_server._get_document_section_impl(str(employee.id), None, "Rapport", 1, None, 0)
    assert "choices" not in result or all("Finance" not in c["title"] for c in result.get("choices", []))
    assert result.get("title") != "Rapport Finance Confidentiel"


def test_access_revoked_between_turns_is_denied_on_reopen(make_doc, make_test_user, db):
    doc = make_doc(
        title="Doc temporairement accessible",
        department="FINANCE",
        confidentiality="CONFIDENTIAL",
        pages=[(1, None, 1, "x")],
    )
    accountant = make_test_user(username="acc_sec1", role="ACCOUNTANT")

    first = mcp_server._get_document_section_impl(str(accountant.id), str(doc.id), None, 1, None, 0)
    assert "error" not in first

    # Simulate a Director revoking this user's role/access between turns.
    accountant.role = "EMPLOYEE"
    db.commit()

    second = mcp_server._get_document_section_impl(str(accountant.id), str(doc.id), None, 1, None, 0)
    assert second == {"error": mcp_server.DOCUMENT_NOT_FOUND_OR_DENIED}
