"""EmailAccountService against orbitia_test and simulated providers:
authorization flow (state, PKCE, browser binding), isolation between users,
token refresh / revocation, encryption at rest, legacy Gmail migration and
retention of chat results."""

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.connectivity.email_accounts import crypto, service
from app.connectivity.email_accounts.contract import EmailQuery
from app.connectivity.email_accounts.errors import EmailProviderError
from app.connectivity.email_accounts.legacy import LEGACY_KEY, _legacy_fernet, migrate_legacy_gmail_accounts
from app.connectivity.email_accounts.retention import EXPIRED_TEXT, purge_expired
from app.models.chat import Conversation, Message
from app.models.email_connection import EmailConnection, EmailOAuthState
from app.models.runtime_setting import RuntimeSetting
from email_support import GMAIL_READ, SECRET_VALUES, FakeMail, connect, install, use_settings


@pytest.fixture
def fake(monkeypatch):
    use_settings(monkeypatch)
    mail = FakeMail()
    install(monkeypatch, mail)
    return mail


def _params(url):
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_missing_configuration_keeps_mailboxes_disabled(db, make_test_user, monkeypatch):
    use_settings(monkeypatch, gmail_oauth_client_id="", email_token_encryption_key="")
    user = make_test_user(username="mail_noconf", role="EMPLOYEE")
    with pytest.raises(EmailProviderError) as error:
        service.start_authorization(db, user.id, "gmail")
    assert error.value.code == "oauth_config_missing"
    issues = service.provider_issues("gmail")
    assert any("GMAIL_OAUTH_CLIENT_ID" in i for i in issues) and any("EMAIL_TOKEN_ENCRYPTION_KEY" in i for i in issues)
    use_settings(monkeypatch, email_token_encryption_key="not-a-fernet-key")
    assert any("invalide" in i for i in service.provider_issues("outlook"))


def test_authorization_url_is_read_only_with_pkce_and_hashed_state(db, make_test_user, fake):
    user = make_test_user(username="mail_start", role="EMPLOYEE")
    url, binding = service.start_authorization(db, user.id, "gmail")
    params = _params(url)
    assert params["scope"] == GMAIL_READ
    assert params["code_challenge_method"] == "S256" and params["code_challenge"]
    assert "include_granted_scopes" not in params
    row = db.scalars(select(EmailOAuthState)).one()
    assert row.state_hash != params["state"] and params["state"] not in str(row.__dict__)
    assert binding not in str(row.__dict__)
    outlook = _params(service.start_authorization(db, user.id, "outlook")[0])
    assert outlook["scope"] == "openid email profile offline_access https://graph.microsoft.com/Mail.Read"
    with pytest.raises(EmailProviderError) as error:
        service.start_authorization(db, user.id, "yahoo")
    assert error.value.code == "provider_disabled"


def test_successful_connection_stores_encrypted_tokens_only(db, make_test_user, fake):
    user = make_test_user(username="mail_ok", role="EMPLOYEE")
    conn = connect(db, user, "gmail", fake)
    assert conn.status == "connected" and conn.is_primary and conn.email_address == "jean@gmail.example"
    assert conn.granted_scopes == [GMAIL_READ]
    for value in SECRET_VALUES:
        assert value not in (conn.secret_ciphertext or "")
    public = service.list_accounts(db, user.id)[0]
    assert "secret_ciphertext" not in public and not any(v in str(public) for v in SECRET_VALUES)
    assert public["read_permission_granted"] and public["token_state"] == "valid"
    outlook = connect(db, user, "outlook", fake)
    assert outlook.account_kind == "personal" and not outlook.is_primary


@pytest.mark.parametrize("case", ["unknown", "reused", "expired", "no_cookie", "other_cookie"])
def test_invalid_state_or_browser_never_creates_a_connection(db, make_test_user, fake, case):
    user = make_test_user(username=f"mail_state_{case}", role="EMPLOYEE")
    url, binding = service.start_authorization(db, user.id, "gmail")
    state = _params(url)["state"]
    if case == "unknown":
        state = "forged-state"
    if case == "reused":
        service.complete_authorization(db, "gmail", state=state, code="auth-code-xyz", binding=binding,
                                       error=None, error_description=None)
    if case == "expired":
        row = db.scalars(select(EmailOAuthState)).one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    cookie = {"no_cookie": None, "other_cookie": "attacker-browser"}.get(case, binding)
    before = db.query(EmailConnection).count()
    with pytest.raises(EmailProviderError) as error:
        service.complete_authorization(db, "gmail", state=state, code="auth-code-xyz", binding=cookie,
                                       error=None, error_description=None)
    expected = {"unknown": "oauth_state_invalid", "reused": "oauth_state_invalid", "expired": "oauth_state_expired",
                "no_cookie": "oauth_browser_mismatch", "other_cookie": "oauth_browser_mismatch"}[case]
    assert error.value.code == expected
    assert db.query(EmailConnection).count() == before


