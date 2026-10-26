"""Figures asked "of the project" / "from my documents", and web requests.

"Le budget du projet" is NOT a line of the company financial table
(financial_records: Budget IT / Marketing / RH of the company itself).
Such a request is answered from the documents of the current scope only,
and every value keeps where it was read (file, path, page, sentence):

    find_document_amounts (MCP tool: EVERY page of the authorized documents
    of the scope - ACL + folder scope applied by the server, no alert)
      -> amounts with a currency, in a sentence about a budget / amount /
         cost, with the year written in that sentence
      -> grouped by document: two documents may be two different projects,
         so values from different documents are NEVER put on one curve
      -> 2+ distinct years in one document : a curve, one source per point
         1 value                           : a value card, with its source
         values in several documents       : listed with their sources, the
                                             user says which project
         nothing                           : said so - no chart, and no
                                             company-table figure instead

The extraction is deterministic (no model call): a number is only shown
if it is written in a document the user may read, next to its source.

Web: is_web_request detects an explicit request for Google / the internet;
the web search itself (off by default, real provider calls only) lives in
app/websearch and app/agent/web_answer.py.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.rag.normalize import normalize

# --- web ---------------------------------------------------------------------------------

_WEB_RE = re.compile(
    r"\b(google|bing|duckduckgo|chatgpt)\b"
    r"|\b(sur|via|dans|depuis|avec|par|selon|d ?apres)\s+(l ?)?(internet|le web|le net|web)\b"
    r"|\b(recherche|cherche|chercher|rechercher|trouve|trouver|consulte|consulter)\s+(en ligne|sur internet|sur le web)\b"
)

def _norm(text: str) -> str:
    """Lower case, no accents, apostrophes (' and ’) as spaces."""
    return normalize(text or "").replace("'", " ").replace("’", " ")


def is_web_request(text: str) -> bool:
    """An explicit request to use Google / the internet."""
    return bool(_WEB_RE.search(_norm(text)))


# --- "of the project" / "from my documents" ---------------------------------------------

_DOCUMENT_SCOPE_RE = re.compile(
    r"\b(du|de ce|de cet|de notre|de mon|de ma|ce|cet|le|la|cette)\s+"
    r"(projet|marche|contrat|chantier|affaire|consultation|lot|client|offre|appel d ?offres?)\b"
    r"|\b(mes|les|ces|nos|des)\s+(documents|fichiers|dossiers|pieces)\b"
    r"|\bdans (mes|les|ces|nos) (documents|fichiers|dossiers)\b"
)


def is_document_metric_request(text: str) -> bool:
    """The figure belongs to a project / is to be read in the documents."""
    return bool(_DOCUMENT_SCOPE_RE.search(_norm(text)))



_KEYWORD_RE = re.compile(r"\b(budget|budgets|montant|montants|cout|couts|enveloppe|estimation|estimatif|estimative|prix total|valeur)\b")
_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[ .  ]\d{3})+|\d+)(?:,(\d{1,3}))?\s*"
    r"(millions?|milliards?|mille|k|m)?\s*(?:de\s+|d['’]\s*)?"
    r"(€|eur(?:os?)?\b|dt\b|tnd\b|dinars?\b|mdt\b|kdt\b|md\b)",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(19[89]\d|20\d{2})\b")
# A dot followed by a digit is inside a number ("1.200.000 DT"), not a sentence end.
_SENTENCE_RE = re.compile(r"(?:[^.;\n]|\.(?=\d))+(?:[.;\n]|$)")


@dataclass
class AmountMention:
    value: float
    currency: str  # "€" | "DT"
    year: str | None
    sentence: str


@dataclass
class SourcedPoint:
    period: str
    value: float
    currency: str
    source: dict  # {"kind": "local", "title", "path", "page", "excerpt"}


@dataclass
class DocumentMetricOutcome:
    kind: str  # "chart" | "kpi" | "candidates" | "undated" | "none"
    message: str
    points: list[SourcedPoint] = field(default_factory=list)
    candidates: list[SourcedPoint] = field(default_factory=list)
    title: str = ""


