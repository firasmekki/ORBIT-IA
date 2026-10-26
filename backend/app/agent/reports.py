"""Document reports: deterministic pieces of the two report modes.

Mode 1 - ONE document ("rapport / résumé de Architecture.pdf"): the
document is identified by its stable id (never by its name alone), read IN
FULL page by page - long documents in batches whose notes are then merged
(map-reduce) - and the model may use nothing else.

Mode 2 - SEVERAL documents ("synthèse du dossier noveau consultation",
"compare les cahiers des charges"): the most relevant documents of the
chosen scope are read one by one, then merged by theme; the answer always
states how many documents were really read out of how many exist.

Everything here is plain Python (no I/O): intent detection, the resolution
of "au niveau noveau consultation" / "le 2" against a list of homonyms,
page batching and the prompts. The orchestrator does the tool calls.
"""

import re
from dataclasses import dataclass

from app.agent.local_files import _FILENAME_RE, _LOCAL_FILE_TRIGGER_RE
from app.rag.normalize import normalize

# --- intents --------------------------------------------------------------------------------------

_REPORT_NOUN_RE = re.compile(r"\b(?:rapport|synth[èe]se|bilan|compte[- ]rendu)\b", re.IGNORECASE)
# A comparison is a multi-document report only when it is about documents
# ("compare les cahiers des charges"), not about figures ("compare 2025 et
# 2026" stays a grounded question on the data).
_COMPARE_RE = re.compile(r"\b(?:comparaison|comparer|compare|comparatif)\b", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"\b(?:r[ée]sum[ée]|r[ée]sumer|r[ée]sume)\b", re.IGNORECASE)
# "résume" alone is a normal question ("résume la politique RH"); it becomes
# a multi-document report only about a set of documents.
_COLLECTION_RE = re.compile(
    r"\b(?:dossier|r[ée]pertoire|documents|fichiers|tous\s+les|toutes\s+les|ensemble\s+des|cahiers?\s+des\s+charges|offres)\b",
    re.IGNORECASE,
)
# Words that point at a folder in the sentence ("au niveau X", "dans le dossier X").
_FOLDER_CUE_RE = re.compile(r"\b(?:dossier|r[ée]pertoire|niveau|dans|depuis|sous)\b", re.IGNORECASE)


@dataclass(frozen=True)
class DocumentRequest:
    filename_hint: str
    wants_report: bool


def detect_document_request(message: str) -> DocumentRequest | None:
    """"rapport / résumé / lis / analyse <fichier.ext>" - one named file."""
    match = _FILENAME_RE.search(message)
    if not match:
        return None
    wants_report = bool(_REPORT_NOUN_RE.search(message) or _SUMMARY_RE.search(message))
    if not wants_report and not _LOCAL_FILE_TRIGGER_RE.search(message):
        return None
    hint = match.group(1).strip(" \t\n\r.,;:!?»«\"'")
    return DocumentRequest(filename_hint=hint, wants_report=wants_report) if hint else None


def detect_multi_report(message: str) -> bool:
    """A report/synthesis/comparison over several documents (no file named)."""
    if _FILENAME_RE.search(message):
        return False
    if _REPORT_NOUN_RE.search(message):
        return True
    return bool((_SUMMARY_RE.search(message) or _COMPARE_RE.search(message)) and _COLLECTION_RE.search(message))


def wants_full_report(message: str) -> bool:
    return bool(_REPORT_NOUN_RE.search(message) or _SUMMARY_RE.search(message))


# --- folders named in the question --------------------------------------------------------------------


# Folder names that are ordinary words of a request: never a folder
# reference on their own ("synthèse des documents du dossier X").
_GENERIC_FOLDER_NAMES = frozenset({
    "dossier", "dossiers", "document", "documents", "fichier", "fichiers", "projet", "projets", "data", "donnees",
    "docs", "doc", "divers", "autres", "nouveau dossier", "rapport", "rapports", "synthese", "annexe", "annexes",
    "niveau", "dans", "tous", "toutes",
})


def _contains_phrase(haystack: str, phrase: str) -> bool:
    return bool(phrase) and re.search(r"(?<![\w])" + re.escape(phrase) + r"(?![\w])", haystack) is not None


def folders_named_in(message: str, folders: list[str], *, require_cue: bool = True) -> list[str]:
    """Folders ("noveau consultation/", "SAGEMCOM SME/RE__PSSI/") whose name
    appears in the message - accent/case-insensitive, whole words, the most
    specific (longest) names first. Names under 3 characters are ignored
    ("3S" is too easily a word). Without a cue word ("dossier", "dans", "au
    niveau"...) nothing is matched unless require_cue is False (a short
    reply to "which one?" is itself the cue)."""
    text = normalize(message)
    if require_cue and not _FOLDER_CUE_RE.search(message):
        return []
    # Ranked by how much of the sentence the name covers - not by path
    # length: "dans le dossier noveau consultation" must pick
    # "noveau consultation/", never "Delice bouzid/Dossier/" because the
    # word "dossier" happens to be a folder name too.
    found: list[tuple[int, int, str]] = []
    for folder in folders:
        parts = [p for p in folder.strip("/").split("/") if p]
        if not parts:
            continue
        full, last = normalize("/".join(parts)), normalize(parts[-1])
        if len(full) >= 3 and full not in _GENERIC_FOLDER_NAMES and _contains_phrase(text, full):
            found.append((len(full), -len(parts), folder))
        elif len(last) >= 3 and last not in _GENERIC_FOLDER_NAMES and _contains_phrase(text, last):
            found.append((len(last), -len(parts), folder))
    found.sort(key=lambda item: (-item[0], -item[1]))
    return [folder for _, _, folder in found]