def test_consent_errors_and_missing_read_permission(db, make_test_user, fake):
    user = make_test_user(username="mail_consent", role="EMPLOYEE")
    for error_value, description, code in (("access_denied", None, "oauth_denied"),
                                           ("consent_required", "AADSTS65001: admin approval", "tenant_policy_blocked")):
        url, binding = service.start_authorization(db, user.id, "outlook")
        with pytest.raises(EmailProviderError) as error:
            service.complete_authorization(db, "outlook", state=_params(url)["state"], code=None, binding=binding,
                                           error=error_value, error_description=description)
        assert error.value.code == code
    fake.gmail_scope = "openid email"  # read box unticked on the consent screen
    with pytest.raises(EmailProviderError) as error:
        connect(db, user, "gmail", fake)
    assert error.value.code == "permission_missing"
    assert db.query(EmailConnection).count() == 0


def test_users_never_reach_each_others_accounts_tokens_or_messages(db, make_test_user, fake):
    owner = make_test_user(username="mail_owner", role="EMPLOYEE")
    other = make_test_user(username="mail_other", role="EMPLOYEE")
    conn = connect(db, owner, "gmail", fake)
    page = service.search(db, owner.id, conn.id, EmailQuery(limit=2), recent=True)
    ref = page["items"][0]["ref"]
    calls = len(fake.requests)
    assert service.list_accounts(db, other.id) == []
    for attempt in (lambda: service.search(db, other.id, conn.id, EmailQuery(limit=2)),
                    lambda: service.get_message(db, other.id, ref),
                    lambda: service.test_connection(db, other.id, conn.id),
                    lambda: service.update_account(db, other.id, conn.id, is_enabled=False),
                    lambda: service.disconnect(db, other.id, conn.id)):
        with pytest.raises(EmailProviderError) as error:
            attempt()
        assert error.value.code == "account_not_found"
    assert len(fake.requests) == calls, "no provider call on behalf of another user"
    forged = ref[:-4] + ("0000" if not ref.endswith("0000") else "1111")
    with pytest.raises(EmailProviderError) as error:
        service.get_message(db, owner.id, forged)
    assert error.value.code == "message_not_found"
    assert db.get(EmailConnection, conn.id).is_enabled is True


def test_expired_token_is_refreshed_once_and_stored_encrypted(db, make_test_user, fake):
    user = make_test_user(username="mail_refresh", role="EMPLOYEE")
    conn = connect(db, user, "outlook", fake)
    conn.access_token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    service.search(db, user.id, conn.id, EmailQuery(limit=1), recent=True)
    db.refresh(conn)
    tokens = crypto.decrypt(crypto.context(conn.id, user.id, "outlook"), conn.secret_ciphertext)
    assert fake.refresh_calls == 1 and tokens["refresh_token"] == "ms-refresh-2"  # rotated token kept
    assert conn.access_token_expires_at > datetime.now(UTC)
    service.search(db, user.id, conn.id, EmailQuery(limit=1), recent=True)
    assert fake.refresh_calls == 1


def test_revoked_refresh_token_and_temporary_failures_are_told_apart(db, make_test_user, fake):
    user = make_test_user(username="mail_revoked", role="EMPLOYEE")
    conn = connect(db, user, "gmail", fake)
    conn.access_token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    fake.gmail_refresh_status = 503
    with pytest.raises(EmailProviderError):
        service.search(db, user.id, conn.id, EmailQuery(limit=1))
    db.refresh(conn)
    assert conn.status == "temporary_error"
    fake.gmail_refresh_status = 400
    with pytest.raises(EmailProviderError) as error:
        service.search(db, user.id, conn.id, EmailQuery(limit=1))
    db.refresh(conn)
    assert error.value.code == "token_revoked" and conn.status == "token_expired_or_revoked"
    assert service.refresh_status(db, user.id, conn.id)["status"] == "token_expired_or_revoked"


