"""Server folder sources: the backend scans a mounted folder itself
(app/services/source_scanner.py), inventories EVERY file, indexes only new
or changed ones, never retires anything on an unreachable/empty folder;
the chat can be narrowed to sub-folders (Grant.folder_scope) without any
re-indexing; a document that keeps failing never blocks the queues."""

import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

import app.mcp.server as mcp_server
import app.routers.workspaces as workspaces_router
from app.core.config import get_settings
from app.models.audit import AuditLog
from app.models.document import Document
from app.models.source_file import SourceFile
from app.models.workspace import Workspace
from app.policy.engine import check_document_in_scope
from app.policy.rules import resolve_effective_grant
from app.policy.workspace import grant_in_workspace
from app.rag.embeddings import EmbeddingServiceError
from app.routers.chat import _folder_scope
from app.services import embedding_worker, extraction_worker, source_roots, source_scanner, workspace_sync
from app.services.errors import ServiceError
from tests.conftest import hot_vector


@pytest.fixture(autouse=True)
def _fake_services(monkeypatch, tmp_path):
    monkeypatch.setattr("app.rag.retriever.embed_text", lambda query: hot_vector(0))
    monkeypatch.setattr("app.rag.ingest.embed_texts", lambda texts: [hot_vector(0) for _ in texts])
    stored: dict[str, bytes] = {}
    monkeypatch.setattr("app.services.workspace_sync.upload_file", lambda key, content, **k: stored.__setitem__(key, content))
    monkeypatch.setattr(extraction_worker, "download_file", lambda key: stored[key])
    settings = get_settings()
    monkeypatch.setattr(settings, "source_mount_base", str(tmp_path / "sources"))
    monkeypatch.setattr(settings, "source_root_1_host", "C:\\Users\\ORBIT\\Desktop")
    monkeypatch.setattr(settings, "source_root_2_host", "")
    monkeypatch.setattr(settings, "source_root_3_host", "")
    (tmp_path / "sources" / "1").mkdir(parents=True)
    return stored


@pytest.fixture
def desktop(tmp_path):
    """The mounted root (/sources/1 <-> C:\\Users\\ORBIT\\Desktop)."""
    return tmp_path / "sources" / "1"


def _write(path, content: bytes, *, mtime: datetime | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))


def _source_workspace(db, owner, path="projets", name="projets"):
    ws = Workspace(name=name, owner_id=owner.id, source_root="1", source_path=path, source_status="idle")
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def _projets(desktop):
    root = desktop / "projets"
    _write(root / "BIC" / "rapport BIC.txt", b"Audit BIC : THDi mesure 6,2 pourcent sur le transformateur T1.")
    _write(root / "BIC" / "photos" / "schema.md", b"# Schema BIC\n\nTableau general basse tension.")
    _write(root / "Delice CLN" / "offre.txt", b"Offre Delice CLN : batterie de condensateurs 400 kvar.")
    _write(root / "Delice CLN" / "archive.rar", b"Rar!\x1a\x07\x00 binary")
    _write(root / "Delice CLN" / "vide.txt", b"")
    _write(root / "Delice CLN" / "~$offre.docx", b"lock")
    _write(root / "Delice CLN" / "Thumbs.db", b"x")
    _write(root / "CAPCONDO" / "app" / "node_modules" / "lib" / "index.js", b"module.exports = 1")
    _write(root / "CAPCONDO" / "logs" / "journal chantier.txt", b"Journal du chantier CAPCONDO.")
    return root


def _states(db, ws):
    return {r.relative_path: (r.state, r.reason) for r in db.query(SourceFile).filter(SourceFile.workspace_id == ws.id)}


# --- the scan -------------------------------------------------------------------------------------


