"""Document reports (app/agent/reports.py + orchestrator): the right
document is chosen by its stable id (homonyms resolved by folder or
number, never mixed), a single-document report sees that document only and
in full, a multi-document synthesis states its real coverage; images and
scans without text are not errors; the inventory reconciles with the disk.

Real MCP tool implementations and a real database behind the
orchestrator; only the LLM is replaced, to see exactly what it is given."""

import os
from contextlib import asynccontextmanager
from io import BytesIO

import pytest
from PIL import Image, ImageDraw, ImageFont

import app.agent.orchestrator as orchestrator
import app.mcp.server as mcp_server
from app.agent import reports
from app.core.config import get_settings
from app.core.migrations import bootstrap_schema
from app.models.document import Document
from app.models.workspace import Workspace
from app.policy.rules import resolve_effective_grant
from app.rag import extract as extract_mod
from app.rag.extract import ExtractionError, extract
from app.services import extraction_worker, source_scanner, vision_worker, workspace_sync
from tests.conftest import hot_vector

ARCHI_CONSULT = "Architecture de supervision de la consultation : automates S7, serveur SCADA redondant."
ARCHI_SAGEMCOM = "Architecture SAGEMCOM : télémaintenance par VPN, pare-feu industriel."


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    monkeypatch.setattr("app.rag.retriever.embed_text", lambda query: hot_vector(0))
    monkeypatch.setattr("app.rag.ingest.embed_texts", lambda texts: [hot_vector(0) for _ in texts])
    stored: dict[str, bytes] = {}
    monkeypatch.setattr("app.services.workspace_sync.upload_file", lambda key, content, **k: stored.__setitem__(key, content))
    monkeypatch.setattr(extraction_worker, "download_file", lambda key: stored[key])
    monkeypatch.setattr(vision_worker, "download_file", lambda key: stored[key])
    return stored


def _workspace(db, owner, name="projets"):
    ws = Workspace(name=name, owner_id=owner.id)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def _doc(make_doc, ws, path, pages, department="GENERAL", confidentiality="INTERNAL"):
    """pages: [(page_no, text)]"""
    return make_doc(
        title=path.rsplit("/", 1)[-1], department=department, confidentiality=confidentiality,
        pages=[(page, None, 1, text) for page, text in pages], workspace_id=ws.id, relative_path=path,
    )


def _wire(monkeypatch, user, replies=None):
    """The orchestrator wired to the real MCP implementations, with the
    workspace AND folder scope taken from the token it mints itself."""
    tokens: list[dict] = []
    calls: list[list[dict]] = []
    replies = list(replies or [])

    @asynccontextmanager
    async def session(token):
        yield token

    def mint(**claims):
        tokens.append(claims)
        return claims

    def scope(token):
        return {"workspace_id": token.get("workspace_id"), "folder_scope": tuple(token.get("folder_scope") or ())}

    uid = str(user.id)
    impls = {
        "search_documents": lambda a, s: mcp_server._search_documents_impl(uid, a["query"], a.get("top_k", 5), **s),
        "get_document_section": lambda a, s: mcp_server._get_document_section_impl(
            uid, a.get("document_id"), a.get("document_name"), a.get("page"), a.get("section"), a.get("offset", 0), **s
        ),
        "get_document": lambda a, s: mcp_server._get_document_impl(uid, a["document_id"], **s),
        "list_folders": lambda a, s: mcp_server._list_folders_impl(uid, **s),
        "list_documents": lambda a, s: mcp_server._list_documents_impl(uid, None, None, None, None, None, **s),
        "search_database": lambda a, s: {"results": []},
    }

    async def call_tool(token, name, arguments):
        return impls[name](arguments, scope(token))

    async def llm(messages, tools=None, **_options):
        calls.append(messages)
        return {"content": replies.pop(0) if replies else "## Rapport\nContenu fidèle (p. 1).", "tool_calls": None}

    monkeypatch.setattr(orchestrator, "open_mcp_session", session)
    monkeypatch.setattr(orchestrator, "mint_internal_token", mint)
    monkeypatch.setattr(orchestrator, "call_tool", call_tool)
    monkeypatch.setattr(orchestrator, "chat_completion", llm)
    return calls, tokens


async def _turn(user, ws, message, context=None):
    return await orchestrator.run_agent_turn(
        user_id=str(user.id), grant=resolve_effective_grant(user), message=message, history=[],
        workspace_id=str(ws.id), workspace_name=ws.name, last_reference_context=context,
    )


