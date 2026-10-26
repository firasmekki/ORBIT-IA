"""Personal documents and the Director's explicit access rules, against a
real database - through every read path (REST list/get, MCP exact search,
RAG, by-id access, the "who can see this" panel).

The scenario the product promises: ONE folder shared with the whole
company, where each person sees only what concerns them - a developer sees
their own payslip and never a colleague's, the accountant sees every
payslip, HR every leave record - and the Director can open or close any
file or folder to anyone, a "deny" always winning."""

import itertools
import uuid

import pytest
from fastapi import HTTPException

import app.mcp.server as mcp_server
import app.routers.documents as documents_router
from app.models.access_rule import DocumentAccessRule
from app.models.audit import AuditLog
from app.models.document import Document
from app.models.workspace import Workspace
from app.policy.engine import check_document_visibility, document_scope, outside_document_scope
from app.policy.rules import resolve_effective_grant
from app.policy.workspace import grant_in_workspace
from app.schemas.document import DocumentUpdate
from app.services import workspace_sync
from tests.conftest import hot_vector

PAYSLIP_DEV = "Bulletin de paie septembre : salaire net 2 450 euros pour Quokka."
PAYSLIP_EMP = "Bulletin de paie septembre : salaire net 1 900 euros pour Wombat."


@pytest.fixture(autouse=True)
def _fake_embeddings_and_storage(monkeypatch):
    monkeypatch.setattr("app.rag.retriever.embed_text", lambda query: hot_vector(0))
    monkeypatch.setattr("app.rag.ingest.embed_texts", lambda texts: [hot_vector(0) for _ in texts])
    monkeypatch.setattr("app.services.workspace_sync.upload_file", lambda *a, **k: None)


def _shared_workspace(db, owner, name="orbitaitest"):
    ws = Workspace(
        name=name, owner_id=owner.id, default_department="GENERAL", default_confidentiality="INTERNAL",
        visibility="organization",
    )
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def _doc(make_doc, make_test_chunk, db, ws, title, text, *, department, confidentiality, path=None, subject=None):
    doc = make_doc(
        title=title, department=department, confidentiality=confidentiality, pages=[(1, None, 1, text)],
        workspace_id=ws.id, relative_path=path or title,
    )
    if subject is not None:
        doc.subject_user_id = subject.id
        db.commit()
        db.refresh(doc)
    make_test_chunk(document_id=doc.id, chunk_text=text, embedding=hot_vector(0))
    return doc


def _titles(db, user, ws):
    return sorted(d.title for d in documents_router.list_documents(workspace_id=ws.id, db=db, user=user))


def _rule(db, ws, user, effect, *, document=None, folder=None):
    db.add(DocumentAccessRule(
        workspace_id=ws.id, user_id=user.id, document_id=document.id if document else None,
        folder_path=folder, effect=effect,
    ))
    db.commit()