def test_scan_inventories_every_file_and_indexes_the_supported_ones(db, make_test_user, desktop):
    owner = make_test_user(username="src_owner", role="DIRECTOR")
    _projets(desktop)
    ws = _source_workspace(db, owner)

    result = source_scanner.scan_workspace(db, ws)

    states = _states(db, ws)
    assert states["BIC/rapport BIC.txt"][0] == "document"
    assert states["BIC/photos/schema.md"][0] == "document"
    assert states["Delice CLN/offre.txt"][0] == "document"
    # a business folder called "logs" is NOT skipped
    assert states["CAPCONDO/logs/journal chantier.txt"][0] == "document"
    assert states["Delice CLN/archive.rar"] == ("ignored", "format .rar non pris en charge")
    assert states["Delice CLN/vide.txt"] == ("ignored", "fichier vide")
    assert states["Delice CLN/~$offre.docx"] == ("ignored", "fichier temporaire Office")
    assert states["Delice CLN/Thumbs.db"] == ("ignored", "fichier système")
    # a tool directory is recorded (with its reason), not walked
    assert states["CAPCONDO/app/node_modules/"][0] == "ignored"
    assert not any("index.js" in path for path in states)
    assert (result.queued, result.ignored, result.removed) == (4, 5, 0)

    docs = db.query(Document).filter(Document.workspace_id == ws.id).all()
    assert sorted(d.relative_path for d in docs) == [
        "BIC/photos/schema.md", "BIC/rapport BIC.txt", "CAPCONDO/logs/journal chantier.txt", "Delice CLN/offre.txt",
    ]
    assert all(d.status == "queued" and d.origin == "sync" for d in docs)

    summary = source_scanner.inventory_summary(db, ws)
    assert summary["detected_files"] == 8 and summary["excluded_directories"] == 1
    assert summary["documents"]["queued"] == 4 and summary["complete"] is False


def test_rescan_does_not_reread_unchanged_files_and_picks_up_changes(db, make_test_user, desktop, monkeypatch):
    owner = make_test_user(username="src_rescan", role="DIRECTOR")
    root = _projets(desktop)
    ws = _source_workspace(db, owner)
    source_scanner.scan_workspace(db, ws)

    calls = []
    real_index_file = workspace_sync.index_file
    monkeypatch.setattr(
        workspace_sync, "index_file", lambda *a, **k: calls.append(a[3]) or real_index_file(*a, **k)
    )
    again = source_scanner.scan_workspace(db, ws)
    assert calls == [] and again.unchanged == 4 and again.queued == 0

    _write(root / "BIC" / "rapport BIC.txt", b"Audit BIC revu : THDi 4,1 pourcent.", mtime=datetime.now(UTC) + timedelta(hours=1))
    _write(root / "BIC" / "nouveau.txt", b"Nouveau releve BIC.")
    changed = source_scanner.scan_workspace(db, ws)
    assert sorted(calls) == ["BIC/nouveau.txt", "BIC/rapport BIC.txt"] and changed.queued == 2
    doc = db.query(Document).filter(Document.workspace_id == ws.id, Document.relative_path == "BIC/rapport BIC.txt").one()
    assert doc.status == "queued"


def test_deleted_file_is_retired_but_kept_for_the_audit(db, make_test_user, desktop):
    owner = make_test_user(username="src_delete", role="DIRECTOR")
    root = _projets(desktop)
    ws = _source_workspace(db, owner)
    source_scanner.scan_workspace(db, ws)

    (root / "Delice CLN" / "offre.txt").unlink()
    (root / "Delice CLN" / "archive.rar").unlink()
    result = source_scanner.scan_workspace(db, ws)

    assert result.removed == 1
    doc = db.query(Document).filter(Document.relative_path == "Delice CLN/offre.txt").one()
    assert doc.is_active is False and doc.status == "removed"
    states = _states(db, ws)
    assert "Delice CLN/offre.txt" not in states and "Delice CLN/archive.rar" not in states
    assert db.query(AuditLog).filter(AuditLog.action == "WORKSPACE_FILE_REMOVED").count() == 1