def _seen(calls) -> str:
    return "\n".join(m.get("content") or "" for call in calls for m in call)


# --- 1. same name in two folders -------------------------------------------------------------------------


async def test_homonyms_are_listed_with_full_paths_and_the_reply_picks_one(db, make_doc, make_test_user, monkeypatch):
    owner = make_test_user(username="rep_homonyms", role="DIRECTOR")
    ws = _workspace(db, owner)
    consult = _doc(make_doc, ws, "noveau consultation/Architecture.pdf", [(1, ARCHI_CONSULT)])
    _doc(make_doc, ws, "SAGEMCOM SME/Architecture.pdf", [(1, ARCHI_SAGEMCOM)])
    calls, _ = _wire(monkeypatch, owner)

    ask = await _turn(owner, ws, "je veux un rapport et un resume pour Architecture.pdf")
    assert "`SAGEMCOM SME/Architecture.pdf`" in ask.answer and "`noveau consultation/Architecture.pdf`" in ask.answer
    assert ask.reference_context["kind"] == "document_choice" and calls == []

    # the user's actual reply from the incident
    report = await _turn(owner, ws, "au niveau noveau consultation", ask.reference_context)
    assert [s["relative_path"] for s in report.sources] == ["noveau consultation/Architecture.pdf"]
    seen = _seen(calls)
    assert ARCHI_CONSULT in seen and ARCHI_SAGEMCOM not in seen  # never mixed
    assert "Plusieurs documents" not in report.answer
    assert report.reference_context["document_id"] == str(consult.id)
    # the original request is what gets answered, not "au niveau noveau consultation"
    assert calls[-1][-1]["content"] == "je veux un rapport et un resume pour Architecture.pdf"
    assert not any(t["tool"] == "search_documents" for t in report.tool_trace)


async def test_a_number_also_picks_and_a_folder_in_the_request_needs_no_question(db, make_doc, make_test_user, monkeypatch):
    owner = make_test_user(username="rep_number", role="DIRECTOR")
    ws = _workspace(db, owner)
    _doc(make_doc, ws, "noveau consultation/Architecture.pdf", [(1, ARCHI_CONSULT)])
    _doc(make_doc, ws, "SAGEMCOM SME/Architecture.pdf", [(1, ARCHI_SAGEMCOM)])
    calls, _ = _wire(monkeypatch, owner)

    ask = await _turn(owner, ws, "rapport sur Architecture.pdf")
    second = ask.reference_context["candidates"][1]["relative_path"]
    picked = await _turn(owner, ws, "2", ask.reference_context)
    assert [s["relative_path"] for s in picked.sources] == [second]

    direct = await _turn(owner, ws, "fais un rapport sur Architecture.pdf dans le dossier SAGEMCOM SME")
    assert [s["relative_path"] for s in direct.sources] == ["SAGEMCOM SME/Architecture.pdf"]
    assert ARCHI_CONSULT not in _seen(calls[-1:])


async def test_the_document_found_just_before_is_reused_without_asking(db, make_doc, make_test_user, monkeypatch):
    owner = make_test_user(username="rep_context", role="DIRECTOR")
    ws = _workspace(db, owner)
    consult = _doc(make_doc, ws, "noveau consultation/Architecture.pdf", [(1, ARCHI_CONSULT)])
    _doc(make_doc, ws, "SAGEMCOM SME/Architecture.pdf", [(1, ARCHI_SAGEMCOM)])
    _wire(monkeypatch, owner)
    previous_search = {
        "kind": "keyword_search", "keyword": "Solutions",
        "documents": [{"title": "Architecture.pdf", "document_id": str(consult.id), "locations": [{"page": 1, "section": None}]}],
    }
    result = await _turn(owner, ws, "je veux un rapport et un resume pour Architecture.pdf", previous_search)
    assert [s["document_id"] for s in result.sources] == [str(consult.id)]
    assert [t["tool"] for t in result.tool_trace] == ["get_document"]


# --- 2. one PDF -> one report ---------------------------------------------------------------------------------


