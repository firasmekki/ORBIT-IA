"""Real MCP server exposing internal company tools over streamable-HTTP.

Runs as its own process (`python -m app.mcp.server`), reachable only from
inside the private Docker network - it is never published to the host or
the Internet (see docker-compose.yml). The only other component allowed to
talk to it is the AI Agent Orchestrator, and only with a valid short-lived
internal token minted by the backend for the authenticated end user.

Every tool independently re-checks the caller's permission with the same
policy engine used by the REST API and the RAG retriever (defense in
depth): even a compromised or buggy orchestrator cannot get data out of
here that the underlying role/grant does not carry. Permissions are always
resolved fresh from the database (`resolve_effective_grant`), not trusted
from the internal token's payload - a Director's per-user access grant (or
revocation) takes effect on the very next tool call, no new token needed.
"""

import dataclasses
import logging
import re
import uuid
from functools import partial
from datetime import datetime, timedelta

import anyio
from mcp.server.fastmcp import Context, FastMCP
from sqlalchemy import func, or_, true

from app.agent.chart import ChartValidationError, read_sheet_rows, resolve_sheet_name, validate_chart_spec
from app.core.alert_service import create_alert, security_event_key
from app.core.audit_logger import log_event
from app.core.database import SessionLocal
from app.core.storage import download_file
from app.mcp.context import IdentityError, IdentityExpired, extract_identity
from app.models.document import Document, DocumentPage
from app.models.finance import FinancialRecord
from app.models.user import User
from app.policy.engine import (
    ACCESS_DENIED,
    AMBIGUOUS,
    NOT_FOUND,
    SERVICE_UNAVAILABLE,
    VALIDATION_ERROR,
    check_document_access,
    check_document_in_scope,
    check_tool_access,
    document_scope,
    outside_document_scope,
    retrieval_scope,
    tool_error,
)
from app.policy.rules import Grant, resolve_effective_grant
from app.policy.workspace import WorkspaceAccessError, grant_in_workspace, resolve_workspace
from app.rag.embeddings import EmbeddingServiceError
from app.rag.normalize import normalize
from app.rag.retriever import retrieve_with_out_of_scope_signal
from app.services import text_search
from app.services.errors import ServiceError

MAX_LIST_RESULTS = 200
MAX_SECTION_CHARS = 2000
MAX_TITLE_CHOICES = 10

# Identical for a nonexistent document_id and one that exists but is
# outside the caller's grant - see get_document_section's module-level
# note below for why this has to be byte-for-byte the same everywhere
# (including the tool_trace decision/reason the end user's own chat UI
# shows, not just this string).
DOCUMENT_NOT_FOUND_OR_DENIED = "document introuvable ou accès non autorisé à cette section"

mcp_app = FastMCP(
    name="orbitia-mcp",
    instructions=(
        "Internal Orbitia company tools: search_documents, get_document, "
        "search_database, get_company_information, search_keyword, "
        "list_documents. Every call is scoped to the authenticated caller "
        "resolved server-side; there is no user_id or identity argument on "
        "any tool."
    ),
    host="0.0.0.0",
    port=8090,
)


def _load_user_and_grant(
    db, user_id: str, workspace_id: str | None = None, folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive"
) -> tuple[User | None, Grant | None]:
    """The caller and their RBAC/ABAC grant, evaluated inside the workspace
    named by the signed internal token - re-checked here (defense in depth:
    the backend already checked it before minting the token). A workspace
    the user may not use yields no grant at all, so every tool refuses."""
    try:
        user = db.get(User, uuid.UUID(user_id))
    except ValueError:
        return None, None
    if user is None or not user.is_active:
        return None, None
    grant = resolve_effective_grant(user)
    if grant is None:
        return user, None
    try:
        workspace = resolve_workspace(db, user, workspace_id)
    except WorkspaceAccessError:
        return user, None
    scoped = grant_in_workspace(grant, workspace, db)
    # Signed into the token by the backend; only ever narrows the grant.
    if folder_scope:
        scoped = dataclasses.replace(scoped, folder_scope=frozenset(folder_scope))
    if folder_depth == "direct":
        scoped = dataclasses.replace(scoped, folder_depth="direct")
    return user, scoped


def _identity_error(exc: IdentityError) -> dict:
    """A forged/missing identity is refused (ACCESS_DENIED - a security
    event); an expired one is a technical timeout (SERVICE_UNAVAILABLE): a
    long report outliving its token must not raise a false security alert."""
    if isinstance(exc, IdentityExpired):
        return tool_error(SERVICE_UNAVAILABLE, str(exc))
    return tool_error(ACCESS_DENIED, str(exc))


def _document_names(doc: Document) -> list[str]:
    """Every name a user may call a document by: its title, its original
    filename, and - for a synced workspace file - its relative path and the
    bare file name ("RH/procedure.docx" and "procedure.docx")."""
    names = [doc.title, doc.source_filename or ""]
    if doc.relative_path:
        names += [doc.relative_path, doc.relative_path.rsplit("/", 1)[-1]]
    return [normalize(n) for n in names if n]


def _audit(db, *, user: User | None, role: str | None, action: str, decision: str, **kwargs) -> None:
    log_event(
        user_id=user.id if user else None,
        username=user.username if user else None,
        role=role,
        action=action,
        decision=decision,
        **kwargs,
    )


# --- sync implementations (run off the event loop via anyio.to_thread) -----