def test_unreachable_or_suddenly_empty_folder_never_wipes_the_index(db, make_test_user, desktop):
    owner = make_test_user(username="src_missing", role="DIRECTOR")
    root = _projets(desktop)
    ws = _source_workspace(db, owner)
    source_scanner.scan_workspace(db, ws)
    active_before = db.query(Document).filter(Document.workspace_id == ws.id, Document.is_active.is_(True)).count()

    # emptied (or a mount that came back empty): refused, nothing retired
    for path in sorted(root.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    with pytest.raises(RuntimeError):
        source_scanner.scan_workspace(db, ws)
    db.rollback()

    # gone altogether: reported as "missing" by the loop, nothing retired either
    root.rmdir()
    ws.source_scan_requested_at = datetime.now(UTC)
    db.commit()
    assert source_scanner.run_next_scan() is True
    db.expire_all()
    ws = db.get(Workspace, ws.id)
    assert ws.source_status == "missing" and "introuvable" in ws.source_error
    assert db.query(Document).filter(Document.workspace_id == ws.id, Document.is_active.is_(True)).count() == active_before


def test_paused_source_is_not_scanned(db, make_test_user, desktop):
    owner = make_test_user(username="src_pause", role="DIRECTOR")
    _projets(desktop)
    ws = _source_workspace(db, owner)
    ws.source_enabled = False
    ws.source_scan_requested_at = datetime.now(UTC)
    db.commit()
    assert source_scanner.run_next_scan() is False


# --- choosing the folder ------------------------------------------------------------------------------


def test_windows_paths_are_understood_and_cannot_escape_the_root(desktop):
    (desktop / "projets" / "BIC").mkdir(parents=True)
    root, relative = source_roots.parse_host_path('"C:\\Users\\ORBIT\\Desktop\\projets\\BIC\\"')
    assert (root.id, relative) == ("1", "projets/BIC")
    assert source_roots.parse_host_path("c:/users/orbit/desktop")[1] == ""
    with pytest.raises(ServiceError):
        source_roots.parse_host_path("D:\\Archives")
    for bad in ("../secret", "projets/../../etc", "C:/Windows"):
        with pytest.raises(ServiceError):
            source_roots.resolve_dir(root, bad)
    listing = source_roots.list_subfolders(root, "projets")
    assert listing["folders"] == ["BIC"] and listing["host_path"] == "C:\\Users\\ORBIT\\Desktop\\projets"


def test_connecting_a_folder_is_director_only_and_refuses_overlaps(db, make_test_user, desktop):
    director = make_test_user(username="src_dir", role="DIRECTOR")
    employee = make_test_user(username="src_emp", role="EMPLOYEE")
    _projets(desktop)

    with pytest.raises(HTTPException) as exc:
        workspaces_router._validate_new_source(db, employee, "1", "projets")
    assert exc.value.status_code == 403

    created = workspaces_router.create_workspace(
        workspaces_router.WorkspaceCreate(name="projets", source_root="1", source_path="projets"), db=db, user=director
    )
    assert created.source.host_path == "C:\\Users\\ORBIT\\Desktop\\projets" and created.source.status == "idle"

    # a client sub-folder of a connected folder: use it as a chat scope instead
    with pytest.raises(HTTPException) as exc:
        workspaces_router._validate_new_source(db, director, "1", "projets/BIC")
    assert exc.value.status_code == 409 and "périmètre" in exc.value.detail
    with pytest.raises(HTTPException) as exc:
        workspaces_router._validate_new_source(db, director, "1", "")
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        workspaces_router._validate_new_source(db, director, "1", "nexiste/pas")
    assert exc.value.status_code == 404


def test_a_server_source_workspace_refuses_browser_sync(db, make_test_user, desktop):
    owner = make_test_user(username="src_nobrowser", role="DIRECTOR")
    ws = _source_workspace(db, owner)
    with pytest.raises(HTTPException) as exc:
        workspaces_router._browser_syncable(db, owner, ws.id)
    assert exc.value.status_code == 409


# --- chat scope ---------------------------------------------------------------------------------------


def test_folder_scope_narrows_every_search_without_reindexing(db, make_test_user, desktop):
    owner = make_test_user(username="src_scope", role="DIRECTOR")
    _projets(desktop)
    ws = _source_workspace(db, owner)
    source_scanner.scan_workspace(db, ws)
    while extraction_worker_process():
        pass
    while embedding_worker.process_next_document():
        pass
    uid, wsid = str(owner.id), str(ws.id)

    everywhere = mcp_server._search_documents_impl(uid, "BIC Delice", 10, workspace_id=wsid)
    assert {r["relative_path"].split("/")[0] for r in everywhere["results"]} >= {"BIC", "Delice CLN"}

    only_bic = mcp_server._search_documents_impl(uid, "BIC Delice", 10, workspace_id=wsid, folder_scope=("BIC/",))
    assert only_bic["results"] and all(r["relative_path"].startswith("BIC/") for r in only_bic["results"])

    exact = mcp_server._search_document_text_impl(uid, "Delice", False, True, False, workspace_id=wsid, folder_scope=("BIC/",))
    assert exact["total_occurrences"] == 0
    exact_cln = mcp_server._search_document_text_impl(
        uid, "condensateurs", False, True, False, workspace_id=wsid, folder_scope=("Delice CLN/",)
    )
    assert [d["relative_path"] for d in exact_cln["documents"]] == ["Delice CLN/offre.txt"]

    # "BIC/" never matches a sibling folder "BICX/"
    _, grant = mcp_server._load_user_and_grant(db, uid, wsid, ("BIC/",))
    offre = db.query(Document).filter(Document.relative_path == "Delice CLN/offre.txt").one()
    assert check_document_in_scope(grant=grant, document=offre).allowed is False
    full_grant = grant_in_workspace(resolve_effective_grant(owner), ws, db)
    assert check_document_in_scope(grant=full_grant, document=offre).allowed is True


def extraction_worker_process():
    return extraction_worker.process_next_extraction()


def test_chat_folder_scope_is_validated():
    assert _folder_scope(["BIC", "Delice CLN/", "BIC"]) == ["BIC/", "Delice CLN/"]
    assert _folder_scope(None) == []
    for bad in (["../etc"], ["C:/Windows"], ["/"]):
        with pytest.raises(HTTPException):
            _folder_scope(bad)


# --- queues never blocked by one bad file -----------------------------------------------------------------------


def _queued_doc(db, owner, ws, path, content):
    doc, _ = workspace_sync.index_file(db, owner, ws, path, content, defer_extraction=True)
    return doc


def test_a_file_that_crashes_the_extractor_is_failed_after_three_attempts(db, make_test_user, monkeypatch):
    owner = make_test_user(username="src_crash", role="DIRECTOR")
    ws = Workspace(name="crash", owner_id=owner.id)
    db.add(ws)
    db.commit()
    bad = _queued_doc(db, owner, ws, "corrompu.txt", b"bad bytes")
    good = _queued_doc(db, owner, ws, "sain.txt", b"Contenu lisible.")
    contents = {bad.minio_object_key: b"bad bytes", good.minio_object_key: b"Contenu lisible."}
    monkeypatch.setattr(extraction_worker, "download_file", lambda key: contents[key])
    real_extract = extraction_worker.extract

    def extract(filename, content):
        if filename == "corrompu.txt":
            raise ValueError("struct.error: unpack requires a buffer")
        return real_extract(filename, content)

    monkeypatch.setattr(extraction_worker, "extract", extract)
    for _ in range(6):
        extraction_worker.process_next_extraction()
    db.expire_all()
    bad, good = db.get(Document, bad.id), db.get(Document, good.id)
    assert bad.status == "failed" and bad.index_attempts == 3 and "unpack requires" in bad.index_error
    assert good.status == "indexing" and good.index_error is None

    # retried on demand, with a fresh counter
    assert workspaces_router.retry_failed(ws.id, db=db, user=owner) == {"requeued": 1}
    db.expire_all()
    assert db.get(Document, bad.id).status == "queued" and db.get(Document, bad.id).index_attempts == 0


def test_a_document_the_embedding_service_rejects_does_not_block_the_queue(db, make_test_user, monkeypatch):
    owner = make_test_user(username="src_poison", role="DIRECTOR")
    ws = Workspace(name="poison", owner_id=owner.id)
    db.add(ws)
    db.commit()
    poison, _ = workspace_sync.index_file(db, owner, ws, "poison.txt", b"POISON texte refuse.")
    fine, _ = workspace_sync.index_file(db, owner, ws, "fine.txt", b"Texte normal.")

    def embed_texts(texts):
        if any("POISON" in t for t in texts):
            raise EmbeddingServiceError("HTTP 500 from the embedding service")
        return [hot_vector(0) for _ in texts]

    monkeypatch.setattr("app.rag.ingest.embed_texts", embed_texts)
    for _ in range(5):
        embedding_worker.process_next_document()
    db.expire_all()
    assert db.get(Document, fine.id).status == "indexed"
    poison = db.get(Document, poison.id)
    assert poison.status == "failed" and poison.index_attempts == 3
    assert "recherche exacte" in poison.index_error

    # the service itself down: nothing is marked failed, the worker waits
    other, _ = workspace_sync.index_file(db, owner, ws, "other.txt", b"POISON encore.")
    monkeypatch.setattr("app.rag.ingest.embed_texts", lambda texts: (_ for _ in ()).throw(EmbeddingServiceError("down")))
    with pytest.raises(EmbeddingServiceError):
        embedding_worker.process_next_document()
    db.expire_all()
    assert db.get(Document, other.id).status == "indexing" and db.get(Document, other.id).index_attempts == 0