async def test_single_document_report_reads_all_pages_and_states_its_limits(db, make_doc, make_test_user, monkeypatch):
    owner = make_test_user(username="rep_single", role="DIRECTOR")
    ws = _workspace(db, owner)
    pages = [(1, "Objet : supervision énergétique."), (2, "Exigence : disponibilité 99,5 %."), (4, "Conclusion : lot unique.")]
    _doc(make_doc, ws, "noveau consultation/Architecture.pdf", pages)
    _doc(make_doc, ws, "noveau consultation/CCTP.pdf", [(1, "Cahier des charges : autre document.")])
    calls, _ = _wire(monkeypatch, owner, replies=["# Rapport : Supervision\n## Synthèse exécutive\nDisponibilité 99,5 % (p. 2)."])

    result = await _turn(owner, ws, "rapport sur noveau consultation/Architecture.pdf")

    assert len(calls) == 1
    system = "\n".join(m["content"] for m in calls[0] if m["role"] == "system")
    assert "UN SEUL document" in system and "## Limites de lecture" in system
    assert "[p. 1]" in system and "[p. 4]" in system and "Conclusion : lot unique." in system  # the whole document
    assert "Pages sans texte extractible (scan, image ou page blanche) : 3" in system
    assert "Cahier des charges" not in system  # nothing from another document
    assert result.answer.startswith("# Rapport : Supervision")
    assert "Source unique : `noveau consultation/Architecture.pdf`" in result.answer
    assert "1 page(s) sans texte" in result.answer
    assert [s["relative_path"] for s in result.sources] == ["noveau consultation/Architecture.pdf"]


async def test_a_long_document_is_read_in_page_batches_then_merged(db, make_doc, make_test_user, monkeypatch):
    owner = make_test_user(username="rep_long", role="DIRECTOR")
    ws = _workspace(db, owner)
    pages = [(n, f"Page {n} : exigence numéro {n}. " + "texte technique " * 300) for n in range(1, 21)]
    _doc(make_doc, ws, "Rapport annuel.pdf", pages)
    calls, _ = _wire(monkeypatch, owner)

    result = await _turn(owner, ws, "résume Rapport annuel.pdf")

    total = sum(len(t) for _, t in pages)
    expected_batches = len(reports.batch_pages(reports.tagged_pages([{"page": n, "text": t} for n, t in pages]), orchestrator.REPORT_BATCH_CHARS))
    assert total > orchestrator.REPORT_SINGLE_PASS_CHARS and expected_batches > 1
    assert len(calls) == expected_batches + 1  # notes per batch, then the report
    batch_text = "\n".join(c[1]["content"] for c in calls[:-1])
    assert all(f"[p. {n}]" in batch_text for n in range(1, 21))  # every page read
    assert f"lecture intégrale en {expected_batches} lots" in result.answer


# --- 3. / 4. several documents, folder named in the question ---------------------------------------------------


async def test_multi_document_synthesis_stays_in_the_named_folder_and_states_coverage(db, make_doc, make_test_user, make_test_chunk, monkeypatch):
    owner = make_test_user(username="rep_multi", role="DIRECTOR")
    ws = _workspace(db, owner)
    inside = [
        _doc(make_doc, ws, "noveau consultation/CCTP lot 1.pdf", [(1, "Supervision : exigence de redondance du serveur SCADA.")]),
        _doc(make_doc, ws, "noveau consultation/CCTP lot 2.pdf", [(2, "Supervision : historisation des mesures sur 10 ans.")]),
        _doc(make_doc, ws, "noveau consultation/Annexe.pdf", [(1, "Supervision : liste des compteurs.")]),
    ]
    outside = _doc(make_doc, ws, "BIC/CCTP BIC.pdf", [(1, "Supervision BIC : exigence hors périmètre.")])
    for doc in [*inside, outside]:
        make_test_chunk(document_id=doc.id, chunk_text=doc.content, embedding=hot_vector(0))
    calls, tokens = _wire(monkeypatch, owner)

    result = await _turn(owner, ws, "fais une synthèse des exigences de supervision du dossier noveau consultation")

    assert tokens[-1]["folder_scope"] == ["noveau consultation/"]  # narrowed by a signed token
    seen = _seen(calls)
    assert "hors périmètre" not in seen
    assert {s["relative_path"] for s in result.sources} == {d.relative_path for d in inside}
    assert "Documents réellement consultés : 3 sur 3 document(s) du périmètre" in result.answer
    assert "\n\n---\n" in result.answer  # footer never glued to the text (no setext heading)
    assert "MULTI" not in result.answer and "synthèse multi-documents" in calls[-1][0]["content"]
    assert len(calls) == 4  # one reading per document + the synthesis


