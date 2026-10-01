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
_WORD = r"[^\s\"'.,;:!?»«]+"
# Up to 3 tokens (covers "firas mekki" - a first+last name is the most
# common real multi-word case), NOT unbounded: an open-ended capture is
# what caused the original over-capture bug ("où apparaît firas dans les
# documents" -> "firas dans les documents"). The remaining risk of eating a
# trailing clause is handled by _trim_at_stopword below, not by the regex
# itself - most of these extractors are naturally bounded by a required
# trailing anchor (" apparaît", " mawjoud", ...) where backtracking already
# resolves to the minimal capture; only the open-ended ones ("cherche le
# mot X", "contient le mot X" - nothing has to follow X) actually need the
# stopword trim to stay safe.
_PHRASE = rf"({_WORD}(?:\s+{_WORD}){{0,2}})"
_STRIP_CHARS = " \t\n\r.,;:!?»«\"'"

_TRAILING_STOPWORDS = {
    "dans", "de", "du", "des", "sur", "pour", "avec", "sans", "et", "ou",
    "qui", "que", "ce", "cette", "ces", "il", "elle", "la", "le", "les",
    "dis", "dis-moi", "montre", "montre-moi", "donne", "donne-moi",
    "affiche", "affiche-moi", "stp", "svp",
}


def _trim_at_stopword(phrase: str) -> str:
    """Cuts a multi-word capture at the first word that looks like the
    start of a trailing clause rather than part of the keyword itself -
    e.g. "firas mekki dis" -> "firas mekki", "firas dans" -> "firas"."""
    kept: list[str] = []
    for word in phrase.split():
        if word.strip(_STRIP_CHARS).lower() in _TRAILING_STOPWORDS:
            break
        kept.append(word)
    return " ".join(kept)


