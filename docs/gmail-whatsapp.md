# Gmail and WhatsApp

## Gmail (and Outlook)

Personal mailboxes are documented in [email-accounts.md](email-accounts.md):
Gmail and Outlook connect per user with OAuth, strictly read-only
(`gmail.readonly`, `Mail.Read`); Yahoo Mail is prepared but disabled until
Yahoo grants developer access. The first Gmail connector's optional sending
(`gmail.send`, its routes, page and tests) has been removed; its accounts are
copied into `email_connections` without deleting the old runtime_settings row.

Google quota notes still apply: `messages.list` costs 5 units and
`messages.get` 20 units per call; Google may change quotas and billing terms
([Gmail API quotas](https://developers.google.com/workspace/gmail/api/reference/quota)).

## WhatsApp Click to Chat

Add contacts manually with a `+` and international country code, followed by
digits. ORBIT builds an official `https://wa.me/` link and opens WhatsApp only
after the user confirms the preview. Review the message and press Send in
WhatsApp yourself; ORBIT does not send it. Contacts are stored per user. No
address book is imported. WhatsApp Business Cloud API, inbound webhooks and
template messages are not enabled.

## Limits

WhatsApp Cloud API is not enabled. Meta bills Cloud API use according to a
changeable rate card and applicable pricing rules; review the current country
and message-category pricing at deployment. The official
[Meta Business terms](https://www.whatsapp.com/legal/meta-terms-whatsapp-business)
describe API fees; see also [Cloud API pricing](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing).
No live Google, Microsoft or Meta account was connected or tested during this implementation.