def test_report_intents_do_not_hijack_ordinary_questions():
    assert reports.detect_document_request("rapport sur Architecture.pdf").wants_report
    assert reports.detect_document_request("lis budget.xlsx").wants_report is False
    assert reports.detect_multi_report("synthèse du dossier BIC")
    assert reports.detect_multi_report("compare les cahiers des charges des deux offres")
    assert not reports.detect_multi_report("donner une comparaison entre l'année 2025 ET 2026")
    assert not reports.detect_multi_report("quelle est la puissance du transformateur ?")
    assert reports.folders_named_in("au niveau noveau consultation", ["noveau consultation/", "BIC/"]) == ["noveau consultation/"]
    assert reports.folders_named_in("noveau consultation", ["noveau consultation/"]) == []  # no cue word
    assert reports.folders_named_in("dans le dossier 3S", ["3S/"]) == []  # too short to be safe


# --- 7. / 8. images and extraction errors --------------------------------------------------------------------


def _png(draw_text: str | None = None) -> bytes:
    image = Image.new("RGB", (900, 220), "white")
    if draw_text:
        ImageDraw.Draw(image).text((30, 60), draw_text, fill="black", font=ImageFont.load_default(size=64))
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_image_kinds_are_told_apart():
    text = extract("schema.png", _png("TRANSFORMATEUR T1")).text  # real OCR
    assert "TRANSFORMATEUR" in text.upper()
    with pytest.raises(ExtractionError) as blank:
        extract("icone.png", _png())
    assert blank.value.kind == "no_text_image"
    with pytest.raises(ExtractionError) as broken:
        extract("photo.jpg", b"\xff\xd8\xff\xe0 tronque")
    assert broken.value.kind == "corrupt"


def test_ocr_engine_failure_is_not_reported_as_an_image_without_text(monkeypatch):
    monkeypatch.setattr(extract_mod, "_ocr_image_bytes", lambda b: "")
    monkeypatch.setattr(extract_mod, "_ocr_engine_available", lambda: False)
    with pytest.raises(ExtractionError) as failed:
        extract("schema.png", _png())
    assert failed.value.kind == "ocr_failed"


def test_worker_files_images_without_text_apart_from_errors(db, make_test_user):
    owner = make_test_user(username="rep_images", role="DIRECTOR")
    ws = _workspace(db, owner, "images")
    icon, _ = workspace_sync.index_file(db, owner, ws, "icones/fleche.png", _png(), defer_extraction=True)
    broken, _ = workspace_sync.index_file(db, owner, ws, "Bonjour.docx", b"PK\x03\x04 pas un vrai docx", defer_extraction=True)
    while extraction_worker.process_next_extraction():
        pass
    db.expire_all()
    icon, broken = db.get(Document, icon.id), db.get(Document, broken.id)
    assert (icon.status, icon.issue_kind) == ("no_text", "no_text_image")
    assert (broken.status, broken.issue_kind) == ("failed", "corrupt") and "endommagé" in broken.index_error
    summary = source_scanner.inventory_summary(db, ws)
    assert summary["documents"]["no_text"] == 1 and summary["documents"]["failed"] == 1
    assert summary["issues"] == {"no_text_image": 1, "corrupt": 1}


def test_visual_analysis_describes_queued_images_one_at_a_time(db, make_test_user, monkeypatch):
    owner = make_test_user(username="rep_vision", role="DIRECTOR")
    ws = _workspace(db, owner, "vision")
    photo, _ = workspace_sync.index_file(db, owner, ws, "site/armoire.png", _png(), defer_extraction=True)
    extraction_worker.process_next_extraction()
    db.query(Document).filter(Document.id == photo.id).update({Document.status: "visual_queued"})
    db.commit()

    async def ready():
        return {"status": "ready"}

    async def describe(**kwargs):
        assert kwargs["mime_type"] == "image/jpeg"  # re-encoded, bounded
        return {"description": "Catégorie : technique. Schéma unifilaire d'une armoire TGBT.", "text": ["TGBT"]}, None

    monkeypatch.setattr(vision_worker, "get_vision_status", ready)
    monkeypatch.setattr(vision_worker, "describe_image", describe)
    assert vision_worker.process_next_visual() == vision_worker.WORKED
    assert vision_worker.process_next_visual() == vision_worker.IDLE  # nothing left
    db.expire_all()
    photo = db.get(Document, photo.id)
    assert photo.status == "indexing" and photo.issue_kind == "visual_technique"
    assert photo.content.startswith(vision_worker.VISUAL_PREFIX) and "armoire TGBT" in photo.content


