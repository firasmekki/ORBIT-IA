"""Personal mailbox connections (Gmail, Outlook, later Yahoo) - read-only.

One row per mailbox a user connected. A user may connect several mailboxes,
including several of the same provider; a row only ever belongs to its
`user_id` and every read goes through app/connectivity/email_accounts/
service.py, which filters on it. Disconnecting is a soft delete: the
encrypted secret is erased, the row stays (with `disconnected_at`) so the
history of the connection is not lost.

Email content is never stored here: only the account identity, the granted
scopes, the status and the encrypted OAuth tokens.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

PROVIDERS = ("gmail", "outlook", "yahoo")


class EmailConnection(Base):
    __tablename__ = "email_connections"
    __table_args__ = (
        # The same mailbox once per user (reconnecting reuses the row);
        # several mailboxes per user and per provider are fine.
        Index(
            "uq_email_connections_active_account",
            "user_id", "provider", "external_account_id",
            unique=True, postgresql_where=text("disconnected_at IS NULL"),
        ),
        Index(
            "uq_email_connections_one_primary",
            "user_id",
            unique=True, postgresql_where=text("is_primary AND disconnected_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    auth_method: Mapped[str] = mapped_column(String(16), nullable=False, default="oauth2")
    email_address: Mapped[str] = mapped_column(String(320), nullable=False)
    # As returned by the provider; NULL when it gave none - never derived
    # from the address.
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Gmail: the lower-cased address; Microsoft: "tid:oid" (work/school) or
    # "sub" (personal account).
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Fernet token of {"ctx": ..., "data": {access_token, refresh_token}} -
    # see crypto.py. NULL once disconnected.
    secret_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    granted_scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    # A code from errors.py, never a raw provider message.
    last_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnect_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Id of the account in the first Gmail connector's runtime_settings blob
    # (legacy.py) - NULL for every connection made since.
    legacy_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EmailOAuthState(Base):
    """One pending authorization. Only hashes are stored: the `state` sent
    to the provider and the browser-binding cookie are never kept in clear,
    so a database read cannot be replayed into a callback."""

    __tablename__ = "email_oauth_states"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    browser_binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    # Fernet-encrypted PKCE code_verifier (crypto.py).
    pkce_verifier_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Re-authorizing an existing connection: the account chosen at the
    # provider must be that one.
    reconnect_connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
