"""Exact text search over the company documents a user may read - the
engine behind the `search_document_text` MCP tool (and the legacy
`search_keyword`). Deliberately NOT the vector RAG: "how many times does
Firas appear" needs a real match count, which embeddings cannot give.

Security model (same as every retrieval path):
  internal JWT -> User -> effective Grant -> retrieval_scope()
  -> SQL restricted to the authorized documents -> match -> count.
A document outside the grant is never read for the user's result. It is
probed separately, only when the user's own result is empty, and only to
raise a Director-facing alert: nothing about it (existence, count,
department, title, content) ever goes back to the caller.
"""

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.alert_service import create_alert, security_event_key
from app.core.audit_logger import log_event
from app.models.document import Document, DocumentPage
from app.models.user import User
from app.policy.engine import document_scope, outside_document_scope, retrieval_scope
from app.policy.rules import Grant
from app.rag.normalize import normalize
from app.rag.search_keyword import (
    KeywordMatch,
    build_exact_pattern,
    build_pattern,
    find_matches,
    find_raw_matches,
    is_arabic_keyword,
)
from app.services.errors import invalid

logger = logging.getLogger("orbitia.search")

MAX_QUERY_LENGTH = 200
# Occurrences listed per document (the COUNT always covers them all): enough
# for the results panel to show every line of a typical document.
DEFAULT_MATCHES_PER_DOCUMENT = 50
OUT_OF_SCOPE_PROBE_LIMIT = 50


def search_document_text(
    db: Session,
    user: User,
    grant: Grant,
    query: str,
    *,
    case_sensitive: bool = False,
    whole_word: bool = True,
    count_only: bool = False,
    max_matches_per_document: int = DEFAULT_MATCHES_PER_DOCUMENT,
    audit_action: str = "DOCUMENT_TEXT_SEARCH",
) -> tuple[dict, dict | None]:
    """(result, out_of_scope_hint). The hint is for the caller's alerting
    and audit only - never to be returned to the user or the model."""
    query = (query or "").strip()
    if not query or len(query) > MAX_QUERY_LENGTH:
        raise invalid("terme de recherche vide ou trop long")

    if case_sensitive:
        pattern = build_exact_pattern(query, whole_word=whole_word)
        matcher = find_raw_matches
    else:
        pattern = build_pattern(query, whole_word)
        matcher = find_matches
    if pattern is None:
        raise invalid("terme de recherche vide ou invalide")

    departments, _ = retrieval_scope(grant=grant)
    normalized_query = normalize(query)
    # SQL pre-filter narrows candidates; the regex above stays the only
    # source of truth for the count. Arabic skips it (unaccent does not
    # fold Arabic variants - see is_arabic_keyword).
    use_prefilter = not is_arabic_keyword(query)

    def prefilter(sql_query, column):
        if not use_prefilter:
            return sql_query
        if case_sensitive:
            return sql_query.filter(column.like(f"%{_escape_like(query)}%", escape="\\"))
        return sql_query.filter(func.unaccent(func.lower(column)).ilike(f"%{_escape_like(normalized_query)}%", escape="\\"))

    # Documents indexed before page tracking existed have no document_pages
    # rows: they are searched on their full text, under the same scope.
    paged_ids = db.query(DocumentPage.document_id).distinct()
    documents: dict[str, dict] = {}

    def collect(doc: Document, matches: list[KeywordMatch], page_no, section) -> None:
        entry = documents.setdefault(
            str(doc.id),
            {
                "document_id": str(doc.id),
                "title": doc.title,
                "relative_path": doc.relative_path,
                "department": doc.department,
                "confidentiality": doc.confidentiality,
                "occurrences": 0,
                "matches": [],
            },
        )
        entry["occurrences"] += len(matches)
        if not count_only:
            for match in matches:
                if len(entry["matches"]) < max_matches_per_document:
                    entry["matches"].append(
                        {"page": page_no, "section": section, "line": match.line, "context": match.excerpt}
                    )

    if departments:
        in_scope = document_scope(grant)  # workspace + active + RBAC/ABAC, in SQL
        pages = db.query(DocumentPage, Document).join(Document, Document.id == DocumentPage.document_id).filter(*in_scope)
        for page, doc in prefilter(pages, DocumentPage.text).all():
            matches = matcher(page.text, page.line_offset, pattern)
            if matches:
                collect(doc, matches, page.page_no, page.section)
        unpaged = db.query(Document).filter(*in_scope, ~Document.id.in_(paged_ids))
        for doc in prefilter(unpaged, Document.content).all():
            matches = matcher(doc.content, 1, pattern)
            if matches:
                collect(doc, matches, None, None)

    total = sum(d["occurrences"] for d in documents.values())
    out_of_scope_hint = None
    if total == 0:
        out_of_scope_hint = _probe_out_of_scope(db, grant, paged_ids, prefilter, matcher, pattern)

    ranked = sorted(documents.values(), key=lambda d: d["occurrences"], reverse=True)
    result = {
        "query": query,
        "case_sensitive": case_sensitive,
        "whole_word": whole_word,
        "total_occurrences": total,
        "document_count": len(ranked),
        "documents": ranked,
    }

    log_event(
        user_id=user.id,
        username=user.username,
        role=grant.role,
        action=audit_action,
        decision="ALLOW",
        reason="ok",
        extra={
            "query": query,
            "case_sensitive": case_sensitive,
            "whole_word": whole_word,
            "count_only": count_only,
            "total_occurrences": total,
            "document_count": len(ranked),
            "out_of_scope_hint": out_of_scope_hint,
        },
    )
    logger.info(
        "SEARCH tool=search_document_text user_id=%s role=%s term=%r case_sensitive=%s whole_word=%s "
        "authorized_documents=%d authorized_matches=%d out_of_scope_match=%s",
        user.id, grant.role, query, case_sensitive, whole_word, len(ranked), total, bool(out_of_scope_hint),
    )
    if out_of_scope_hint:
        _alert_out_of_scope(user, grant, query, out_of_scope_hint)
    return result, out_of_scope_hint


