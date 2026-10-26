"""Reads a search result page - safely, and as untrusted data.

Guards (each failure is a status, never an exception the caller must guess):
  - http / https only, ports 80 / 443, no user:password in the URL;
  - every address the host resolves to must be public (no loopback,
    private, link-local - e.g. 169.254.169.254 -, multicast, reserved):
    the backend must never be made to call the internal network (SSRF);
  - redirects followed by hand, at most MAX_REDIRECTS, each re-checked;
  - robots.txt of the site respected (cached per host);
  - only HTML / plain text; at most max_bytes read; one timeout per call.
Known limit: the address is checked just before the request; a DNS answer
that changes in between (rebinding) is not pinned.

The text is extracted with lxml (scripts, styles, navigation removed) and
cut to MAX_TEXT_CHARS. It is DATA for the model, never instructions.
"""

import ipaddress
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.websearch.providers import USER_AGENT

MAX_REDIRECTS = 3
MAX_TEXT_CHARS = 6000
ROBOTS_CACHE_SECONDS = 3600
_ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
_robots_cache: dict[str, tuple[float, RobotFileParser | None]] = {}


@dataclass
class FetchedPage:
    url: str
    status: str  # fetched | blocked_robots | unsafe_url | http_error | unsupported_type | timeout | network | empty
    text: str = ""
    title: str = ""
    published: str | None = None
    note: str = ""
    truncated: bool = False
    fetched_at: str = ""
    final_url: str | None = None


class UnsafeUrl(Exception):
    pass


def _resolver(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def check_url(url: str, resolve=_resolver) -> None:
    """Raises UnsafeUrl unless `url` is a public http(s) address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrl("schéma non autorisé")
    if parsed.username or parsed.password:
        raise UnsafeUrl("identifiants dans l'URL")
    if parsed.port not in (None, 80, 443):
        raise UnsafeUrl("port non autorisé")
    host = (parsed.hostname or "").strip(".").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise UnsafeUrl("hôte interne")
    try:
        addresses = [host] if _is_ip(host) else resolve(host)
    except OSError:
        raise UnsafeUrl("nom d'hôte introuvable") from None
    if not addresses:
        raise UnsafeUrl("nom d'hôte introuvable")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise UnsafeUrl("adresse non publique")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _robots_allows(client: httpx.Client, url: str, resolve) -> bool:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    cached = _robots_cache.get(origin)
    if cached and time.monotonic() - cached[0] < ROBOTS_CACHE_SECONDS:
        parser = cached[1]
    else:
        parser = None
        try:
            check_url(origin + "/robots.txt", resolve)
            response = client.get(origin + "/robots.txt", follow_redirects=False)
            if response.status_code == 200 and len(response.content) <= 500_000:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
            elif response.status_code in (401, 403):
                parser = RobotFileParser()
                parser.disallow_all = True
        except (httpx.HTTPError, UnsafeUrl):
            parser = None  # no readable robots.txt: allowed, as crawlers do
        _robots_cache[origin] = (time.monotonic(), parser)
    return parser is None or parser.can_fetch(USER_AGENT, url)


def fetch_page(
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
    transport: httpx.BaseTransport | None = None,
    resolve=_resolver,
) -> FetchedPage:
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        with httpx.Client(timeout=timeout_seconds, transport=transport,
                          headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"}) as client:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                check_url(current, resolve)
                if not _robots_allows(client, current, resolve):
                    return FetchedPage(url=url, status="blocked_robots", note="lecture refusée par le robots.txt du site", fetched_at=fetched_at)
                with client.stream("GET", current, follow_redirects=False) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            return FetchedPage(url=url, status="http_error", note="redirection sans destination", fetched_at=fetched_at)
                        current = urljoin(current, location)
                        continue
                    if response.status_code >= 400:
                        return FetchedPage(url=url, status="http_error", note=f"HTTP {response.status_code}", fetched_at=fetched_at)
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if content_type not in _ALLOWED_TYPES:
                        return FetchedPage(url=url, status="unsupported_type", note=f"type {content_type or 'inconnu'} non lu", fetched_at=fetched_at)
                    body, truncated = b"", False
                    for chunk in response.iter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            body, truncated = body[:max_bytes], True
                            break
                    encoding = response.encoding or "utf-8"
                break
            else:
                return FetchedPage(url=url, status="http_error", note="trop de redirections", fetched_at=fetched_at)
    except UnsafeUrl as exc:
        return FetchedPage(url=url, status="unsafe_url", note=f"adresse refusée ({exc})", fetched_at=fetched_at)
    except httpx.TimeoutException:
        return FetchedPage(url=url, status="timeout", note=f"pas de réponse en {timeout_seconds:g} s", fetched_at=fetched_at)
    except httpx.HTTPError as exc:
        return FetchedPage(url=url, status="network", note=f"erreur réseau ({type(exc).__name__})", fetched_at=fetched_at)

    raw = body.decode(encoding, errors="replace")
    if content_type == "text/plain":
        title, text, published = "", " ".join(raw.split()), None
    else:
        title, text, published = extract_html(raw)
    if not text.strip():
        return FetchedPage(url=url, status="empty", note="aucun texte lisible", fetched_at=fetched_at)
    return FetchedPage(url=url, status="fetched", text=text[:MAX_TEXT_CHARS], title=title, published=published,
                       truncated=truncated or len(text) > MAX_TEXT_CHARS, fetched_at=fetched_at, final_url=current)


def extract_html(raw: str) -> tuple[str, str, str | None]:
    """(title, visible text, publication date if declared)."""
    import lxml.html  # noqa: PLC0415

    try:
        tree = lxml.html.fromstring(raw)
    except (ValueError, lxml.etree.ParserError):
        return "", "", None
    published = None
    for name in ("article:published_time", "article:modified_time", "og:updated_time", "date", "dc.date", "DC.date.issued"):
        values = tree.xpath(f'//meta[@property="{name}" or @name="{name}"]/@content')
        if values:
            published = str(values[0])[:40]
            break
    title = " ".join(tree.xpath("string(//title)").split())[:200]
    for bad in tree.xpath("//script|//style|//noscript|//nav|//footer|//header|//form|//iframe|//svg|//aside"):
        bad.drop_tree()
    text = " ".join(tree.text_content().split())
    return title, text, published
