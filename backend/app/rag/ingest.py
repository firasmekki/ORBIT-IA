from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentChunk, DocumentPage
from app.rag.embeddings import embed_texts
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


def _page_ranges(pages: list) -> list[tuple[int, int]]:
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


def _page_id_for_chunk(chunk: TextChunk, page_ranges: list[tuple[int, int]], page_rows: list):
    """A chunk that straddles two pages is attributed to the page containing
    its FIRST character - a definite, documented rule (not an average or a
    guess). Returns None if the chunk's position is unknown."""
    if not chunk.has_position:
        return None
    for (start, end), row in zip(page_ranges, page_rows):
        if start <= chunk.start < end:
            return row.id
    return None


EMBED_BATCH_SIZE = 64


@dataclass(frozen=True)
class _PageRef:
    id: object
    text: str


def write_pages(db: Session, document: Document, pages: list[ExtractedPage] | None = None) -> list[DocumentPage]:
    """Phase 1 - instant: (re)writes the document's DocumentPage rows (and
    drops its old chunks). From this commit on, the document is fully
    readable and exact-searchable (search_keyword / search_document_text /
    get_document_section work on pages), whatever its size. `pages` comes
    from app.rag.extract for real files; None (hand-typed documents) gives
    one synthetic page covering the whole content."""
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
    db.commit()
    return page_rows


def embed_document(
    db: Session,
    document: Document,
    *,
    batch_size: int = EMBED_BATCH_SIZE,
    on_progress: Callable[[int, int], None] | None = None,
    still_wanted: Callable[[], bool] | None = None,
) -> int:
    """Phase 2 - the slow part, the one only semantic search needs: chunks
    the content, embeds the chunks in batches and links each chunk to the
    page it starts in. Commits after every batch, so semantic search covers
    a large document progressively. Returns the number of chunks, or -1 if
    `still_wanted()` said to stop (the file changed again meanwhile)."""
    # Plain values, not ORM rows: if the file is re-synced meanwhile, its
    # pages are replaced by another session and these rows would vanish.
    page_refs = [
        _PageRef(id=row.id, text=row.text)
        for row in db.query(DocumentPage)
        .filter(DocumentPage.document_id == document.id)
        .order_by(DocumentPage.order_index)
    ]
    # Same character ranges as when the pages were built: the flattened
    # content is "\n\n".join(page texts) by construction (app.rag.extract).
    page_ranges = _page_ranges(page_refs)
    content = document.content
    db.query(DocumentChunk).filter(DocumentChunk.document_id == document.id).delete()
    db.commit()

    chunks = chunk_text(content)
    for first in range(0, len(chunks), batch_size):
        if still_wanted is not None and not still_wanted():
            return -1
        batch = chunks[first : first + batch_size]
        vectors = embed_texts([chunk.text for chunk in batch])
        # Checked again: the content may have changed during the (slow) call.
        if still_wanted is not None and not still_wanted():
            return -1
        for index, (chunk, embedding) in enumerate(zip(batch, vectors), start=first):
            db.add(
                DocumentChunk(
                    document_id=document.id,
                    chunk_index=index,
                    chunk_text=chunk.text,
                    embedding=embedding,
                    document_page_id=_page_id_for_chunk(chunk, page_ranges, page_refs),
                )
            )
        try:
            db.commit()
        except IntegrityError:
            # Its pages were replaced between the check and the commit: the
            # new version is queued again and will be embedded from scratch.
            db.rollback()
            return -1
        if on_progress is not None:
            on_progress(min(first + batch_size, len(chunks)), len(chunks))
    return len(chunks)


def ingest_document(db: Session, document: Document, pages: list[ExtractedPage] | None = None) -> int:
    """Both phases, synchronously (manual uploads, seed, watched folder).
    Returns the number of chunks written."""
    write_pages(db, document, pages)
    return embed_document(db, document)
