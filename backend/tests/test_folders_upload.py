"""Uploads from any PC into a workspace folder, and folders shared "like
Dropbox" - against a real database, through the real routers and the one
SQL visibility filter.

Scenario: the server PC syncs the company folder (shared with everyone);
HR drops a file from another PC into it - it lands in the chosen folder,
shows who added it, and survives the next sync of the server's folder;
the Director restricts a folder and shares it with one person only."""

import io
import itertools

import pytest
from fastapi import HTTPException, UploadFile

import app.mcp.server as mcp_server
import app.routers.documents as documents_router
import app.routers.workspaces as workspaces_router
from app.models.audit import AuditLog
from app.models.document import Document
from app.models.workspace import Workspace
from app.policy.engine import check_document_visibility, document_scope, outside_document_scope
from app.policy.rules import resolve_effective_grant
from app.policy.workspace import grant_in_workspace
from app.services import embedding_worker, workspace_sync
from tests.conftest import hot_vector


@pytest.fixture(autouse=True)
def _fake_embeddings_and_storage(monkeypatch):
    monkeypatch.setattr("app.rag.retriever.embed_text", lambda query: hot_vector(0))
    monkeypatch.setattr("app.rag.ingest.embed_texts", lambda texts: [hot_vector(0) for _ in texts])
    monkeypatch.setattr("app.services.workspace_sync.upload_file", lambda *a, **k: None)
    monkeypatch.setattr("app.routers.documents.upload_file", lambda *a, **k: None)


def _workspace(db, owner, *, visibility="organization", name="orbitaitest"):
    ws = Workspace(
        name=name, owner_id=owner.id, default_department="GENERAL", default_confidentiality="INTERNAL",
        visibility=visibility,
    )
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


async def _upload(db, user, ws, filename, content, *, folder=None, department="GENERAL", confidentiality="INTERNAL"):
    # upload_document is a plain `def` (runs in the thread pool): not awaited.
    return documents_router.upload_document(
        title=filename, department=department, confidentiality=confidentiality,
        file=UploadFile(file=io.BytesIO(content), filename=filename), workspace_id=ws.id, folder=folder,
        db=db, user=user,
    )


def _titles(db, user, ws):
    return sorted(d.title for d in documents_router.list_documents(workspace_id=ws.id, db=db, user=user))


def _sync(db, owner, ws, files):
    manifest = [
        workspace_sync.ManifestEntry(relative_path=p, sha256=__import__("hashlib").sha256(c).hexdigest(), size=len(c))
        for p, c in files.items()
    ]
    plan = workspace_sync.plan_sync(db, owner, ws, manifest, "orbitaitest")
    for path in plan["to_upload"]:
        workspace_sync.index_file(db, owner, ws, path, files[path])
    return plan


@pytest.fixture
def team(make_test_user):
    return {
        "director": make_test_user(username="fu_dir", role="DIRECTOR"),
        "hr": make_test_user(username="fu_hr", role="HR"),
        "dev": make_test_user(username="fu_dev", role="DEVELOPER"),
        "emp": make_test_user(username="fu_emp", role="EMPLOYEE"),
    }


async def test_upload_from_another_pc_lands_in_the_folder_shows_its_author_and_survives_the_sync(db, team):
    ws = _workspace(db, team["director"])
    _sync(db, team["director"], ws, {"Commun/note.md": b"Note commune."})

    doc = await _upload(db, team["hr"], ws, "planning.xlsx.txt", b"Planning equipe : Quokka.", folder="Commun")
    assert doc.relative_path == "Commun/planning.xlsx.txt" and doc.origin == "upload"
    assert doc.added_by_name == "fu_hr"
    # readable and exact-searchable right away, by everyone allowed
    assert "planning.xlsx.txt" in _titles(db, team["emp"], ws)
    hits = mcp_server._search_document_text_impl(str(team["emp"].id), "Quokka", False, True, False, workspace_id=str(ws.id))
    assert hits["total_occurrences"] == 1
    while embedding_worker.process_next_document():
        pass

    # the server PC syncs its folder again - which does not contain the upload
    plan = _sync(db, team["director"], ws, {"Commun/note.md": b"Note commune."})
    assert plan["removed"] == []
    assert "planning.xlsx.txt" in _titles(db, team["dev"], ws)
    assert db.query(AuditLog).filter(AuditLog.action == "DOCUMENT_UPLOADED", AuditLog.username == "fu_hr").count() == 1


