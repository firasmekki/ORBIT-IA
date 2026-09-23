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
