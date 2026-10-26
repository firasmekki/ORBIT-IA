"""Permission-aware retrieval.

The department/confidentiality filter is applied inside the SQL query that
performs the vector similarity search - a chunk the caller's role is not
allowed to see is never fetched from the database in the first place, let
alone handed to the LLM. There is nothing to "ask the model to ignore".

`retrieve_with_out_of_scope_signal` additionally runs a second, inverted
query to detect the common "silent filtering" case: an employee asks about
something that exists but sits outside their grant (e.g. RH asking about
individual salaries). The in-scope search alone would just come back empty
or irrelevant - which looks identical to "nothing exists about that topic"
and makes for a very unsatisfying, unauditable product experience. The
out-of-scope query returns only metadata (department/confidentiality),
never content or title, and only when it is a strictly closer match than
anything the caller could legitimately see - just enough to (a) let the
assistant say "this looks like it's outside your access" instead of a bare
non-answer, and (b) let the caller raise a Director-facing alert.
"""

import logging
import math
import re

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentChunk, DocumentPage
from app.policy.engine import document_scope, outside_document_scope, retrieval_scope
from app.policy.rules import Grant
from app.rag.embeddings import embed_text
from app.rag.normalize import normalize

logger = logging.getLogger("orbitia.search")

# --- hybrid (lexical + vector) ranking ------------------------------------------
#
# Embeddings carry almost no signal for a rare proper noun ("Tarek") or an
# identifier: a chunk that literally contains the term can rank below
# unrelated text. So the in-scope candidates are ALSO fetched lexically
# (whole word, case/accent-insensitive, same department/confidentiality
# filter in the same SQL) and every candidate is scored on both:
#   lexical = sum of the matched terms' IDF / sum of all query terms' IDF
#   final   = blend of lexical and vector similarity - lexical-heavy for a
#             short query (a name), vector-heavy for a long question.
# When no in-scope chunk contains any query term, the ranking is exactly the
# previous pure vector one.

_STOPWORDS = set(
    """
    les des une un le la du de d l au aux et ou en dans sur pour par avec sans est sont etre a ont qui que quoi
    quel quelle quels quelles ce cet cette ces son sa ses leur leurs mon ma mes ton ta tes nos vos notre votre
    il elle ils elles on nous vous je tu me te se y ne pas plus tres bien donc mais comme si quand comment
    pourquoi combien ou peux pouvez fait faire donne donner montre moi trouve cherche recherche entreprise
    the of and to in for on with is are what who
    """.split()
)
MAX_LEXICAL_TERMS = 6
MAX_LEXICAL_CANDIDATES = 60


def lexical_terms(query: str) -> list[str]:
    """Normalized (case/accent-folded) content words of a query, in order."""
    terms: list[str] = []
    for word in re.findall(r"[a-z0-9]+", normalize(query)):
        if len(word) >= 3 and word not in _STOPWORDS and word not in terms:
            terms.append(word)
    return terms[:MAX_LEXICAL_TERMS]


def _word_regex(term: str) -> str:
    # PostgreSQL ARE word boundaries; `term` is [a-z0-9]+ (lexical_terms), never needs escaping.
    return rf"\m{term}\M"


def _normalized_chunk_text():
    return func.unaccent(func.lower(DocumentChunk.chunk_text))