async def test_same_name_in_the_same_folder_is_refused_not_overwritten(db, team):
    ws = _workspace(db, team["director"])
    await _upload(db, team["hr"], ws, "a.txt", b"Version RH.", folder="Commun")
    with pytest.raises(HTTPException) as exc:
        await _upload(db, team["dev"], ws, "a.txt", b"Version dev.", folder="Commun/")
    assert exc.value.status_code == 409
    await _upload(db, team["dev"], ws, "a.txt", b"Version dev.", folder="Tech")  # other folder: fine


async def test_upload_can_never_raise_a_file_above_ones_own_level(db, team):
    ws = _workspace(db, team["director"])
    with pytest.raises(HTTPException) as exc:
        await _upload(db, team["dev"], ws, "b.txt", b"x", department="FINANCE", confidentiality="SECRET")
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await _upload(db, team["dev"], ws, "c.txt", b"x", folder="../hors")
    assert exc.value.status_code == 422


async def test_restricted_folder_is_seen_only_by_the_people_it_is_shared_with(db, team):
    ws = _workspace(db, team["director"])
    _sync(db, team["director"], ws, {"Direction/plan.md": b"Plan Quokka.", "Commun/note.md": b"Note."})
    director, hr, dev = team["director"], team["hr"], team["dev"]

    workspaces_router.set_folder_restriction(
        ws.id, workspaces_router.FolderRestrictionIn(folder_path="Direction", restricted=True), db=db, user=director
    )
    assert "plan.md" not in _titles(db, hr, ws) and "plan.md" not in _titles(db, dev, ws)
    assert "plan.md" in _titles(db, director, ws)

    workspaces_router.share_folder(
        ws.id, workspaces_router.FolderShareIn(folder_path="Direction/", username="fu_dev"), db=db, user=director
    )
    assert "plan.md" in _titles(db, dev, ws)  # shared with him, whatever his role
    assert "plan.md" not in _titles(db, hr, ws)
    # dropping a file: only where it was shared
    await _upload(db, dev, ws, "idee.md", b"Idee.", folder="Direction")
    with pytest.raises(HTTPException) as exc:
        await _upload(db, hr, ws, "intrus.md", b"x", folder="Direction/Sous-dossier")
    assert exc.value.status_code == 403

    # the folder list: HR does not even learn the name; the Director sees the sharing
    hr_folders = [f.path for f in workspaces_router.list_folders(ws.id, db=db, user=hr).folders]
    assert hr_folders == ["Commun/"]
    managed = workspaces_router.list_folders(ws.id, db=db, user=director)
    direction = next(f for f in managed.folders if f.path == "Direction/")
    assert managed.can_manage and direction.restricted and [s.username for s in direction.shares] == ["fu_dev"]

    workspaces_router.set_folder_restriction(
        ws.id, workspaces_router.FolderRestrictionIn(folder_path="Direction/", restricted=False), db=db, user=director
    )
    assert "plan.md" in _titles(db, hr, ws)  # role-based again