def _search_documents_impl(user_id: str, query: str, top_k: int, *, workspace_id: str | None = None, folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive") -> dict:
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        decision = check_tool_access(grant=grant, tool_name="search_documents")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_SEARCH_DOCUMENTS", decision="DENY", reason=decision.reason)
            return tool_error(ACCESS_DENIED, decision.reason)

        try:
            results, out_of_scope_hint = retrieve_with_out_of_scope_signal(
                db, grant=grant, query=query, top_k=min(top_k, 10)
            )
        except EmbeddingServiceError as exc:
            # Infrastructure outage, not a permission decision - must never
            # be phrased as an access refusal to the end user.
            _audit(
                db,
                user=user,
                role=grant.role,
                action="MCP_SEARCH_DOCUMENTS",
                decision="DENY",
                reason=f"service unavailable: {exc}",
            )
            return tool_error(SERVICE_UNAVAILABLE, str(exc), service_unavailable=True)

        _audit(
            db,
            user=user,
            role=grant.role,
            action="MCP_SEARCH_DOCUMENTS",
            decision="ALLOW",
            reason="ok",
            extra={"query": query, "result_count": len(results), "out_of_scope_hint": out_of_scope_hint},
        )
        response = {
            "results": [
                {
                    "document_id": str(r["document_id"]),
                    "title": r["title"],
                    "relative_path": r.get("relative_path"),
                    "department": r["department"],
                    "confidentiality": r["confidentiality"],
                    "excerpt": r["chunk_text"][:600],
                    # Relevance score: 1.0 = identical direction, 0.0 =
                    # unrelated, negative = opposite - higher is more
                    # relevant. Results are already ordered by it.
                    "score": r["score"],
                    # Only set when this chunk's page/section could be
                    # exactly determined (see app/rag/ingest.py) - never a
                    # guess, null when unknown (e.g. a document indexed
                    # before this linkage existed, pending re-ingestion).
                    "page": r["page"],
                    "section": r["section"],
                }
                for r in results
            ]
        }
        if out_of_scope_hint:
            # Metadata only (department/confidentiality) - never the
            # document's title or content. Lets the orchestrator flag this
            # as a restricted match instead of silently answering "nothing
            # found", and lets it raise a Director-facing alert.
            response["restricted_match"] = out_of_scope_hint
        return response
    finally:
        db.close()


# Pages worth reading for amounts: a budget/amount/cost word AND a currency.
_AMOUNT_PAGE_KEYWORDS = r"(budget|montant|co[uû]t|enveloppe|estimati)"
_AMOUNT_PAGE_CURRENCY = r"([0-9] ?(€|eur|dt|tnd|dinar|mdt|kdt))"
MAX_AMOUNT_PAGES = 5000


def _find_document_amounts_impl(user_id: str, *, workspace_id: str | None = None, folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive") -> dict:
    """Server-side scan of the scope (document_scope: ACL + workspace +
    folders + depth, applied in the SQL before any row is read). Same
    right as search_documents - it reads the same authorized documents.
    Raises no alert: it is not a user's term search."""
    from app.agent.document_metrics import extract_amounts  # noqa: PLC0415 - shared deterministic extractor

    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        decision = check_tool_access(grant=grant, tool_name="search_documents")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_FIND_DOCUMENT_AMOUNTS", decision="DENY", reason=decision.reason)
            return tool_error(ACCESS_DENIED, decision.reason)
        documents_in_scope = db.query(func.count(Document.id)).filter(*document_scope(grant)).scalar()
        rows = (
            db.query(Document.id, Document.title, Document.relative_path, DocumentPage.page_no, DocumentPage.section, DocumentPage.text)
            .join(DocumentPage, DocumentPage.document_id == Document.id)
            .filter(*document_scope(grant))
            .filter(DocumentPage.text.op("~*")(_AMOUNT_PAGE_KEYWORDS), DocumentPage.text.op("~*")(_AMOUNT_PAGE_CURRENCY))
            .order_by(Document.relative_path, DocumentPage.order_index)
            .limit(MAX_AMOUNT_PAGES)
            .all()
        )
        mentions, documents_with_amounts = [], set()
        for doc_id, title, path, page_no, section, text_ in rows:
            for m in extract_amounts(text_ or ""):
                documents_with_amounts.add(doc_id)
                mentions.append({
                    "document_id": str(doc_id), "title": title, "relative_path": path, "page": page_no,
                    "section": section, "value": m.value, "currency": m.currency, "year": m.year, "sentence": m.sentence,
                })
        _audit(
            db, user=user, role=grant.role, action="MCP_FIND_DOCUMENT_AMOUNTS", decision="ALLOW", reason="ok",
            extra={"documents_in_scope": documents_in_scope, "pages_read": len(rows), "mentions": len(mentions)},
        )
        return {
            "documents_in_scope": documents_in_scope,
            "pages_read": len(rows),
            "pages_truncated": len(rows) >= MAX_AMOUNT_PAGES,
            "documents_with_amounts": len(documents_with_amounts),
            "mentions": mentions,
        }
    finally:
        db.close()


def _get_document_impl(user_id: str, document_id: str, *, workspace_id: str | None = None, folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive") -> dict:
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        tool_decision = check_tool_access(grant=grant, tool_name="get_document")
        if not tool_decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_GET_DOCUMENT", decision="DENY", reason=tool_decision.reason)
            return tool_error(ACCESS_DENIED, tool_decision.reason)

        try:
            doc_uuid = uuid.UUID(document_id)
        except ValueError:
            return tool_error(VALIDATION_ERROR, "identifiant de document invalide")

        doc = db.get(Document, doc_uuid)
        if doc is None:
            _audit(
                db,
                user=user,
                role=grant.role,
                action="MCP_GET_DOCUMENT",
                decision="DENY",
                resource_id=document_id,
                reason="document not found",
            )
            return tool_error(NOT_FOUND, DOCUMENT_NOT_FOUND_OR_DENIED, concealed=True)

        decision = check_document_in_scope(grant=grant, document=doc)
        _audit(
            db,
            user=user,
            role=grant.role,
            action="MCP_GET_DOCUMENT",
            decision="ALLOW" if decision.allowed else "DENY",
            resource_type="document",
            resource_id=str(doc.id),
            reason=decision.reason,
            extra={"title": doc.title, "department": doc.department, "confidentiality": doc.confidentiality},
        )
        if not decision.allowed:
            code = NOT_FOUND if decision.out_of_context else ACCESS_DENIED
            return tool_error(code, DOCUMENT_NOT_FOUND_OR_DENIED, concealed=True)

        pages = (
            db.query(DocumentPage.page_no, DocumentPage.section, DocumentPage.text)
            .filter(DocumentPage.document_id == doc.id)
            .order_by(DocumentPage.order_index)
            .all()
        )
        return {
            "document_id": str(doc.id),
            "title": doc.title,
            "relative_path": doc.relative_path,
            "department": doc.department,
            "confidentiality": doc.confidentiality,
            "content": doc.content,
            # Same text, split as extracted (PDF page / sheet / heading):
            # lets a report cite "p. 4" instead of guessing.
            "pages": [{"page": p.page_no, "section": p.section, "text": p.text} for p in pages],
            "status": doc.status,
        }
    finally:
        db.close()


def _list_folders_impl(user_id: str, *, workspace_id: str | None = None, folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive") -> dict:
    """The folders of the workspace holding documents this caller may read,
    with their document count - names only, the same visibility filter as
    every other tool (document_scope). Lets the assistant understand "dans
    le dossier noveau consultation" without guessing a path."""
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        decision = check_tool_access(grant=grant, tool_name="list_documents")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_LIST_FOLDERS", decision="DENY", reason=decision.reason)
            return tool_error(ACCESS_DENIED, decision.reason)
        counts: dict[str, int] = {}
        total = 0
        for (path,) in db.query(Document.relative_path).filter(*document_scope(grant)):
            total += 1
            parts = (path or "").split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                folder = "/".join(parts[:depth]) + "/"
                counts[folder] = counts.get(folder, 0) + 1
        _audit(db, user=user, role=grant.role, action="MCP_LIST_FOLDERS", decision="ALLOW", reason="ok",
               extra={"folders": len(counts)})
        return {"folders": [{"path": p, "document_count": c} for p, c in sorted(counts.items())], "document_count": total}
    finally:
        db.close()


def _search_document_text_impl(
    user_id: str, query: str, case_sensitive: bool, whole_word: bool, count_only: bool,
    *,
    workspace_id: str | None = None,
    folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive",
) -> dict:
    """Exact text search (app/services/text_search.py). The result carries
    NO trace of documents outside the caller's grant - not even that some
    exist: the out-of-scope probe only feeds the audit row and a Director
    alert raised inside the service."""
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        decision = check_tool_access(grant=grant, tool_name="search_document_text")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="DOCUMENT_TEXT_SEARCH", decision="DENY", reason=decision.reason)
            return tool_error(ACCESS_DENIED, decision.reason)
        try:
            result, _hint = text_search.search_document_text(
                db, user, grant, query, case_sensitive=case_sensitive, whole_word=whole_word, count_only=count_only
            )
        except ServiceError as exc:
            return exc.as_tool_error()
        except Exception as exc:  # noqa: BLE001 - infrastructure, never a permission decision
            _audit(db, user=user, role=grant.role, action="DOCUMENT_TEXT_SEARCH", decision="ERROR", reason=f"service unavailable: {exc}")
            return tool_error(SERVICE_UNAVAILABLE, str(exc), service_unavailable=True)
        return result
    finally:
        db.close()


def _search_keyword_impl(user_id: str, keyword: str, whole_word: bool, *, workspace_id: str | None = None, folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive") -> dict:
    """Legacy exact-count tool, same engine as search_document_text
    (case-insensitive), kept in its original result shape for existing
    callers. `restricted_match` / `alert_raised` are internal: the agent
    never shows them (see orchestrator._call_search_keyword)."""
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        decision = check_tool_access(grant=grant, tool_name="search_keyword")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_SEARCH_KEYWORD", decision="DENY", reason=decision.reason)
            return tool_error(ACCESS_DENIED, decision.reason)
        try:
            result, hint = text_search.search_document_text(
                db, user, grant, keyword, whole_word=whole_word, audit_action="MCP_SEARCH_KEYWORD"
            )
        except ServiceError:
            return tool_error(VALIDATION_ERROR, "mot-clé vide ou invalide")
        except Exception as exc:  # noqa: BLE001
            _audit(db, user=user, role=grant.role, action="MCP_SEARCH_KEYWORD", decision="ERROR", reason=f"service unavailable: {exc}")
            return tool_error(SERVICE_UNAVAILABLE, str(exc), service_unavailable=True)

        response: dict = {
            "keyword": keyword,
            "whole_word": whole_word,
            "total_count": result["total_occurrences"],
            "documents": [
                {
                    "document_id": d["document_id"],
                    "title": d["title"],
                    "department": d["department"],
                    "confidentiality": d["confidentiality"],
                    "relative_path": d.get("relative_path"),
                    "count": d["occurrences"],
                    "locations": [
                        {"page": m["page"], "section": m["section"], "line": m["line"], "excerpt": m["context"]}
                        for m in d["matches"]
                    ],
                }
                for d in result["documents"]
            ],
        }
        if hint:
            response["restricted_match"] = hint
            response["alert_raised"] = True
        return response
    finally:
        db.close()


def _list_documents_impl(
    user_id: str,
    document_type: str | None,
    department: str | None,
    date_min: str | None,
    date_max: str | None,
    author: str | None,
    *,
    workspace_id: str | None = None,
    folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive",
) -> dict:
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        decision = check_tool_access(grant=grant, tool_name="list_documents")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_LIST_DOCUMENTS", decision="DENY", reason=decision.reason)
            return tool_error(ACCESS_DENIED, decision.reason)

        filters = {
            "document_type": document_type,
            "department": department,
            "date_min": date_min,
            "date_max": date_max,
            "author": author,
        }
        departments, max_rank = retrieval_scope(grant=grant)
        if not departments:
            _audit(db, user=user, role=grant.role, action="MCP_LIST_DOCUMENTS", decision="ALLOW", reason="ok", extra={"filters": filters, "result_count": 0})
            return {"results": []}

        # A department filter only narrows document_scope - which already
        # decides visibility (role, personal documents, Director rules): a
        # department outside the role still lists the user's own payslip,
        # and nothing else, with no signal about what else is there.
        query = (
            db.query(Document, User)
            .outerjoin(User, User.id == Document.owner_id)
            .filter(*document_scope(grant))
        )
        if department:
            query = query.filter(Document.department == department)
        try:
            if date_min:
                query = query.filter(Document.created_at >= datetime.fromisoformat(date_min))
            if date_max:
                query = query.filter(Document.created_at < datetime.fromisoformat(date_max) + timedelta(days=1))
        except ValueError:
            return tool_error(VALIDATION_ERROR, "format de date invalide, utilisez AAAA-MM-JJ")

        rows = query.order_by(Document.created_at.desc()).limit(MAX_LIST_RESULTS).all()

        results = []
        normalized_type = document_type.lower().lstrip(".") if document_type else None
        normalized_author = normalize(author) if author else None
        for doc, owner in rows:
            ext = doc.source_filename.rsplit(".", 1)[-1].lower() if doc.source_filename and "." in doc.source_filename else None
            if normalized_type and ext != normalized_type:
                continue
            author_name = owner.full_name if owner else None
            if normalized_author and (not author_name or normalized_author not in normalize(author_name)):
                continue
            results.append(
                {
                    "document_id": str(doc.id),
                    "title": doc.title,
                    "department": doc.department,
                    "confidentiality": doc.confidentiality,
                    "type": ext,
                    "relative_path": doc.relative_path,
                    "author": author_name,
                    "created_at": doc.created_at.isoformat(),
                }
            )

        _audit(
            db,
            user=user,
            role=grant.role,
            action="MCP_LIST_DOCUMENTS",
            decision="ALLOW",
            reason="ok",
            extra={"filters": filters, "result_count": len(results)},
        )
        return {"results": results}
    finally:
        db.close()


def _get_document_section_impl(
    user_id: str,
    document_id: str | None,
    document_name: str | None,
    page: int | None,
    section: str | None,
    offset: int,
    *,
    workspace_id: str | None = None,
    folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive",
) -> dict:
    """Security-critical: a nonexistent document_id and one that exists but
    is outside the caller's grant must be completely indistinguishable to
    the caller - same error string, same tool_trace decision/reason (the
    chat UI shows tool_trace to the requester themselves, not just to a
    Director, so even that has to match). The alert for a genuine denial is
    raised directly from here (create_alert, same mechanism
    routers/documents.py's update_document already uses) rather than via
    routers/chat.py's tool_trace-scanning - that scan can't tell these two
    DENY entries apart, deliberately, so it can't be what decides whether
    to page a Director either.

    document_name resolution only ever searches documents already inside
    `retrieval_scope` - an unauthorized document is never a candidate, so
    "no match" from that path can only mean "not found among what you can
    see" and never leaks whether something else matches elsewhere.
    """
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        decision = check_tool_access(grant=grant, tool_name="get_document_section")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_GET_DOCUMENT_SECTION", decision="DENY", reason=decision.reason)
            return tool_error(ACCESS_DENIED, decision.reason)

        if not document_id and not document_name:
            return tool_error(VALIDATION_ERROR, "précisez document_id ou document_name")

        departments, max_rank = retrieval_scope(grant=grant)

        doc: Document | None = None
        if document_id:
            try:
                doc_uuid = uuid.UUID(document_id)
            except ValueError:
                doc = None
            else:
                doc = db.get(Document, doc_uuid)
        else:
            normalized_name = normalize(document_name)
            candidates: list[Document] = db.query(Document).filter(*document_scope(grant)).all() if departments else []
            exact = [d for d in candidates if normalized_name in _document_names(d)]
            partial_matches = [
                d for d in candidates if normalized_name and any(normalized_name in n for n in _document_names(d))
            ]
            matches = exact or partial_matches

            if not matches:
                # Nothing the caller may read. An EXACT name match on a
                # protected document of the same workspace is still a real
                # attempt on it: handed to the access check below so it is
                # refused, audited and alerted - with the very same neutral
                # answer as a genuine absence.
                protected = [
                    d for d in db.query(Document).filter(*outside_document_scope(grant)).all()
                    if normalized_name and normalized_name in _document_names(d)
                ]
                if len(protected) != 1:
                    _audit(
                        db, user=user, role=grant.role, action="MCP_GET_DOCUMENT_SECTION", decision="ALLOW",
                        reason="ok", extra={"document_name": document_name, "match_count": 0},
                    )
                    return tool_error(NOT_FOUND, DOCUMENT_NOT_FOUND_OR_DENIED, concealed=True)
                matches = protected
            if len(matches) > 1:
                _audit(
                    db, user=user, role=grant.role, action="MCP_GET_DOCUMENT_SECTION", decision="ALLOW",
                    reason="ok", extra={"document_name": document_name, "match_count": len(matches)},
                )
                return {
                    "choices": [
                        {"document_id": str(d.id), "title": d.title, "relative_path": d.relative_path}
                        for d in sorted(matches, key=lambda d: (d.relative_path or d.title).lower())[:MAX_TITLE_CHOICES]
                    ],
                    "match_count": len(matches),
                    "page": page,
                    "section": section,
                }
            doc = matches[0]

        if doc is None:
            _audit(
                db, user=user, role=grant.role, action="MCP_GET_DOCUMENT_SECTION", decision="DENY",
                resource_id=document_id, reason="document not found",
            )
            return tool_error(NOT_FOUND, DOCUMENT_NOT_FOUND_OR_DENIED, concealed=True)

        access_decision = check_document_in_scope(grant=grant, document=doc)
        if not access_decision.allowed and access_decision.out_of_context:
            _audit(
                db, user=user, role=grant.role, action="MCP_GET_DOCUMENT_SECTION", decision="DENY",
                resource_type="document", resource_id=str(doc.id), reason=access_decision.reason,
            )
            return tool_error(NOT_FOUND, DOCUMENT_NOT_FOUND_OR_DENIED, concealed=True)
        if not access_decision.allowed:
            _audit(
                db, user=user, role=grant.role, action="MCP_GET_DOCUMENT_SECTION", decision="DENY",
                resource_type="document", resource_id=str(doc.id), reason=access_decision.reason,
                extra={"title": doc.title, "department": doc.department, "confidentiality": doc.confidentiality},
            )
            create_alert(
                alert_type="DOCUMENT_ACCESS_DENIED",
                user_id=user.id,
                username=user.username,
                role=user.role,
                title=f"Tentative d'accès refusée dans l'assistant IA ({user.full_name})",
                description=(
                    f"Document visé : « {doc.title} » ({doc.department}/{doc.confidentiality})\n"
                    f"Refus : {access_decision.reason}"
                ),
                resource_type="document",
                resource_id=str(doc.id),
                dedup_key=security_event_key(user.id, "DOCUMENT_ACCESS_DENIED", "get_document_section", doc.id),
            )
            return tool_error(ACCESS_DENIED, DOCUMENT_NOT_FOUND_OR_DENIED, concealed=True, alert_raised=True)

        page_query = db.query(DocumentPage).filter(DocumentPage.document_id == doc.id)
        if page is None and not section:
            # No location given at all (e.g. a reference_context-driven
            # follow-up whose source result had no known page/section yet -
            # a legacy chunk pending re-ingestion) - open the document from
            # its first page/section rather than demanding a location the
            # caller has no way to know. An explicit page/section request
            # that genuinely doesn't exist still errors normally below.
            match = page_query.order_by(DocumentPage.order_index).first()
            if match is None:
                return tool_error(NOT_FOUND, f"« {doc.title} » n'a aucun contenu indexé")
            page, section = match.page_no, match.section
        elif page is not None:
            match = page_query.filter(DocumentPage.page_no == page).first()
        else:
            normalized_section = normalize(section)
            candidates_pages = [p for p in page_query.all() if p.section]
            exact_pages = [p for p in candidates_pages if normalize(p.section) == normalized_section]
            partial_pages = [p for p in candidates_pages if normalized_section in normalize(p.section)]
            found_pages = exact_pages or partial_pages
            match = found_pages[0] if found_pages else None

        if match is None:
            _audit(
                db, user=user, role=grant.role, action="MCP_GET_DOCUMENT_SECTION", decision="ALLOW",
                resource_type="document", resource_id=str(doc.id), reason="page/section not found",
                extra={"title": doc.title, "page": page, "section": section},
            )
            where = f"page {page}" if page is not None else f"section « {section} »"
            return tool_error(NOT_FOUND, f"{where} introuvable dans « {doc.title} »")

        total_length = len(match.text)
        start = max(0, offset)
        end = min(total_length, start + MAX_SECTION_CHARS)
        chunk = match.text[start:end]
        truncated = end < total_length

        _audit(
            db, user=user, role=grant.role, action="MCP_GET_DOCUMENT_SECTION", decision="ALLOW",
            resource_type="document", resource_id=str(doc.id), reason="ok",
            extra={"title": doc.title, "page": match.page_no, "section": match.section, "offset": start},
        )
        return {
            "document_id": str(doc.id),
            "title": doc.title,
            "relative_path": doc.relative_path,
            "department": doc.department,
            "confidentiality": doc.confidentiality,
            "page": match.page_no,
            "section": match.section,
            "text": chunk,
            "offset": start,
            "total_length": total_length,
            "truncated": truncated,
        }
    finally:
        db.close()


_STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "de", "du", "quel", "quelle", "quels", "quelles",
    "est", "sont", "pour", "avec", "dans", "sur", "moi", "donne", "montre", "quelle", "que",
    "qui", "combien", "budget", "budgets", "svp", "please", "the", "what", "is", "for", "me", "show",
    # pronouns / glue: never what a financial line is about
    "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles", "et", "ou", "au", "aux", "en", "par",
    "ce", "cet", "cette", "ces", "mon", "ma", "mes", "ton", "ta", "tes", "son", "sa", "ses", "notre", "nos",
    "peut", "peux", "pouvez", "fais", "faire", "trace", "courbe", "graphique", "evolution", "annee", "annees",
    "total", "totale", "montant", "montants", "valeur", "valeurs",
}


def _significant_terms(query: str) -> set[str]:
    """Normalized whole words that can identify a financial line."""
    return {
        w for w in re.findall(r"[a-z0-9]+", normalize(query))
        if len(w) >= 2 and not w.isdigit() and w not in _STOPWORDS
    }


def _label_words(label: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", normalize(label or "")))


def _search_database_impl(user_id: str, query: str, *, workspace_id: str | None = None, folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive") -> dict:
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        decision = check_tool_access(grant=grant, tool_name="search_database")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_SEARCH_DATABASE", decision="DENY", reason=decision.reason)
            return tool_error(ACCESS_DENIED, decision.reason)

        departments, max_rank = retrieval_scope(grant=grant)
        base_query = db.query(FinancialRecord).filter(
            FinancialRecord.department.in_(departments),
            FinancialRecord.confidentiality_rank <= max_rank,
        )

        # Authorized rows only (department + confidentiality above), THEN
        # matched. An explicit empty query lists the authorized catalog (the
        # chart planner picks in it only what the user names). A free-text
        # query matches on WHOLE significant words, accents and case
        # ignored: "tu" no longer matches "Infras-tu-cture" (the bug that
        # put "Budget IT / Infrastructure 2025" under "le budget du projet").
        # Only generic words -> nothing (never an arbitrary record); a year
        # written in the query is a structured criterion.
        authorized = base_query.order_by(FinancialRecord.label).all()
        if not query.strip():
            rows = authorized
        else:
            terms = _significant_terms(query)
            years = {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", query)}
            rows = [
                r for r in authorized
                if terms and terms & _label_words(r.label) and (not years or r.year in years)
            ]
        rows = rows[:20]
        _audit(
            db,
            user=user,
            role=grant.role,
            action="MCP_SEARCH_DATABASE",
            decision="ALLOW",
            reason="ok",
            extra={"query": query, "result_count": len(rows)},
        )
        return {
            "results": [
                {"label": r.label, "amount": float(r.amount), "year": r.year, "confidentiality": r.confidentiality}
                for r in rows
            ]
        }
    finally:
        db.close()


def _get_company_information_impl(user_id: str, topic: str, *, workspace_id: str | None = None, folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive") -> dict:
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        decision = check_tool_access(grant=grant, tool_name="get_company_information")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_COMPANY_INFO", decision="DENY", reason=decision.reason)
            return tool_error(ACCESS_DENIED, decision.reason)

        rows = (
            db.query(Document)
            .filter(
                *document_scope(grant),
                Document.department == "GENERAL",
                (Document.title.ilike(f"%{topic}%") | Document.content.ilike(f"%{topic}%")),
            )
            .limit(3)
            .all()
        )
        _audit(
            db,
            user=user,
            role=grant.role,
            action="MCP_COMPANY_INFO",
            decision="ALLOW",
            reason="ok",
            extra={"topic": topic, "result_count": len(rows)},
        )
        return {
            "results": [
                {"document_id": str(d.id), "title": d.title, "excerpt": d.content[:600]} for d in rows
            ]
        }
    finally:
        db.close()


SPREADSHEET_EXTENSIONS = {"xlsx"}


def _resolve_spreadsheet_document(
    db, grant: Grant, document_id: str | None, document_name: str | None
) -> tuple[Document | None, dict | None]:
    """Same resolution contract as _get_document_section_impl's document_id/
    document_name lookup (exact-then-partial, ACL-scoped, ambiguous name ->
    choices) - except it also matches the original filename
    (`source_filename`), not just the title, since a user naming a
    spreadsheet almost always types it with its extension
    ("Orbitia_Test_Entreprise_Complet.xlsx"), which never appears in the
    title (the extension is stripped at upload)."""
    departments, max_rank = retrieval_scope(grant=grant)

    if document_id:
        try:
            doc_uuid = uuid.UUID(document_id)
        except ValueError:
            return None, tool_error(NOT_FOUND, DOCUMENT_NOT_FOUND_OR_DENIED, concealed=True, error_kind="file_not_found")
        return db.get(Document, doc_uuid), None

    normalized_name = normalize(document_name or "")
    candidates: list[Document] = db.query(Document).filter(*document_scope(grant)).all() if departments else []
    names = _document_names

    exact = [d for d in candidates if normalized_name in names(d)]
    partial = [d for d in candidates if normalized_name and any(normalized_name in n for n in names(d))]
    matches = exact or partial

    if not matches:
        # Nothing the caller may see. An EXACT name match outside their
        # grant is still a genuine attempt on a protected document (case B):
        # it is handed back so the caller's check_document_access refuses
        # it, audits it and alerts - while the user-facing answer stays the
        # same neutral DOCUMENT_NOT_FOUND_OR_DENIED as a real absence.
        # Exact only: a partial match on someone else's document is far too
        # weak a signal to page a Director over.
        if normalized_name:
            out_of_scope = db.query(Document).filter(*outside_document_scope(grant)).all()
            protected = [d for d in out_of_scope if normalized_name in names(d)]
            if len(protected) == 1:
                return protected[0], None
        return None, tool_error(NOT_FOUND, DOCUMENT_NOT_FOUND_OR_DENIED, concealed=True, error_kind="file_not_found")
    if len(matches) > 1:
        titles = ", ".join(d.title for d in matches[:MAX_TITLE_CHOICES])
        return None, {
            "code": AMBIGUOUS,
            "status": "ambiguous",
            "error": f"plusieurs documents correspondent à « {document_name} » ({titles}) - précisez lequel.",
            "error_kind": "ambiguous_file",
            "choices": [
                {"document_id": str(d.id), "title": d.title, "relative_path": d.relative_path} for d in matches[:MAX_TITLE_CHOICES]
            ],
        }
    return matches[0], None


def _resolve_spreadsheet_sheet(db, doc: Document, sheet_name: str | None) -> tuple[str | None, dict | None]:
    """Sheet names are resolved against the real DocumentPage.section values
    recorded at ingestion (one per sheet, see app/rag/extract.py's
    _extract_xlsx_blocks) - never against a guess or the first sheet found,
    unless there is genuinely only one to choose from."""
    sheet_names = [
        p.section
        for p in db.query(DocumentPage).filter(DocumentPage.document_id == doc.id).order_by(DocumentPage.order_index).all()
        if p.section
    ]
    if not sheet_names:
        return None, tool_error(NOT_FOUND, f"« {doc.title} » ne contient aucune feuille indexée", error_kind="sheet_not_found")

    resolved, err = resolve_sheet_name(sheet_names, sheet_name, doc.title)
    if err is not None:
        code, message, kind = err
        # The real sheet names, so the agent can offer them as choices.
        return None, tool_error(code, message, error_kind=kind, sheets=sheet_names)
    return resolved, None


def _load_sheet_rows(doc: Document, sheet_title: str) -> tuple[dict | None, dict | None]:
    """Re-reads the ORIGINAL .xlsx from MinIO and parses it directly with
    openpyxl - deliberately NOT the flattened RAG text (DocumentPage.text),
    which stringifies every cell and loses the header/row structure a chart
    needs. This is the one place in the codebase that re-parses uploaded
    spreadsheet bytes at query time (see app/rag/extract.py's docstring,
    which documents that the RAG pipeline never does)."""
    ext = (doc.source_filename or "").rsplit(".", 1)[-1].lower() if doc.source_filename and "." in doc.source_filename else ""
    if ext not in SPREADSHEET_EXTENSIONS:
        return None, tool_error(VALIDATION_ERROR, f"« {doc.title} » n'est pas un classeur Excel (.xlsx)", error_kind="not_spreadsheet")
    if not doc.minio_object_key:
        return None, tool_error(NOT_FOUND, f"le fichier original de « {doc.title} » n'est plus disponible", error_kind="file_unavailable")

    try:
        raw = download_file(doc.minio_object_key)
    except Exception:  # noqa: BLE001 - MinIO down/unreachable, not a data problem
        return None, tool_error(SERVICE_UNAVAILABLE, "le service de fichiers est momentanément indisponible", service_unavailable=True)

    sheet_data, err = read_sheet_rows(raw, sheet_title)
    if err is not None:
        code, message, kind = err
        return None, tool_error(code, message, error_kind=kind)
    return sheet_data, None


def _read_spreadsheet_impl(
    user_id: str, document_id: str | None, document_name: str | None, sheet_name: str | None,
    *,
    workspace_id: str | None = None,
    folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive",
) -> dict:
    """Structured, ACL-scoped, never-approximated read of one sheet of an
    internal .xlsx document - the priority data source for generate_chart
    when a user names a specific file/sheet (see
    app/agent/orchestrator.py::_run_chart_from_spreadsheet). Deliberately
    the only tool that re-parses raw file bytes at query time - see
    _load_sheet_rows. No caching anywhere in this path: every call re-opens
    a fresh DB session and re-downloads/re-parses the file from MinIO, so
    the dataset is always resolved fresh, never reused across turns."""
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        decision = check_tool_access(grant=grant, tool_name="read_spreadsheet_data")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_READ_SPREADSHEET", decision="DENY", reason=decision.reason)
            return tool_error(ACCESS_DENIED, decision.reason, error_kind="policy_denied")

        if not document_id and not document_name:
            return tool_error(VALIDATION_ERROR, "précisez document_id ou document_name", error_kind="file_not_found")

        doc, err = _resolve_spreadsheet_document(db, grant, document_id, document_name)
        if err is not None:
            _audit(
                db, user=user, role=grant.role, action="MCP_READ_SPREADSHEET",
                decision="DENY" if err.get("code") == ACCESS_DENIED else "ALLOW",
                reason=err.get("error", "ambiguous document name"),
                extra={"document_name": document_name},
            )
            return err

        if doc is None:
            _audit(db, user=user, role=grant.role, action="MCP_READ_SPREADSHEET", decision="ALLOW", reason="document not found")
            return tool_error(NOT_FOUND, DOCUMENT_NOT_FOUND_OR_DENIED, concealed=True, error_kind="file_not_found")

        access_decision = check_document_in_scope(grant=grant, document=doc)
        if not access_decision.allowed and access_decision.out_of_context:
            _audit(
                db, user=user, role=grant.role, action="MCP_READ_SPREADSHEET", decision="DENY",
                resource_type="document", resource_id=str(doc.id), reason=access_decision.reason,
            )
            return tool_error(NOT_FOUND, DOCUMENT_NOT_FOUND_OR_DENIED, concealed=True, error_kind="file_not_found")
        if not access_decision.allowed:
            _audit(
                db, user=user, role=grant.role, action="MCP_READ_SPREADSHEET", decision="DENY",
                resource_type="document", resource_id=str(doc.id), reason=access_decision.reason,
            )
            create_alert(
                alert_type="DOCUMENT_ACCESS_DENIED",
                user_id=user.id, username=user.username, role=user.role,
                title=f"Tentative d'accès refusée dans l'assistant IA ({user.full_name})",
                description=(
                    f"Document visé (lecture de tableur) : « {doc.title} » ({doc.department}/{doc.confidentiality})\n"
                    f"Refus : {access_decision.reason}"
                ),
                resource_type="document", resource_id=str(doc.id),
                dedup_key=security_event_key(user.id, "DOCUMENT_ACCESS_DENIED", "read_spreadsheet_data", doc.id),
            )
            return tool_error(
                ACCESS_DENIED, DOCUMENT_NOT_FOUND_OR_DENIED, concealed=True, alert_raised=True, error_kind="file_not_found"
            )

        resolved_sheet, err = _resolve_spreadsheet_sheet(db, doc, sheet_name)
        if err is not None:
            _audit(
                db, user=user, role=grant.role, action="MCP_READ_SPREADSHEET", decision="ALLOW", reason="sheet not resolved",
                resource_type="document", resource_id=str(doc.id), extra={"sheet_name": sheet_name},
            )
            return err

        sheet_data, err = _load_sheet_rows(doc, resolved_sheet)
        if err is not None:
            _audit(
                db, user=user, role=grant.role, action="MCP_READ_SPREADSHEET",
                decision="ALLOW" if err.get("service_unavailable") else "DENY",
                resource_type="document", resource_id=str(doc.id), reason=err.get("error"),
            )
            return err

        _audit(
            db, user=user, role=grant.role, action="MCP_READ_SPREADSHEET", decision="ALLOW", reason="ok",
            resource_type="document", resource_id=str(doc.id),
            extra={"title": doc.title, "sheet": resolved_sheet, "row_count": len(sheet_data["rows"])},
        )
        return {
            "document_id": str(doc.id),
            "title": doc.title,
            "source_filename": doc.source_filename,
            "department": doc.department,
            "confidentiality": doc.confidentiality,
            "sheet": resolved_sheet,
            "columns": sheet_data["headers"],
            "rows": sheet_data["rows"],
            "row_count": len(sheet_data["rows"]),
        }
    finally:
        db.close()


def _generate_chart_impl(
    user_id: str,
    chart_type: str,
    title: str,
    category_field: str | None,
    category_label: str | None,
    value_fields: list[dict],
    data: list[dict],
    sources: list[dict] | None,
    *,
    workspace_id: str | None = None,
    folder_scope: tuple[str, ...] = (), folder_depth: str = "recursive",
) -> dict:
    """Validates and echoes back an already-built chart spec - it never
    fetches data itself. The caller (app/agent/orchestrator.py's
    _run_generate_chart) is responsible for having sourced `data` from an
    ACL-checked tool (search_database) or from data the user typed
    themselves in the current message; this tool's only job is a final
    structural check (app/agent/chart.py::validate_chart_spec) plus an
    audit entry, so every chart that reaches the frontend went through the
    same audited checkpoint as every other tool result."""
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id, workspace_id, folder_scope, folder_depth)
        decision = check_tool_access(grant=grant, tool_name="generate_chart")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_GENERATE_CHART", decision="DENY", reason=decision.reason)
            return tool_error(ACCESS_DENIED, decision.reason)

        try:
            validate_chart_spec(
                chart_type=chart_type, title=title, category_field=category_field, value_fields=value_fields, data=data
            )
        except ChartValidationError as exc:
            _audit(
                db, user=user, role=grant.role, action="MCP_GENERATE_CHART", decision="ALLOW", reason="ok",
                extra={"chart_type": chart_type, "title": title, "validation_error": str(exc)},
            )
            return tool_error(VALIDATION_ERROR, str(exc))

        _audit(
            db, user=user, role=grant.role, action="MCP_GENERATE_CHART", decision="ALLOW", reason="ok",
            extra={"chart_type": chart_type, "title": title, "row_count": len(data)},
        )
        return {
            "chart": {
                "chart_type": chart_type,
                "title": title,
                "category_field": category_field,
                "category_label": category_label,
                "value_fields": value_fields,
                "data": data,
                "sources": sources or [],
            }
        }
    finally:
        db.close()


# --- MCP tool declarations (schemas exposed to the LLM have no identity field) ---


@mcp_app.tool()
async def search_documents(query: str, ctx: Context, top_k: int = 5) -> dict:
    """Search internal company documents for passages relevant to `query`.
    Only returns excerpts from documents the caller is authorized to read."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_search_documents_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id, query, top_k)


@mcp_app.tool()
async def find_document_amounts(ctx: Context) -> dict:
    """Every amount (with a currency) written in a sentence about a budget,
    an amount or a cost, in ALL the pages of the documents the caller may
    read in the current scope - each with its document, path, page and
    sentence. Read-only, no model involved."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_find_document_amounts_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id)


@mcp_app.tool()
async def get_document(document_id: str, ctx: Context) -> dict:
    """Retrieve the full content of one internal document by id, if the caller is authorized to read it."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_get_document_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id, document_id)


@mcp_app.tool()
async def search_document_text(
    query: str, ctx: Context, case_sensitive: bool = False, whole_word: bool = True, count_only: bool = False
) -> dict:
    """EXACT text search in the internal documents the caller may read - for
    "combien de fois X apparaît", "est-ce que X est présent", a name, an
    identifier or an exact phrase (not the semantic search_documents).
    Returns the total number of occurrences and, per document, the
    occurrences with page/section/line and surrounding context.
    case_sensitive: match the exact case; whole_word: whole words only (a
    phrase is matched as a whole); count_only: counts without contexts."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_search_document_text_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id, query, case_sensitive, whole_word, count_only
    )


@mcp_app.tool()
async def search_keyword(keyword: str, ctx: Context, whole_word: bool = True) -> dict:
    """Count exact occurrences of `keyword` across internal documents the
    caller is authorized to read (case/accent-insensitive, Arabic alef and
    tashkeel normalized). Returns a total count and, per document, up to 5
    locations (page/section, line, short excerpt) - the count itself is
    always exact even when locations shown are capped."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_search_keyword_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id, keyword, whole_word)


@mcp_app.tool()
async def list_documents(
    ctx: Context,
    document_type: str | None = None,
    department: str | None = None,
    date_min: str | None = None,
    date_max: str | None = None,
    author: str | None = None,
) -> dict:
    """List internal documents the caller is authorized to see, optionally
    filtered by file type (pdf/xlsx/docx/txt/md), department, creation date
    range (ISO AAAA-MM-JJ), and author. A department outside the caller's
    access returns an empty list, not an error."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_list_documents_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id, document_type, department, date_min, date_max, author
    )


@mcp_app.tool()
async def list_folders(ctx: Context) -> dict:
    """Folders of the current workspace that contain documents the caller may
    read, with document counts (internal - used to resolve a folder named in
    the question; never offered to the model)."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_list_folders_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id
    )


@mcp_app.tool()
async def get_document_section(
    ctx: Context,
    document_id: str | None = None,
    document_name: str | None = None,
    page: int | None = None,
    section: str | None = None,
    offset: int = 0,
) -> dict:
    """Read one page (PDF) or section (heading/"Bloc N") of an internal
    document the caller is authorized to see - returns the exact text, not
    a summary. Identify the document with either `document_id` or
    `document_name` (exact-then-partial title match among authorized
    documents; ambiguous names return `choices` to pick from instead of
    text). Omit both `page` and `section` to open the document from its
    first page/section. Long sections are capped at 2000 characters - pass
    the returned `offset` back to continue reading. A nonexistent document
    and an unauthorized one return the identical error, by design."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_get_document_section_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id, document_id, document_name, page, section, offset
    )


@mcp_app.tool()
async def read_spreadsheet_data(
    ctx: Context,
    document_id: str | None = None,
    document_name: str | None = None,
    sheet_name: str | None = None,
) -> dict:
    """Read the exact structured rows/columns of one sheet of an internal
    Excel (.xlsx) document the caller is authorized to see - returns the
    real column headers and typed cell values read directly from the
    original file, never a summary or an approximation. Identify the
    document with `document_id` or `document_name` (matches the title or
    the original filename, e.g. "Orbitia_Test_Entreprise_Complet.xlsx");
    identify the sheet with `sheet_name` (matches the sheet's real name,
    e.g. "CA mensuel"). Use this - never search_database - whenever the
    user names a specific file or sheet; a nonexistent file/sheet is an
    error, never a silent fallback to another data source."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_read_spreadsheet_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id, document_id, document_name, sheet_name)


@mcp_app.tool()
async def generate_chart(
    ctx: Context,
    chart_type: str,
    title: str,
    value_fields: list[dict],
    data: list[dict],
    category_field: str | None = None,
    category_label: str | None = None,
    sources: list[dict] | None = None,
) -> dict:
    """Validates a chart specification already built from verified data
    (search_database results or data the user typed in this message) and
    returns it for the frontend to render - never fetches or invents data
    itself. `chart_type` is one of line/bar/scatter (category_field
    required) or pie (category_field required, exactly one value_field) or
    scatter (category_field must be omitted, exactly two value_fields: x
    then y). A missing data point must be omitted or null, never 0 or a
    guessed value."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_generate_chart_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id,
        chart_type,
        title,
        category_field,
        category_label,
        value_fields,
        data,
        sources,
    )


@mcp_app.tool()
async def search_database(query: str, ctx: Context) -> dict:
    """Search internal structured financial/company database records (budgets, salary grids, ...).
    Only available to roles with financial data access (e.g. Director, Accountant)."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_search_database_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id, query)


@mcp_app.tool()
async def get_company_information(topic: str, ctx: Context) -> dict:
    """Get general company information available to all employees (policies, onboarding, general org info)."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return _identity_error(exc)
    return await anyio.to_thread.run_sync(
        partial(_get_company_information_impl, workspace_id=identity.workspace_id, folder_scope=identity.folder_scope, folder_depth=identity.folder_depth), identity.user_id, topic)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
    mcp_app.run(transport="streamable-http")
