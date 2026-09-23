from sqlalchemy.orm import Session

from app.models.document import Document, DocumentChunk, DocumentPage
from app.rag.embeddings import embed_text
from app.rag.extract import ExtractedPage

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
        if len(candidate) <= chunk_size:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
        if len(paragraph) <= chunk_size:
            buffer = paragraph
        else:
            # a single paragraph longer than chunk_size: hard-split with overlap
            start = 0
            while start < len(paragraph):
                end = start + chunk_size
                chunks.append(paragraph[start:end])
                start = end - overlap
            buffer = ""
    if buffer:
        chunks.append(buffer)
    return chunks


def ingest_document(db: Session, document: Document, pages: list[ExtractedPage] | None = None) -> int:
    """Chunk, embed and (re)index a document. Returns the number of chunks written.

    Also (re)writes app.models.document.DocumentPage rows used by
    search_keyword / get_document_section. `pages` comes from
    app.rag.extract for real file uploads; documents created by hand
    (DocumentCreate, no source file) pass None and get a single synthetic
    page covering the whole content, so every document is always
    keyword-searchable regardless of how it was created.
    """
    db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).delete()
    db.query(DocumentPage).filter(DocumentPage.document_id == document.id).delete()

    chunks = chunk_text(document.content)
    for index, chunk in enumerate(chunks):
        embedding = embed_text(chunk)
        db.add(
            DocumentChunk(
                document_id=document.id,
                chunk_index=index,
                chunk_text=chunk,
                embedding=embedding,
            )
        )

    effective_pages = pages if pages else [ExtractedPage(page_no=None, section=None, line_offset=1, text=document.content)]
    for index, page in enumerate(effective_pages):
        db.add(
            DocumentPage(
                document_id=document.id,
                page_no=page.page_no,
                section=page.section,
                order_index=index,
                line_offset=page.line_offset,
                text=page.text,
            )
        )

    db.commit()
    return len(chunks)
