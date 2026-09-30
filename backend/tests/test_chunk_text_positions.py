"""Pure unit tests for app.rag.ingest.chunk_text's position tracking - no DB
needed, this only exercises the offset math and its self-verification."""

from app.rag.ingest import CHUNK_OVERLAP, chunk_text


def test_single_short_paragraph_position():
    text = "Bonjour le monde."
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].start == 0
    assert chunks[0].end == len(text)
    assert text[chunks[0].start : chunks[0].end] == chunks[0].text


def test_two_paragraphs_merged_into_one_chunk_position():
    text = "Premier paragraphe.\n\nDeuxième paragraphe."
    chunks = chunk_text(text, chunk_size=1000)
    assert len(chunks) == 1
    c = chunks[0]
    assert text[c.start : c.end] == c.text
    assert c.start == 0


def test_paragraphs_split_into_separate_chunks_have_correct_offsets():
    p1 = "A" * 50
    p2 = "B" * 50
    text = f"{p1}\n\n{p2}"
    chunks = chunk_text(text, chunk_size=60)  # forces a split: p1 alone, then p2 alone
    assert len(chunks) == 2
    for c in chunks:
        assert text[c.start : c.end] == c.text
    assert chunks[0].text == p1
    assert chunks[1].text == p2
    assert chunks[1].start == len(p1) + 2  # past "\n\n"


def test_long_paragraph_hard_split_has_correct_offsets_and_overlap():
    paragraph = "".join(f"word{i:04d} " for i in range(300))  # long single paragraph, no \n\n
    chunks = chunk_text(paragraph, chunk_size=200, overlap=50)
    assert len(chunks) > 1
    for c in chunks:
        assert paragraph[c.start : c.end] == c.text
    # consecutive hard-split pieces overlap by `overlap` characters
    assert chunks[1].start == chunks[0].end - CHUNK_OVERLAP or chunks[1].start == chunks[0].start + 200 - 50


def test_empty_text_returns_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_all_returned_chunks_are_self_consistent():
    text = "Intro.\n\n" + ("Long block. " * 200) + "\n\nConclusion finale ici."
    chunks = chunk_text(text, chunk_size=300, overlap=40)
    assert len(chunks) > 2
    for c in chunks:
        if c.has_position:
            assert text[c.start : c.end] == c.text