@pytest.fixture
def company(db, make_doc, make_test_user, make_test_chunk):
    """A shared folder with two payslips (FINANCE/CONFIDENTIAL, personal), an
    HR leave record (personal), and a common GENERAL note."""
    people = {
        "director": make_test_user(username="pa_dir", role="DIRECTOR"),
        "accountant": make_test_user(username="pa_acc", role="ACCOUNTANT"),
        "hr": make_test_user(username="pa_hr", role="HR"),
        "dev": make_test_user(username="pa_dev", role="DEVELOPER"),
        "dev2": make_test_user(username="pa_dev2", role="DEVELOPER"),
        "emp": make_test_user(username="pa_emp", role="EMPLOYEE"),
    }
    ws = _shared_workspace(db, people["director"])
    docs = {
        "payslip_dev": _doc(make_doc, make_test_chunk, db, ws, "paie_dev.pdf", PAYSLIP_DEV, department="FINANCE",
                            confidentiality="CONFIDENTIAL", path="Personnel/pa_dev/paie_dev.pdf", subject=people["dev"]),
        "payslip_emp": _doc(make_doc, make_test_chunk, db, ws, "paie_emp.pdf", PAYSLIP_EMP, department="FINANCE",
                            confidentiality="CONFIDENTIAL", path="Personnel/pa_emp/paie_emp.pdf", subject=people["emp"]),
        "leave_dev": _doc(make_doc, make_test_chunk, db, ws, "conges_dev.docx", "Congés 2026 : 12 jours restants (Quokka).",
                          department="HR", confidentiality="CONFIDENTIAL", path="Personnel/pa_dev/conges_dev.docx",
                          subject=people["dev"]),
        "tasks_dev2": _doc(make_doc, make_test_chunk, db, ws, "taches_dev2.md", "Tâches sprint : migration Quokka.",
                           department="GENERAL", confidentiality="INTERNAL", path="Personnel/pa_dev2/taches_dev2.md",
                           subject=people["dev2"]),
        "note": _doc(make_doc, make_test_chunk, db, ws, "note.md", "Note commune : réunion lundi (Quokka).",
                     department="GENERAL", confidentiality="INTERNAL", path="Commun/note.md"),
        "budget": _doc(make_doc, make_test_chunk, db, ws, "budget.xlsx", "Budget global Quokka 2026.",
                       department="FINANCE", confidentiality="CONFIDENTIAL", path="Finance/budget.xlsx"),
    }
    return ws, people, docs


def test_each_person_sees_their_own_and_only_what_their_role_manages(db, company):
    ws, p, _ = company
    assert _titles(db, p["dev"], ws) == ["conges_dev.docx", "note.md", "paie_dev.pdf"]
    assert _titles(db, p["dev2"], ws) == ["note.md", "taches_dev2.md"]
    assert _titles(db, p["emp"], ws) == ["note.md", "paie_emp.pdf"]
    # the accountant manages FINANCE personal documents - not HR ones, not a GENERAL task list
    assert _titles(db, p["accountant"], ws) == ["budget.xlsx", "note.md", "paie_dev.pdf", "paie_emp.pdf"]
    # HR manages HR personal documents - never payslips
    assert _titles(db, p["hr"], ws) == ["conges_dev.docx", "note.md"]
    assert len(_titles(db, p["director"], ws)) == 6


def test_a_colleagues_payslip_is_unreachable_by_every_read_path(db, company):
    ws, p, d = company
    dev, wsid = p["dev"], str(ws.id)
    # exact search: only his own payslip
    hits = mcp_server._search_document_text_impl(str(dev.id), "salaire", False, True, False, workspace_id=wsid)
    assert hits["total_occurrences"] == 1 and "1 900" not in str(hits)
    # RAG
    rag = mcp_server._search_documents_impl(str(dev.id), "bulletin de paie salaire", 10, workspace_id=wsid)
    assert PAYSLIP_EMP not in str(rag) and "paie_emp.pdf" not in str(rag)
    # by id (MCP and REST)
    assert "error" in mcp_server._get_document_impl(str(dev.id), str(d["payslip_emp"].id), workspace_id=wsid)
    with pytest.raises(HTTPException) as exc:
        documents_router.get_document(d["payslip_emp"].id, db=db, user=dev)
    assert exc.value.status_code == 403
    # ...while his own opens, although FINANCE/CONFIDENTIAL is above a developer's role
    assert documents_router.get_document(d["payslip_dev"].id, db=db, user=dev).content == PAYSLIP_DEV
    # listing a department outside the role still shows only his own
    listed = mcp_server._list_documents_impl(str(dev.id), None, "FINANCE", None, None, None, workspace_id=wsid)
    assert [r["title"] for r in listed["results"]] == ["paie_dev.pdf"]


def test_director_allow_rule_opens_a_file_and_a_folder_to_one_person(db, company):
    ws, p, d = company
    _rule(db, ws, p["dev"], "allow", document=d["budget"])
    assert "budget.xlsx" in _titles(db, p["dev"], ws)
    assert "budget.xlsx" not in _titles(db, p["dev2"], ws)  # one person only

    _rule(db, ws, p["hr"], "allow", folder="Personnel/pa_dev2/")
    assert "taches_dev2.md" in _titles(db, p["hr"], ws)
    # a folder rule never leaks to a sibling whose name merely starts the same
    assert "paie_dev.pdf" not in _titles(db, p["hr"], ws)


