"""Readable text and attachment metadata from mail payloads.

Shared by the adapters: text/plain is preferred, text/html is converted to
text as a fallback, attachments are listed (name, type, size) but their
content is never fetched. Everything is bounded - a huge or malformed
message yields a truncated or empty body, never an exception.
"""

import base64
import html
import re
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser

from app.connectivity.email_accounts.contract import AttachmentMeta, EmailAddress

# Upper bound of encoded bytes decoded for one body part, whatever the
# body_chars requested (a 30 MB HTML newsletter is never fully parsed).
MAX_PART_BYTES = 400_000


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "section"}
    _SKIP = {"script", "style", "head", "title", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML: fall back to tag stripping
        return normalize_text(html.unescape(re.sub(r"<[^>]+>", " ", markup)))
    return normalize_text("".join(parser.parts))


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def truncate(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit].rstrip() + " […]", True)


def decode_mime_words(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(make_header(decode_header(value))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return value.strip()


def parse_address(value: str | None) -> EmailAddress | None:
    """'Jean Dupont <j@x.tn>' -> EmailAddress(address, name); a missing
    display name stays None (the caller shows the address instead)."""
    if not value:
        return None
    name, address = parseaddr(decode_mime_words(value) or "")
    if not address or "@" not in address:
        return None
    return EmailAddress(address=address.strip(), name=(name.strip().strip('"') or None))


def parse_address_list(value: str | None, limit: int = 20) -> list[EmailAddress]:
    if not value:
        return []
    result = []
    for name, address in getaddresses([decode_mime_words(value) or ""])[:limit]:
        if address and "@" in address:
            result.append(EmailAddress(address=address.strip(), name=(name.strip().strip('"') or None)))
    return result


def _b64url(data: str) -> bytes:
    return base64.urlsafe_b64decode(data[:MAX_PART_BYTES] + "=" * (-len(data[:MAX_PART_BYTES]) % 4))


def _charset(part: dict) -> str:
    for header in part.get("headers") or []:
        if (header.get("name") or "").lower() == "content-type":
            match = re.search(r'charset="?([\w.-]+)"?', header.get("value") or "", re.I)
            if match:
                return match.group(1)
    return "utf-8"


def _decode(data: bytes, charset: str) -> str:
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def gmail_parts(payload: dict) -> tuple[str, str, list[AttachmentMeta]]:
    """Walks a Gmail API `payload` (format=full): returns (body, source,
    attachments) with source in text/plain | text/html | none."""
    plain: list[str] = []
    rich: list[str] = []
    attachments: list[AttachmentMeta] = []

    def walk(part: dict, depth: int = 0):
        if depth > 12:
            return
        mime = (part.get("mimeType") or "").lower()
        body = part.get("body") or {}
        filename = part.get("filename") or ""
        disposition = next(
            ((h.get("value") or "").lower() for h in part.get("headers") or []
             if (h.get("name") or "").lower() == "content-disposition"),
            "",
        )
        if filename or body.get("attachmentId"):
            if len(attachments) < 30:
                attachments.append(AttachmentMeta(
                    filename=filename or None, mime_type=mime or None,
                    size_bytes=body.get("size") if isinstance(body.get("size"), int) else None,
                    is_inline=disposition.startswith("inline"),
                ))
            return
        data = body.get("data")
        if data and mime == "text/plain":
            plain.append(_decode(_b64url(data), _charset(part)))
        elif data and mime == "text/html":
            rich.append(_decode(_b64url(data), _charset(part)))
        for child in part.get("parts") or []:
            walk(child, depth + 1)

    walk(payload or {})
    if any(p.strip() for p in plain):
        return normalize_text("\n\n".join(plain)), "text/plain", attachments
    if any(r.strip() for r in rich):
        return html_to_text("\n".join(rich)), "text/html", attachments
    return "", "none", attachments