async def test_only_the_director_or_the_owner_manages_folder_sharing(db, team):
    ws = _workspace(db, team["director"])
    for call in (
        lambda: workspaces_router.set_folder_restriction(
            ws.id, workspaces_router.FolderRestrictionIn(folder_path="X", restricted=True), db=db, user=team["hr"]
        ),
        lambda: workspaces_router.share_folder(
            ws.id, workspaces_router.FolderShareIn(folder_path="X", username="fu_hr"), db=db, user=team["hr"]
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 403
    assert workspaces_router.list_folders(ws.id, db=db, user=team["hr"]).can_manage is False


async def test_sharing_a_folder_of_a_private_workspace_opens_that_folder_only(db, team):
    """Dropbox: the person sees the shared folder - not the rest of the
    workspace, although their role would allow it."""
    ws = _workspace(db, team["director"], visibility="private", name="prive")
    _sync(db, team["director"], ws, {"Projet/specs.md": b"Specs.", "Autre/notes.md": b"Notes."})
    dev = team["dev"]
    with pytest.raises(HTTPException):
        documents_router.list_documents(workspace_id=ws.id, db=db, user=dev)

    workspaces_router.share_folder(
        ws.id, workspaces_router.FolderShareIn(folder_path="Projet", username="fu_dev"), db=db, user=team["director"]
    )
    assert "prive" in [w.name for w in workspaces_router.list_workspaces(db=db, user=dev)]
    assert _titles(db, dev, ws) == ["specs.md"]
    assert [f.path for f in workspaces_router.list_folders(ws.id, db=db, user=dev).folders] == ["Projet/"]
    hits = mcp_server._search_document_text_impl(str(dev.id), "Notes", False, True, False, workspace_id=str(ws.id))
    assert hits["total_occurrences"] == 0


async def test_personal_folders_on_upload(db, team):
    ws = _workspace(db, team["director"])
    own = await _upload(db, team["emp"], ws, "arret_maladie.txt", b"Arret maladie 3 jours.", folder="Personnel/fu_emp")
    assert own.subject_user_id == team["emp"].id
    assert (own.department, own.confidentiality) == ("HR", "CONFIDENTIAL")
    assert "arret_maladie.txt" in _titles(db, team["emp"], ws)  # his own, above his role
    assert "arret_maladie.txt" in _titles(db, team["hr"], ws)
    assert "arret_maladie.txt" not in _titles(db, team["dev"], ws)

    with pytest.raises(HTTPException) as exc:  # a colleague's personal folder
        await _upload(db, team["dev"], ws, "x.txt", b"x", folder="Personnel/fu_emp")
    assert exc.value.status_code == 403
    by_hr = await _upload(db, team["hr"], ws, "contrat.txt", b"Contrat.", folder="Personnel/fu_emp")
    assert by_hr.subject_user_id == team["emp"].id and by_hr.added_by_name == "fu_hr"


async def test_sql_and_python_agree_with_restricted_folders_and_folder_only_shares(db, team):
    ws = _workspace(db, team["director"], visibility="private", name="prive")
    _sync(db, team["director"], ws, {"A/x.md": b"x", "A/B/y.md": b"y", "C/z.md": b"z"})
    workspaces_router.set_folder_restriction(
        ws.id, workspaces_router.FolderRestrictionIn(folder_path="A", restricted=True), db=db, user=team["director"]
    )
    workspaces_router.share_folder(
        ws.id, workspaces_router.FolderShareIn(folder_path="A/B", username="fu_dev"), db=db, user=team["director"]
    )
    workspaces_router.share_folder(
        ws.id, workspaces_router.FolderShareIn(folder_path="C", username="fu_hr", effect="deny"), db=db, user=team["director"]
    )
    for person, doc in itertools.product(team.values(), db.query(Document).filter(Document.workspace_id == ws.id)):
        grant = grant_in_workspace(resolve_effective_grant(person), ws, db)
        in_sql = db.query(Document.id).filter(Document.id == doc.id, *document_scope(grant)).first() is not None
        outside = db.query(Document.id).filter(Document.id == doc.id, *outside_document_scope(grant)).first() is not None
        assert in_sql == check_document_visibility(grant=grant, document=doc).allowed, (person.username, doc.relative_path)
        assert outside == (not in_sql)
    assert _titles(db, team["dev"], ws) == ["y.md"]
