"""Integration tests for DocumentChunk.document_page_id - the exact chunk-
to-page mapping computed in app/rag/ingest.py. Runs through the real
ingest_document()/chunk_text() path against a real database; embed_text is
monkeypatched to a fixed dummy vector so these tests don't depend on Ollama
being reachable (the mapping logic doesn't look at embedding content at
all, only chunk/page character positions)."""

from app.models.document import CONFIDENTIALITY_RANK, Document, DocumentChunk, DocumentPage
from app.rag.extract import ExtractedPage
from app.rag.ingest import ingest_document


def _patch_embed(monkeypatch):
    monkeypatch.setattr("app.rag.ingest.embed_text", lambda text: [0.0] * 768)


def test_chunk_linked_to_correct_single_page(db, monkeypatch):
    _patch_embed(monkeypatch)
    doc = Document(
        title="Doc", department="GENERAL", confidentiality="INTERNAL",
        confidentiality_rank=CONFIDENTIALITY_RANK["INTERNAL"], content="Contenu court.",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    pages = [ExtractedPage(page_no=1, section=None, line_offset=1, text="Contenu court.")]
    ingest_document(db, doc, pages=pages)

    chunks = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).all()
    assert len(chunks) == 1
    assert chunks[0].document_page_id is not None
    page = db.get(DocumentPage, chunks[0].document_page_id)
    assert page.page_no == 1


def test_chunks_linked_to_correct_distinct_pages(db, monkeypatch):
    _patch_embed(monkeypatch)
    page1_text = "Section un. " * 20
    page2_text = "Section deux, complètement différente. " * 20
    doc = Document(
        title="Doc multi-page", department="GENERAL", confidentiality="INTERNAL",
        confidentiality_rank=CONFIDENTIALITY_RANK["INTERNAL"], content=f"{page1_text}\n\n{page2_text}",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    pages = [
        ExtractedPage(page_no=1, section=None, line_offset=1, text=page1_text),
        ExtractedPage(page_no=2, section=None, line_offset=1, text=page2_text),
    ]
    ingest_document(db, doc, pages=pages)

    chunks = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).order_by(DocumentChunk.chunk_index).all()
    assert len(chunks) >= 2
    pages_by_id = {p.id: p for p in db.query(DocumentPage).filter(DocumentPage.document_id == doc.id).all()}

    first_chunk_page = pages_by_id[chunks[0].document_page_id]
    last_chunk_page = pages_by_id[chunks[-1].document_page_id]
    assert first_chunk_page.page_no == 1
    assert last_chunk_page.page_no == 2


def test_section_name_correctly_linked(db, monkeypatch):
    _patch_embed(monkeypatch)
    text = "Contenu de la section Introduction."
    doc = Document(
        title="Doc section", department="GENERAL", confidentiality="INTERNAL",
        confidentiality_rank=CONFIDENTIALITY_RANK["INTERNAL"], content=text,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    pages = [ExtractedPage(page_no=None, section="Introduction", line_offset=1, text=text)]
    ingest_document(db, doc, pages=pages)

    chunk = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).first()
    page = db.get(DocumentPage, chunk.document_page_id)
    assert page.section == "Introduction"


def test_manually_created_document_gets_synthetic_page_link(db, monkeypatch):
    """DocumentCreate flow (no source file) - ingest_document is called
    with pages=None, which synthesizes a single page with page_no=None,
    section=None. The chunk IS linked (not orphaned), but page/section on
    the linked page are both null - search_documents must report null,
    never invent a page number."""
    _patch_embed(monkeypatch)
    doc = Document(
        title="Doc manuel", department="GENERAL", confidentiality="INTERNAL",
        confidentiality_rank=CONFIDENTIALITY_RANK["INTERNAL"], content="Texte tapé à la main.",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    ingest_document(db, doc, pages=None)

    chunk = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).first()
    assert chunk.document_page_id is not None
    page = db.get(DocumentPage, chunk.document_page_id)
    assert page.page_no is None
    assert page.section is None


def test_chunk_near_page_boundary_attributed_to_page_containing_its_start(db, monkeypatch):
    """A chunk spanning (or starting right at) a page boundary must be
    attributed to the page containing its FIRST character - a documented
    rule, not an average. chunk_size is set small enough that the split
    lands very close to the page 1 / page 2 boundary."""
    _patch_embed(monkeypatch)
    page1_text = "X" * 100  # exactly fills close to one chunk
    page2_text = "Y" * 100
    doc = Document(
        title="Doc boundary", department="GENERAL", confidentiality="INTERNAL",
        confidentiality_rank=CONFIDENTIALITY_RANK["INTERNAL"], content=f"{page1_text}\n\n{page2_text}",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    pages = [
        ExtractedPage(page_no=1, section=None, line_offset=1, text=page1_text),
        ExtractedPage(page_no=2, section=None, line_offset=1, text=page2_text),
    ]
    ingest_document(db, doc, pages=pages)

    chunks = db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).order_by(DocumentChunk.chunk_index).all()
    pages_by_id = {p.id: p for p in db.query(DocumentPage).filter(DocumentPage.document_id == doc.id).all()}

    # Every chunk must map to a page whose character range genuinely
    # contains that chunk's start - verified directly against the source
    # content (rebuilding the exact same "\n\n".join used by _assemble()).
    full_text = doc.content
    page_ranges = []
    cursor = 0
    for p in pages:
        start = cursor
        end = start + len(p.text)
        page_ranges.append((start, end, p.page_no))
        cursor = end + 2

    for chunk in chunks:
        assert chunk.document_page_id is not None, "every chunk here has a verifiable position"
        mapped_page = pages_by_id[chunk.document_page_id]
        chunk_start = full_text.find(chunk.chunk_text)
        assert chunk_start != -1
        expected_page_no = next(pn for (s, e, pn) in page_ranges if s <= chunk_start < e)
        assert mapped_page.page_no == expected_page_no


def test_chunk_text_and_embeddings_unaffected_by_page_linking(db, monkeypatch):
    """The page-linking feature must not change what gets embedded/searched
    - only adds metadata alongside the existing chunk_text/embedding."""
    calls = []

    def fake_embed(text):
        calls.append(text)
        return [0.1] * 768

    monkeypatch.setattr("app.rag.ingest.embed_text", fake_embed)

    doc = Document(
        title="Doc", department="GENERAL", confidentiality="INTERNAL",
        confidentiality_rank=CONFIDENTIALITY_RANK["INTERNAL"], content="Un texte à indexer normalement.",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    count = ingest_document(db, doc, pages=None)
    assert count == 1
    assert len(calls) == 1
    assert calls[0] == "Un texte à indexer normalement."
