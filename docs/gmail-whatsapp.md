# Gmail and WhatsApp

## Google OAuth setup

The Gmail connector starts with Google's official API read-only scope
`https://www.googleapis.com/auth/gmail.readonly`. Sending is a separate,
optional `gmail.send` authorization. It never asks for a Gmail password.
Accounts and tokens are isolated by authenticated user. Tokens are
encrypted in `runtime_settings`. Mail is searched on demand with a result
limit; attachments are not downloaded and message bodies are not indexed or
retained by this connector.

1. In [Google Cloud Console](https://console.cloud.google.com/), create or
   select a project and enable **Gmail API** under APIs & Services.
2. Configure the OAuth consent screen (app name, support email, audience, and
   developer contact). For a testing audience, add the intended Google accounts
   as test users. Google verification requirements may apply to the consent
   screen and Gmail scopes depending on distribution.
3. Create an OAuth client of type **Web application**. Add the exact callback
   URI as an Authorized redirect URI. Local default:
   `http://localhost:8081/api/connectivity/gmail/callback`. For deployment use
   HTTPS and register the corresponding HTTPS URI. Google requires an exact
   match, including scheme, case, and trailing slash.
4. Set `GMAIL_OAUTH_CLIENT_ID`, `GMAIL_OAUTH_CLIENT_SECRET`,
   `GMAIL_OAUTH_REDIRECT_URI`, and `GMAIL_TOKEN_ENCRYPTION_KEY` in the root
   `.env`. Use the Google Web client ID (ending in
   `.apps.googleusercontent.com`), and the exact callback route
   `/api/connectivity/gmail/callback`; the local default is
   `http://localhost:8081/api/connectivity/gmail/callback`. Generate a dedicated
   encryption key locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"`;
   it produces 43 random characters. Do not reuse the client secret as the
   encryption key. Compose reads the root `.env` and passes these credentials
   only to the backend container. After editing `.env`, recreate that container
   with `docker compose up -d --no-deps backend` (a plain restart does not reload
   container environment variables). Gmail stays unavailable until all four
   settings pass readiness checks. The UI reports missing or invalid variable
   names without displaying their values; it never generates or overwrites
   credentials. Keep the client secret server-side.
5. Sign in as the user whose mailbox should be connected, then open
   **Connectivity → Gmail multi-account → Add a Google account**, grant
   access, and return to Connectivity. Connect as many accounts as needed, test
   them, then select an account to search.

OAuth uses a random, single-use `state` value that expires after ten minutes,
the fixed server callback and the server-side OAuth client secret. Google may
require verification for Gmail scopes depending on app audience and publishing
status. Disconnect revokes the token at Google and deletes local token data.
Email bodies and credentials are not written to logs.

Google currently describes standard Gmail API usage as having no additional
charge within quota. For projects created on or after May 1, 2026, Google lists
1,200,000 quota units/minute/project, 6,000 units/minute/user/project, and a
daily threshold of 80,000,000 units before billing. Google says usage above the
threshold may become billable later in 2026 after notice; projects used from
November 2025 through April 2026 may retain different quota values. Check the
project's Quotas and Billing pages because Google may change these terms.
The documented method costs currently include `messages.list` at 5 units,
`messages.get` at 20 units, and `messages.send` at 100 units. Gmail also applies
per-user sending limits; send failures/timeouts are not retried automatically
because a timed-out send can have an uncertain result.

Official sources: [Gmail API quotas](https://developers.google.com/workspace/gmail/api/reference/quota),
[OAuth for web servers](https://developers.google.com/identity/protocols/oauth2/web-server),
[Gmail API error handling](https://developers.google.com/workspace/gmail/api/guides/handle-errors).

## WhatsApp Click to Chat

Add contacts manually with a `+` and international country code, followed by
digits. ORBIT builds an official `https://wa.me/` link and opens WhatsApp only
after the user confirms the preview. Review the message and press Send in
WhatsApp yourself; ORBIT does not send it. Contacts are stored per user. No
address book is imported. WhatsApp Business Cloud API, inbound webhooks and
template messages are not enabled.

## Implemented limits

After OAuth setup, Gmail account management, read-only search, message/thread
reading, and the connectivity-page workbench are available. The local Qwen chat
exposes bounded `gmail_search`, `gmail_read`, and `gmail_read_thread` tools for
that authenticated user's enabled accounts. Gmail sending is off until the user
separately reauthorizes `gmail.send`; then the page prepares a preview and uses
a single-use confirmation tied to sender, recipient, subject and body. A timeout
consumes the confirmation and is reported as uncertain so it cannot trigger an
automatic duplicate. WhatsApp Cloud API is not enabled. Meta bills Cloud API use
according to a changeable rate card and applicable pricing rules; review the
current country and message-category pricing at deployment. The official
[Meta Business terms](https://www.whatsapp.com/legal/meta-terms-whatsapp-business)
describe API fees; see also [Cloud API pricing](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing).
No live Google or Meta account was connected or tested during this implementation.
