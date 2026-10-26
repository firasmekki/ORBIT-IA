"""Provider-neutral types. The agent, the routes and the frontend only ever
see these - never a Gmail or Graph payload.

Every fact (sender, address, subject, date, attachments, ids) is copied from
what the provider returned; a missing value stays None, it is never guessed.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal

ProviderId = Literal["gmail", "outlook", "yahoo"]


@dataclass(frozen=True)
class Capabilities:
    server_side_search: bool
    unread_filter: bool
    threads: Literal["native", "header_reconstructed", "none"]
    deep_link: bool
    programmatic_revoke: bool
    max_page_size: int


@dataclass(frozen=True)
class EmailQuery:
    """What to look for, independent of any provider's query syntax."""

    text: str | None = None
    from_address: str | None = None
    from_name: str | None = None
    subject: str | None = None
    received_after: datetime | None = None  # timezone-aware
    received_before: datetime | None = None
    unread_only: bool = False
    has_attachments: bool | None = None
    limit: int = 10

    def describe(self) -> dict:
        return {
            "limit": self.limit,
            "unread_only": self.unread_only,
            "received_after": self.received_after.isoformat() if self.received_after else None,
            "received_before": self.received_before.isoformat() if self.received_before else None,
            "from": self.from_address or self.from_name,
            "has_text": bool(self.text or self.subject),
        }


@dataclass(frozen=True)
class EmailAddress:
    address: str
    name: str | None = None


@dataclass(frozen=True)
class AttachmentMeta:
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    is_inline: bool = False


@dataclass
class EmailSummaryItem:
    """A list entry: metadata only, no body."""

    message_id: str  # the provider's own id (wrapped into a signed ref by service.py)
    thread_id: str | None
    sender: EmailAddress | None
    to: list[EmailAddress]
    subject: str | None
    received_at: datetime | None
    snippet: str | None
    is_unread: bool | None
    has_attachments: bool | None
    web_link: str | None


@dataclass
class EmailMessage(EmailSummaryItem):
    cc: list[EmailAddress] = field(default_factory=list)
    body_text: str = ""
    body_truncated: bool = False
    body_source: Literal["text/plain", "text/html", "none"] = "none"
    attachments: list[AttachmentMeta] = field(default_factory=list)


@dataclass
class EmailPage:
    items: list[EmailSummaryItem]
    next_cursor: str | None = None


@dataclass
class EmailThread:
    thread_id: str
    messages: list[EmailMessage]
    complete: bool = True


@dataclass(frozen=True)
class ConnectedIdentity:
    """What an adapter learned when an authorization completed."""

    email_address: str
    external_account_id: str
    display_name: str | None
    granted_scopes: tuple[str, ...]
    tokens: dict  # {access_token, refresh_token, expires_at (iso)}
    tenant_id: str | None = None
    account_kind: str | None = None


def to_jsonable(value):
    """Dataclass -> JSON-safe dict (datetimes as ISO strings)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return to_jsonable(asdict(value))
    return value