def retrieve_with_out_of_scope_signal(
    db: Session, *, grant: Grant | None, query: str, top_k: int = 5
) -> tuple[list[dict], dict | None]:
    departments, _ = retrieval_scope(grant=grant)
    query_embedding = embed_text(query)
    distance_expr = DocumentChunk.embedding.cosine_distance(query_embedding)

    in_scope_results: list[dict] = []
    best_in_scope_distance: float | None = None
    if departments:
        # Workspace + active + department + confidentiality, inside the SQL of
        # BOTH the vector and the lexical query: a chunk outside the grant
        # is never fetched, so it can never reach the LLM.
        in_scope = document_scope(grant)

        def candidates(extra_filter=None, limit: int = top_k):
            query = (
                db.query(DocumentChunk, Document, distance_expr.label("distance"), DocumentPage)
                .join(Document, Document.id == DocumentChunk.document_id)
                .outerjoin(DocumentPage, DocumentPage.id == DocumentChunk.document_page_id)
                .filter(*in_scope)
            )
            if extra_filter is not None:
                query = query.filter(extra_filter)
            return query.order_by(distance_expr).limit(limit).all()

        vector_rows = candidates(limit=top_k * 3)
        terms = lexical_terms(query)
        lexical_rows = []
        idf: dict[str, float] = {}
        if terms:
            normalized_text = _normalized_chunk_text()
            lexical_rows = candidates(
                or_(*[normalized_text.op("~")(_word_regex(t)) for t in terms]), limit=MAX_LEXICAL_CANDIDATES
            )
            if lexical_rows:
                # Rarity weighting, over the caller's own authorized chunks:
                # "Tarek" (1 chunk) outweighs "entreprise" (every chunk).
                total = db.query(func.count(DocumentChunk.id)).join(Document).filter(*in_scope).scalar() or 1
                for t in terms:
                    df = (
                        db.query(func.count(DocumentChunk.id))
                        .join(Document)
                        .filter(*in_scope, normalized_text.op("~")(_word_regex(t)))
                        .scalar()
                    )
                    idf[t] = math.log((total + 1) / (df + 1)) + 1.0

        by_chunk = {row[0].id: row for row in [*vector_rows, *lexical_rows]}
        idf_total = sum(idf.values()) or 1.0
        lexical_weight = (0.7 if len(terms) <= 2 else 0.4) if lexical_rows else 0.0
        scored = []
        for chunk, doc, distance, page in by_chunk.values():
            words = set(re.findall(r"[a-z0-9]+", normalize(chunk.chunk_text)))
            matched = [t for t in terms if t in words]
            lexical = sum(idf.get(t, 0.0) for t in matched) / idf_total if idf else 0.0
            # pgvector's cosine_distance is exactly 1 - cosine_similarity for
            # the `<=>` operator: 1.0 = identical direction, 0.0 = orthogonal.
            similarity = 1 - float(distance)
            final = lexical_weight * lexical + (1 - lexical_weight) * similarity
            scored.append((final, matched, similarity, chunk, doc, distance, page))
        scored.sort(key=lambda item: item[0], reverse=True)

        in_scope_results = [
            {
                "document_id": doc.id,
                "title": doc.title,
                "relative_path": doc.relative_path,
                "department": doc.department,
                "confidentiality": doc.confidentiality,
                "chunk_text": chunk.chunk_text,
                # Higher is more relevant. Pure cosine similarity when no
                # query term occurs in any authorized chunk (see above).
                "score": round(final, 3),
                "match": ("lexical+vector" if matched else "vector") if lexical_weight else "vector",
                "matched_terms": matched,
                "page": page.page_no if page is not None else None,
                "section": page.section if page is not None else None,
            }
            for final, matched, similarity, chunk, doc, distance, page in scored[:top_k]
        ]
        if vector_rows:
            best_in_scope_distance = float(vector_rows[0][2])
        logger.info(
            "SEARCH tool=search_documents role=%s lexical_terms=%s lexical_results=%d vector_results=%d "
            "authorized_results=%d",
            grant.role if grant else None, terms, len(lexical_rows), len(vector_rows), len(in_scope_results),
        )

    out_of_scope_row = (
        db.query(Document, distance_expr.label("distance"))
        .join(DocumentChunk, DocumentChunk.document_id == Document.id)
        .filter(*outside_document_scope(grant))
        .order_by(distance_expr)
        .limit(1)
        .first()
    )

    out_of_scope_hint: dict | None = None
    if out_of_scope_row is not None:
        doc, distance = out_of_scope_row
        distance = float(distance)
        # Only flag it when it's a materially closer match than what the
        # caller was legitimately shown - otherwise routine unrelated
        # documents would trip this on every query.
        if best_in_scope_distance is None or distance < best_in_scope_distance:
            out_of_scope_hint = {"department": doc.department, "confidentiality": doc.confidentiality}

    return in_scope_results, out_of_scope_hint