_ORDINALS = {
    "premier": 1, "premiere": 1, "1er": 1, "deuxieme": 2, "second": 2, "seconde": 2, "troisieme": 3,
    "quatrieme": 4, "cinquieme": 5, "sixieme": 6, "septieme": 7, "huitieme": 8, "neuvieme": 9, "dixieme": 10,
    "dernier": -1, "derniere": -1,
}


def resolve_choice(message: str, candidates: list[dict], *, folders_only: bool = False) -> list[dict]:
    """The candidate(s) a reply to "lequel ?" designates: a number ("2",
    "le 2", "deuxième"), a path, or a folder ("au niveau noveau
    consultation"). Returns every candidate still compatible - one means
    resolved, several means narrowed, none means "not an answer".
    folders_only: the message is a full request ("rapport sur X.pdf dans
    noveau consultation"), not a reply - numbers/ordinals in it mean nothing."""
    text = normalize(message).strip()
    # A full relative path written in the request designates its document,
    # whatever words surround it ("rapport pour noveau consultation/Architecture.pdf").
    exact = [c for c in candidates if c.get("relative_path") and "/" in c["relative_path"]
             and normalize(c["relative_path"]) in text]
    if exact:
        longest = max(len(c["relative_path"]) for c in exact)
        return [c for c in exact if len(c["relative_path"]) == longest]
    if not folders_only:
        number = re.fullmatch(r"(?:le|la|n°|numero|no|choix)?\s*(\d{1,2})\s*[.)]?", text)
        if number:
            n = int(number.group(1))
            return [candidates[n - 1]] if 1 <= n <= len(candidates) else []
        for word, n in _ORDINALS.items():
            if _contains_phrase(text, word):
                if n == -1:
                    return candidates[-1:]
                return [candidates[n - 1]] if n <= len(candidates) else []
    elif not _FOLDER_CUE_RE.search(message):
        return []
    # A path or folder: score each candidate by how much of its folder path
    # the reply names; keep the best ones.
    scored = []
    for candidate in candidates:
        path = candidate.get("relative_path") or ""
        folders = path.split("/")[:-1]
        score = 0
        for depth in range(len(folders)):
            name = normalize(folders[depth])
            if len(name) >= 3 and _contains_phrase(text, name):
                score += 1
        if path and normalize(path) in text:
            score += 10
        if score:
            scored.append((score, candidate))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [candidate for score, candidate in scored if score == best]


def match_in_context(hint: str, context: dict | None) -> dict | None:
    """The document of the previous answer that the user names again
    ("rapport sur Architecture.pdf" right after a search that found
    noveau consultation/Architecture.pdf): reuse its stable id - only when
    exactly one document of that context carries that name."""
    if not context:
        return None
    docs = list(context.get("documents") or []) + list(context.get("candidates") or [])
    if context.get("kind") == "document_focus" and context.get("document_id"):
        docs.append(context)
    wanted = normalize(hint)
    matches = {}
    for doc in docs:
        names = {normalize(doc.get("title") or "")}
        path = doc.get("relative_path") or ""
        if path:
            names.update({normalize(path), normalize(path.rsplit("/", 1)[-1])})
        if wanted in names and doc.get("document_id"):
            matches[doc["document_id"]] = doc
    return next(iter(matches.values())) if len(matches) == 1 else None


# --- reading a document in pages ------------------------------------------------------------------------


def page_label(page: dict) -> str:
    if page.get("page") is not None:
        return f"p. {page['page']}"
    if page.get("section"):
        return f"section « {page['section']} »"
    return "texte"


def tagged_pages(pages: list[dict]) -> list[tuple[str, str]]:
    """[(label, text)] - the text of each page prefixed by its reference."""
    return [(page_label(p), f"[{page_label(p)}]\n{(p.get('text') or '').strip()}") for p in pages if (p.get("text") or "").strip()]


def batch_pages(pages: list[tuple[str, str]], max_chars: int) -> list[list[tuple[str, str]]]:
    """Consecutive pages grouped up to max_chars; a single longer page is cut."""
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    size = 0
    for label, text in pages:
        while len(text) > max_chars:
            if current:
                batches.append(current)
                current, size = [], 0
            batches.append([(label, text[:max_chars])])
            text = f"[{label}, suite]\n" + text[max_chars:]
        if current and size + len(text) > max_chars:
            batches.append(current)
            current, size = [], 0
        current.append((label, text))
        size += len(text)
    if current:
        batches.append(current)
    return batches


