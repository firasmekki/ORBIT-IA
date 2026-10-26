"""Folder hierarchy (app/services/folder_tree.py) and search depth
(policy Grant.folder_depth, routers/chat.py scope_mode): real parent/child
links from the stored paths, three counters never mixed, server-side
scope for search / synthesis / MCP tools."""

import asyncio
import time

import pytest

import app.routers.chat as chat_router
from app.agent import reports
from app.agent.orchestrator import AgentTurnResult
from app.core.config import get_settings
from app.core.security import mint_internal_token, verify_internal_token
from app.mcp import server as mcp_server
from app.models.document import Document
from app.models.workspace import Workspace
from app.policy.engine import check_document_in_scope, document_scope
from app.policy.rules import resolve_effective_grant
from app.policy.workspace import grant_in_workspace
from app.services.folder_tree import build_tree, folder_listing

PAGE = [(1, None, 1, "texte")]
PATHS = [
    "racine.pdf",
    "noveau consultation/appel.pdf",
    "noveau consultation/Attijari/CCTP/cctp.pdf",
    "noveau consultation/Attijari/CV2/cv.pdf",
    "noveau consultation/Exeliencia/CLINIQUE/Reponse_GTC_Technique_et_Financiere.docx",
    "noveau consultation/Exeliencia/Leoni/a.pdf",
    "noveau consultation/afric/projets.pdf",
    "Attijari/CCTP/autre.pdf",
    "Attijari/Rapports/r1.pdf",
    "Délice CLC/Études thermiques/note été.pdf",
    "noveau consultation 2/frere.pdf",
]


def _workspace(db, owner, **kwargs):
    ws = Workspace(name="projets", owner_id=owner.id, **kwargs)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


@pytest.fixture
def tree_ws(db, make_test_user, make_doc):
    owner = make_test_user(username="tree_dir", role="DIRECTOR")
    ws = _workspace(db, owner)
    docs = {
        p: make_doc(title=p.rsplit("/", 1)[-1], department="GENERAL", confidentiality="INTERNAL", pages=PAGE,
                    workspace_id=ws.id, relative_path=p)
        for p in PATHS
    }
    grant = grant_in_workspace(resolve_effective_grant(owner), ws, db)
    return owner, ws, grant, docs


def _scoped(grant, folders, depth="recursive"):
    import dataclasses

    return dataclasses.replace(grant, folder_scope=frozenset(folders), folder_depth=depth)


def _visible(db, grant):
    return sorted(p for (p,) in db.query(Document.relative_path).filter(*document_scope(grant)))


# --- 1. 2. 3. 4. 9. the tree ------------------------------------------------------------------------------------


def test_parent_has_exactly_its_real_subfolders():
    tree = build_tree(PATHS)
    assert tree.children["noveau consultation/"] == {
        "noveau consultation/Attijari/", "noveau consultation/Exeliencia/", "noveau consultation/afric/",
    }
    assert tree.children["noveau consultation/Exeliencia/"] == {
        "noveau consultation/Exeliencia/CLINIQUE/", "noveau consultation/Exeliencia/Leoni/",
    }


def test_a_subfolder_appears_only_under_its_real_parent():
    tree = build_tree(PATHS)
    owners = [parent for parent, kids in tree.children.items() if "noveau consultation/Exeliencia/CLINIQUE/" in kids]
    assert owners == ["noveau consultation/Exeliencia/"]
    assert "noveau consultation/Exeliencia/CLINIQUE/" not in tree.children[""]


def test_two_folders_with_the_same_name_stay_distinct():
    tree = build_tree(PATHS)
    assert "Attijari/CCTP/" in tree.stats and "noveau consultation/Attijari/CCTP/" in tree.stats
    assert tree.stats["Attijari/CCTP/"].total == 1
    assert tree.stats["noveau consultation/Attijari/CCTP/"].total == 1
    # "noveau consultation 2" is a sibling, not a child, of "noveau consultation"
    assert "noveau consultation 2/" in tree.children[""]


def test_direct_and_descendant_counters_are_never_mixed():
    tree = build_tree(PATHS)
    folder = tree.stats["noveau consultation/"]
    assert (folder.direct, folder.total, len(tree.children["noveau consultation/"])) == (1, 6, 3)
    assert (tree.stats[""].direct, tree.stats[""].total) == (1, len(PATHS))
    assert tree.stats["noveau consultation/Exeliencia/"].direct == 0


