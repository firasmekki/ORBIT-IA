"""Authorization-flow primitives shared by every OAuth provider.

The flow is bound three ways: to the ORBIT user (state row), to the browser
that started it (an HttpOnly cookie whose hash is stored with the state) and
to the code exchange (PKCE S256). A Google/Microsoft consent URL forwarded to
someone else therefore cannot attach their mailbox to the sender's account:
their browser has no binding cookie (the account-confusion attack).
"""

import base64
import hashlib
import secrets
from urllib.parse import urlsplit

STATE_TTL_SECONDS = 600
BINDING_COOKIE = "orbit_email_oauth"


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def valid_redirect_uri(value: str, expected_path: str) -> bool:
    """Exact callback path, HTTPS (HTTP only on localhost), no credentials,
    query or fragment - providers require an exact match anyway."""
    try:
        if not value or value != value.strip():
            return False
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        if parsed.path != expected_path:
            return False
        return parsed.scheme == "https" or (
            parsed.scheme == "http" and parsed.hostname.lower() in ("localhost", "127.0.0.1", "::1")
        )
    except (ValueError, AttributeError):
        return False