def test_ciphertext_copied_to_another_row_is_unreadable(db, make_test_user, fake):
    first = make_test_user(username="mail_cipher_a", role="EMPLOYEE")
    second = make_test_user(username="mail_cipher_b", role="EMPLOYEE")
    a = connect(db, first, "gmail", fake)
    fake.gmail_profile = "other@gmail.example"
    b = connect(db, second, "gmail", fake)
    b.secret_ciphertext = a.secret_ciphertext
    db.commit()
    with pytest.raises(EmailProviderError) as error:
        service.search(db, second.id, b.id, EmailQuery(limit=1))
    assert error.value.code == "token_unreadable"
    db.refresh(b)
    assert b.status == "token_expired_or_revoked"


def test_disconnect_revokes_at_google_and_erases_local_secrets(db, make_test_user, fake):
    user = make_test_user(username="mail_disconnect", role="EMPLOYEE")
    gmail = connect(db, user, "gmail", fake)
    outlook = connect(db, user, "outlook", fake)
    result = service.disconnect(db, user.id, gmail.id)
    assert result["revocation"] == "revoked" and fake.revoked == ["gm-refresh-1"]
    db.refresh(gmail)
    assert gmail.secret_ciphertext is None and gmail.disconnected_at is not None and gmail.status == "disconnected"
    assert [a["id"] for a in result["accounts"]] == [str(outlook.id)] and result["accounts"][0]["is_primary"]
    ms = service.disconnect(db, user.id, outlook.id)
    assert ms["revocation"] == "not_supported" and ms["manual_revoke_url"].startswith("https://")
    with pytest.raises(EmailProviderError) as error:
        service.disconnect(db, user.id, uuid.uuid4())
    assert error.value.code == "account_not_found"


def test_legacy_gmail_accounts_are_copied_once_and_never_deleted(db, make_test_user, monkeypatch):
    use_settings(monkeypatch, gmail_token_encryption_key="legacy-key-0123456789-0123456789")
    user = make_test_user(username="mail_legacy", role="EMPLOYEE")
    f = _legacy_fernet()
    legacy = {str(user.id): {
        "a1": {"email": "ancien@gmail.example", "enabled": True, "primary": True, "scope": GMAIL_READ,
               "token": f.encrypt(b"old-access").decode(), "refresh_token": f.encrypt(b"old-refresh").decode(),
               "expires_at": "2026-01-01T00:00:00+00:00"},
        "a2": {"email": "envoi@gmail.example", "enabled": True, "primary": False,
               "scope": f"{GMAIL_READ} https://www.googleapis.com/auth/gmail.send",
               "token": f.encrypt(b"x").decode(), "refresh_token": f.encrypt(b"y").decode()},
    }, "not-a-uuid": {}}
    db.add(RuntimeSetting(key=LEGACY_KEY, value=legacy))
    db.commit()
    counts = migrate_legacy_gmail_accounts(db)
    assert counts == {"migrated": 1, "reauthorization_required": 1, "skipped": 1}
    rows = {c.email_address: c for c in db.scalars(select(EmailConnection))}
    assert rows["ancien@gmail.example"].status == "connected" and rows["ancien@gmail.example"].is_primary
    sender = rows["envoi@gmail.example"]
    assert sender.status == "authorization_required" and sender.secret_ciphertext is None
    assert all("gmail.send" not in s for c in rows.values() for s in c.granted_scopes)
    assert migrate_legacy_gmail_accounts(db)["migrated"] == 0 and db.query(EmailConnection).count() == 2
    assert db.get(RuntimeSetting, LEGACY_KEY).value == legacy  # untouched


def test_retention_replaces_expired_email_results_only(db, make_test_user):
    user = make_test_user(username="mail_retention", role="EMPLOYEE")
    conv = Conversation(user_id=user.id, title="t")
    db.add(conv)
    db.commit()
    old = Message(conversation_id=conv.id, role="assistant", content="Résumé : facture avant le 31/10",
                  email_results={"type": "email_results", "account": {"provider": "gmail"}, "items": [{"x": 1}]},
                  email_results_expires_at=datetime.now(UTC) - timedelta(days=1))
    fresh = Message(conversation_id=conv.id, role="assistant", content="Récent",
                    email_results={"type": "email_results", "items": []},
                    email_results_expires_at=datetime.now(UTC) + timedelta(days=5))
    other = Message(conversation_id=conv.id, role="assistant", content="Réponse documents")
    db.add_all([old, fresh, other])
    db.commit()
    assert purge_expired(db) == 1
    for row in (old, fresh, other):
        db.refresh(row)
    assert old.content == EXPIRED_TEXT and old.email_results == {"type": "email_results", "expired": True,
                                                                 "account": {"provider": "gmail"}}
    assert fresh.content == "Récent" and other.content == "Réponse documents"
    assert purge_expired(db) == 0
