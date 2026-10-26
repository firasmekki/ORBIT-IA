from app.connectivity.email_accounts.providers.gmail import GmailAdapter
from app.connectivity.email_accounts.providers.outlook import OutlookAdapter
from app.connectivity.email_accounts.providers.yahoo import YahooAdapter

ADAPTERS = {"gmail": GmailAdapter, "outlook": OutlookAdapter, "yahoo": YahooAdapter}
PROVIDER_LABELS = {name: cls.label for name, cls in ADAPTERS.items()}


def get_adapter(provider: str, account_email: str | None = None):
    cls = ADAPTERS.get(provider)
    if cls is None:
        raise KeyError(provider)
    return cls(account_email)
