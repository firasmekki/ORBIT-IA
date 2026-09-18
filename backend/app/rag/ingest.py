from sqlalchemy.orm import Session

from app.models.document import Document, DocumentChunk
from app.rag.embeddings import embed_text

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


def ingest_document(db: Session, document: Document) -> int:
    """Chunk, embed and (re)index a document. Returns the number of chunks written."""
    db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).delete()

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
    db.commit()
    return len(chunks)