_TRIGGERS = [
    # FR: cherche/recherche/trouve/chercher ... mot/terme/nom - `.*` between
    # the verb and the noun on purpose: real phrasing rarely has them
    # adjacent ("chercher dans les documents ... contient le mot X").
    re.compile(r"\b(?:cherche|recherche|trouve|cherch(?:er|ez))\b.*\b(?:mot|terme|nom)\b", re.IGNORECASE),
    # FR: "il y a un document qui contient le mot/terme X" - contains/
    # containing, independent of the cherche/trouve verbs above.
    re.compile(r"\bcontient\b.*\b(?:mot|terme)\b", re.IGNORECASE),
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
    re.compile(rf"(?:cherche|recherche|trouve|cherch(?:er|ez))\b.*?\b(?:le\s+)?(?:mot|terme|nom)\s+(?:est\s+)?[\"']?{_PHRASE}[\"']?", re.IGNORECASE),
    re.compile(rf"\bcontient\b.*?\b(?:le\s+)?(?:mot|terme)\s+[\"']?{_PHRASE}[\"']?", re.IGNORECASE),
    re.compile(
        rf"combien\s+de\s+fois\s+(?:le\s+mot\s+|le\s+terme\s+)?[\"']?{_PHRASE}[\"']?\s+(?:apparaît|apparait|revient|figure)",
        re.IGNORECASE,
    ),
    re.compile(rf"où\s+(?:est-ce\s+que\s+)?(?:apparaît|apparait|se trouve)\s+[\"']?{_PHRASE}[\"']?", re.IGNORECASE),
    # Single-word only below (_WORD, not _PHRASE): these aren't anchored on
    # the left the way the ones above are (nothing has to precede the
    # capture group), so a multi-word cap would greedily swallow whatever
    # trigger word came before it too (e.g. "qadech marra firas mawjouda"
    # -> "qadech marra firas" instead of "firas") - caught by
    # tests/test_intent.py::test_derja_word_before_mawjouda.
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
            keyword = _trim_at_stopword(match.group(1).strip(_STRIP_CHARS))
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
_ORDINAL_ALT = "|".join(sorted(_ORDINAL_WORDS, key=len, reverse=True))
_ARTICLES = r"le|la|du|des|au|un|une"
_NTH_CHOICE_RE = re.compile(rf"\b(?:{_ARTICLES})\s+({_ORDINAL_ALT})\b", re.IGNORECASE)
# High-confidence version: the ordinal is directly followed by a noun that
# unambiguously means "one of the previous list" (résultat/occurrence/
# document/choix) - checked BEFORE section-name extraction gets a chance to
# run, so "la section correspondante du premier résultat" resolves via this
# (nth_choice=1) instead of _SECTION_NAME_RE wrongly capturing
# "correspondante" as if it were a real section name.
_ANCHORED_NTH_RE = re.compile(
    rf"\b(?:{_ARTICLES})\s+({_ORDINAL_ALT})\s+(?:occurrence|résultat|resultat|document|choix)\b",
    re.IGNORECASE,
)

_SECTION_TRIGGER = re.compile(
    r"\b(?:ouvre|ouvrir|montre(?:-moi)?|affiche(?:-moi)?)\b.*\b(?:page|section|bloc|paragraphe|occurrence|résultat|resultat)\b"
    r"|\bpremi(?:er|ère)\s+(?:occurrence|résultat|resultat)\b"
    r"|^\s*(?:suite|continue|continuer)\s*[.!]?\s*$"
    r"|\b(?:" + _ARTICLES + r")\s+(?:" + _ORDINAL_ALT + r")\b",
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

_FIRST_OCCURRENCE_RE = re.compile(r"\bpremi(?:er|ère)\s+(?:occurrence|résultat|resultat)\b", re.IGNORECASE)
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

    anchored_nth = _ANCHORED_NTH_RE.search(message)
    if anchored_nth:
        return SectionRequest(nth_choice=_ORDINAL_WORDS[anchored_nth.group(1).lower()])

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


# --- generate_chart intent --------------------------------------------------
#
# Same split as everything above: a trigger confirms "the user wants a
# visualization", type-keywords (when present) pick line/bar/pie/scatter.
# No type-word at all -> chart_type=None, left for the orchestrator to
# default sensibly (see _run_generate_chart) or ask, per the brief's "si le
# choix reste réellement ambigu -> demander une clarification". A bare data
# question ("Quel est le chiffre d'affaires ?") must never trigger this -
# every path below requires an explicit visualization verb/noun, not just
# a data-sounding subject.

# --- explicit file/sheet reference (priority data source for charts) -----
#
# Deliberately dumb extraction, same philosophy as the rest of this module:
# just enough to hand a candidate filename/sheet name to
# read_spreadsheet_data's own ACL-scoped resolution (app/mcp/server.py),
# which is the actual authority on whether it exists/is authorized - this
# only decides "does the message look like it's naming one".

_FILE_HINT_RE = re.compile(r"\b([\w\-]+\.(?:xlsx|xls|csv))\b", re.IGNORECASE)
_SHEET_HINT_RE = re.compile(
    r"feuille\s*[:\s]\s*[«\"]?\s*([^,.\n]+?)\s*(?=[,.\n]|$|\bpour\b|\bcolonne\b|\baffiche\b|\bcompare\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SpreadsheetReference:
    file_hint: str | None
    sheet_hint: str | None


def detect_spreadsheet_reference(message: str) -> SpreadsheetReference | None:
    file_match = _FILE_HINT_RE.search(message)
    sheet_match = _SHEET_HINT_RE.search(message)
    file_hint = file_match.group(1) if file_match else None
    sheet_hint = sheet_match.group(1).strip(_STRIP_CHARS + " ") if sheet_match else None
    if not file_hint and not sheet_hint:
        return None
    return SpreadsheetReference(file_hint=file_hint, sheet_hint=sheet_hint or None)


_CHART_TYPE_WORDS: dict[str, tuple[str, ...]] = {
    # Order matters: scatter/pie's more specific phrases are checked before
    # the shorter, more general bar/line ones so e.g. "relation entre" (
    # scatter) never gets shadowed by an incidental "comparaison" match.
    "scatter": ("nuage de points", "scatter", "corrélation", "relation entre"),
    "pie": ("camembert", "circulaire", "répartition", "proportion", "parts de", "pie chart"),
    "line": ("courbe", "évolution", "tendance", "line chart"),
    "bar": ("histogramme", "barres", "classement", "bar chart"),
}

_GENERIC_CHART_TRIGGER = re.compile(r"\b(?:graphique|diagramme|visualis[eé]r?|visualisation)\b", re.IGNORECASE)
_CHART_VERB_RE = re.compile(r"\b(?:affiche|montre|fais|g[ée]n[èe]re|cr[ée]e[rz]?)\b", re.IGNORECASE)
# "compare" alone is ambiguous with a future compare_documents tool (not
# built yet) - only counted as a chart signal when the subject is clearly
# numeric/data, not "compare ces deux documents".
_CHART_DATA_NOUNS_RE = re.compile(
    r"\b(?:ventes?|budgets?|d[ée]penses?|revenus?|chiffre d'affaires|produits?|d[ée]partements?|co[uû]ts?)\b",
    re.IGNORECASE,
)
_COMPARE_RE = re.compile(r"\bcompar[eèz]\w*\b", re.IGNORECASE)
# Wider than _CHART_VERB_RE: an explicit file/sheet reference (see
# detect_spreadsheet_reference above) already carries most of the intent
# signal on its own, so a softer action verb - "compare"/"utilise" as well
# as the usual affiche/montre/fais/génère/crée - is enough alongside it.
# Never used on its own (only combined with a spreadsheet reference below),
# so it doesn't loosen the bare-data-question guard for every other message.
_CHART_OR_SPREADSHEET_VERB_RE = re.compile(
    r"\b(?:affiche|montre|fais|g[ée]n[èe]re|cr[ée]e[rz]?|compar[eèz]\w*|utilise[rz]?)\b", re.IGNORECASE
)

_TRIGGER_WORD_STRIP_RE = re.compile(
    r"\b(?:affiche|montre|fais|g[ée]n[èe]re|cr[ée]e[rz]?|graphique|diagramme|visualis[eé]r?|visualisation|"
    r"courbe|évolution|tendance|histogramme|barres|classement|camembert|circulaire|répartition|proportion|"
    r"nuage de points|scatter|corrélation|relation entre|compar[eèz]\w*|une?|des?|du|de|le|la|les|moi|-moi)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ChartIntent:
    chart_type: str | None  # None = no explicit type word, orchestrator decides
    subject: str  # message with trigger/filler words stripped - used as the search_database query


def detect_chart_intent(message: str) -> ChartIntent | None:
    lowered = message.lower()

    matched_type = None
    for chart_type, words in _CHART_TYPE_WORDS.items():
        if any(w in lowered for w in words):
            matched_type = chart_type
            break

    has_generic = bool(_GENERIC_CHART_TRIGGER.search(message))
    # "visualise"/"visualiser" already carries its own imperative - doesn't
    # need a separate affiche/montre/fais alongside it the way "graphique"
    # or "diagramme" (nouns) do.
    is_self_sufficient_trigger = bool(re.search(r"\bvisualis", message, re.IGNORECASE))
    has_verb = bool(_CHART_VERB_RE.search(message))
    has_data_compare = bool(_COMPARE_RE.search(message)) and bool(_CHART_DATA_NOUNS_RE.search(message))
    # An explicit "fichier X.xlsx" / "feuille Y" reference plus any
    # reasonably action-ish verb is unambiguous chart-from-spreadsheet
    # intent, even without a "graphique"/"courbe" word - see
    # app/agent/orchestrator.py::_run_chart_from_spreadsheet.
    has_spreadsheet_request = bool(detect_spreadsheet_reference(message)) and bool(
        _CHART_OR_SPREADSHEET_VERB_RE.search(message)
    )

    if (
        matched_type is None
        and not (has_generic and has_verb)
        and not is_self_sufficient_trigger
        and not has_data_compare
        and not has_spreadsheet_request
    ):
        return None
    if matched_type is None and has_data_compare:
        matched_type = "bar"

    subject = _TRIGGER_WORD_STRIP_RE.sub(" ", message).strip(_STRIP_CHARS + " ")
    return ChartIntent(chart_type=matched_type, subject=subject)
