"""Post-generation grounding check for LLM-composed answers.

The system prompt tells the model never to invent a figure, but a small
local model (llama3.2:1b) does not reliably obey that - asked to "compare
2025 and 2026" with only a 2025 payroll figure in its context, it filled
every missing cell with a plausible-looking number. A prompt is a request,
not a guarantee, so this module enforces the rule deterministically on the
model's *output*, the same way the ACL is enforced in SQL rather than by
asking the model to ignore what it shouldn't see:

- `Evidence` records exactly what the model was given this turn - the
  already-ACL-filtered excerpts/records/tool results, plus what the user
  typed themselves. Nothing else counts as a source.
- `verify_answer` checks every numerical value in the answer against that
  evidence (rounding-tolerant: "4,1 M€" matches "4,1 millions d'euros" and
  "4100000.00"). In a "Libellé : valeur" line or a table row it also checks
  that the label's words actually appear in the source sentence holding
  that number - so a payroll figure relabelled "budget de l'entreprise"
  is caught even though the number itself is real.
- An untraceable value is never shown: its cell / value / sentence is
  replaced by "Non disponible dans les sources" (or removed), and a note
  says so. No retry - a second sampling of the same model is no more
  trustworthy than the first, and this keeps the outcome deterministic.
- Only documents that actually contributed to the final answer (a traced
  value, a cited title, or substantial content overlap) are returned as
  sources - not everything retrieval happened to surface.
"""

import logging
import re
from dataclasses import dataclass, field

from app.rag.normalize import normalize

logger = logging.getLogger("orbitia.grounding")

NO_DATA_ANSWER = "Je ne dispose pas de cette donnée dans les documents accessibles."
UNAVAILABLE = "Non disponible dans les sources"
REDACTION_NOTE = (
    "Note : les valeurs qui ne figurent pas dans les documents accessibles ont été retirées de cette réponse."
)

_SCALES = {
    "k": 1e3,
    "mille": 1e3,
    "m": 1e6,
    "mio": 1e6,
    "million": 1e6,
    "millions": 1e6,
    "md": 1e9,
    "mds": 1e9,
    "milliard": 1e9,
    "milliards": 1e9,
}

