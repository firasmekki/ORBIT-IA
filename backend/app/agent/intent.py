"""Regex-based intent detection for requests the 1.5B model should never be
trusted to route or answer by itself - exact counting questions.

This is deliberately dumb (fixed patterns, no NLU): the small local model is
already known to be unreliable about calling tools consistently (see
orchestrator.py's grounding comments), so anything that has an exact,
checkable answer is intercepted here and handled 100% in Python before the
model ever sees the message - it never gets a chance to recompute or
reword a number.

Detection is split into two independent passes on purpose: TRIGGERS only
answers "is this a keyword-counting question at all", EXTRACTORS only
answers "what's the keyword" - a quoted substring always wins when present
(most reliable signal, no guessing about where the word ends or which
side of a trigger word it sits on), so an odd word order in Derja/Arabic
phrasing still works as long as the keyword itself is quoted.
"""

import re
from dataclasses import dataclass

from app.rag.normalize import normalize

_QUOTED = re.compile(r"[\"'«»](.+?)[\"'»]")
# Single token only for unquoted extraction - deliberately not multi-word:
# with no closing delimiter to bound it, a greedy multi-word capture eats
# trailing words it was never meant to ("où apparaît firas dans les
# documents" -> "firas dans les documents" instead of "firas"). Multi-word
# keywords still work fine, just quote them - the quoted path above
# extracts them exactly, with no ambiguity about where they end.
_WORD = r"[^\s\"'.,;:!?»«]+"
_STRIP_CHARS = " \t\n\r.,;:!?»«\"'"

_TRIGGERS = [
    # FR: cherche/recherche/trouve le mot/terme/nom ...
    re.compile(r"(?:cherche|recherche|trouve)\s+(?:le\s+)?(?:mot|terme|nom)\b", re.IGNORECASE),
    # FR: combien de fois ...
    re.compile(r"combien\s+de\s+fois\b", re.IGNORECASE),
    # FR: où apparaît / se trouve ...
    re.compile(r"où\s+(?:est-ce\s+que\s+)?(?:apparaît|apparait|se trouve)\b", re.IGNORECASE),
    # Derja (latin script): 9adeh/kadeh/qadech ... mawjouda ; win mawjouda ...
    re.compile(r"\b(?:9adeh|9ades|kadeh|kadech|qadech|qadeh)\b.*\bmawjoud", re.IGNORECASE),
    re.compile(r"\bwin\s+mawjouda\b", re.IGNORECASE),
    # Arabic: كم مرة ...
    re.compile(r"كم\s+مرة"),
]

# Only used when nothing is quoted - best-effort, ordered most-specific
# first. Each must capture exactly the keyword in group(1).
_EXTRACTORS = [
    re.compile(rf"(?:cherche|recherche|trouve)\s+(?:le\s+)?(?:mot|terme|nom)\s+(?:est\s+)?[\"']?({_WORD})[\"']?", re.IGNORECASE),
    re.compile(
        rf"combien\s+de\s+fois\s+(?:le\s+mot\s+|le\s+terme\s+)?[\"']?({_WORD})[\"']?\s+(?:apparaît|apparait|revient|figure)",
        re.IGNORECASE,
    ),
    re.compile(rf"où\s+(?:est-ce\s+que\s+)?(?:apparaît|apparait|se trouve)\s+[\"']?({_WORD})[\"']?", re.IGNORECASE),
    re.compile(rf"({_WORD})\s+mawjoud", re.IGNORECASE),
    re.compile(rf"mawjoud[a-z]*\s+({_WORD})", re.IGNORECASE),
    re.compile(rf"win\s+mawjouda\s+({_WORD})", re.IGNORECASE),
    re.compile(rf"كم\s+مرة\s+({_WORD})"),
]


@dataclass(frozen=True)
class KeywordIntent:
    keyword: str
    whole_word: bool = True


def detect_keyword_intent(message: str) -> KeywordIntent | None:
    if not any(trigger.search(message) for trigger in _TRIGGERS):
        return None

    quoted = _QUOTED.search(message)
    if quoted:
        keyword = quoted.group(1).strip(_STRIP_CHARS)
        return KeywordIntent(keyword=keyword) if keyword else None

    for extractor in _EXTRACTORS:
        match = extractor.search(message)
        if match:
            keyword = match.group(1).strip(_STRIP_CHARS)
            if keyword:
                return KeywordIntent(keyword=keyword)
    return None