def missing_pdf_pages(pages: list[dict]) -> list[int]:
    """Page numbers absent between 1 and the last page with text: pages the
    extraction found no text on (scans, blank pages)."""
    numbers = sorted({p["page"] for p in pages if p.get("page") is not None})
    if not numbers:
        return []
    present = set(numbers)
    return [n for n in range(1, numbers[-1] + 1) if n not in present]


def relevant_excerpt(pages: list[dict], query: str, max_chars: int) -> tuple[str, list[str]]:
    """The pages of one document most related to the question (shared words),
    in reading order, up to max_chars - plus the labels used."""
    words = {w for w in re.findall(r"\w{4,}", normalize(query))}
    tagged = tagged_pages(pages)
    scored = sorted(
        range(len(tagged)),
        key=lambda i: (-sum(1 for w in words if w in normalize(tagged[i][1])), i),
    )
    chosen, size = [], 0
    for i in scored:
        text = tagged[i][1]
        if size and size + len(text) > max_chars:
            continue
        chosen.append(i)
        size += min(len(text), max_chars)
        if size >= max_chars:
            break
    chosen.sort()
    return "\n\n".join(tagged[i][1][:max_chars] for i in chosen), [tagged[i][0] for i in chosen]


# --- prompts --------------------------------------------------------------------------------------------

SINGLE_REPORT_RULES = (
    "Tu rédiges un rapport fidèle sur UN SEUL document : « {label} ».\n"
    "Règles impératives :\n"
    "- Utilise UNIQUEMENT le contenu de ce document fourni ci-dessous. N'ajoute rien venant d'un autre document "
    "ni de tes connaissances générales.\n"
    "- Si une information n'y figure pas, écris « Non précisé dans le document ». N'invente jamais.\n"
    "- Cite la page ou la section entre parenthèses, en reprenant les repères du texte : (p. 3), (section « Budget »).\n"
    "- Réponds en français, en Markdown propre (titres #, ##, listes -, tableaux |), sans bloc de code.\n"
    "Structure exacte :\n"
    "# Rapport : <titre ou objet du document>\n"
    "**Document analysé :** {label}\n"
    "## Objectif du document\n"
    "## Synthèse exécutive\n"
    "## Principales sections\n"
    "## Exigences, données et conclusions\n"
    "## Tableaux utiles  (un tableau Markdown seulement si le document contient des données chiffrées ou tabulaires ; "
    "sinon écris « Aucun tableau exploitable dans le document. »)\n"
    "## Références  (liste des pages/sections utilisées)\n"
    "## Limites de lecture  (reprends les informations de lecture ci-dessous, sans en ajouter)\n"
)

ANSWER_FROM_DOCUMENT_RULES = (
    "Réponds à la demande en te basant uniquement sur le document « {label} » fourni ci-dessous. "
    "Cite les pages ou sections, par exemple (p. 3). Si l'information n'y est pas, dis-le. "
    "Réponds en français, en Markdown propre."
)

MAP_NOTES_RULES = (
    "Voici une partie ({labels}) du document « {label} ». Prends des notes de lecture fidèles et concises, en "
    "puces Markdown : objet, titres de sections, exigences, données chiffrées, conclusions, contenu des tableaux. "
    "Chaque puce se termine par sa référence, par exemple (p. 4). Ne résume pas au point de perdre une exigence "
    "ou un chiffre. N'invente rien, n'ajoute aucun commentaire."
)

DOC_NOTES_RULES = (
    "Document {index} : « {label} » (extraits : {labels}). En rapport avec la demande « {question} », prends des "
    "notes fidèles en puces Markdown (objet du document, exigences, données chiffrées, conclusions), chaque puce "
    "avec sa référence (p. N). 200 mots au maximum. Si l'extrait ne traite pas du sujet, écris seulement "
    "« Hors sujet pour cette demande. ». N'invente rien."
)

MULTI_REPORT_RULES = (
    "Tu rédiges une synthèse multi-documents à partir des notes de lecture ci-dessous ({count} document(s) "
    "réellement consulté(s)). Règles impératives :\n"
    "- N'utilise que ces notes. Cite chaque information avec son document et sa page : (Doc 2, p. 4).\n"
    "- Ne prétends jamais avoir lu d'autres documents que ceux listés.\n"
    "- Réponds en français, en Markdown propre (titres, listes, tableaux), sans bloc de code.\n"
    "- Sois concis : 800 mots au maximum, un tableau des différences de 6 lignes au maximum, et termine "
    "toujours par les sections Contradictions et Sources.\n"
    "Structure exacte :\n"
    "# Synthèse : <sujet>\n"
    "## Vue d'ensemble\n"
    "## Analyse par thème  (regroupe les informations par thème, un sous-titre ### par thème)\n"
    "## Exigences communes\n"
    "## Différences entre documents\n"
    "## Contradictions  (si aucune : « Aucune contradiction relevée dans les extraits consultés. »)\n"
    "## Sources  (tableau : N° | Document | Pages citées)\n"
)
