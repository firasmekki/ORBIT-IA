"""Mailbox requests in the chat, recognized deterministically (no model).

detect_email_request() decides whether a message is about the user's own
mailbox ("le dernier email reçu", "mes mails non lus de cette semaine",
"résume mes emails Outlook") - not merely mentioning the word email ("l'adresse
email de Tarek dans les documents") - and turns it into an EmailRequest:
provider/account hints, count, period, filters and what to produce.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from app.rag.normalize import normalize


def _norm(message: str) -> str:
    """Lower-case, accent-free, apostrophes and hyphens as spaces ('aujourd'hui',
    'e-mail') - except inside an email address, kept intact."""
    text = re.sub("[’'`«»\"“”]", " ", normalize(message or ""))
    text = " ".join(tok if "@" in tok else tok.replace("-", " ") for tok in text.split())
    return re.sub(r"\s+", " ", text).strip()

_MAIL_NOUN = r"(?:e ?mails?|mails?|courriels?|inbox|boite (?:mail|email|e mail|de reception|aux lettres)|messagerie)"
_MAIL_RE = re.compile(rf"\b{_MAIL_NOUN}\b")
_RECEIVED_MESSAGES_RE = re.compile(r"\bmessages? (?:recus?|provenant|envoyes? par|de la boite|non lus?)\b")
_PROVIDER_RE = {
    "gmail": re.compile(r"\b(?:gmail|google mail)\b"),
    "outlook": re.compile(r"\b(?:outlook|hotmail|live\.com|msn|office 365|microsoft 365|m365)\b"),
    "yahoo": re.compile(r"\byahoo\b"),
}
_PERSONAL_RE = re.compile(
    r"\b(?:mes|ma|mon|my|dernier|derniers|derniere|dernieres|latest|last|recent|recents|recus?|received|"
    r"non lus?|unread|aujourd ?hui|today|hier|yesterday|cette semaine|this week|ce mois|boite|inbox|"
    r"demandent|necessitent|requierent|action)\b"
)
_NOT_MAILBOX_RE = re.compile(
    r"\b(?:adresse (?:e ?mail|mail|electronique)|documents?|fichiers?|pdf|dossiers?|workspace|contrat)\b"
)
# Imperative/infinitive verbs only: "les emails envoyés par X" is a read.
_WRITE_RE = re.compile(
    r"\b(?:envoie|envoies|envoyer|reponds|repondre|transfere|transferer|forward|supprime|supprimer|efface|effacer|"
    r"archive|archiver|deplace|deplacer|redige et envoie|send|reply|delete|move)\b"
    r"|\bmarquer? (?:comme|en|tous|tout)\b|\bmark (?:as|all)\b"
)
_NUMBER_WORDS = {
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8,
    "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "quinze": 15, "vingt": 20, "one": 1, "two": 2,
    "three": 3, "four": 4, "five": 5, "ten": 10, "twenty": 20,
}
_COUNT_RE = re.compile(rf"\b(\d{{1,3}}|{'|'.join(_NUMBER_WORDS)}) (?:derniers? |dernieres? |last |latest |plus recents? )?{_MAIL_NOUN}")
_COUNT_BEFORE_RE = re.compile(rf"\b(?:derniers?|dernieres?|last|latest) (\d{{1,3}}|{'|'.join(_NUMBER_WORDS)})\b")
_SINGLE_RE = re.compile(rf"\b(?:le|mon|the|my) (?:dernier|tout dernier|last|latest|plus recent) (?:{_MAIL_NOUN}|message)\b")
_SUMMARY_RE = re.compile(r"\b(?:resume|resumes|resumer|synthese|synthetise|recapitul\w*|summar\w*|explique|analyse)\b")
_REPORT_RE = re.compile(r"\b(?:rapport|bilan|report|point sur|etat des lieux)\b")
_ACTION_RE = re.compile(
    r"\b(?:demandent?|necessitent?|requierent?|attendent?|need|needs|require|requires)\b.{0,30}\b(?:action|reponse|traitement)\b"
    r"|\ba traiter\b|\baction de ma part\b"
)
# "avec les actions à effectuer": list the actions in the summary - not a filter.
_ACTIONS_WANTED_RE = re.compile(r"\bactions? (?:a (?:effectuer|faire|mener)|suggerees?|requises?)\b|\bto do\b")
_DEADLINE_RE = re.compile(r"\b(?:echeances?|deadlines?|dates? limites?|delais?)\b")
_UNREAD_RE = re.compile(r"\b(?:non lus?|pas lus?|unread|non ouverts?)\b")
_ATTACHMENT_RE = re.compile(r"\b(?:pieces? jointes?|attachments?|avec pj)\b")
_FROM_RE = re.compile(
    r"\b(?:de la part de|provenant de|envoyes? par|recus? de|venant de|emanant de|from|de)\s+"
    r"((?:[\w.+-]+@[\w-]+(?:\.[\w-]+)+)|(?:[a-z0-9][\w'&.-]*(?:\s+[a-z0-9][\w'&.-]*){0,3}))"
)
_ABOUT_RE = re.compile(r"\b(?:concernant|a propos de|au sujet de|sur le sujet|about|regarding|qui parlent? de|mentionnant)\s+(.{2,80})$")
_ADDRESS_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Words that end a sender name or are not one ("de cette semaine", "de mon Outlook").
_FROM_STOP = {
    "cette", "ce", "cet", "ces", "la", "le", "les", "l", "mon", "ma", "mes", "aujourd", "hier", "today", "yesterday",
    "gmail", "outlook", "hotmail", "yahoo", "mail", "mails", "email", "emails", "courriel", "courriels", "boite",
    "action", "ma part", "my", "this", "the", "plus", "moins", "semaine", "mois", "jour", "jours",
}
_READ_VERB_RE = re.compile(
    r"\b(?:resume|resumer|affiche|afficher|montre|montrer|liste|lister|lis|lire|donne|recherche|rechercher|cherche|"
    r"chercher|trouve|trouver|quels?|quelles?|combien|show|list|find|search|summarize|read)\b"
)
_DEMONSTRATIVE_SENDER_RE =re.compile(r"\b(?:de|par|from)\s+(?:ce|cette|cet|that|this)\s+(fournisseur|client|expediteur|contact|partenaire|sender|supplier|vendor)\b")
_PERIOD_STOP_RE = re.compile(
    r"\b(?:aujourd ?hui|today|hier|yesterday|cette semaine|this week|semaine derniere|last week|ce mois|this month|"
    r"depuis|since|avec|with|non lus?|unread|sur (?:mon|ma)|dans (?:mon|ma)|qui|et)\b.*$"
)


@dataclass
class EmailRequest:
    providers: set[str] = field(default_factory=set)
    addresses: set[str] = field(default_factory=set)
    limit: int | None = None
    limit_is_default: bool = True
    received_after: datetime | None = None
    received_before: datetime | None = None
    period_label: str | None = None
    unread_only: bool = False
    has_attachments: bool | None = None
    from_address: str | None = None
    from_name: str | None = None
    ambiguous_sender: str | None = None  # "ce fournisseur" with nothing to resolve it
    text: str | None = None
    summarize: bool = False
    report: bool = False
    needs_action: bool = False
    deadlines: bool = False
    write_request: bool = False

    @property
    def is_search(self) -> bool:
        return bool(self.from_address or self.from_name or self.text)


def is_email_request(message: str) -> bool:
    text = _norm(message)
    provider = any(r.search(text) for r in _PROVIDER_RE.values())
    received = bool(_RECEIVED_MESSAGES_RE.search(text))
    mailbox = bool(_MAIL_RE.search(text) or received)
    if not mailbox and not provider:
        return False
    if received or (mailbox and _WRITE_RE.search(text)):
        # "les messages reçus / provenant de..." is about a mailbox; a request to
        # send/delete/... one is too (answered with the read-only refusal).
        return True
    if _NOT_MAILBOX_RE.search(text) and not provider and not re.search(r"\b(?:mes|ma boite|inbox)\b", text):
        return False
    if mailbox and _READ_VERB_RE.search(text) and _FROM_RE.search(text) and _sender(text) != (None, None):
        return True  # "résume les emails de Sonelgaz" (not "rédige un email de relance": no reading verb)
    return provider and mailbox or (mailbox and bool(_PERSONAL_RE.search(text))) or (provider and bool(_PERSONAL_RE.search(text)))


def _count(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _start_of_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _period(text: str, now: datetime) -> tuple[datetime | None, datetime | None, str | None]:
    today = _start_of_day(now)
    if re.search(r"\b(?:aujourd ?hui|today|ce jour)\b", text):
        return today, None, "aujourd'hui"
    if re.search(r"\b(?:hier|yesterday)\b", text):
        return today - timedelta(days=1), today, "hier"
    if re.search(r"\b(?:semaine derniere|semaine passee|last week)\b", text):
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(days=7), monday, "la semaine dernière"
    if re.search(r"\b(?:cette semaine|this week|de la semaine)\b", text):
        return today - timedelta(days=today.weekday()), None, "cette semaine"
    if re.search(r"\b(?:ce mois|this month|du mois)\b", text):
        return today.replace(day=1), None, "ce mois-ci"
    days = re.search(r"\b(?:depuis|ces|les|last|past)\s+(\d{1,3}|" + "|".join(_NUMBER_WORDS) + r")\s+(?:derniers\s+)?(?:jours|days)\b", text)
    if days and _count(days.group(1)):
        n = min(_count(days.group(1)), 365)
        return today - timedelta(days=n - 1), None, f"les {n} derniers jours"
    since = re.search(r"\bdepuis le (\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", text)
    if since:
        day, month = int(since.group(1)), int(since.group(2))
        year = int(since.group(3)) if since.group(3) else now.year
        year = year + 2000 if year < 100 else year
        try:
            start = now.replace(year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
            return start, None, f"depuis le {day:02d}/{month:02d}/{year}"
        except ValueError:
            return None, None, None
    return None, None, None


def _sender(text: str) -> tuple[str | None, str | None]:
    address = None
    match = _FROM_RE.search(text)
    if not match:
        return None, None
    value = match.group(1).strip(" .,;:!?")
    if "@" in value:
        address = value
        return address, None
    value = _PERIOD_STOP_RE.sub("", value).strip()
    words = [w for w in value.split() if w]
    while words and words[-1] in _FROM_STOP:
        words.pop()
    if not words or words[0] in _FROM_STOP or len(" ".join(words)) < 2:
        return None, None
    return None, " ".join(words)


def parse_email_request(message: str, *, now: datetime, tz: tzinfo) -> EmailRequest:
    text = _norm(message)
    original_addresses = {a.lower() for a in _ADDRESS_RE.findall(message or "")}
    request = EmailRequest()
    request.providers = {p for p, r in _PROVIDER_RE.items() if r.search(text)}
    request.write_request = bool(_WRITE_RE.search(text))
    local_now = now.astimezone(tz)
    request.received_after, request.received_before, request.period_label = _period(text, local_now)
    request.unread_only = bool(_UNREAD_RE.search(text))
    request.has_attachments = True if _ATTACHMENT_RE.search(text) else None
    request.needs_action = bool(_ACTION_RE.search(text))
    request.deadlines = bool(_DEADLINE_RE.search(text))
    request.report = bool(_REPORT_RE.search(text))
    request.summarize = (bool(_SUMMARY_RE.search(text) or _ACTIONS_WANTED_RE.search(text))
                         or request.report or request.needs_action or request.deadlines)

    count = _COUNT_RE.search(text) or _COUNT_BEFORE_RE.search(text)
    if count and _count(count.group(1)):
        request.limit, request.limit_is_default = _count(count.group(1)), False
    elif _SINGLE_RE.search(text):
        request.limit, request.limit_is_default = 1, False

    if _DEMONSTRATIVE_SENDER_RE.search(text):
        request.ambiguous_sender = _DEMONSTRATIVE_SENDER_RE.search(text).group(1)
    else:
        from_address, from_name = _sender(text)
        # an address the user typed is kept with its original case
        if from_address:
            request.from_address = next((a for a in original_addresses if a == from_address.lower()), from_address)
        request.from_name = from_name
    request.addresses = original_addresses - ({request.from_address.lower()} if request.from_address else set())
    about = _ABOUT_RE.search(text)
    if about:
        request.text = _PERIOD_STOP_RE.sub("", about.group(1)).strip(" ?.!") or None
    return request
