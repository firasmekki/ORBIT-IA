"""Exact keyword counting over app.models.document.DocumentPage rows.

Deliberately separate from the vector RAG path (app/rag/retriever.py): an
exact count has to be a real regex match, never a semantic "close enough"
result, and it has to run over non-overlapping text (DocumentChunk overlaps
by design, which would double-count).

The SQL layer (app/mcp/server.py's search_keyword tool) only ever uses this
module's `build_pattern` + `find_matches` to do the actual counting -
whatever a database ILIKE pre-filter narrows down to is a candidate set,
never the source of truth for the count itself.
"""

import re
from dataclasses import dataclass

from app.rag.normalize import normalize, normalize_with_map

EXCERPT_RADIUS = 60

_ARABIC_RANGE = re.compile("[ء-ي]")


@dataclass(frozen=True)
class KeywordMatch:
    line: int
    excerpt: str


def is_arabic_keyword(keyword: str) -> bool:
    """True if the keyword contains Arabic-script characters. Postgres's
    `unaccent` dictionary only folds Latin diacritics, not Arabic alef
    variants/tashkeel - a SQL ILIKE/unaccent pre-filter would silently miss
    valid matches for an Arabic keyword, so callers must skip the SQL
    pre-filter entirely in that case and scan all authorized pages in
    Python instead (still exact, just without the narrowing optimization)."""
    return bool(_ARABIC_RANGE.search(keyword))


def build_pattern(keyword: str, whole_word: bool) -> re.Pattern[str] | None:
    """Returns None if the keyword is empty after normalization (e.g. pure
    whitespace or pure diacritics) - callers should treat that as a
    validation error, not a zero-result search."""
    normalized_keyword = normalize(keyword).strip()
    if not normalized_keyword:
        return None
    escaped = re.escape(normalized_keyword)
    pattern = rf"\b{escaped}\b" if whole_word else escaped
    return re.compile(pattern, re.UNICODE)


def find_matches(text: str, line_offset: int, pattern: re.Pattern[str]) -> list[KeywordMatch]:
    """All matches in `text` (a DocumentPage.text), line numbers already
    shifted by `line_offset` (DocumentPage.line_offset) so they address the
    document's full flattened text, not just this page/section in
    isolation. Returns every match - callers decide how many to display,
    but the count (`len(result)`) must always be the true total."""
    normalized, index_map = normalize_with_map(text)
    matches: list[KeywordMatch] = []
    for m in pattern.finditer(normalized):
        if m.start() == m.end():
            continue  # empty match (shouldn't happen with a real keyword, guards against infinite loops)
        start_orig = index_map[m.start()]
        end_orig = index_map[m.end() - 1] + 1
        excerpt_start = max(0, start_orig - EXCERPT_RADIUS)
        excerpt_end = min(len(text), end_orig + EXCERPT_RADIUS)
        local_line = text.count("\n", 0, start_orig) + 1
        matches.append(
            KeywordMatch(line=line_offset + local_line - 1, excerpt=text[excerpt_start:excerpt_end])
        )
    return matches