def test_old_ocr_failures_are_reclassified_as_images_without_text(db, make_test_user):
    owner = make_test_user(username="rep_migrate", role="DIRECTOR")
    ws = _workspace(db, owner, "legacy")
    old, _ = workspace_sync.index_file(db, owner, ws, "1.png", b"x", defer_extraction=True)
    old.status, old.issue_kind = "failed", None
    old.index_error = "Impossible de lire le contenu de « 1.png » : aucun texte détecté par OCR dans cette image"
    db.commit()
    bootstrap_schema(db.connection())
    db.commit()
    db.expire_all()
    assert (db.get(Document, old.id).status, db.get(Document, old.id).issue_kind) == ("no_text", "no_text_image")


# --- 9. / 10. exclusions and inventory vs. disk -----------------------------------------------------------


def test_inventory_plus_excluded_files_equals_the_files_on_disk(db, make_test_user, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "source_mount_base", str(tmp_path / "sources"))
    monkeypatch.setattr(settings, "source_root_1_host", "C:\\Users\\ORBIT\\Desktop")
    root = tmp_path / "sources" / "1" / "projets"
    files = {
        "BIC/rapport.txt": b"Rapport BIC.",
        "BIC/photo.heic": b"heic",
        "CAPCONDO/app/node_modules/a/index.js": b"1",
        "CAPCONDO/app/node_modules/a/b/c.js": b"2",
        "CAPCONDO/app/.git/HEAD": b"ref",
        "CAPCONDO/logs/journal.txt": b"Journal.",
        "vide.txt": b"",
    }
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(content)
    owner = make_test_user(username="rep_inventory", role="DIRECTOR")
    ws = Workspace(name="projets", owner_id=owner.id, source_root="1", source_path="projets", source_status="idle")
    db.add(ws)
    db.commit()

    source_scanner.scan_workspace(db, ws)
    summary = source_scanner.inventory_summary(db, ws)

    on_disk = sum(len(names) for _, _, names in os.walk(root))  # what Windows would count
    assert on_disk == 7
    assert summary["excluded_directories"] == 2 and summary["excluded_files"] == 3
    assert summary["detected_files"] + summary["excluded_files"] == on_disk
    # every inventoried file is in exactly one box
    docs = sum(summary["documents"].values())
    assert summary["detected_files"] == docs + summary["ignored"] + summary["unreadable"]


# --- the grounding guard on report text ----------------------------------------------------------------------


def test_page_references_are_kept_but_invented_figures_are_still_removed():
    from app.agent.grounding import Evidence, verify_answer

    evidence = Evidence()
    evidence.add("Le seuil de THDi est fixé à 5 % pour le transformateur T1.", document_id="d1", title="Audit.pdf")
    answer = (
        "- **Différences entre compteur et analyseur** (p. 11) : voir pages 8 et 9.\n"
        "- Seuil THDi : 5 % (p. 3).\n"
        "- Puissance installée : 1250 kVA (p. 4)."
    )
    report = verify_answer(answer, evidence, {"d1": {"document_id": "d1"}})
    assert "(p. 11) : voir pages 8 et 9." in report.answer  # references untouched, not cut after "p."
    assert "Seuil THDi : 5 % (p. 3)." in report.answer  # traced figure kept
    assert "1250" not in report.answer  # invented figure still removed


def test_a_generic_folder_name_never_beats_the_folder_actually_named():
    folders = ["Delice bouzid/", "Delice bouzid/Dossier/", "noveau consultation/", "Nouveau dossier/", "BIC/"]
    message = "fais une synthèse des exigences de gestion énergétique dans le dossier noveau consultation"
    assert reports.folders_named_in(message, folders)[0] == "noveau consultation/"
    assert reports.folders_named_in("synthèse des documents du dossier", folders) == []


async def test_an_expired_token_is_a_timeout_not_an_access_denial(monkeypatch):
    import jwt as pyjwt

    from app.mcp import context as mcp_context
    from app.policy.engine import SERVICE_UNAVAILABLE

    def expired(token):
        raise pyjwt.ExpiredSignatureError("Signature has expired")

    class Request:
        headers = {"x-internal-auth": "token"}

    class Ctx:
        request_context = type("RC", (), {"request": Request()})()

    monkeypatch.setattr(mcp_context, "verify_internal_token", expired)
    result = await mcp_server.get_document("00000000-0000-0000-0000-000000000000", Ctx())
    assert result["code"] == SERVICE_UNAVAILABLE
