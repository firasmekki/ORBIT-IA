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

import re
import uuid

import anyio
from mcp.server.fastmcp import Context, FastMCP
from sqlalchemy import func, or_, true

from app.core.audit_logger import log_event
from app.core.database import SessionLocal
from app.mcp.context import IdentityError, extract_identity
from app.models.document import Document, DocumentPage
from app.models.finance import FinancialRecord
from app.models.user import User
from app.policy.engine import check_document_access, check_tool_access, retrieval_scope
from app.policy.rules import Grant, resolve_effective_grant
from app.rag.embeddings import EmbeddingServiceError
from app.rag.normalize import normalize
from app.rag.retriever import retrieve_with_out_of_scope_signal
from app.rag.search_keyword import build_pattern, find_matches, is_arabic_keyword

MAX_LOCATIONS_PER_DOCUMENT = 5

mcp_app = FastMCP(
    name="orbitia-mcp",
    instructions=(
        "Internal Orbitia company tools: search_documents, get_document, "
        "search_database, get_company_information. Every call is scoped to "
        "the authenticated caller resolved server-side; there is no user_id "
        "or identity argument on any tool."
    ),
    host="0.0.0.0",
    port=8090,
)


def _load_user_and_grant(db, user_id: str) -> tuple[User | None, Grant | None]:
    try:
        user = db.get(User, uuid.UUID(user_id))
    except ValueError:
        return None, None
    if user is None or not user.is_active:
        return None, None
    return user, resolve_effective_grant(user)


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


def _search_documents_impl(user_id: str, query: str, top_k: int) -> dict:
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id)
        decision = check_tool_access(grant=grant, tool_name="search_documents")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_SEARCH_DOCUMENTS", decision="DENY", reason=decision.reason)
            return {"error": decision.reason}

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
            return {"error": str(exc), "service_unavailable": True}

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
                    "department": r["department"],
                    "confidentiality": r["confidentiality"],
                    "excerpt": r["chunk_text"][:600],
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


def _get_document_impl(user_id: str, document_id: str) -> dict:
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id)
        tool_decision = check_tool_access(grant=grant, tool_name="get_document")
        if not tool_decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_GET_DOCUMENT", decision="DENY", reason=tool_decision.reason)
            return {"error": tool_decision.reason}

        try:
            doc_uuid = uuid.UUID(document_id)
        except ValueError:
            return {"error": "identifiant de document invalide"}

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
            return {"error": "document introuvable"}

        decision = check_document_access(grant=grant, department=doc.department, confidentiality=doc.confidentiality)
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
            return {"error": f"Accès refusé : {decision.reason}"}

        return {
            "document_id": str(doc.id),
            "title": doc.title,
            "department": doc.department,
            "confidentiality": doc.confidentiality,
            "content": doc.content,
        }
    finally:
        db.close()


def _search_keyword_impl(user_id: str, keyword: str, whole_word: bool) -> dict:
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id)
        decision = check_tool_access(grant=grant, tool_name="search_keyword")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_SEARCH_KEYWORD", decision="DENY", reason=decision.reason)
            return {"error": decision.reason}

        pattern = build_pattern(keyword, whole_word)
        if pattern is None:
            return {"error": "mot-clé vide ou invalide"}

        departments, max_rank = retrieval_scope(grant=grant)
        normalized_kw = normalize(keyword)
        # unaccent() only folds Latin diacritics (é/è/ç/...), not Arabic
        # alef variants/tashkeel - an Arabic keyword skips this SQL
        # pre-filter entirely and scans all authorized pages directly in
        # Python instead, so it can never silently drop a real match.
        use_sql_prefilter = not is_arabic_keyword(keyword)

        def _prefiltered(query):
            if use_sql_prefilter and normalized_kw:
                return query.filter(func.unaccent(func.lower(DocumentPage.text)).ilike(f"%{normalized_kw}%"))
            return query

        try:
            documents: dict[str, dict] = {}
            total_count = 0
            if departments:
                in_scope_query = (
                    db.query(DocumentPage, Document)
                    .join(Document, Document.id == DocumentPage.document_id)
                    .filter(Document.department.in_(departments), Document.confidentiality_rank <= max_rank)
                )
                for page, doc in _prefiltered(in_scope_query).all():
                    matches = find_matches(page.text, page.line_offset, pattern)
                    if not matches:
                        continue
                    total_count += len(matches)
                    entry = documents.setdefault(
                        str(doc.id),
                        {
                            "document_id": str(doc.id),
                            "title": doc.title,
                            "department": doc.department,
                            "confidentiality": doc.confidentiality,
                            "count": 0,
                            "locations": [],
                        },
                    )
                    entry["count"] += len(matches)
                    for match in matches:
                        if len(entry["locations"]) < MAX_LOCATIONS_PER_DOCUMENT:
                            entry["locations"].append(
                                {"page": page.page_no, "section": page.section, "line": match.line, "excerpt": match.excerpt}
                            )

            # Same silent-filtering-detection pattern as
            # retrieve_with_out_of_scope_signal: only probed when the
            # caller's own authorized results came back empty, and only a
            # metadata signal (department/confidentiality) is ever returned
            # - never which document, its title, or its content.
            out_of_scope_hint = None
            if total_count == 0:
                out_of_scope_condition = (
                    or_(~Document.department.in_(departments), Document.confidentiality_rank > max_rank)
                    if departments
                    else true()
                )
                out_of_scope_query = (
                    db.query(DocumentPage, Document)
                    .join(Document, Document.id == DocumentPage.document_id)
                    .filter(out_of_scope_condition)
                )
                for page, doc in _prefiltered(out_of_scope_query).limit(50).all():
                    if find_matches(page.text, page.line_offset, pattern):
                        out_of_scope_hint = {"department": doc.department, "confidentiality": doc.confidentiality}
                        break
        except Exception as exc:  # noqa: BLE001
            # A query/code failure here is an infrastructure problem, not a
            # permission decision - must never be phrased as (or alerted
            # like) an access refusal. routers/chat.py specifically excludes
            # any tool_trace reason starting with "service unavailable"
            # from raising a Director alert (see EmbeddingServiceError
            # handling in _search_documents_impl for the same convention).
            _audit(
                db,
                user=user,
                role=grant.role,
                action="MCP_SEARCH_KEYWORD",
                decision="DENY",
                reason=f"service unavailable: {exc}",
            )
            return {"error": str(exc), "service_unavailable": True}

        _audit(
            db,
            user=user,
            role=grant.role,
            action="MCP_SEARCH_KEYWORD",
            decision="ALLOW",
            reason="ok",
            extra={
                "keyword": keyword,
                "whole_word": whole_word,
                "total_count": total_count,
                "out_of_scope_hint": out_of_scope_hint,
            },
        )

        response: dict = {
            "keyword": keyword,
            "whole_word": whole_word,
            "total_count": total_count,
            "documents": sorted(documents.values(), key=lambda d: d["count"], reverse=True),
        }
        if out_of_scope_hint:
            response["restricted_match"] = out_of_scope_hint
        return response
    finally:
        db.close()


_STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "de", "du", "quel", "quelle", "quels", "quelles",
    "est", "sont", "pour", "avec", "dans", "sur", "moi", "donne", "montre", "quelle", "que",
    "qui", "combien", "budget", "svp", "please", "the", "what", "is", "for", "me", "show",
}


def _search_database_impl(user_id: str, query: str) -> dict:
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id)
        decision = check_tool_access(grant=grant, tool_name="search_database")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_SEARCH_DATABASE", decision="DENY", reason=decision.reason)
            return {"error": decision.reason}

        departments, max_rank = retrieval_scope(grant=grant)
        base_query = db.query(FinancialRecord).filter(
            FinancialRecord.department.in_(departments),
            FinancialRecord.confidentiality_rank <= max_rank,
        )

        # Free-text queries rarely match a record label as an exact
        # substring ("quel est le budget IT 2025" vs. "Budget IT /
        # Infrastructure 2025"), so match on any significant keyword
        # instead of the whole phrase.
        tokens = [w for w in re.findall(r"[a-zA-ZÀ-ÿ]{2,}", query.lower()) if w not in _STOPWORDS]
        if tokens:
            base_query = base_query.filter(or_(*[FinancialRecord.label.ilike(f"%{t}%") for t in tokens]))

        rows = base_query.limit(20).all()
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


def _get_company_information_impl(user_id: str, topic: str) -> dict:
    db = SessionLocal()
    try:
        user, grant = _load_user_and_grant(db, user_id)
        decision = check_tool_access(grant=grant, tool_name="get_company_information")
        if not decision.allowed:
            _audit(db, user=user, role=grant.role if grant else None, action="MCP_COMPANY_INFO", decision="DENY", reason=decision.reason)
            return {"error": decision.reason}

        _, max_rank = retrieval_scope(grant=grant)
        rows = (
            db.query(Document)
            .filter(
                Document.department == "GENERAL",
                Document.confidentiality_rank <= max_rank,
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


# --- MCP tool declarations (schemas exposed to the LLM have no identity field) ---


@mcp_app.tool()
async def search_documents(query: str, ctx: Context, top_k: int = 5) -> dict:
    """Search internal company documents for passages relevant to `query`.
    Only returns excerpts from documents the caller is authorized to read."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return {"error": str(exc)}
    return await anyio.to_thread.run_sync(_search_documents_impl, identity.user_id, query, top_k)


@mcp_app.tool()
async def get_document(document_id: str, ctx: Context) -> dict:
    """Retrieve the full content of one internal document by id, if the caller is authorized to read it."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return {"error": str(exc)}
    return await anyio.to_thread.run_sync(_get_document_impl, identity.user_id, document_id)


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
        return {"error": str(exc)}
    return await anyio.to_thread.run_sync(_search_keyword_impl, identity.user_id, keyword, whole_word)


@mcp_app.tool()
async def search_database(query: str, ctx: Context) -> dict:
    """Search internal structured financial/company database records (budgets, salary grids, ...).
    Only available to roles with financial data access (e.g. Director, Accountant)."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return {"error": str(exc)}
    return await anyio.to_thread.run_sync(_search_database_impl, identity.user_id, query)


@mcp_app.tool()
async def get_company_information(topic: str, ctx: Context) -> dict:
    """Get general company information available to all employees (policies, onboarding, general org info)."""
    try:
        identity = extract_identity(ctx)
    except IdentityError as exc:
        return {"error": str(exc)}
    return await anyio.to_thread.run_sync(_get_company_information_impl, identity.user_id, topic)


if __name__ == "__main__":
    mcp_app.run(transport="streamable-http")
