"""Personal mailboxes, strictly read-only, behind one provider-neutral contract.

    contract.py   data types every layer above the adapters uses
    errors.py     one error taxonomy for every provider
    crypto.py     Fernet encryption of tokens at rest, bound to their row
    mime.py       MIME / HTML to readable text, attachment metadata
    oauth.py      state, PKCE and browser binding of the authorization flow
    providers/    one adapter per provider (Gmail, Outlook; Yahoo disabled)
    service.py    the only code that touches email_connections
    legacy.py     one-way copy of the first Gmail connector's accounts
    retention.py  expiry of email results kept in the chat history

Nothing here can send, reply, forward, move, flag, mark as read or delete a
message: no adapter has such a method and the HTTP layer refuses anything
but GET on the mail APIs (providers/base.py).
"""
