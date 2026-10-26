"""Which kind of answer a question needs - decided once, before any tool runs.

    DIRECT_LOCAL      greeting, thanks, small talk, writing / rewriting /
                      translating a text given in the message, a simple
                      calculation: the local model answers (greetings: a fixed
                      reply, no model at all) - no document, no web, no
                      financial table, no vision
    GENERAL_KNOWLEDGE a general question that does not depend on the
                      company's documents ("explique la différence entre
                      courant alternatif et continu"): the local model
                      answers from its own knowledge, said as such - never
                      "introuvable dans les documents"
    LOCAL_DOCUMENTS   about the company / the project / a file: the existing
                      document pipeline (RAG, reports, charts, sections...)
    WEB_REQUIRED      internet asked in words, or current / regulatory
                      information (app/websearch/intent.py decides that part)
    HYBRID            the user's documents AND current external information

Order (first match wins):
  1. deterministic rules - no model needed, so they hold when the model is
     down: greetings, explicit document requests (every detector the
     pipeline already has), explicit / current web needs, direct tasks,
     general-knowledge questions ("explique", "qu'est-ce que", "what is",
     "ما هو"...);
  2. the local model as a classifier, its answer constrained to a JSON
     schema and validated - only for what the rules left undecided;
  3. if the classifier is unavailable or gives anything invalid:
     LOCAL_DOCUMENTS (the behaviour ORBIT always had - and if the model is
     down, that path now answers with a plain availability message).

A model error never turns a question into a document search: there is no
fallback that searches documents (see orchestrator._llm_unavailable_result).
"""

import re
from dataclasses import dataclass

from app.agent import reports
from app.agent.chart_dialog import is_variation_request
from app.agent.document_metrics import is_document_metric_request
from app.agent.intent import (
    detect_chart_intent,
    detect_keyword_intent,
    detect_list_documents_intent,
    detect_search_intent,
    detect_section_request,
)
from app.agent.local_files import detect_local_file_intent
from app.agent.llm_client import LLMServiceError
from app.rag.normalize import normalize
from app.websearch.intent import HYBRID as WEB_HYBRID
from app.websearch.intent import WEB, WEB_DISABLED, SearchDecision, build_web_query

DIRECT_LOCAL = "DIRECT_LOCAL"
GENERAL_KNOWLEDGE = "GENERAL_KNOWLEDGE"
LOCAL_DOCUMENTS = "LOCAL_DOCUMENTS"
WEB_REQUIRED = "WEB_REQUIRED"
HYBRID = "HYBRID"
INTENTS = (DIRECT_LOCAL, GENERAL_KNOWLEDGE, LOCAL_DOCUMENTS, WEB_REQUIRED, HYBRID)


@dataclass
class Route:
    intent: str
    needs_local_documents: bool
    needs_web_search: bool
    reason: str
    search_query: str | None = None
    decided_by: str = "rules"  # rules | model | fallback
    reply: str | None = None  # fixed answer (greetings): no model, no tool
    # The question would deserve the web, but the administrator set it to
    # "on explicit request only": answered locally, and said so.
    web_skipped: bool = False

    def as_dict(self) -> dict:
        return {
            "intent": self.intent, "needs_local_documents": self.needs_local_documents,
            "needs_web_search": self.needs_web_search, "reason": self.reason,
            "search_query": self.search_query, "decided_by": self.decided_by,
        }


def _norm(text: str) -> str:
    return " ".join(normalize(text or "").replace("'", " ").replace("’", " ").replace("-", " ").split())


# --- 1a. greetings and small talk (FR, EN, AR, Tunisian) ---------------------------------------------------

_GREETING = (
    r"bonjour|bonsoir|salut|coucou|hello|hi|hey|good morning|good evening|aslema|asslema|3aslema|ahla|ahlan|marhba"
    r"|salam|salem|salam alaykoum|salam alikom|assalamou alaykoum|bonne journee|bonne soiree"
    r"|مرحبا|اهلا|السلام عليكم|سلام|صباح الخير|مساء الخير"
)
_HOW_ARE_YOU = (
    r"comment (vas|allez) tu|comment (vas|allez) vous|comment ca va|ca va|tu vas bien|vous allez bien|how are you"
    r"|how is it going|labes|labess|chnahwalek|chnowa ahwalek|كيف حالك|كيف الحال|لاباس"
)
_THANKS = r"merci( beaucoup| bien| infiniment)?|thanks?( you)?|thank you|yaatik saha|ya3tik saha|chokran|choukran|شكرا|بارك الله فيك"
_BYE = r"au revoir|a bientot|bonne nuit|bye|goodbye|see you|bslema|besslema|مع السلامة|الى اللقاء"
_WHO = r"qui es tu|qui etes vous|tu es qui|c est quoi orbit|who are you|what are you|شكون انت|من انت"
_FILLER = r"\s*(?:,|!|\.|\?|;|:|-)*\s*(?:orbit(?: ai)?|l assistant|assistant|svp|stp|toi|vous)?\s*(?:,|!|\.|\?)*\s*"
_SMALL_TALK_RE = re.compile(
    rf"^(?:(?:{_GREETING}|{_HOW_ARE_YOU}|{_THANKS}|{_BYE}|{_WHO}){_FILLER})+$"
)
_LEADING_GREETING_RE = re.compile(rf"^(?:{_GREETING}){_FILLER}")