def test_director_deny_rule_wins_over_role_ownership_and_allow(db, company):
    ws, p, d = company
    _rule(db, ws, p["accountant"], "deny", folder="Finance/")
    assert "budget.xlsx" not in _titles(db, p["accountant"], ws)
    assert "paie_dev.pdf" in _titles(db, p["accountant"], ws)  # other folders untouched

    _rule(db, ws, p["dev"], "deny", document=d["payslip_dev"])
    _rule(db, ws, p["dev"], "allow", folder="Personnel/pa_dev/")
    assert "paie_dev.pdf" not in _titles(db, p["dev"], ws)
    hits = mcp_server._search_document_text_impl(str(p["dev"].id), "salaire", False, True, False, workspace_id=str(ws.id))
    assert hits["total_occurrences"] == 0


def test_sql_scope_and_python_check_always_agree(db, company):
    """document_scope (every list/search) and check_document_visibility (by
    id, access panel) are two writings of one rule: compared for every
    person x document, with rules of every kind in place - and the
    "outside" probe filter is their exact complement."""
    ws, p, d = company
    _rule(db, ws, p["dev"], "allow", document=d["budget"])
    _rule(db, ws, p["hr"], "allow", folder="Personnel/pa_dev2/")
    _rule(db, ws, p["accountant"], "deny", folder="Finance/")
    _rule(db, ws, p["emp"], "deny", document=d["payslip_emp"])
    for person, doc in itertools.product(p.values(), d.values()):
        grant = grant_in_workspace(resolve_effective_grant(person), ws, db)
        in_sql = db.query(Document.id).filter(Document.id == doc.id, *document_scope(grant)).first() is not None
        outside = db.query(Document.id).filter(Document.id == doc.id, *outside_document_scope(grant)).first() is not None
        in_python = check_document_visibility(grant=grant, document=doc).allowed
        assert in_sql == in_python, (person.username, doc.title)
        assert outside == (not in_sql), (person.username, doc.title)


def test_access_panel_explains_why_each_person_sees_it(db, company):
    ws, p, d = company
    _rule(db, ws, p["dev2"], "allow", document=d["payslip_dev"])
    _rule(db, ws, p["accountant"], "deny", document=d["payslip_dev"])
    access = documents_router.document_access(d["payslip_dev"].id, db=db, user=p["director"])
    basis = {person.username: person.basis for person in access.visible_to}
    assert basis == {"pa_dir": "role", "pa_dev": "personal", "pa_dev2": "granted"}
    assert [person.username for person in access.denied_by_rule] == ["pa_acc"]
    assert access.can_manage_rules
    # the developer may look at his own document's panel, without the Director's details
    own = documents_router.document_access(d["payslip_dev"].id, db=db, user=p["dev"])
    assert own.denied_by_rule == [] and not own.can_manage_rules