def test_accents_spaces_and_special_characters_are_kept_as_written(db, tree_ws):
    _, ws, grant, _ = tree_ws
    owner = tree_ws[0]
    listing = folder_listing(db, owner, ws, grant, parent="Délice CLC/")
    assert listing["folder"]["name"] == "Délice CLC"
    assert [f["name"] for f in listing["folders"]] == ["Études thermiques"]
    assert listing["breadcrumb"] == [{"name": "projets", "path": ""}, {"name": "Délice CLC", "path": "Délice CLC/"}]
    found = folder_listing(db, owner, ws, grant, query="etudes")  # accent-insensitive search
    assert [r["path"] for r in found["results"]] == ["Délice CLC/Études thermiques/"]


def test_listing_shows_breadcrumb_counters_and_direct_documents(db, tree_ws):
    owner, ws, grant, _ = tree_ws
    listing = folder_listing(db, owner, ws, grant, parent="noveau consultation/")
    folder = listing["folder"]
    assert (folder["direct_documents"], folder["subfolder_count"], folder["total_documents"]) == (1, 3, 6)
    assert [b["name"] for b in listing["breadcrumb"]] == ["projets", "noveau consultation"]
    assert [f["name"] for f in listing["folders"]] == ["afric", "Attijari", "Exeliencia"]
    assert [d["relative_path"] for d in listing["documents"]] == ["noveau consultation/appel.pdf"]
    exeliencia = next(f for f in listing["folders"] if f["name"] == "Exeliencia")
    assert (exeliencia["direct_documents"], exeliencia["subfolder_count"], exeliencia["total_documents"]) == (0, 2, 2)


def test_folder_search_returns_full_paths_for_same_names(db, tree_ws):
    # 10. the full path differentiates same-name folders
    owner, ws, grant, _ = tree_ws
    found = folder_listing(db, owner, ws, grant, query="cctp")
    assert [r["path"] for r in found["results"]] == ["Attijari/CCTP/", "noveau consultation/Attijari/CCTP/"]


def test_unknown_folder_is_not_found(db, tree_ws):
    owner, ws, grant, _ = tree_ws
    with pytest.raises(LookupError):
        folder_listing(db, owner, ws, grant, parent="n'existe pas/")


# --- 5. empty folder of the source --------------------------------------------------------------------------


def test_empty_folder_of_the_source_stays_visible_to_the_manager(db, make_test_user, tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "source_mount_base", str(tmp_path / "sources"))
    monkeypatch.setattr(settings, "source_root_1_host", "C:\\Users\\ORBIT\\Desktop")
    root = tmp_path / "sources" / "1" / "projets"
    (root / "BIC" / "Archives vides").mkdir(parents=True)
    (root / "BIC" / "node_modules").mkdir()
    (root / "Nouveau client").mkdir()
    owner = make_test_user(username="tree_src", role="DIRECTOR")
    ws = _workspace(db, owner, source_root="1", source_path="projets", source_status="idle")
    grant = grant_in_workspace(resolve_effective_grant(owner), ws, db)

    top = folder_listing(db, owner, ws, grant)
    assert {f["name"]: (f["total_documents"], f["has_children"]) for f in top["folders"]} == {
        "BIC": (0, True), "Nouveau client": (0, False),
    }
    bic = folder_listing(db, owner, ws, grant, parent="BIC/")
    assert [f["name"] for f in bic["folders"]] == ["Archives vides"]  # technical dirs skipped


# --- 6. 7. 11. search depth, server side --------------------------------------------------------------------


def test_current_folder_only_excludes_its_subfolders(db, tree_ws):
    _, _, grant, _ = tree_ws
    assert _visible(db, _scoped(grant, {"noveau consultation/"}, "direct")) == ["noveau consultation/appel.pdf"]
    assert _visible(db, _scoped(grant, set(), "direct")) == ["racine.pdf"]  # root only


def test_recursive_includes_descendants_but_never_siblings(db, tree_ws):
    _, _, grant, _ = tree_ws
    visible = _visible(db, _scoped(grant, {"noveau consultation/"}))
    assert "noveau consultation/Exeliencia/CLINIQUE/Reponse_GTC_Technique_et_Financiere.docx" in visible
    assert "noveau consultation/afric/projets.pdf" in visible
    assert "noveau consultation 2/frere.pdf" not in visible and "Attijari/CCTP/autre.pdf" not in visible
    assert len(visible) == 6


