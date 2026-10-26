"""Where to look: LOCAL, WEB, HYBRID or WEB_DISABLED - and what is sent out.

    LOCAL         the user's documents only (default: most questions)
    WEB           internet: explicitly asked ("cherche sur internet",
                  "d'après Google"), or current / official information
                  ("dernières évolutions de la réglementation")
    HYBRID        both, compared: current/official information about the
                  user's own material ("compare ma consultation avec les
                  dernières exigences officielles")
    WEB_DISABLED  the question needs the web but it is off (administrator)
                  or not allowed automatically (trigger "manual")

Decided automatically for each question - the user picks nothing. Never a
web search for every question. Trigger "manual" (Admin): the web only when
the question asks for it in words ("cherche sur internet..."). The web is
only "available" when enabled AND configured (see web_unavailable_reason).

build_web_query keeps only the subject of the user's own question: no
conversation history, no document text, no retrieved passage ever leaves
the server through a search query.
"""

import re
from dataclasses import dataclass

from app.agent.document_metrics import is_web_request
from app.rag.normalize import normalize, normalize_with_map

LOCAL, WEB, HYBRID, WEB_DISABLED = "LOCAL", "WEB", "HYBRID", "WEB_DISABLED"
DIRECT_LOCAL = "DIRECT_LOCAL"
# "local": internal, the documents-only answer given when the web cannot be used.
USER_MODES = ("auto", "local")

_CURRENT_RE = re.compile(
    r"\b(dernieres?|derniers?|recente?s?|recemment|actuel(le)?s?|actuellement|aujourd hui|en ce moment"
    r"|cette annee|en vigueur|nouvelles? (loi|lois|reglementation|norme|normes|exigences?)"
    r"|reglementation|legislation|loi|lois|decret|arrete|journal officiel|officiel(le)?s?|actualite"
    r"|prix du marche|cours (du|de la|des)|taux de change|site officiel)\b"
)
_LOCAL_RE = re.compile(
    r"\b(ma|mon|mes|notre|nos|cette|ce|cet|ces)\s+(consultation|document|documents|fichier|fichiers|dossier|projet"
    r"|offre|cahier|cctp|dao|rapport|contrat|marche|fournisseur|client|devis|pli)\b"
    r"|\bpage \d+\b|\bdans (mes|les|nos) (documents|fichiers|dossiers)\b"
)
_MISSING_RE = re.compile(r"\b(manquant(e|es|s)?|completer|complete|externes?|publique?s?|en ligne)\b")


@dataclass
class SearchDecision:
    mode: str
    needs_web: bool  # the question asks for internet / current information
    explicit: bool  # asked in words ("cherche sur internet", "d'après Google")
    reason: str


def _norm(text: str) -> str:
    return normalize(text or "").replace("'", " ").replace("’", " ")


def decide_search_mode(message: str, requested: str = "auto", *, enabled: bool, trigger: str = "auto") -> SearchDecision:
    requested = requested if requested in USER_MODES else "auto"
    if requested == "local":
        return SearchDecision(LOCAL, False, True, "réponse à partir des documents uniquement")
    text = _norm(message)
    asked_in_words = is_web_request(message)
    current = bool(_CURRENT_RE.search(text))
    local_refs = bool(_LOCAL_RE.search(text))
    missing = bool(_MISSING_RE.search(text))

    if asked_in_words:
        mode, explicit, reason = (HYBRID if local_refs else WEB), True, "recherche Internet demandée explicitement"
    elif (current or missing) and local_refs:
        mode, explicit, reason = HYBRID, False, "information actuelle ou externe sur vos propres documents"
    elif current:
        mode, explicit, reason = WEB, False, "information actuelle ou officielle"
    else:
        if local_refs:
            return SearchDecision(LOCAL, False, False, "question sur vos documents")
        return SearchDecision(DIRECT_LOCAL, False, False, "question générale ou tâche directe")

    if not enabled:
        return SearchDecision(WEB_DISABLED, True, explicit, "recherche Web désactivée par l'administrateur")
    if trigger == "manual" and not explicit:
        return SearchDecision(DIRECT_LOCAL, True, False, "recherche Web seulement sur demande explicite (réglage administrateur)")
    return SearchDecision(mode, True, explicit, reason)


_STRIP_RES = [
    re.compile(r"\b(cherche|recherche|rechercher|chercher|trouve|trouver|regarde|verifie|consulte)\s+(sur|dans|via)\s+"
               r"(google|internet|le web|le net|bing)\b", re.I),
    re.compile(r"\b(d ?apres|selon|via|sur|avec)\s+(google|internet|le web|le net|bing)\b", re.I),
    re.compile(r"\b(google|internet|en ligne)\b", re.I),
    re.compile(r"\b(tu peux|tu peut|peux tu|pourrais tu|pouvez vous|est ce que|svp|stp|s il te plait|s il vous plait|merci"
               r"|je veux|je voudrais|j aimerais|donne moi|dis moi|explique moi)\b", re.I),
    re.compile(r"\b(compare|comparer)\s+(les?|la|des)?\s*\w*\s*(de|dans)\s+(ma|mon|mes|notre|nos|cette|ce|ces)\s+\w+\s+avec\b", re.I),
    re.compile(r"\b(de|dans|avec|pour)\s+(ma|mon|mes|notre|nos|cette|ce|cet|ces)\s+(consultation|document|documents"
               r"|fichier|fichiers|dossier|projet|offre|cahier|cctp|dao|rapport|contrat)\b", re.I),
]


def build_web_query(message: str, max_chars: int = 200) -> str:
    """The subject of the question, nothing else: accents kept for the
    search engine, polite / tool words and references to "my documents"
    removed. Derived from THIS message only."""
    text = (message or "").replace("’", " ").replace("'", " ")
    plain, index_map = normalize_with_map(text)
    # Spans are found on the accent-free form, then blanked in the original.
    keep = list(text)
    for pattern in _STRIP_RES:
        for match in pattern.finditer(plain):
            for i in range(match.start(), match.end()):
                if i < len(index_map):
                    keep[index_map[i]] = " "
    text = "".join(keep)
    text = re.sub(r"[?!«»\"]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,.;:-'")
    words = text.split()
    return " ".join(words[:25])[:max_chars]


# Words that say nothing about WHAT to look for: a query made only of them
# (plus "ce fournisseur", "ce projet"...) is not sent - the subject would
# have to come from the documents, and document content never leaves the
# server automatically. The user is asked to name it.
_GENERIC = set(
    """
    trouve trouver cherche chercher recherche rechercher information informations info infos donnee donnees
    manquante manquantes manquant manquants complete completer complementaires concernant sur propos les des
    une un le la de du et ou a au aux en pour avec quelles quels quelle quel sont est c ca cela ceci
    gerer gere gerre faire fais utiliser utilise acces acceder peux peut pouvez possible capable
    plus autres autre tout tous toutes details detail ce cet cette ces fournisseur client projet
    """.split()
)


def query_is_specific(query: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", _norm(query)) if len(w) >= 2]
    return any(w not in _GENERIC for w in words)
