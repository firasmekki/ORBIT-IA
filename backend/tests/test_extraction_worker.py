"""/sync/file only stores and queues a file; its text is extracted in the
background (app/services/extraction_worker.py). Storage is an in-memory
dict, embeddings are fake - the extraction and the queue run for real
against the test database."""

import hashlib

import pytest

from app.models.audit import AuditLog
from app.models.document import Document, DocumentPage
from app.models.workspace import Workspace
from app.rag.extract import extract
from app.services import embedding_worker, extraction_worker, workspace_sync
from tests.conftest import hot_vector


@pytest.fixture
def storage(monkeypatch):
    objects: dict[str, bytes] = {}
    monkeypatch.setattr("app.services.workspace_sync.upload_file", lambda key, data, **k: objects.__setitem__(key, data))
    monkeypatch.setattr("app.services.extraction_worker.download_file", lambda key: objects[key])
    monkeypatch.setattr("app.rag.ingest.embed_texts", lambda texts: [hot_vector(0) for _ in texts])
    return objects


@pytest.fixture
def owner_ws(db, make_test_user):
    owner = make_test_user(username="extract_owner", role="EMPLOYEE")
    ws = Workspace(name="extraction", owner_id=owner.id, default_department="GENERAL", default_confidentiality="INTERNAL")
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return owner, ws


def _queue(db, owner, ws, path, content):
    return workspace_sync.index_file(db, owner, ws, path, content, defer_extraction=True)


def _drain():
    while extraction_worker.process_next_extraction():
        pass


def test_queued_file_returns_at_once_then_is_extracted_and_embedded(db, storage, owner_ws):
    owner, ws = owner_ws
    doc, outcome = _queue(db, owner, ws, "notes.txt", b"Recrutement de Tarek.")
    assert outcome == "created" and doc.status == "queued" and doc.content == ""
    assert db.query(DocumentPage).count() == 0

    _drain()
    db.expire_all()
    doc = db.get(Document, doc.id)
    assert doc.status == "indexing" and doc.content == "Recrutement de Tarek."
    assert db.query(DocumentPage).filter(DocumentPage.document_id == doc.id).count() == 1

    while embedding_worker.process_next_document():
        pass
    db.expire_all()
    assert db.get(Document, doc.id).status == "indexed"


def test_same_file_sent_again_while_queued_is_unchanged(db, storage, owner_ws):
    owner, ws = owner_ws
    _queue(db, owner, ws, "a.txt", b"contenu")
    _, outcome = _queue(db, owner, ws, "a.txt", b"contenu")
    assert outcome == "unchanged"


def test_smallest_files_are_extracted_first(db, storage, owner_ws):
    owner, ws = owner_ws
    big, _ = _queue(db, owner, ws, "big.txt", b"x" * 5000)
    small, _ = _queue(db, owner, ws, "small.txt", b"petit")
    assert extraction_worker.process_next_extraction()
    db.expire_all()
    assert db.get(Document, small.id).status == "indexing"
    assert db.get(Document, big.id).status == "queued"


def test_file_without_text_is_marked_failed_with_the_reason_audited(db, storage, owner_ws):
    owner, ws = owner_ws
    doc, _ = _queue(db, owner, ws, "vide.txt", b"   \n  ")
    _drain()
    db.expire_all()
    assert db.get(Document, doc.id).status == "failed"
    entry = db.query(AuditLog).filter(AuditLog.action == "DOCUMENT_EXTRACTION_FAILED").one()
    assert entry.resource_id == str(doc.id) and "aucun texte" in entry.reason


def test_update_keeps_previous_text_until_the_new_one_is_extracted(db, storage, owner_ws):
    owner, ws = owner_ws
    doc, _ = _queue(db, owner, ws, "budget.txt", b"Budget initial : 100.")
    _drain()
    updated, outcome = _queue(db, owner, ws, "budget.txt", b"Budget revu : 250.")
    assert outcome == "updated" and updated.status == "queued" and updated.content == "Budget initial : 100."
    _drain()
    db.expire_all()
    doc = db.get(Document, doc.id)
    assert doc.content == "Budget revu : 250." and doc.content_hash == hashlib.sha256(b"Budget revu : 250.").hexdigest()


def test_extraction_of_an_outdated_version_is_discarded(db, storage, owner_ws, monkeypatch):
    owner, ws = owner_ws
    doc, _ = _queue(db, owner, ws, "plan.txt", b"version 1")
    real_extract = extraction_worker.extract

    def extract_while_file_changes(filename, content):
        result = real_extract(filename, content)
        # The file is re-synced while version 1 is being read.
        _queue(db, owner, ws, "plan.txt", b"version 2")
        return result

    monkeypatch.setattr(extraction_worker, "extract", extract_while_file_changes)
    assert extraction_worker.process_next_extraction()
    db.expire_all()
    doc = db.get(Document, doc.id)
    assert doc.status == "queued" and doc.content == ""  # version 1's text never stored

    monkeypatch.setattr(extraction_worker, "extract", real_extract)
    _drain()
    db.expire_all()
    assert db.get(Document, doc.id).content == "version 2"


def test_interrupted_extractions_are_requeued_at_startup(db, storage, owner_ws):
    owner, ws = owner_ws
    doc, _ = _queue(db, owner, ws, "a.txt", b"contenu")
    db.query(Document).filter(Document.id == doc.id).update({Document.status: "extracting"})
    db.commit()
    assert extraction_worker.requeue_interrupted() == 1
    db.expire_all()
    assert db.get(Document, doc.id).status == "queued"


def test_nul_bytes_are_stripped_from_extracted_text():
    result = extract("nul.txt", b"Pli\x00 technique\n\x00")
    assert "\x00" not in result.text and result.text == "Pli technique"
    assert all("\x00" not in p.text for p in result.pages)