def _currency(raw: str) -> str:
    raw = raw.lower()
    return "€" if raw.startswith(("€", "eur")) else "DT"


def _scale(unit: str | None, currency_raw: str) -> float:
    unit = (unit or "").lower()
    currency_raw = currency_raw.lower()
    if unit.startswith("milliard"):
        return 1e9
    if unit.startswith("million") or unit == "m" or currency_raw in ("mdt", "md"):
        return 1e6
    if unit in ("k", "mille") or currency_raw == "kdt":
        return 1e3
    return 1.0


def extract_amounts(text: str) -> list[AmountMention]:
    """Amounts with a currency in sentences about a budget / an amount /
    a cost; the year is the one written in the same sentence (closest to
    the amount), or None."""
    mentions = []
    for sentence in _SENTENCE_RE.findall(text or ""):
        if not _KEYWORD_RE.search(normalize(sentence)):
            continue
        years = [(m.start(), m.group(1)) for m in _YEAR_RE.finditer(sentence)]
        for match in _AMOUNT_RE.finditer(sentence):
            integer = re.sub(r"[ .  ]", "", match.group(1))
            if len(integer) == 4 and _YEAR_RE.fullmatch(integer) and not match.group(3):
                continue  # "2025 DT"? a year, not an amount
            value = float(f"{integer}.{match.group(2) or 0}") * _scale(match.group(3), match.group(4))
            if value <= 0:
                continue
            year = min(years, key=lambda y: abs(y[0] - match.start()))[1] if years else None
            mentions.append(AmountMention(value=value, currency=_currency(match.group(4)), year=year,
                                          sentence=" ".join(sentence.split())[:300]))
    return mentions


def mentions_from_search_rows(rows: list[dict]) -> list[dict]:
    """search_documents rows -> amount mentions (same shape as the
    find_document_amounts tool returns)."""
    mentions = []
    for row in rows:
        for m in extract_amounts(row.get("excerpt") or ""):
            mentions.append({
                "document_id": str(row.get("document_id")), "title": row.get("title"),
                "relative_path": row.get("relative_path"), "page": row.get("page"), "section": row.get("section"),
                "value": m.value, "currency": m.currency, "year": m.year, "sentence": m.sentence,
            })
    return mentions


MAX_LISTED = 20


def _point(mention: dict) -> SourcedPoint:
    return SourcedPoint(
        period=mention.get("year") or "",
        value=float(mention["value"]),
        currency=mention["currency"],
        source={
            "kind": "local", "document_id": mention.get("document_id"), "title": mention.get("title"),
            "path": mention.get("relative_path"), "page": mention.get("page"), "section": mention.get("section"),
            "excerpt": mention.get("sentence") or "",
        },
    )


def _examined(scope_name: str, documents_in_scope: int | None, pages_read: int | None) -> str:
    if documents_in_scope is None:
        return f"les documents de « {scope_name} »"
    return (
        f"les {documents_in_scope} documents de « {scope_name} » ({pages_read or 0} pages parlant de budget, "
        "de montant ou de coût avec une devise)"
    )


def _table(points: list[SourcedPoint], with_year: bool) -> str:
    head = "| Année | Montant | Document | Page | Extrait |" if with_year else "| Montant | Document | Page | Extrait |"
    sep = "|---|---:|---|---|---|" if with_year else "|---:|---|---|---|"
    lines = [head, sep]
    for p in points[:MAX_LISTED]:
        cells = [_fmt(p), p.source["path"] or p.source["title"] or "document", str(p.source["page"] or "—"),
                 (p.source["excerpt"] or "").replace("|", "/")[:140]]
        lines.append("| " + " | ".join(([p.period] if with_year else []) + cells) + " |")
    if len(points) > MAX_LISTED:
        lines.append(f"\n… et {len(points) - MAX_LISTED} autres montants.")
    return "\n".join(lines)


