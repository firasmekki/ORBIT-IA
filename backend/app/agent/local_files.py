"""Local File Agent: resolves a user's filename reference ("lis
rapport.pdf") against the lightweight workspace index the frontend sent
along with the chat request - never against the real filesystem, which
the backend (a Docker container) never has access to. The actual file
bytes always come from the client (see app/agent/orchestrator.py's
_run_local_file_turn and app/routers/chat.py's /api/chat/resume): this
module only does the *deterministic, no-LLM* parts - detecting that a
message is asking about a local file at all, and picking which workspace
entry it means - exactly the same "Voie A" split used everywhere else in
this codebase (see app/agent/intent.py).

Nothing here ever reads a file - app/rag/extract.py (already the single
extraction entry point for uploaded company documents) is reused for the
actual text extraction once the client has supplied the bytes.
"""

import re
from dataclasses import dataclass

from app.rag.normalize import normalize

_STRIP_CHARS = " \t\n\r.,;:!?»«\"'"

# Any of these verbs, anywhere in the message, signals "this is about a
# local file" - deliberately broad (lire/analyser/résumer/ouvrir and their
# conjugations), narrowed down by _FILENAME_RE actually finding a
# plausible filename token right after. A message with the verb but no
# matching filename-shaped token is not a local-file intent (e.g. "lis la
# documentation RH" - no extension - falls through to the normal RAG
# path instead, which is correct: that's a company document, not a local
# file).
_LOCAL_FILE_TRIGGER_RE = re.compile(
    r"\b(?:lis|lire|lu|ouvre|ouvrir|analyse|analyser|r[ée]sume|r[ée]sumer)\b",
    re.IGNORECASE,
)

# A filename-shaped token: word characters (incl. accented French
# letters, hyphens - deliberately NO spaces, or "Lis rapport.pdf" would
# greedily capture "Lis rapport" as the filename) followed by a dot and a
# 2-5 character extension. Scanned with .search() so it's found wherever
# it sits in the sentence ("Analyse le fichier budget.xlsx" still finds
# "budget.xlsx"). Only ever used to *suggest* a filename to look up in
# the real index (resolve_file_in_index below), never trusted as a path.
_FILENAME_RE = re.compile(r"[\"'«]?\b([\wÀ-ÿ][\wÀ-ÿ\-]*\.[A-Za-z0-9]{2,5})\b[\"'»]?")


@dataclass(frozen=True)
class LocalFileIntent:
    filename_hint: str
    raw_message: str


def detect_local_file_intent(message: str) -> LocalFileIntent | None:
    if not _LOCAL_FILE_TRIGGER_RE.search(message):
        return None
    match = _FILENAME_RE.search(message)
    if not match:
        return None
    filename_hint = match.group(1).strip(_STRIP_CHARS + " ")
    if not filename_hint:
        return None
    return LocalFileIntent(filename_hint=filename_hint, raw_message=message)


def resolve_file_in_index(filename_hint: str, index: list) -> object | list | None:
    """Exact-then-partial match on the file's basename (`.name`), case/
    accent-insensitive - same resolution shape as get_document_section's
    document_name lookup. Returns a single entry, a list (ambiguous - the
    caller must ask which one), or None (not found). `index` is a list of
    objects with a `.name` attribute (LocalFileEntry in practice; kept
    duck-typed here so this stays trivially unit-testable without the
    Pydantic schema)."""
    normalized_hint = normalize(filename_hint)
    if not normalized_hint:
        return None
    exact = [f for f in index if normalize(f.name) == normalized_hint]
    partial = [f for f in index if normalized_hint in normalize(f.name)]
    matches = exact or partial
    if not matches:
        return None
    if len(matches) > 1:
        return matches
    return matches[0]


def is_safe_relative_path(relative_path: str) -> bool:
    """Defense in depth only - the File System Access API's own traversal
    (getFileHandle/getDirectoryHandle) cannot produce a path escaping the
    picked root in the first place (no ../ support at all in that API),
    so a malicious relative_path can only ever arrive here from a
    deliberately crafted HTTP request, not from the normal frontend flow.
    Still validated before the string is ever used (e.g. echoed into an
    audit log or matched against the index) - never trust a client string
    just because the client "should" have built it safely."""
    if not relative_path or relative_path.startswith(("/", "\\")):
        return False
    if ":" in relative_path:  # rules out "C:\..." / "D:/..." absolute paths
        return False
    normalized = relative_path.replace("\\", "/")
    parts = normalized.split("/")
    return not any(part in ("..", ".") for part in parts)