REPLY_GREETING = "Bonjour ! Comment puis-je vous aider ?"
REPLY_HOW_ARE_YOU = "Bonjour ! Je vais bien, merci. Comment puis-je vous aider ?"
REPLY_THANKS = "Avec plaisir ! N'hésitez pas si vous avez une autre question."
REPLY_BYE = "Au revoir, et bonne journée !"
REPLY_WHO = (
    "Je suis ORBIT AI, l'assistant local de votre entreprise. Je réponds à partir des documents auxquels vous avez "
    "accès, de mes connaissances générales et, si un administrateur l'a activée, d'une recherche sur Internet."
)
REPLY_GREETING_EN = "Hello! How can I help you?"
REPLY_GREETING_AR = "مرحبًا! كيف يمكنني مساعدتك؟"


def small_talk_reply(message: str) -> str | None:
    """A fixed, safe reply for a message that is only greetings / thanks /
    small talk - None as soon as it carries anything else."""
    text = _norm(message)
    if not text or len(text.split()) > 10 or not _SMALL_TALK_RE.match(text):
        return None
    if re.search(rf"\b(?:{_WHO})\b", text):
        return REPLY_WHO
    if re.search(rf"\b(?:{_HOW_ARE_YOU})\b", text):
        return REPLY_HOW_ARE_YOU
    if re.search(rf"\b(?:{_THANKS})\b", text):
        return REPLY_THANKS
    if re.search(rf"\b(?:{_BYE})\b", text):
        return REPLY_BYE
    if re.search(r"[؀-ۿ]", message):
        return REPLY_GREETING_AR
    if re.search(r"\b(hello|hi|hey|good morning|good evening)\b", text):
        return REPLY_GREETING_EN
    return REPLY_GREETING


def strip_greeting(message: str) -> str:
    """"Bonjour, quel est le budget IT ?" -> "quel est le budget IT ?" (for routing only)."""
    text = (message or "").strip()
    match = _LEADING_GREETING_RE.match(_norm(text))
    if not match:
        return text
    words = len(match.group(0).split())
    return " ".join(text.split()[words:]) or text


# Precise operations of the document pipeline: never sent to the web, even
# with a word like "derniers" ("liste les derniers documents ajoutés").
PIPELINE_REASONS = {
    "suite d'un échange en cours sur vos documents", "fichier local de votre espace de travail", "document nommé",
    "rapport ou synthèse de documents", "recherche d'un terme", "liste de documents", "section d'un document",
    "graphique sur vos données", "recherche dans vos documents", "fichier nommé",
}


# --- 1b. the company's material ---------------------------------------------------------------------------

_COMPANY_RE = re.compile(
    r"\b(ma|mon|mes|notre|nos|cette|ce|cet|ces|le|la|les|du|des|au|aux)\s+"
    r"(consultation|consultations|document|documents|fichier|fichiers|dossier|dossiers|projet|projets|offre|offres"
    r"|cahier des charges|cctp|cctps|dao|rapport|rapports|contrat|contrats|marche|fournisseur|fournisseurs|client|clients"
    r"|devis|facture|factures|pli|plis|chantier|lot|lots|soumission|bordereau|pv|proces verbal|compte rendu|reunion"
    r"|budget|budgets|masse salariale|salaire|salaires|conges|politique|procedure|procedures|equipe|employes|entreprise"
    r"|societe|espace de travail|workspace|base de donnees)\b"
    r"|\b(budget|masse salariale|salaire|conges|resultat net|chiffre d affaires|bilan|grille salariale)\b"
    r"|\b(my|our|this|the)\s+(documents?|files?|folder|contract|report|project|tender|offer|invoice|budget|company|workspace)\b"
    r"|\b(in|from)\s+my\s+(documents?|files?)\b"
    r"|(ملف|ملفات|وثيقة|وثائق|مستند|مستندات|العقد|عقدي|تقرير|الميزانية|مشروع|المشروع|شركتنا)"
)
_FILENAME_RE = re.compile(r"\b[\w\-. ]{1,80}\.(pdf|docx?|xlsx?|xlsm|csv|pptx?|txt|md|odt|ods|png|jpe?g|webp)\b", re.I)


