from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.document import Document, DocumentChunk, DocumentPage
from app.rag.embeddings import embed_text
from app.rag.extract import ExtractedPage

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120


@dataclass(frozen=True)
class TextChunk:
    text: str
    # Character offsets within the text passed to chunk_text() - (-1, -1)
    # if the position couldn't be verified (see the self-check at the
    # bottom of chunk_text). Never approximate: either exact, or unknown.
    start: int
    end: int

    @property
    def has_position(self) -> bool:
        return self.start >= 0


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[TextChunk]:
    text = text.strip()
    if not text:
        return []

    # Track each raw paragraph's true start offset within `text` (before
    # stripping) as we split - str.split("\n\n") itself doesn't preserve
    # position, so this walks the same split points by hand. Needed to
    # compute each chunk's exact character span, which
    # app.rag.ingest.ingest_document uses to link a chunk to the
    # DocumentPage it falls within (search_documents' page/section fields).
    raw_paragraphs: list[tuple[int, str]] = []
    cursor = 0
    for part in text.split("\n\n"):
        stripped = part.strip()
        if stripped:
            lead = len(part) - len(part.lstrip())
            raw_paragraphs.append((cursor + lead, stripped))
        cursor += len(part) + 2  # +2 for the "\n\n" delimiter consumed by split

    chunks: list[TextChunk] = []
    buffer = ""
    buffer_start = 0
    for para_start, paragraph in raw_paragraphs:
        candidate = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
        if len(candidate) <= chunk_size:
            if not buffer:
                buffer_start = para_start
            buffer = candidate
            continue
        if buffer:
            chunks.append(TextChunk(text=buffer, start=buffer_start, end=buffer_start + len(buffer)))
        if len(paragraph) <= chunk_size:
            buffer = paragraph
            buffer_start = para_start
        else:
            # a single paragraph longer than chunk_size: hard-split with overlap
            start = 0
            while start < len(paragraph):
                end = start + chunk_size
                piece = paragraph[start:end]
                chunks.append(TextChunk(text=piece, start=para_start + start, end=para_start + start + len(piece)))
                start = end - overlap
            buffer = ""
    if buffer:
        chunks.append(TextChunk(text=buffer, start=buffer_start, end=buffer_start + len(buffer)))

    # Self-verify every computed span before trusting it: if the offset
    # math and the actual chunk text ever disagree (an edge case in
    # whitespace normalization, for instance), fall back to "position
    # unknown" for that one chunk rather than risk linking it to the wrong
    # page - the embedding/search behavior is completely unaffected either
    # way, only the page/section field on that result degrades to null.
    verified: list[TextChunk] = []
    for c in chunks:
        if 0 <= c.start <= c.end <= len(text) and text[c.start : c.end] == c.text:
            verified.append(c)
        else:
            verified.append(TextChunk(text=c.text, start=-1, end=-1))
    return verified


def _page_ranges(pages: list[ExtractedPage]) -> list[tuple[int, int]]:
    """Character range each page occupies within the flattened text - exact
    by construction, since app.rag.extract._assemble() builds that flattened
    text as exactly "\\n\\n".join(page.text for page in pages)."""
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for page in pages:
        start = cursor
        end = start + len(page.text)
        ranges.append((start, end))
        cursor = end + 2
    return ranges


def _page_id_for_chunk(chunk: TextChunk, page_ranges: list[tuple[int, int]], page_rows: list[DocumentPage]):
    """A chunk that straddles two pages is attributed to the page containing
    its FIRST character - a definite, documented rule (not an average or a
    guess). Returns None if the chunk's position is unknown."""
    if not chunk.has_position:
        return None
    for (start, end), row in zip(page_ranges, page_rows):
        if start <= chunk.start < end:
            return row.id
    return None


def ingest_document(db: Session, document: Document, pages: list[ExtractedPage] | None = None) -> int:
    """Chunk, embed and (re)index a document. Returns the number of chunks written.

    Also (re)writes app.models.document.DocumentPage rows used by
    search_keyword / get_document_section, and links each DocumentChunk to
    the page/section it falls within (search_documents' page/section
    fields) via DocumentChunk.document_page_id. `pages` comes from
    app.rag.extract for real file uploads; documents created by hand
    (DocumentCreate, no source file) pass None and get a single synthetic
    page covering the whole content, so every document is always
    keyword-searchable regardless of how it was created.
    """
    db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).delete()
    db.query(DocumentPage).filter(DocumentPage.document_id == document.id).delete()

    effective_pages = pages if pages else [ExtractedPage(page_no=None, section=None, line_offset=1, text=document.content)]
    page_rows: list[DocumentPage] = []
    for index, page in enumerate(effective_pages):
        row = DocumentPage(
            document_id=document.id,
            page_no=page.page_no,
            section=page.section,
            order_index=index,
            line_offset=page.line_offset,
            text=page.text,
        )
        db.add(row)
        page_rows.append(row)
    db.flush()  # need page_rows[i].id populated before chunks can reference them

    page_ranges = _page_ranges(effective_pages)

    chunks = chunk_text(document.content)
    for index, chunk in enumerate(chunks):
        embedding = embed_text(chunk.text)
        db.add(
            DocumentChunk(
                document_id=document.id,
                chunk_index=index,
                chunk_text=chunk.text,
                embedding=embedding,
                document_page_id=_page_id_for_chunk(chunk, page_ranges, page_rows),
            )
        )

    db.commit()
    return len(chunks)