# --- list_documents intent -------------------------------------------------

_LIST_TRIGGER = re.compile(
    r"\b(?:liste|listes?|montre(?:-moi)?|affiche(?:-moi)?|quels?\s+sont)\b.*\bdocuments?\b", re.IGNORECASE
)

# Normalized (accent/case-folded via app.rag.normalize) French synonym ->
# department code. Matched as whole words against the normalized message,
# not the department codes themselves (a user says "les documents RH", not
# "les documents HR").
_DEPARTMENT_SYNONYMS = {
    "rh": "HR",
    "ressources humaines": "HR",
    "hr": "HR",
    "finance": "FINANCE",
    "financier": "FINANCE",
    "financiers": "FINANCE",
    "comptabilite": "FINANCE",
    "tech": "TECH",
    "technique": "TECH",
    "informatique": "TECH",
    "it": "TECH",
    "general": "GENERAL",
    "generale": "GENERAL",
    "generaux": "GENERAL",
    "exec": "EXEC",
    "direction": "EXEC",
    "executif": "EXEC",
}

_AUTHOR_RE = re.compile(r"\b(?:de|par)\s+([A-ZÀ-Ý][\wÀ-ÿ\-]+(?:\s+[A-ZÀ-Ý][\wÀ-ÿ\-]+)?)")
_DATE_TOKEN = r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})"
_DATE_MIN_RE = re.compile(rf"(?:depuis|apr[eè]s)\s+(?:le\s+)?{_DATE_TOKEN}", re.IGNORECASE)
_DATE_MAX_RE = re.compile(rf"avant\s+(?:le\s+)?{_DATE_TOKEN}", re.IGNORECASE)


@dataclass(frozen=True)
class ListDocumentsIntent:
    department: str | None = None
    author: str | None = None
    date_min: str | None = None
    date_max: str | None = None


def _date_to_iso(date_str: str) -> str:
    if "/" in date_str:
        day, month, year = date_str.split("/")
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return date_str


def detect_list_documents_intent(message: str) -> ListDocumentsIntent | None:
    """Best-effort filter extraction, same spirit as detect_keyword_intent -
    a filter that isn't recognized is just left unset (broader results),
    never a reason to fail the whole intent: `list_documents` with fewer
    filters than the user meant is a minor inconvenience, not a correctness
    bug the way a miscounted keyword would be."""
    if not _LIST_TRIGGER.search(message):
        return None

    normalized_message = normalize(message)
    department = None
    for synonym, code in _DEPARTMENT_SYNONYMS.items():
        if re.search(rf"\b{re.escape(synonym)}\b", normalized_message):
            department = code
            break

    author_match = _AUTHOR_RE.search(message)
    author = author_match.group(1).strip(_STRIP_CHARS) if author_match else None

    date_min_match = _DATE_MIN_RE.search(message)
    date_min = _date_to_iso(date_min_match.group(1)) if date_min_match else None
    date_max_match = _DATE_MAX_RE.search(message)
    date_max = _date_to_iso(date_max_match.group(1)) if date_max_match else None

    return ListDocumentsIntent(department=department, author=author, date_min=date_min, date_max=date_max)


# --- get_document_section intent -------------------------------------------
#
# Split the same way as everything above: a trigger confirms "this is a
# read-a-section request", then independent extractors pull out whichever
# pieces are present (page/section/block, an explicit document name, a
# reference back to a previous turn - "ce document", "la première
# occurrence", "le deuxième", "suite"). The orchestrator combines whatever
# came out with the previous turn's stored reference_context (see
# app/models/chat.py's Message.reference_context) to resolve a concrete
# call - this module only extracts signals, it never resolves anything
# itself (no DB access here, and no reason to duplicate the ACL-aware
# title/section matching that already lives in the MCP tool).

_ORDINAL_WORDS = {
    "premier": 1, "première": 1, "premiere": 1, "1er": 1, "1ere": 1, "1ère": 1,
    "deuxième": 2, "deuxieme": 2, "second": 2, "seconde": 2, "2e": 2, "2eme": 2, "2ème": 2,
    "troisième": 3, "troisieme": 3, "3e": 3, "3eme": 3, "3ème": 3,
    "quatrième": 4, "quatrieme": 4, "4e": 4, "4eme": 4, "4ème": 4,
    "cinquième": 5, "cinquieme": 5, "5e": 5, "5eme": 5, "5ème": 5,
}
_NTH_CHOICE_RE = re.compile(
    r"\b(?:le|la)\s+(" + "|".join(sorted(_ORDINAL_WORDS, key=len, reverse=True)) + r")\b", re.IGNORECASE
)