_NUMBER_RE = re.compile(
    r"(?<![\w.,])"
    r"(?P<num>\d{1,3}(?:[   ]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)*)"
    r"(?P<scale>[   ]*(?:milliards?|mds?|millions?|mio|mille|k|m)(?![a-zà-ÿ]))?"
    r"(?P<pct>[   ]*%)?",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
_SENTENCE_SPLIT_RE = re.compile(r"\n+|(?<=[.!?;])\s+")
_WORD_RE = re.compile(r"[a-z]+")
_COMPARISON_RE = re.compile(r"\b(compar\w*|versus|vs|evolution|ecart|difference)\b")

# Words that carry no indicator meaning in a "Libellé : valeur" label -
# requiring them to appear in the source sentence would only produce false
# alarms ("Montant : 310 000 €").
_LABEL_STOPWORDS = {
    "les", "des", "une", "pour", "par", "sur", "dans", "avec", "est", "sont", "aux", "selon", "son", "ses",
    "leur", "leurs", "cette", "ces", "qui", "que", "dont", "the", "and", "for",
    "montant", "montants", "valeur", "valeurs", "annee", "annees", "indicateur", "indicateurs",
    "environ", "soit", "total", "totale", "totaux", "totales", "chiffre", "donnee", "donnees",
}
# Content-overlap source attribution ignores these too, on top of anything
# already present in the user's own question.
_OVERLAP_STOPWORDS = _LABEL_STOPWORDS | {
    "document", "documents", "disponible", "disponibles", "information", "informations", "source", "sources",
    "entreprise", "accessibles", "dispose", "concernant", "egalement", "toutefois",
}
_MIN_OVERLAP_TOKENS = 3


def _stem(word: str) -> str:
    # Crude French plural/feminine folding - enough to match "salariale"
    # against "salarial", "dépenses" against "dépense".
    for suffix in ("es", "s", "x", "e"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def _stems(text: str, stopwords: set[str] = _LABEL_STOPWORDS, min_len: int = 3) -> set[str]:
    return {_stem(w) for w in _WORD_RE.findall(normalize(text)) if len(w) >= min_len and w not in stopwords}


def requested_years(message: str) -> list[int]:
    """Distinct years named in the user's message, in order of appearance."""
    years: list[int] = []
    for match in _YEAR_RE.finditer(message):
        year = int(match.group(1))
        if year not in years:
            years.append(year)
    return years


def is_comparison_request(message: str) -> bool:
    return len(requested_years(message)) >= 2 or bool(_COMPARISON_RE.search(normalize(message)))


# --- plotting code ------------------------------------------------------------

CHART_CODE_NOTICE = (
    "Je ne fournis pas de code pour tracer un graphique : les graphiques s'affichent directement dans "
    "le chat, à partir de données réelles uniquement. Indiquez le fichier et la feuille, par exemple : "
    "« À partir du fichier Orbitia_Test_Entreprise_Complet.xlsx, feuille CA mensuel, génère un graphique "
    "en courbe »."
)
_PLOTTING_RE = re.compile(
    r"matplotlib|pyplot|\bplt\.|plotly|seaborn|ggplot|\bpd\.|import pandas|chart\.js|new Chart\(|recharts|\bd3\.",
    re.IGNORECASE,
)
_FENCED_BLOCK_RE = re.compile(r"```[^\n]*\n.*?(?:```|\Z)", re.DOTALL)
_PLOTTING_LINE_RE = re.compile(
    r"^\s*(?:(?:import|from)\s+(?:matplotlib|pandas|plotly|seaborn)\b.*|.*\bplt\.\w+\(.*)$", re.MULTILINE
)


def strip_chart_code(answer: str) -> tuple[str, bool]:
    """A small model asked for a chart it cannot ground tends to write a
    matplotlib script instead, usually around numbers it made up. The UI
    renders charts itself (ChartBlock), so plotting code is never a valid
    answer: it is removed and replaced by how to get the real chart.
    Unrelated code (a snippet quoted from a technical document) is kept."""
    removed = False

    def drop_plotting_block(match: re.Match) -> str:
        nonlocal removed
        if _PLOTTING_RE.search(match.group(0)):
            removed = True
            return ""
        return match.group(0)

    cleaned = _FENCED_BLOCK_RE.sub(drop_plotting_block, answer)
    cleaned, stray_lines = _PLOTTING_LINE_RE.subn("", cleaned)
    removed = removed or stray_lines > 0
    if not removed:
        return answer, False
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return (f"{cleaned}\n\n{CHART_CODE_NOTICE}" if cleaned else CHART_CODE_NOTICE), True


# --- number parsing -----------------------------------------------------------


@dataclass(frozen=True)
class _Number:
    start: int
    end: int
    candidates: tuple[tuple[float, int], ...]  # (value, displayed decimals) - >1 only when ambiguous
    scale: float
    bare: bool  # no scale, no %, no separator

    def is_year(self) -> bool:
        return self.bare and any(1900 <= v <= 2100 and d == 0 for v, d in self.candidates)

    def is_small_bare_int(self) -> bool:
        return self.bare and all(d == 0 and v <= 10 for v, d in self.candidates)


def _parse_raw(raw: str) -> tuple[tuple[float, int], ...]:
    spaced = bool(re.search(r"[   ]", raw))
    s = re.sub(r"[   ]", "", raw)
    if spaced:
        # "4 100 000,50": spaces group thousands, so a comma is the decimal mark.
        whole, _, frac = s.partition(",")
        return ((float(f"{whole}.{frac}" if frac else whole), len(frac)),)
    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        decimal = "," if s.rfind(",") > s.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        whole, _, frac = s.replace(thousands, "").partition(decimal)
        return ((float(f"{whole}.{frac}"), len(frac)),)
    for sep in (",", "."):
        if sep in s:
            parts = s.split(sep)
            if len(parts) > 2:
                return ((float("".join(parts)), 0),)  # "4,100,000" / "4.100.000"
            whole, frac = parts
            as_decimal = (float(f"{whole}.{frac}"), len(frac))
            if len(frac) == 3:
                # "4,100" - French decimal or English thousands; both kept.
                return (as_decimal, (float(whole + frac), 0))
            return (as_decimal,)
    return ((float(s), 0),)


def _find_numbers(text: str) -> list[_Number]:
    numbers: list[_Number] = []
    for m in _NUMBER_RE.finditer(text):
        scale_word = (m.group("scale") or "").strip().lower()
        scale = _SCALES.get(scale_word, 1.0)
        raw = m.group("num")
        candidates = tuple((v * scale, d) for v, d in _parse_raw(raw))
        bare = not scale_word and not m.group("pct") and not re.search(r"[   .,]", raw)
        numbers.append(_Number(m.start(), m.end(), candidates, scale, bare))
    return numbers


def _values_match(answer_value: float, decimals: int, scale: float, source_value: float) -> bool:
    if answer_value == source_value:
        return True
    if answer_value == 0 or source_value == 0:
        return False
    # Accept a value rounded to the precision it is displayed with ("4 M€"
    # for 4 100 000), never a different magnitude: within half a display
    # unit AND within 5%.
    diff = abs(answer_value - source_value)
    display_unit = scale * (10 ** -decimals)
    return diff <= display_unit / 2 + 1e-9 * abs(source_value) and diff / abs(source_value) <= 0.05


# --- evidence -----------------------------------------------------------------


@dataclass
class _Sentence:
    values: list[float]
    stems: set[str]


@dataclass
class _EvidenceItem:
    document_id: str | None
    title: str | None
    text: str
    from_user: bool
    sentences: list[_Sentence] = field(default_factory=list)
    stems: set[str] = field(default_factory=set)
    # Years named in the document title ("Plan stratégique 2026") - a
    # sentence of that document is about that year even if it doesn't say so.
    title_years: set[int] = field(default_factory=set)


@dataclass
class Evidence:
    """Everything the model was actually shown this turn. Only ever fed
    with ACL-filtered tool output and the user's own words."""

    items: list[_EvidenceItem] = field(default_factory=list)

    def add(self, text: str, *, document_id: str | None = None, title: str | None = None, from_user: bool = False) -> None:
        if not text and not title:
            return
        full = f"{title}\n{text}" if title else text
        title_stems = _stems(title) if title else set()
        item = _EvidenceItem(document_id=document_id, title=title, text=full, from_user=from_user)
        item.title_years = set(requested_years(title)) if title else set()
        for sentence in _SENTENCE_SPLIT_RE.split(full):
            if not sentence.strip():
                continue
            values: list[float] = []
            for number in _find_numbers(sentence):
                for value, _ in number.candidates:
                    values.append(value)
                    if number.scale != 1.0:
                        values.append(value / number.scale)  # "4,1 millions" also backs a bare "4,1"
            item.sentences.append(_Sentence(values=values, stems=_stems(sentence) | title_stems))
        item.stems = _stems(full, _OVERLAP_STOPWORDS, min_len=5)
        self.items.append(item)

    def add_user_message(self, text: str) -> None:
        self.add(text, from_user=True)

    def add_tool_result(self, tool_name: str, result: dict) -> None:
        """Mirror of what the orchestrator's tool loop hands the model."""
        if "error" in result:
            return
        if tool_name == "get_document" and "document_id" in result:
            self.add(result.get("content") or "", document_id=str(result["document_id"]), title=result.get("title"))
            return
        if tool_name == "search_database":
            for record in result.get("results", []):
                self.add(f"{record.get('label')} ({record.get('year')}) : {record.get('amount')}")
            return
        items = result.get("results")
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict):
                    doc_id = it.get("document_id")
                    self.add(
                        str(it.get("excerpt") or it.get("content") or ""),
                        document_id=str(doc_id) if doc_id else None,
                        title=it.get("title"),
                    )
            return
        text = result.get("text") or result.get("content")
        if isinstance(text, str):
            doc_id = result.get("document_id")
            self.add(text, document_id=str(doc_id) if doc_id else None, title=result.get("title"))


# --- verification -------------------------------------------------------------


@dataclass
class GroundingReport:
    answer: str
    sources: list[dict]
    unsupported: list[str]


def _line_label(line: str, position: int) -> str | None:
    stripped = line.strip()
    if stripped.startswith("|"):
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        return cells[0] if cells else ""
    colon = line.find(":")
    if 0 <= colon < position:
        return line[:colon]
    return None


def _is_list_marker(line: str, number: _Number) -> bool:
    return bool(re.fullmatch(r"\s*(?:[-*>#]\s*)*", line[: number.start])) and line[number.end : number.end + 1] in (".", ")")


# "(p. 11)", "pages 3 à 5", "p. 8 et p. 9", "Doc 2", "section 4": a pointer
# into the sources, not a figure claimed about the world - nothing to trace.
_REFERENCE_BEFORE_RE = re.compile(
    r"(?:\b(?:p|pp)\.|\bpages?|\bdoc(?:ument)?|\bsection|\bannexe|\bchapitre|\blot|\bn°)\s*"
    r"(?:\d+\s*(?:à|a|-|–|et|,)\s*(?:(?:p|pp)\.\s*)?)?$",
    re.IGNORECASE,
)


def _is_reference(line: str, number: _Number) -> bool:
    return bool(number.bare and _REFERENCE_BEFORE_RE.search(line[max(0, number.start - 24) : number.start]))


_DATE_RE = re.compile(
    r"\b(?P<day>0?[1-9]|[12]\d|3[01])\s+(?P<month>janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre|"
    r"january|february|march|april|may|june|july|august|september|october|november|december)\s+(?P<year>(?:19|20)\d{2})\b"
)


def _date_component_backing(line: str, number: _Number, evidence: Evidence) -> set[str]:
    """Keep a day number only when the complete day/month/year occurs in evidence."""
    if not number.bare:
        return set()
    normalized_line = normalize(line)
    # normalize() removes accents, so match the date in its normalized form.
    for match in _DATE_RE.finditer(normalized_line):
        if not (match.start("day") <= number.start and number.end <= match.end("day")):
            continue
        date_text = match.group(0)
        backed = {item.document_id for item in evidence.items
                  if item.document_id and date_text in normalize(item.text)}
        if backed:
            return backed
    return set()


def _trace_number(
    number: _Number, label: str | None, evidence: Evidence, required_year: int | None = None
) -> tuple[bool, set[str]]:
    """(traceable, document ids that back it). `required_year`: the value
    sits in a table column headed by that year - so its source must be
    about that year too, not merely contain the same number (a 2025 amount
    copied into a 2024 column is a fabricated 2024 value)."""
    label_stems = _stems(label) if label else set()
    backing: set[str] = set()
    traced = False
    for item in evidence.items:
        for sentence in item.sentences:
            if not any(
                _values_match(value, decimals, number.scale, source_value)
                for value, decimals in number.candidates
                for source_value in sentence.values
            ):
                continue
            if (
                required_year is not None
                and not item.from_user
                and float(required_year) not in sentence.values
                and required_year not in item.title_years
            ):
                continue
            # Typed by the user, or a year: the number itself is the claim,
            # there is no source label to be faithful to - and a bare year
            # turns up in half the corpus, so it never makes a document
            # count as "used" on its own.
            if item.from_user or number.is_year():
                traced = True
            elif label_stems <= sentence.stems:
                traced = True
                if item.document_id:
                    backing.add(item.document_id)
    return traced, backing


def _redact_line(line: str, bad: list[_Number]) -> str | None:
    stripped = line.strip()
    if stripped.startswith("|"):
        leading = line[: len(line) - len(line.lstrip())]
        inner = stripped.strip("|")
        cells = inner.split("|")
        offset = line.index("|") + 1
        out, cursor = [], offset
        for cell in cells:
            span = (cursor, cursor + len(cell))
            cursor = span[1] + 1
            if any(span[0] <= n.start < span[1] for n in bad):
                out.append(f" {UNAVAILABLE} ")
            else:
                out.append(cell)
        return leading + "|" + "|".join(out) + "|"
    # Sentence by sentence (ends are punctuation followed by whitespace, so
    # "4.1" is never split): a "Libellé : valeur" sentence keeps its label
    # and says the value is unavailable; any other sentence carrying an
    # untraceable value is dropped.
    # Never cut after an abbreviation such as "(p. 11)" or "n°.": that left
    # dangling "(p." fragments in the answer.
    bounds = [0, *(m.end() for m in re.finditer(r"(?<=[.!?])(?<!\bp\.)(?<!\bpp\.)(?<!\bn°\.)\s+", line)), len(line)]
    kept = []
    for start, end in zip(bounds, bounds[1:]):
        sentence_bad = [n for n in bad if start <= n.start < end]
        if not sentence_bad:
            kept.append(line[start:end])
            continue
        colon = line.find(":", start, end)
        if 0 <= colon < min(n.start for n in sentence_bad):
            kept.append(line[start : colon + 1] + f" {UNAVAILABLE} ")
    remaining = "".join(kept).strip()
    return remaining or None


def _title_cited(title: str, normalized_answer: str) -> bool:
    core = normalize(re.sub(r"\s*\([^)]*\)", "", title)).strip()
    return len(core) >= 4 and core in normalized_answer


def verify_answer(
    answer: str,
    evidence: Evidence,
    candidate_sources: dict[str, dict],
    *,
    question: str = "",
    redaction_note: str = REDACTION_NOTE,
) -> GroundingReport:
    """Strips every numerical value the evidence cannot back, and narrows
    `candidate_sources` (document_id -> source dict, everything retrieval
    surfaced) down to the documents the final answer actually used."""
    used_doc_ids: set[str] = set()
    unsupported: list[str] = []
    out_lines: list[str] = []

    header_cells: list[str] | None = None  # first row of the current Markdown table
    for line in answer.split("\n"):
        is_table_row = line.strip().startswith("|")
        is_header = is_table_row and header_cells is None
        if is_header:
            header_cells = [c.strip() for c in line.strip().strip("|").split("|")]
        elif not is_table_row:
            header_cells = None
        bad: list[_Number] = []
        for number in _find_numbers(line):
            if number.is_small_bare_int() or _is_list_marker(line, number) or _is_reference(line, number):
                continue
            date_backing = _date_component_backing(line, number, evidence)
            if date_backing:
                used_doc_ids |= date_backing
                continue
            required_year = None
            if is_table_row and not is_header and header_cells:
                column = line[: number.start].count("|") - 1
                if 0 < column < len(header_cells):
                    years = requested_years(header_cells[column])
                    required_year = years[0] if len(years) == 1 else None
            traced, backing = _trace_number(number, _line_label(line, number.start), evidence, required_year)
            if traced:
                used_doc_ids |= backing
            else:
                bad.append(number)
        if not bad:
            out_lines.append(line)
            continue
        unsupported.extend(line[n.start : n.end].strip() for n in bad)
        redacted = _redact_line(line, bad)
        if redacted is not None:
            out_lines.append(redacted)

    final = "\n".join(out_lines).strip()
    if unsupported:
        logger.warning("grounding: removed untraceable values %s from answer", unsupported)
        if not _find_numbers(final) and not re.search(r"[A-Za-zÀ-ÿ]{3,}", final.replace(UNAVAILABLE, "")):
            final = NO_DATA_ANSWER
        else:
            final = f"{final}\n\n{redaction_note}"

    normalized_answer = normalize(final)
    answer_stems = _stems(final, _OVERLAP_STOPWORDS, min_len=5) - _stems(question, _OVERLAP_STOPWORDS, min_len=5)
    if not final.startswith(NO_DATA_ANSWER):
        for item in evidence.items:
            if not item.document_id or item.document_id in used_doc_ids:
                continue
            if (item.title and _title_cited(item.title, normalized_answer)) or len(
                answer_stems & item.stems
            ) >= _MIN_OVERLAP_TOKENS:
                used_doc_ids.add(item.document_id)

    sources = [src for doc_id, src in candidate_sources.items() if doc_id in used_doc_ids]
    return GroundingReport(answer=final, sources=sources, unsupported=unsupported)
