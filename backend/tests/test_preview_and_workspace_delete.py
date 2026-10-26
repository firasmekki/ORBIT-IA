"""Original-file preview (an image is served as an image, the OCR text stays
the searchable content) and removing a workspace from the app - against a
real database, through the real routers."""

import pytest
from fastapi import HTTPException

import app.mcp.server as mcp_server
import app.rag.extract as extract_mod
import app.routers.documents as documents_router
import app.routers.workspaces as workspaces_router
from app.models.alert import Alert
from app.models.audit import AuditLog
from app.models.document import Document
from app.models.workspace import Workspace
from app.policy.workspace import WorkspaceAccessError, resolve_workspace
from app.schemas.document import DocumentSummary
from app.services import workspace_sync
from tests.conftest import hot_vector

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake"
OCR_TEXT = "Budget IT / Infrastructure 2025 : 800%"


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    monkeypatch.setattr("app.rag.retriever.embed_text", lambda query: hot_vector(0))
    monkeypatch.setattr("app.rag.ingest.embed_texts", lambda texts: [hot_vector(0) for _ in texts])
    monkeypatch.setattr("app.services.workspace_sync.upload_file", lambda *a, **k: None)
    monkeypatch.setattr("app.routers.documents.download_file", lambda key: PNG_BYTES)


def _image_doc(make_doc, db, *, filename="Capture d'écran 2026-10-02 094632.png", department="GENERAL", confidentiality="INTERNAL"):
    doc = make_doc(
        title="image", department=department, confidentiality=confidentiality,
        pages=[(None, None, 1, OCR_TEXT)], source_filename=filename,
    )
    doc.minio_object_key = "k"
    db.commit()
    return doc


def _body(response) -> bytes:
    return b"".join(response.body_iterator) if hasattr(response.body_iterator, "__iter__") else b""


def test_an_image_is_served_as_an_image_for_preview_and_as_a_file_for_download(db, make_doc, make_test_user):
    employee = make_test_user(username="pv_emp", role="EMPLOYEE")
    doc = _image_doc(make_doc, db)

    preview = documents_router.download_document_file(doc.id, inline=True, db=db, user=employee)
    assert preview.media_type == "image/png"
    disposition = preview.headers["content-disposition"]
    assert disposition.startswith("inline;")
    assert "filename*=UTF-8''Capture%20d%27%C3%A9cran%202026-10-02%20094632.png" in disposition

    download = documents_router.download_document_file(doc.id, inline=False, db=db, user=employee)
    assert download.headers["content-disposition"].startswith("attachment;")

    # the OCR text is still what the document "contains" - for search and the assistant
    assert documents_router.get_document(doc.id, db=db, user=employee).content == OCR_TEXT
    summary = DocumentSummary.model_validate(doc, from_attributes=True)
    assert summary.file_mime_type == "image/png"  # older rows: from the extension


def test_a_name_outside_latin1_never_breaks_the_header(db, make_doc, make_test_user):
    employee = make_test_user(username="pv_emp2", role="EMPLOYEE")
    doc = _image_doc(make_doc, db, filename="صورة ’rapport’.jpg")
    response = documents_router.download_document_file(doc.id, inline=True, db=db, user=employee)
    assert response.media_type == "image/jpeg"
    response.headers["content-disposition"].encode("latin-1")  # would raise if not encodable


def test_preview_keeps_the_same_permission_check(db, make_doc, make_test_user):
    employee = make_test_user(username="pv_emp3", role="EMPLOYEE")
    secret = _image_doc(make_doc, db, department="FINANCE", confidentiality="SECRET")
    with pytest.raises(HTTPException) as exc:
        documents_router.download_document_file(secret.id, inline=True, db=db, user=employee)
    assert exc.value.status_code == 403


def test_webp_images_are_ocrd_like_png(monkeypatch):
    from io import BytesIO

    from PIL import Image

    monkeypatch.setattr(extract_mod, "_ocr_image_bytes", lambda b: "texte webp")
    webp = BytesIO()
    Image.new("RGB", (8, 8), "white").save(webp, format="WEBP")
    assert extract_mod.extract("photo.webp", webp.getvalue()).text == "texte webp"


# --- removing a workspace from the app -----------------------------------------------------------


def _synced_workspace(db, owner, name="orbitaitest"):
    ws = Workspace(
        name=name, owner_id=owner.id, default_department="GENERAL", default_confidentiality="INTERNAL",
        visibility="organization",
    )
    db.add(ws)
    db.commit()
    workspace_sync.index_file(db, owner, ws, "notes.txt", b"Projet Quokka.")
    return ws


def test_owner_removes_a_workspace_from_the_app_for_everyone_without_erasing_anything(db, make_test_user):
    owner = make_test_user(username="del_dir", role="DIRECTOR")
    hr = make_test_user(username="del_hr", role="HR")
    ws = _synced_workspace(db, owner)
    ws_id = ws.id

    with pytest.raises(HTTPException) as exc:  # not the owner
        workspaces_router.delete_workspace(ws_id, db=db, user=hr)
    assert exc.value.status_code == 403

    workspaces_router.delete_workspace(ws_id, db=db, user=owner)

    for person in (owner, hr):
        assert ws_id not in [w.id for w in workspaces_router.list_workspaces(db=db, user=person)]
        with pytest.raises(WorkspaceAccessError):
            resolve_workspace(db, person, ws_id)
    hits = mcp_server._search_document_text_impl(str(owner.id), "Quokka", False, True, False, workspace_id=str(ws_id))
    assert "error" in hits or hits.get("total_occurrences", 0) == 0
    # nothing erased: rows kept (retired) for the audit trail; no security alert for a deleted id
    assert db.query(Document).filter(Document.workspace_id == ws_id, Document.is_active.is_(False)).count() == 1
    assert db.get(Workspace, ws_id) is not None
    assert db.query(Alert).count() == 0
    audit = db.query(AuditLog).filter(AuditLog.action == "WORKSPACE_DELETED").one()
    assert audit.extra["documents_retired"] == 1

    # the same folder can be connected again later, as a fresh workspace
    again = workspaces_router.create_workspace(workspaces_router.WorkspaceCreate(name="orbitaitest"), db=db, user=owner)
    assert again.id != ws_id


def test_the_default_workspace_cannot_be_removed(db, make_test_user):
    director = make_test_user(username="del_dir2", role="DIRECTOR")
    default = resolve_workspace(db, director, None)
    with pytest.raises(HTTPException) as exc:
        workspaces_router.delete_workspace(default.id, db=db, user=director)
    assert exc.value.status_code == 403