_SECTION_TRIGGER = re.compile(
    r"\b(?:ouvre|ouvrir|montre(?:-moi)?|affiche(?:-moi)?)\b.*\b(?:page|section|bloc|paragraphe|occurrence)\b"
    r"|\bpremi[eè]re?\s+occurrence\b"
    r"|^\s*(?:suite|continue|continuer)\s*[.!]?\s*$"
    r"|\b(?:le|la)\s+(?:" + "|".join(sorted(_ORDINAL_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

_PAGE_NO_RE = re.compile(r"\bpage\s+(\d+)\b", re.IGNORECASE)
_BLOCK_RE = re.compile(r"\bbloc\s+(\d+)\b", re.IGNORECASE)
# [\w.]+ (not the punctuation-excluding _WORD used elsewhere): section
# numbers like "4.2" need the internal dot kept, and a single \w+-style
# token can't include it. Still single-token by design, same reasoning as
# the keyword extractors - an unbounded multi-word capture here would
# swallow a trailing "de Document Name" as part of the section name.
_SECTION_NAME_RE = re.compile(r"\bsection\s+[\"']?([\w.]+)[\"']?", re.IGNORECASE)

_CONTEXT_DOC_RE = re.compile(r"\bde\s+ce\s+document\b|\bce\s+document\b|\bcette\s+page\b|\bce\s+doc\b", re.IGNORECASE)
# "de [Document Name]" at the end of the message, but not "de ce document"
# (handled separately above) - greedy enough to capture multi-word titles,
# bounded by end-of-string or trailing punctuation. `.+?` rather than a
# quote-excluding character class: French titles/text routinely contain
# apostrophes ("l'entreprise") - excluding "'" from the captured run broke
# the match entirely for any name containing one. Surrounding quotes are
# still stripped, just via _STRIP_CHARS after the fact instead of the
# character class itself.
_DOCUMENT_NAME_RE = re.compile(r"\bde\s+(?!ce\s+document\b|cette\s+page\b|ce\s+doc\b)[\"']?(.+?)[\"']?\s*[.!?]?\s*$", re.IGNORECASE)

_FIRST_OCCURRENCE_RE = re.compile(r"\bpremi[eè]re?\s+occurrence\b", re.IGNORECASE)
_CONTINUATION_RE = re.compile(r"^\s*(?:suite|continue|continuer)\s*[.!]?\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class SectionRequest:
    page: int | None = None
    section: str | None = None
    document_name: str | None = None
    use_context_document: bool = False
    first_occurrence: bool = False
    nth_choice: int | None = None
    continuation: bool = False

    @property
    def has_location(self) -> bool:
        return self.page is not None or self.section is not None


def detect_section_request(message: str) -> SectionRequest | None:
    if _CONTINUATION_RE.match(message):
        return SectionRequest(continuation=True)

    if not _SECTION_TRIGGER.search(message):
        return None

    if _FIRST_OCCURRENCE_RE.search(message):
        return SectionRequest(first_occurrence=True)

    nth_match = _NTH_CHOICE_RE.search(message)
    # Only treat "le deuxième" etc. as a choice-list reference when there's
    # no explicit page/section alongside it - "la page 2" already has an
    # unambiguous page number and shouldn't be reinterpreted as "choice #2".
    page_match = _PAGE_NO_RE.search(message)
    block_match = _BLOCK_RE.search(message)
    section_match = _SECTION_NAME_RE.search(message)

    if nth_match and not page_match and not block_match and not section_match:
        return SectionRequest(nth_choice=_ORDINAL_WORDS[nth_match.group(1).lower()])

    page = int(page_match.group(1)) if page_match else None
    section = None
    if block_match:
        section = f"Bloc {block_match.group(1)}"
    elif section_match:
        section = section_match.group(1).strip(_STRIP_CHARS)

    if page is None and section is None:
        return None

    use_context_document = bool(_CONTEXT_DOC_RE.search(message))
    document_name = None
    if not use_context_document:
        name_match = _DOCUMENT_NAME_RE.search(message)
        if name_match:
            document_name = name_match.group(1).strip(_STRIP_CHARS)

    return SectionRequest(
        page=page,
        section=section,
        document_name=document_name,
        use_context_document=use_context_document,
    )
