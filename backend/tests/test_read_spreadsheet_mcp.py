"""MCP-level tests for read_spreadsheet_data (_read_spreadsheet_impl):
ACL-scoped file/sheet resolution, real column/row extraction re-parsed
from the original bytes (monkeypatched download_file - no real MinIO
needed), and the "no fallback" guarantee (wrong file/sheet/department is
always an explicit error, never a different document's data)."""

import io

from openpyxl import Workbook

import app.mcp.server as mcp_server


def _build_xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_spreadsheet_doc(make_doc, db, *, title, sheets, source_filename, department="GENERAL", confidentiality="INTERNAL"):
    doc = make_doc(
        title=title,
        department=department,
        confidentiality=confidentiality,
        pages=[(None, name, i + 1, f"Feuille : {name}\n(placeholder)") for i, name in enumerate(sheets)],
        source_filename=source_filename,
    )
    doc.minio_object_key = "fake-object-key"
    db.commit()
    return doc


def test_reads_real_headers_and_typed_rows(make_doc, make_test_user, db, monkeypatch):
    xlsx_bytes = _build_xlsx_bytes(
        {
            "CA mensuel": [["Mois", "Chiffre d'affaires (TND)"], ["Janvier", 420000], ["Décembre", 610000]],
            "Marketing": [["Mois", "Leads"], ["Janvier", 420]],
        }
    )
    doc = _make_spreadsheet_doc(
        make_doc, db, title="Orbitia_Test_Entreprise_Complet", sheets=["CA mensuel", "Marketing"],
        source_filename="Orbitia_Test_Entreprise_Complet.xlsx",
    )
    monkeypatch.setattr(mcp_server, "download_file", lambda key: xlsx_bytes)
    employee = make_test_user(username="rs_emp1", role="EMPLOYEE")

    result = mcp_server._read_spreadsheet_impl(
        str(employee.id), None, "Orbitia_Test_Entreprise_Complet.xlsx", "CA mensuel"
    )

    assert result["document_id"] == str(doc.id)
    assert result["sheet"] == "CA mensuel"
    assert result["columns"] == ["Mois", "Chiffre d'affaires (TND)"]
    assert result["rows"][0] == {"Mois": "Janvier", "Chiffre d'affaires (TND)": 420000}
    assert result["rows"][-1] == {"Mois": "Décembre", "Chiffre d'affaires (TND)": 610000}
    assert result["row_count"] == 2


def test_different_sheet_same_file_returns_different_data(make_doc, make_test_user, db, monkeypatch):
    xlsx_bytes = _build_xlsx_bytes(
        {
            "CA mensuel": [["Mois", "CA"], ["Janvier", 420000]],
            "Marketing": [["Mois", "Leads"], ["Janvier", 420]],
        }
    )
    _make_spreadsheet_doc(
        make_doc, db, title="Complet", sheets=["CA mensuel", "Marketing"], source_filename="Complet.xlsx"
    )
    monkeypatch.setattr(mcp_server, "download_file", lambda key: xlsx_bytes)
    employee = make_test_user(username="rs_emp2", role="EMPLOYEE")

    ca = mcp_server._read_spreadsheet_impl(str(employee.id), None, "Complet.xlsx", "CA mensuel")
    marketing = mcp_server._read_spreadsheet_impl(str(employee.id), None, "Complet.xlsx", "Marketing")

    assert ca["columns"] != marketing["columns"]
    assert ca["rows"] != marketing["rows"]


def test_unknown_file_is_explicit_error_no_fallback(make_doc, make_test_user, db, monkeypatch):
    _make_spreadsheet_doc(make_doc, db, title="Complet", sheets=["CA mensuel"], source_filename="Complet.xlsx")
    monkeypatch.setattr(mcp_server, "download_file", lambda key: b"")
    employee = make_test_user(username="rs_emp3", role="EMPLOYEE")

    result = mcp_server._read_spreadsheet_impl(str(employee.id), None, "FichierInexistant.xlsx", "CA mensuel")

    assert "error" in result
    assert "columns" not in result


def test_unknown_sheet_lists_available_sheets_no_fallback(make_doc, make_test_user, db, monkeypatch):
    xlsx_bytes = _build_xlsx_bytes({"CA mensuel": [["Mois", "CA"], ["Janvier", 1]]})
    _make_spreadsheet_doc(make_doc, db, title="Complet", sheets=["CA mensuel"], source_filename="Complet.xlsx")
    monkeypatch.setattr(mcp_server, "download_file", lambda key: xlsx_bytes)
    employee = make_test_user(username="rs_emp4", role="EMPLOYEE")

    result = mcp_server._read_spreadsheet_impl(str(employee.id), None, "Complet.xlsx", "Feuille Fantome")

    assert "error" in result
    assert "columns" not in result
    assert "CA mensuel" in result["error"]


def test_ambiguous_sheet_when_not_specified_asks_explicitly(make_doc, make_test_user, db, monkeypatch):
    xlsx_bytes = _build_xlsx_bytes({"A": [["x"], [1]], "B": [["y"], [2]]})
    _make_spreadsheet_doc(make_doc, db, title="Complet", sheets=["A", "B"], source_filename="Complet.xlsx")
    monkeypatch.setattr(mcp_server, "download_file", lambda key: xlsx_bytes)
    employee = make_test_user(username="rs_emp5", role="EMPLOYEE")

    result = mcp_server._read_spreadsheet_impl(str(employee.id), None, "Complet.xlsx", None)

    assert "error" in result
    assert "columns" not in result


def test_single_sheet_auto_resolves_without_sheet_name(make_doc, make_test_user, db, monkeypatch):
    xlsx_bytes = _build_xlsx_bytes({"Seule feuille": [["x"], [1]]})
    _make_spreadsheet_doc(make_doc, db, title="Complet", sheets=["Seule feuille"], source_filename="Complet.xlsx")
    monkeypatch.setattr(mcp_server, "download_file", lambda key: xlsx_bytes)
    employee = make_test_user(username="rs_emp6", role="EMPLOYEE")

    result = mcp_server._read_spreadsheet_impl(str(employee.id), None, "Complet.xlsx", None)

    assert result["sheet"] == "Seule feuille"


def test_department_outside_grant_denied_not_substituted(make_doc, make_test_user, db, monkeypatch):
    xlsx_bytes = _build_xlsx_bytes({"Secret": [["x"], [1]]})
    _make_spreadsheet_doc(
        make_doc, db, title="Confidentiel", sheets=["Secret"], source_filename="Confidentiel.xlsx",
        department="FINANCE", confidentiality="SECRET",
    )
    monkeypatch.setattr(mcp_server, "download_file", lambda key: xlsx_bytes)
    employee = make_test_user(username="rs_emp7", role="EMPLOYEE")  # EMPLOYEE has no FINANCE access

    result = mcp_server._read_spreadsheet_impl(str(employee.id), None, "Confidentiel.xlsx", "Secret")

    assert "error" in result
    assert "columns" not in result


def test_non_xlsx_document_rejected(make_doc, make_test_user, db):
    doc = make_doc(
        title="Rapport", department="GENERAL", confidentiality="INTERNAL",
        pages=[(None, "Feuille1", 1, "texte")], source_filename="Rapport.pdf",
    )
    doc.minio_object_key = "fake-key"
    db.commit()
    employee = make_test_user(username="rs_emp8", role="EMPLOYEE")

    result = mcp_server._read_spreadsheet_impl(str(employee.id), None, "Rapport.pdf", None)

    assert "error" in result
    assert "columns" not in result