def test_only_the_director_assigns_personal_documents_and_rules_and_it_is_audited(db, company):
    ws, p, d = company
    note = d["note"]
    with pytest.raises(HTTPException) as exc:
        documents_router.update_document(note.id, DocumentUpdate(subject_user_id=p["emp"].id), db=db, user=p["hr"])
    assert exc.value.status_code == 403

    documents_router.update_document(note.id, DocumentUpdate(subject_user_id=p["dev"].id), db=db, user=p["director"])
    assert "note.md" not in _titles(db, p["emp"], ws)  # now dev's personal document
    assert "note.md" in _titles(db, p["dev"], ws)
    documents_router.update_document(note.id, DocumentUpdate(subject_user_id=None), db=db, user=p["director"])
    assert "note.md" in _titles(db, p["emp"], ws)  # common again
    assert db.query(AuditLog).filter(AuditLog.action == "DOCUMENT_SUBJECT_CHANGED").count() == 2

    rule = documents_router.set_access_rule(
        d["budget"].id, documents_router.AccessRuleIn(username="pa_emp", effect="allow", folder_path="Finance"),
        db=db, director=p["director"],
    )
    assert rule.folder_path == "Finance/" and "budget.xlsx" in _titles(db, p["emp"], ws)
    with pytest.raises(HTTPException) as exc:  # not a folder of this document
        documents_router.set_access_rule(
            d["budget"].id, documents_router.AccessRuleIn(username="pa_emp", effect="allow", folder_path="Personnel"),
            db=db, director=p["director"],
        )
    assert exc.value.status_code == 422
    listed = documents_router.list_access_rules(d["budget"].id, db=db, _director=p["director"])
    assert [(r.username, r.effect, r.folder_path) for r in listed.rules] == [("pa_emp", "allow", "Finance/")]
    assert listed.folders == ["Finance/"]
    documents_router.delete_access_rule(d["budget"].id, rule.id, db=db, director=p["director"])
    assert "budget.xlsx" not in _titles(db, p["emp"], ws)
    actions = [a for (a,) in db.query(AuditLog.action).filter(AuditLog.action.like("ACCESS_RULE_%"))]
    assert sorted(actions) == ["ACCESS_RULE_REMOVED", "ACCESS_RULE_SET"]


def test_rules_of_one_workspace_never_apply_in_another(db, company, make_doc, make_test_chunk):
    ws, p, d = company
    other = _shared_workspace(db, p["director"], name="autre")
    twin = _doc(make_doc, make_test_chunk, db, other, "budget.xlsx", "Budget global Quokka 2026.",
                department="FINANCE", confidentiality="CONFIDENTIAL", path="Finance/budget.xlsx")
    _rule(db, ws, p["dev"], "allow", folder="Finance/")
    assert "budget.xlsx" in _titles(db, p["dev"], ws)
    assert _titles(db, p["dev"], other) == []
    assert twin.id != d["budget"].id


def test_sync_files_personnel_folders_as_personal_documents(db, make_test_user):
    director = make_test_user(username="ps_dir", role="DIRECTOR")
    dev = make_test_user(username="ps_dev", role="DEVELOPER")
    other_dev = make_test_user(username="ps_dev2", role="DEVELOPER")
    ws = _shared_workspace(db, director)

    payslip, _ = workspace_sync.index_file(db, director, ws, "RH/Personnel/PS_Dev/paie_09.txt", b"Salaire net 2450.")
    stray, _ = workspace_sync.index_file(db, director, ws, "Personnel/inconnu/x.txt", b"Rien.")
    common, _ = workspace_sync.index_file(db, director, ws, "Personnel/readme.txt", b"Lisez-moi.")

    assert payslip.subject_user_id == dev.id
    assert (payslip.department, payslip.confidentiality) == ("HR", "CONFIDENTIAL")
    assert stray.subject_user_id is None and common.subject_user_id is None  # nothing is guessed
    assert "paie_09.txt" in _titles(db, dev, ws)
    assert "paie_09.txt" not in _titles(db, other_dev, ws)
    audit = db.query(AuditLog).filter(AuditLog.action == "WORKSPACE_FILE_INDEXED", AuditLog.resource_id == str(payslip.id)).one()
    assert audit.extra["personal_document_of"] == "ps_dev"


def test_existing_documents_keep_exactly_their_visibility(db, make_doc, make_test_user, make_test_chunk):
    """No subject, no rule (every document before this feature): visibility
    is the plain role rule, unchanged."""
    dev = make_test_user(username="nx_dev", role="DEVELOPER")
    director = make_test_user(username="nx_dir", role="DIRECTOR")
    ws = _shared_workspace(db, director)
    _doc(make_doc, make_test_chunk, db, ws, "archi.md", "Archi.", department="TECH", confidentiality="INTERNAL")
    _doc(make_doc, make_test_chunk, db, ws, "secret.md", "Secret.", department="TECH", confidentiality="SECRET")
    _doc(make_doc, make_test_chunk, db, ws, "paie.md", "Paie.", department="FINANCE", confidentiality="INTERNAL")
    assert _titles(db, dev, ws) == ["archi.md"]
    assert uuid.UUID(str(ws.id))
