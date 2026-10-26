"""Masks credentials before any text leaves the backend.

Project folders hold files such as "acces.txt" ("login : ... / mot de passe :
..."). Their text is indexed like any document, and a document classified
GENERAL / INTERNAL is readable by every role of the workspace. Whatever the
classification, a password, an API key or a token is never shown in a chat
answer, a source excerpt, a search result, an error or a log line, nor given
to the model: it is replaced by [masqué].

Applied at the boundaries (backend side, never trusted to the frontend):
  - every MCP tool result entering the agent (app/agent/mcp_client.py);
  - every assistant message saved or returned (app/routers/chat.py),
    including old messages when they are read back.
The documents themselves are not modified: the file and its index stay
intact; the Director can still open the file itself (Documents page).
"""

import re

MASK = "[masqué]"

# Labels whose value is a secret. Strong: the value is masked whatever it
# looks like. Account labels (login, identifiant, user...): masked too - the
# pair is an access, and half of it is still an access.
_STRONG = (
    r"mot\s*de\s*passe|mots\s*de\s*passe|mdp|password|passwd|passwort|pwd|pass\s*word|passphrase|passcode"
    r"|pass(?=[\"'”]?\s*(?:[:=：]|=>))"
    r"|code\s*(?:d['’]\s*)?acc[eè]s|code\s*pin|pin\s*code|code\s*secret|cl[eé]\s*wi-?fi|wi-?fi\s*key|wpa\s*key"
    r"|api[\s_-]*key|apikey|cl[eé]\s*(?:d['’]\s*)?api|secret(?:[\s_-]*key)?|client[\s_-]*secret|access[\s_-]*key(?:[\s_-]*id)?"
    r"|secret[\s_-]*access[\s_-]*key|private[\s_-]*key|auth[\s_-]*token|access[\s_-]*token|refresh[\s_-]*token|token|jeton"
    r"|كلمة\s*(?:المرور|السر)|رمز\s*الدخول"
)
_ACCOUNT = r"login|identifiant|identifiants|username|user\s*name|user|utilisateur|nom\s*d['’]\s*utilisateur|email\s*de\s*connexion|اسم\s*المستخدم"

# "Login-Adm :", "Mot de passe admin :", "MDP_wifi =": a short qualifier after
# the label - only when a ":" / "=" follows, so "mot de passe oublié" stays text.
_LABEL_SUFFIX = r"(?:[\s\-_]+[\w\-]{1,15}(?=\s*(?:[:=：]|=>)))?"

# label, separator, value (until end of line / next separator).
_LABELLED = re.compile(
    rf"(?P<label>(?:\b(?:{_STRONG})\b|(?<![\w])(?:{_STRONG})){_LABEL_SUFFIX})(?P<sep>\\?[\"'”]?\s*(?:[:=：]|=>|->|\s+est\s+|\s+is\s+)\s*|\s+)"
    rf"(?P<value>[^\s|;,}}\]]{{1,120}})",
    re.IGNORECASE,
)
_ACCOUNT_LABELLED = re.compile(
    rf"(?P<label>\b(?:{_ACCOUNT})\b{_LABEL_SUFFIX})(?P<sep>\\?[\"'”]?\s*(?:[:=：]|=>)\s*)(?P<value>[^\s|;,}}\]]{{1,120}})",
    re.IGNORECASE,
)
_PATTERNS = [
    # scheme://user:password@host
    (re.compile(r"(?P<pre>\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)(?P<value>[^@\s/]+)(?P<post>@)", re.I), "url"),
    # Authorization: Bearer xxx
    (re.compile(r"(?P<pre>\bbearer\s+)(?P<value>[A-Za-z0-9\-._~+/]{12,}=*)", re.I), "bearer"),
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), "pem"),
    # JWT
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "jwt"),
    # well-known key formats
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bsk-[A-Za-z0-9_-]{20,}\b|\bgh[pousr]_[A-Za-z0-9]{30,}\b"
                r"|\bxox[abprs]-[A-Za-z0-9-]{10,}\b|\bAIza[0-9A-Za-z_-]{30,}\b"), "key"),
]
# A word after a "strong" label without ":" is only a secret if it does not
# read like ordinary text ("mot de passe oublié", "password reset", "le token est expiré").
_PLAIN_WORDS = {
    "oublie", "oublié", "oubliee", "oubliée", "du", "de", "des", "le", "la", "les", "est", "sont", "pour", "a", "à",
    "reset", "policy", "politique", "change", "changer", "modifier", "modifie", "expire", "expiré", "expiree",
    "fort", "forte", "faible", "complexe", "requis", "obligatoire", "personnel", "temporaire", "initial",
    "manager", "management", "et", "ou", "the", "of", "for", "to", "and", "or", "manquant", "invalide", "incorrect",
}


def _value_is_secret(match: re.Match) -> bool:
    value = match.group("value").strip("\"'«»()[]")
    if not value or value == MASK:
        return False
    if re.search(r"[:=：]|=>|->", match.group("sep")):
        return True
    # "mot de passe Azerty@12", "le mot de passe est X9!k": only an unusual
    # token, never an ordinary word ("le token est expiré")
    return value.lower() not in _PLAIN_WORDS and (bool(re.search(r"\d|[^\w\s]", value)) or value.isupper())


def redact_secrets(text: str) -> str:
    if not text or not isinstance(text, str):
        return text

    def labelled(match: re.Match) -> str:
        if not _value_is_secret(match):
            return match.group(0)
        return f"{match.group('label')}{match.group('sep')}{MASK}"

    text = _LABELLED.sub(labelled, text)
    text = _ACCOUNT_LABELLED.sub(lambda m: f"{m.group('label')}{m.group('sep')}{MASK}", text)
    for pattern, kind in _PATTERNS:
        if kind in ("url", "bearer"):
            text = pattern.sub(lambda m: f"{m.group('pre')}{MASK}{m.groupdict().get('post') or ''}", text)
        else:
            text = pattern.sub(MASK, text)
    return text


def contains_secret(text: str) -> bool:
    return bool(text) and redact_secrets(text) != text


# Fields that are identifiers, never document text: left as they are.
_KEEP = {
    "document_id", "id", "relative_path", "path", "url", "domain", "department", "confidentiality", "code",
    "status", "kind", "provider", "decision", "tool", "type", "chart_type", "citation", "workspace_id",
}


def redact_payload(value, key: str | None = None):
    """Recursively masks secrets in every text field of a tool result /
    message payload (dicts, lists, strings)."""
    if isinstance(value, str):
        return value if key in _KEEP else redact_secrets(value)
    if isinstance(value, dict):
        return {k: redact_payload(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_payload(v, key) for v in value]
    return value
