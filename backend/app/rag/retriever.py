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

from sqlalchemy import or_, true
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentChunk
from app.policy.engine import retrieval_scope
from app.policy.rules import Grant
from app.rag.embeddings import embed_text


def retrieve_with_out_of_scope_signal(
    db: Session, *, grant: Grant | None, query: str, top_k: int = 5
) -> tuple[list[dict], dict | None]:
    departments, max_rank = retrieval_scope(grant=grant)
    query_embedding = embed_text(query)
    distance_expr = DocumentChunk.embedding.cosine_distance(query_embedding)

    in_scope_results: list[dict] = []
    best_in_scope_distance: float | None = None
    if departments:
        rows = (
            db.query(DocumentChunk, Document, distance_expr.label("distance"))
            .join(Document, Document.id == DocumentChunk.document_id)
            .filter(Document.department.in_(departments), Document.confidentiality_rank <= max_rank)
            .order_by(distance_expr)
            .limit(top_k)
            .all()
        )
        in_scope_results = [
            {
                "document_id": doc.id,
                "title": doc.title,
                "department": doc.department,
                "confidentiality": doc.confidentiality,
                "chunk_text": chunk.chunk_text,
            }
            for chunk, doc, _distance in rows
        ]
        if rows:
            best_in_scope_distance = float(rows[0][2])

    out_of_scope_condition = (
        or_(~Document.department.in_(departments), Document.confidentiality_rank > max_rank)
        if departments
        else true()
    )
    out_of_scope_row = (
        db.query(Document, distance_expr.label("distance"))
        .join(DocumentChunk, DocumentChunk.document_id == Document.id)
        .filter(out_of_scope_condition)
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
