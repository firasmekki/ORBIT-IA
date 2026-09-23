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