def plan_document_metric(
    mentions: list[dict], scope_name: str, *, documents_in_scope: int | None = None, pages_read: int | None = None
) -> DocumentMetricOutcome:
    """`mentions`: amounts read in the authorized documents of the scope
    (find_document_amounts, or mentions_from_search_rows), each with its
    document, path, page and sentence. Never a value from anywhere else."""
    examined = _examined(scope_name, documents_in_scope, pages_read)
    points = [_point(m) for m in mentions]
    if not points:
        return DocumentMetricOutcome(
            kind="none",
            message=(
                f"J'ai examiné {examined} : aucun montant de budget n'y est écrit. Aucun graphique fiable ne peut "
                "donc être produit.\n\n"
                "Les budgets internes de l'entreprise (table financière) ne concernent pas un projet : ils ne sont "
                "pas utilisés ici. Indiquez le document qui contient le budget du projet "
                "(par exemple l'offre financière ou le DAO), ou donnez les valeurs à représenter."
            ),
        )

    dated = [p for p in points if p.period]
    if not dated:
        return DocumentMetricOutcome(
            kind="undated", candidates=points,
            message=(
                f"J'ai examiné {examined} : {len(points)} montant(s) y figurent, mais aucun n'est associé à une "
                "année dans la même phrase. Ils ne peuvent pas être placés sur une courbe d'évolution, et je ne "
                "sais pas lequel est le budget du projet :\n\n" + _table(points, with_year=False)
                + "\n\nAucun graphique fiable ne peut donc être produit. Précisez le document (ou le projet) à utiliser."
            ),
        )

    by_document: dict[str, list[SourcedPoint]] = defaultdict(list)
    for p in dated:
        doc = p.source["document_id"] or p.source["path"]
        if not any(q.period == p.period and q.value == p.value for q in by_document[doc]):
            by_document[doc].append(p)

    # One document = one project context: a curve only inside one document,
    # with one value per year and one currency.
    if len(by_document) == 1:
        doc_points = next(iter(by_document.values()))
        currencies = {p.currency for p in doc_points}
        periods = {p.period for p in doc_points}
        path = doc_points[0].source["path"] or doc_points[0].source["title"]
        if len(currencies) == 1 and len(periods) == len(doc_points) and len(periods) >= 2:
            ordered = sorted(doc_points, key=lambda p: p.period)
            return DocumentMetricOutcome(
                kind="chart", points=ordered, title=f"Budget – {path}",
                message=(
                    f"Évolution tracée à partir de « {path} » uniquement ({len(ordered)} années : "
                    f"{', '.join(p.period for p in ordered)}). Chaque valeur est reliée à sa page source."
                ),
            )
        if len(doc_points) == 1:
            point = doc_points[0]
            return DocumentMetricOutcome(
                kind="kpi", points=[point], title=f"Montant – {path}",
                message=(
                    f"Je n'ai trouvé qu'une seule valeur datée dans {examined} : {_fmt(point)} en {point.period}, "
                    f"dans « {path} »{_page(point)}. Il faut au moins deux années comparables pour tracer une "
                    "évolution : voici la valeur, avec sa source."
                ),
            )

    listed = sorted((p for ps in by_document.values() for p in ps), key=lambda p: (p.source["path"] or "", p.period))
    reason = (
        f"Ces montants proviennent de {len(by_document)} documents différents, qui peuvent concerner des projets "
        "différents : je ne les combine pas sur une même courbe."
        if len(by_document) > 1
        else "Ce document contient plusieurs montants pour une même année ou dans des devises différentes : "
        "je ne peux pas savoir lequel est le budget du projet."
    )
    return DocumentMetricOutcome(
        kind="candidates", candidates=listed,
        message=(
            f"Montants datés trouvés dans {examined} :\n\n" + _table(listed, with_year=True)
            + f"\n\n{reason} Précisez le document (ou le projet) à utiliser."
        ),
    )


def _fmt(point: SourcedPoint) -> str:
    number = f"{point.value:,.0f}".replace(",", " ") if point.value.is_integer() else f"{point.value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{number} {point.currency}"


def _page(point: SourcedPoint) -> str:
    return f", page {point.source['page']}" if point.source.get("page") else ""
