"""Yahoo Mail - prepared, disabled.

Yahoo exposes mailboxes over IMAP only (imap.mail.yahoo.com:993) with SASL
OAUTHBEARER, and the read scope `mail-r` is restricted: Yahoo must approve
the application first (Request Developer Access to Yahoo Mail). Until then
this adapter refuses everything with `provider_disabled`. No app-password
fallback is offered.

When access is granted, the implementation must stay read-only: EXAMINE
(never SELECT), UID SEARCH, UID FETCH with BODY.PEEK only - see
READ_ONLY_IMAP_COMMANDS - and reuse mime.py for parsing.
"""

from app.connectivity.email_accounts.contract import Capabilities
from app.connectivity.email_accounts.errors import EmailProviderError
from app.core.config import get_settings

IMAP_HOST = "imap.mail.yahoo.com"
IMAP_PORT = 993
AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
REVOKE_URL = "https://api.login.yahoo.com/oauth2/revoke"
READ_SCOPE = "mail-r"
READ_ONLY_IMAP_COMMANDS = frozenset({"CAPABILITY", "ID", "AUTHENTICATE", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT", "NOOP"})


class YahooAdapter:
    provider = "yahoo"
    label = "Yahoo Mail"
    callback_path = "/api/connectivity/yahoo/callback"
    capabilities = Capabilities(server_side_search=False, unread_filter=True, threads="header_reconstructed",
                                deep_link=False, programmatic_revoke=True, max_page_size=25)

    def __init__(self, account_email: str | None = None):
        self.account_email = account_email

    def configuration_issues(self) -> list[str]:
        if not get_settings().yahoo_mail_enabled:
            return ["Yahoo Mail désactivé : l'accès développeur Yahoo (scope mail-r) n'a pas encore été obtenu"]
        return ["Adaptateur Yahoo non implémenté : à développer après l'approbation de Yahoo"]

    def _disabled(self, *args, **kwargs):
        raise EmailProviderError("provider_disabled")

    authorization_url = complete_authorization = refresh = verify = _disabled
    list_recent = search = get_message = get_thread = _disabled

    def revoke(self, tokens: dict) -> str:
        return "failed"