def has_document_signal(message: str, *, pending_context: dict | None = None, workspace_index: bool = False) -> str | None:
    """The reason this question is about the company's material, or None.
    Every detector the document pipeline already uses counts."""
    if pending_context:
        return "suite d'un échange en cours sur vos documents"
    if workspace_index or detect_local_file_intent(message):
        return "fichier local de votre espace de travail"
    checks = (
        (reports.detect_document_request, "document nommé"),
        (reports.detect_multi_report, "rapport ou synthèse de documents"),
        (detect_keyword_intent, "recherche d'un terme"),
        (detect_list_documents_intent, "liste de documents"),
        (detect_section_request, "section d'un document"),
        (detect_chart_intent, "graphique sur vos données"),
        (lambda m: detect_search_intent(m, allow_bare=False), "recherche dans vos documents"),
        (is_document_metric_request, "question sur l'entreprise ou ses documents"),
    )
    for check, reason in checks:
        try:
            if check(message):
                return reason
        except Exception:  # noqa: BLE001 - a detector must never break routing
            continue
    if _FILENAME_RE.search(message):
        return "fichier nommé"
    if is_variation_request(message) and re.search(r"\bbudget|montant|cout|salaire|chiffre\b", _norm(message)):
        return "variation d'une donnée de l'entreprise"
    if _COMPANY_RE.search(_norm(message)):
        return "question sur l'entreprise ou ses documents"
    return None


# --- 1c. direct tasks and general knowledge -----------------------------------------------------------------

_DIRECT_TASK_RE = re.compile(
    r"^(reformule|reformuler|corrige|corriger|traduis|traduire|traduction|resume ce texte|resume le texte suivant"
    r"|ameliore|ameliorer|rends plus|redige|rediger|ecris|ecrire|propose|proposer)\b.*"
    r"\b(texte|phrase|paragraphe|email|e mail|mail|courriel|message|lettre|post|annonce|slogan|titre|introduction"
    r"|conclusion|invitation|reponse)\b"
    r"|^(rewrite|rephrase|translate|correct|proofread|draft|write)\b.*\b(text|sentence|paragraph|email|message|letter|post)\b"
    r"|^(اكتب|ترجم|صحح|أعد صياغة|اعد صياغة)\b"
)
_ARITHMETIC_RE = re.compile(
    r"^(combien (font|fait|ca fait)|calcule[rz]?|calcul de|what is|how much is|احسب)?\s*"
    r"[\d\s.,()+\-*/x×÷%^]+(=|\?)?\s*$"
    r"|^(combien (font|fait)|calcule[rz]?)\s+[\d\s.,()+\-*/x×÷%^]+(\s+(pour cent|pourcent|de|sur)\s+[\d\s.,]+)*\s*\??$"
)
_GENERAL_RE = re.compile(
    r"^(explique|expliquez|explique moi|qu est ce que|qu est ce qu|c est quoi|c quoi|definis|definir|definition"
    r"|donne moi la definition|quelle est la difference|quelles sont les differences|difference entre|comment fonctionne"
    r"|comment marche|pourquoi|a quoi sert|que signifie|que veut dire|quel est le principe|quels sont les avantages"
    r"|quels sont les inconvenients|quels sont les types"
    r"|explain|what is|what are|what s the difference|difference between|how does|how do|why|define|what does"
    r"|اشرح|ما هو|ما هي|ما الفرق|كيف يعمل|لماذا|عرف)\b"
)


def is_direct_task(message: str) -> bool:
    text = _norm(message)
    return bool(_DIRECT_TASK_RE.search(text) or _ARITHMETIC_RE.match(text))


def is_general_question(message: str) -> bool:
    return bool(_GENERAL_RE.search(_norm(message)))


# --- 2. the local model as a classifier ----------------------------------------------------------------------

CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": [DIRECT_LOCAL, GENERAL_KNOWLEDGE, LOCAL_DOCUMENTS, WEB_REQUIRED, HYBRID]},
        "needs_local_documents": {"type": "boolean"},
        "needs_web_search": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 160},
        "search_query": {"type": "string", "maxLength": 120},
    },
    "required": ["intent", "needs_local_documents", "needs_web_search", "reason"],
}
CLASSIFIER_PROMPT = """Tu classes la question d'un salarié d'un bureau d'études (énergie, électricité, industrie) qui \
dispose d'un espace de documents d'entreprise (projets, consultations, contrats, rapports, budgets, données RH).
Choisis une seule intention :
- DIRECT_LOCAL : salutation, remerciement, rédaction ou reformulation d'un texte fourni, calcul simple.
- GENERAL_KNOWLEDGE : connaissance générale, stable, indépendante de l'entreprise (définition, principe technique, \
explication, conseil général).
- LOCAL_DOCUMENTS : concerne l'entreprise, ses projets, clients, fournisseurs, contrats, chiffres, personnes ou \
fichiers - la réponse est dans ses documents.
- WEB_REQUIRED : information publique récente, évolutive ou officielle (actualité, réglementation en vigueur, prix \
actuels) qui ne dépend pas des documents de l'entreprise.
- HYBRID : compare ou complète des éléments de l'entreprise avec des informations publiques récentes.
En cas de doute entre GENERAL_KNOWLEDGE et LOCAL_DOCUMENTS, choisis LOCAL_DOCUMENTS.
search_query : seulement si une recherche Web est utile, 3 à 10 mots, sans nom de personne ni donnée interne.
Réponds uniquement avec l'objet JSON demandé."""


