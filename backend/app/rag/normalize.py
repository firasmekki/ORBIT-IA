"""Text normalization for exact keyword search (app/rag/search_keyword.py).

Case- and accent-insensitive, with Arabic-specific unification (alef
variants, ta marbuta, alef maqsura, tashkeel removal). Deliberately NOT used
anywhere near the RAG embedding pipeline - only for search_keyword's exact
regex count, which needs the normalized form for matching but the ORIGINAL
text for excerpts. `normalize_with_map` returns an index that maps every
normalized character back to its exact position in the source string, so a
match found in normalized text can be excerpted from the real one without
drift (a naive normalize-then-throw-away-the-original approach would report
excerpts around the wrong characters whenever accents/diacritics are
dropped, since that changes the string length).
"""

import re
import unicodedata

# Alef with hamza below (U+0625), with hamza above (U+0623), with madda
# (U+0622) all collapse to bare alef (U+0627); ta marbuta (U+0629) collapses
# to ha (U+0647); alef maqsura (U+0649) collapses to ya (U+064A). Code
# points, not literal glyphs pasted in source, so this stays auditable
# regardless of editor/terminal rendering.
_ALEF_VARIANTS = str.maketrans(
    {
        "إ": "ا",
        "أ": "ا",
        "آ": "ا",
        "ة": "ه",
        "ى": "ي",
    }
)

# Arabic combining diacritics (tashkeel: fatha/damma/kasra/sukun/shadda/
# tanwin U+064B-U+065F, superscript alef U+0670, small high/Quranic
# annotation marks U+0610-U+061A / U+06D6-U+06DC / U+06DF-U+06E4 /
# U+06E7-U+06E8 / U+06EA-U+06ED) - dropped entirely, they carry no
# distinguishing weight for a keyword search.
_ARABIC_DIACRITICS = re.compile(
    "[ؐ-ًؚ-ٰٟۖ-ۜ۟-۪ۤۧۨ-ۭ]"
)


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Returns (normalized_text, index_map): index_map[i] is the index in
    `text` that normalized_text[i] corresponds to."""
    out_chars: list[str] = []
    index_map: list[int] = []
    for i, ch in enumerate(text):
        ch = ch.translate(_ALEF_VARIANTS)
        if _ARABIC_DIACRITICS.match(ch):
            continue
        for dch in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(dch):
                continue
            out_chars.append(dch.lower())
            index_map.append(i)
    return "".join(out_chars), index_map


def normalize(text: str) -> str:
    normalized, _ = normalize_with_map(text)
    return normalized