def test_synthesis_reads_respect_the_selected_depth(db, tree_ws):
    owner, ws, grant, docs = tree_ws
    clinique = docs["noveau consultation/Exeliencia/CLINIQUE/Reponse_GTC_Technique_et_Financiere.docx"]
    direct = _scoped(grant, {"noveau consultation/"}, "direct")
    assert not check_document_in_scope(grant=direct, document=clinique).allowed
    assert check_document_in_scope(grant=_scoped(grant, {"noveau consultation/"}), document=clinique).allowed
    # the same through the MCP tool the reports use (get_document)
    refused = mcp_server._get_document_impl(
        str(owner.id), str(clinique.id), workspace_id=str(ws.id),
        folder_scope=("noveau consultation/",), folder_depth="direct",
    )
    assert refused.get("error")
    allowed = mcp_server._get_document_impl(
        str(owner.id), str(clinique.id), workspace_id=str(ws.id), folder_scope=("noveau consultation/",),
    )
    assert not allowed.get("error")


def test_the_depth_is_signed_into_the_internal_token():
    token = mint_internal_token(user_id="u", role="DIRECTOR", departments=[], max_level="SECRET",
                                workspace_id="w", folder_scope=["BIC/"], folder_depth="direct")
    assert verify_internal_token(token)["folder_depth"] == "direct"
    default = mint_internal_token(user_id="u", role="DIRECTOR", departments=[], max_level="SECRET")
    assert verify_internal_token(default)["folder_depth"] == "recursive"


# --- 8. all authorized workspaces ----------------------------------------------------------------------------


def test_all_workspaces_mode_searches_only_workspaces_the_user_may_use(db, make_test_user, monkeypatch):
    user = make_test_user(username="tree_emp", role="EMPLOYEE")
    other = make_test_user(username="tree_other", role="EMPLOYEE")
    mine = _workspace(db, user)
    theirs = _workspace(db, other)
    searched = []

    async def fake_turn(username, **kwargs):
        searched.append(kwargs["workspace_id"])
        return AgentTurnResult(answer=f"résultats de {kwargs['workspace_name']}")

    monkeypatch.setattr(chat_router, "_timed_turn", fake_turn)
    grant = resolve_effective_grant(user)
    result = asyncio.run(chat_router._search_all_workspaces(db, user, grant, "recherche contrat de maintenance"))
    assert str(mine.id) in searched and str(theirs.id) not in searched
    assert "Espace « projets »" in result.answer

    searched.clear()
    synthesis = asyncio.run(chat_router._search_all_workspaces(db, user, grant, "Fais une synthèse du dossier BIC"))
    assert searched == [] and synthesis.answer == chat_router.ALL_WORKSPACES_ONLY_SEARCH


def test_scope_name_says_which_depth_was_searched(db, tree_ws):
    _, ws, _, _ = tree_ws
    assert chat_router._scoped_name(ws, ["BIC/"], "direct") == "projets › BIC (dossier seul, sans sous-dossiers)"
    assert chat_router._scoped_name(ws, [], "direct") == "projets › racine (dossier seul, sans sous-dossiers)"
    assert chat_router._scoped_name(ws, ["BIC/"]) == "projets › BIC"


# --- a full path in the request designates its document ------------------------------------------------------


def test_full_path_in_the_request_picks_that_document():
    candidates = [
        {"relative_path": "noveau consultation/Architecture.pdf"},
        {"relative_path": "SAGEMCOM SME/Architecture.pdf"},
        {"relative_path": "SAGEMCOM SME/RE__PSSI_et_télémaintenance/Architecture.pdf"},
    ]
    picked = reports.resolve_choice(
        "je veux un rapport pour noveau consultation/Architecture.pdf", candidates, folders_only=True
    )
    assert picked == [candidates[0]]
    deep = reports.resolve_choice(
        "rapport de SAGEMCOM SME/RE__PSSI_et_télémaintenance/Architecture.pdf", candidates, folders_only=True
    )
    assert deep == [candidates[2]]


# --- 12. volume -----------------------------------------------------------------------------------------------


def test_tree_of_twenty_thousand_documents_builds_fast():
    paths = [f"client {c}/lot {l}/sous-lot {s}/doc {d}.pdf" for c in range(40) for l in range(10) for s in range(5) for d in range(10)]
    started = time.perf_counter()
    tree = build_tree(paths)
    elapsed = time.perf_counter() - started
    assert len(paths) == 20_000 and tree.stats[""].total == 20_000
    assert tree.stats["client 3/"].total == 500 and len(tree.children["client 3/"]) == 10
    assert elapsed < 1.0, f"{elapsed:.2f}s"
