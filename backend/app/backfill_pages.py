"""One-off backfill: populate app.models.document.DocumentPage for documents
that existed before search_keyword/get_document_section were added (their
DocumentChunk rows already exist, but document_pages was empty for them).

Run with `python -m app.backfill_pages`. Idempotent - re-ingesting a
document that already has document_pages rows just replaces them with the
same content (see ingest_document's delete-then-insert), so this is safe to
re-run.

For documents uploaded as a real file (minio_object_key set), the original
bytes are re-downloaded and re-extracted through app.rag.extract so the
page/section breakdown matches what a fresh upload would produce today.
For hand-typed documents (DocumentCreate, no source file) or if MinIO can't
be reached for a given file, ingest_document's own fallback (a single
synthetic page covering the whole content) is used instead - imperfect
addressing (no real page/section boundaries) but still fully
keyword-searchable, which is the actual goal here.
"""

import sys

from app.core.database import SessionLocal, engine
from app.core.migrations import bootstrap_schema
from app.core.storage import download_file
from app.models.document import Document, DocumentPage
from app.rag.extract import ExtractionError, UnsupportedFileTypeError, extract
from app.rag.ingest import ingest_document


def run() -> None:
    with engine.begin() as conn:
        bootstrap_schema(conn)

    db = SessionLocal()
    try:
        docs = db.query(Document).all()
        print(f"{len(docs)} document(s) to backfill...")
        for doc in docs:
            already = db.query(DocumentPage).filter(DocumentPage.document_id == doc.id).first()
            if already is not None:
                print(f"  skip (already has pages): {doc.title}")
                continue

            pages = None
            if doc.source_filename and doc.minio_object_key:
                try:
                    raw = download_file(doc.minio_object_key)
                    pages = extract(doc.source_filename, raw).pages
                except (UnsupportedFileTypeError, ExtractionError) as exc:
                    print(f"  warning: could not re-extract {doc.title} ({exc}), using fallback single page")
                except Exception as exc:  # noqa: BLE001 - MinIO down, etc.
                    print(f"  warning: could not fetch {doc.title} from storage ({exc}), using fallback single page")

            ingest_document(db, doc, pages=pages)
            print(f"  backfilled: {doc.title} ({len(pages) if pages else 1} page(s))")
    finally:
        db.close()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:  # noqa: BLE001
        print(f"backfill failed: {exc}", file=sys.stderr)
        sys.exit(1)