def _probe_out_of_scope(db, grant, paged_ids, prefilter, matcher, pattern) -> dict | None:
    # Same workspace only: another workspace's content is not this
    # caller's concern, not even as an alert.
    outside = outside_document_scope(grant)
    pages = db.query(DocumentPage, Document).join(Document, Document.id == DocumentPage.document_id).filter(*outside)
    for page, doc in prefilter(pages, DocumentPage.text).limit(OUT_OF_SCOPE_PROBE_LIMIT).all():
        if matcher(page.text, page.line_offset, pattern):
            return {"department": doc.department, "confidentiality": doc.confidentiality}
    unpaged = db.query(Document).filter(*outside, ~Document.id.in_(paged_ids))
    for doc in prefilter(unpaged, Document.content).limit(OUT_OF_SCOPE_PROBE_LIMIT).all():
        if matcher(doc.content, 1, pattern):
            return {"department": doc.department, "confidentiality": doc.confidentiality}
    return None


def _alert_out_of_scope(user: User, grant: Grant, query: str, hint: dict) -> None:
    """The term exists only where this user may not read. Nothing of it
    reaches the user; the Director is told (once per term, deduplicated)."""
    create_alert(
        alert_type="CHAT_ACCESS_DENIED",
        user_id=user.id,
        username=user.username,
        role=user.role,
        title=f"Recherche hors périmètre dans l'assistant IA ({user.full_name})",
        description=(
            f"Terme recherché : « {query} »\n"
            f"Présent uniquement dans des documents hors du périmètre du rôle {grant.role} "
            f"({hint['department']}/{hint['confidentiality']}). "
            "Aucun contenu n'a été transmis à l'utilisateur ni au modèle."
        ),
        resource_type="search",
        dedup_key=security_event_key(
            user.id, "KEYWORD_OUT_OF_SCOPE", normalize(query), hint["department"], hint["confidentiality"]
        ),
    )


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