async def classify_with_model(message: str, complete_json, previous_question: str | None = None) -> Route | None:
    """None if the model is unavailable or answers anything invalid."""
    context = f"Question précédente de l'utilisateur : « {previous_question[:200]} »\n" if previous_question else ""
    try:
        answer = await complete_json(
            [
                {"role": "system", "content": CLASSIFIER_PROMPT},
                {"role": "user", "content": f"{context}Question : « {message[:600]} »"},
            ],
            CLASSIFIER_SCHEMA,
            max_tokens=120,
        )
    except LLMServiceError:
        return None
    except Exception:  # noqa: BLE001 - a classifier failure falls back, never breaks the turn
        return None
    if not isinstance(answer, dict) or answer.get("intent") not in INTENTS:
        return None
    intent = answer["intent"]
    needs_docs = bool(answer.get("needs_local_documents")) or intent in (LOCAL_DOCUMENTS, HYBRID)
    needs_web = bool(answer.get("needs_web_search")) or intent in (WEB_REQUIRED, HYBRID)
    query = answer.get("search_query")
    return Route(
        intent=intent, needs_local_documents=needs_docs, needs_web_search=needs_web,
        reason=str(answer.get("reason") or "classification du modèle local")[:160],
        search_query=str(query)[:120] if needs_web and isinstance(query, str) and query.strip() else None,
        decided_by="model",
    )


# --- the decision ----------------------------------------------------------------------------------------------

async def route_question(
    message: str,
    *,
    web_decision: SearchDecision,
    complete_json,
    pending_context: dict | None = None,
    workspace_index: bool = False,
    has_image: bool = False,
    previous_question: str | None = None,
    web_auto: bool = False,
) -> Route:
    """`web_auto`: the administrator enabled the web with the automatic
    trigger - the only case where the classifier may ask for the web."""
    reply = None if has_image else small_talk_reply(message)
    if reply is not None:
        return Route(DIRECT_LOCAL, False, False, "salutation ou échange courtois", reply=reply)

    core = strip_greeting(message)
    web_query = build_web_query(core) if web_decision.needs_web else None
    document_reason = has_document_signal(core, pending_context=pending_context, workspace_index=workspace_index)
    # The web only when app/websearch/intent.py allows it: an explicit request,
    # or current information with the "automatic" trigger. With "on explicit
    # request only" (needs_web but mode not WEB), no web - and no classifier
    # can bring it back.
    web_mode = web_decision.mode in (WEB, WEB_HYBRID, WEB_DISABLED)
    web_skipped = web_decision.needs_web and not web_mode

    if document_reason and web_mode and (document_reason not in PIPELINE_REASONS or web_decision.explicit):
        return Route(HYBRID, True, True, f"{document_reason} + information externe", web_query)
    if document_reason:
        return Route(LOCAL_DOCUMENTS, True, False, document_reason, web_skipped=web_skipped)
    if web_mode:
        hybrid = web_decision.mode == WEB_HYBRID
        return Route(HYBRID if hybrid else WEB_REQUIRED, hybrid, True, web_decision.reason, web_query)
    if web_skipped:
        return Route(GENERAL_KNOWLEDGE, False, False, "recherche Web seulement sur demande explicite", web_skipped=True)
    if is_direct_task(core):
        return Route(DIRECT_LOCAL, False, False, "rédaction, reformulation ou calcul")
    if is_general_question(core):
        return Route(GENERAL_KNOWLEDGE, False, False, "question de connaissance générale")
    if has_image:
        return Route(LOCAL_DOCUMENTS, True, False, "question sur l'image jointe et vos documents", decided_by="fallback")

    classified = await classify_with_model(core, complete_json, previous_question)
    if classified is not None:
        if classified.needs_web_search and not web_auto:
            # the model may propose the web only when the administrator set
            # it on and automatic; off / "on explicit request": never.
            classified.needs_web_search = False
            classified.web_skipped = web_decision.needs_web or classified.intent == WEB_REQUIRED
            classified.intent = LOCAL_DOCUMENTS if classified.needs_local_documents else GENERAL_KNOWLEDGE
        return classified
    return Route(LOCAL_DOCUMENTS, True, False, "classification indisponible : recherche dans vos documents",
                 decided_by="fallback")
