"""Web search providers behind one interface.

    Brave Search API     https://api.search.brave.com/res/v1/web/search
                         header X-Subscription-Token, count <= 20
    Google Programmable  https://www.googleapis.com/customsearch/v1
    Search JSON API      key + cx, num <= 10. Google closed this API to new
                         customers and discontinues it on 2027-01-01
                         (developers.google.com/custom-search/v1/overview):
                         only for an account that already has it.

A provider returns WebResult rows or raises WebSearchError with a `kind`
(config, auth, quota, timeout, network, provider, bad_response) and a
message that never contains the API key. Keys travel in HTTP headers, never
in a URL (httpx logs URLs; the query string of search calls is also dropped
from those log lines by _RedactSearchUrls).
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

USER_AGENT = "ORBIT-AI/1.0 (assistant documentaire local)"
SEARCH_API_HOSTS = {"api.search.brave.com", "www.googleapis.com"}


class _RedactSearchUrls(logging.Filter):
    """httpx logs every request URL at INFO: for the search APIs the query
    string (the user's question) is dropped from that line."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 2:
            url = record.args[1]
            host = getattr(url, "host", None)
            if host in SEARCH_API_HOSTS or getattr(url, "path", "").endswith("/search"):
                record.args = (record.args[0], str(url).split("?", 1)[0], *record.args[2:])
        return True


logging.getLogger("httpx").addFilter(_RedactSearchUrls())


@dataclass
class WebResult:
    title: str
    url: str
    domain: str
    snippet: str
    published: str | None = None
    provider: str = ""
    retrieved_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    score: float | None = None
    engines: list[str] = field(default_factory=list)


class WebSearchError(Exception):
    """kind: config | auth | quota | timeout | network | provider | bad_response"""

    def __init__(self, kind: str, message: str, *, retry_after: str | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


class WebSearchProvider:
    id = ""
    label = ""

    def __init__(self, *, api_key: str, timeout_seconds: float, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    def configured(self) -> tuple[bool, str | None]:
        if not self.api_key:
            return False, f"Clé API {self.label} absente."
        return True, None

    def search(self, query: str, *, max_results: int, language: str, country: str) -> list[WebResult]:
        ok, reason = self.configured()
        if not ok:
            raise WebSearchError("config", reason or "configuration incomplète")
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self._transport,
                              headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
                response = self._request(client, query, max_results=max_results, language=language, country=country)
        except httpx.TimeoutException:
            raise WebSearchError("timeout", f"{self.label} n'a pas répondu en {self.timeout_seconds:g} s.") from None
        except httpx.HTTPError as exc:
            raise WebSearchError("network", f"{self.label} injoignable (erreur réseau : {type(exc).__name__}).") from None
        self.elapsed = time.monotonic() - started
        return self._parse(response)

    def _request(self, client, query, *, max_results, language, country) -> httpx.Response:
        raise NotImplementedError

    def _parse(self, response: httpx.Response) -> list[WebResult]:
        raise NotImplementedError

    def _json(self, response: httpx.Response) -> dict:
        try:
            body = response.json()
        except ValueError:
            raise WebSearchError("bad_response", f"Réponse illisible de {self.label} (HTTP {response.status_code}).") from None
        return body if isinstance(body, dict) else {}


# Countries the Brave Search API accepts for `country` (its "codes" page;
# same list as the CountryCode enum of brave-search-python-client), plus ALL.
# Tunisia, Morocco and Algeria are NOT in it: with them Brave answers 422.
BRAVE_COUNTRIES = frozenset("""
ALL AR AU AT BE BR CA CL DK FI FR DE HK IN ID IT JP KR MY MX NL NZ NO CN PL PT PH RU SA ZA ES SE CH TW TR GB US
""".split())
# search_lang values accepted by Brave among the languages offered here.
BRAVE_LANGUAGES = frozenset({"fr", "en", "ar"})


class BraveSearchProvider(WebSearchProvider):
    id = "brave"
    label = "Brave Search"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def _request(self, client, query, *, max_results, language, country) -> httpx.Response:
        params = {"q": query, "count": max(1, min(int(max_results), 20)), "safesearch": "moderate"}
        # Only values Brave accepts are sent (an unsupported one is a 422 for
        # the whole search); nothing = Brave's defaults.
        if language and language.lower() in BRAVE_LANGUAGES:
            params["search_lang"] = language.lower()
        if country and country.upper() in BRAVE_COUNTRIES:
            params["country"] = country.upper()
        return client.get(self.endpoint, params=params, headers={"X-Subscription-Token": self.api_key})

    def _parse(self, response: httpx.Response) -> list[WebResult]:
        status = response.status_code
        # Brave answers an invalid key with HTTP 422 + code
        # SUBSCRIPTION_TOKEN_INVALID (component "authentication") - seen live.
        code, component = "", ""
        if status >= 400:
            try:
                error = (response.json() or {}).get("error") or {}
                code = str(error.get("code") or "")
                component = str((error.get("meta") or {}).get("component") or "")
            except (ValueError, AttributeError):
                pass
        if status in (401, 403) or "TOKEN" in code or component == "authentication":
            raise WebSearchError("auth", f"Clé Brave Search refusée (HTTP {status}{', ' + code if code else ''}) : "
                                 "vérifiez la clé et l'abonnement.")
        if status == 429 or "RATE" in code or "QUOTA" in code or "LIMIT" in code:
            raise WebSearchError("quota", f"Quota ou limite de débit Brave Search atteint (HTTP {status}).",
                                 retry_after=response.headers.get("Retry-After") or response.headers.get("X-RateLimit-Reset"))
        if status == 422:
            raise WebSearchError("config", f"Paramètres refusés par Brave Search (HTTP 422{', ' + code if code else ''}) : "
                                 "vérifiez la langue et le pays.")
        if status >= 400:
            raise WebSearchError("provider", f"Erreur de Brave Search (HTTP {status}).")
        body = self._json(response)
        results = []
        for item in ((body.get("web") or {}).get("results") or []):
            url = str(item.get("url") or "")
            if not url.startswith(("http://", "https://")):
                continue
            snippet = " ".join([str(item.get("description") or ""), *[str(s) for s in item.get("extra_snippets") or []]])
            results.append(WebResult(
                title=str(item.get("title") or url), url=url, domain=domain_of(url), snippet=_plain(snippet),
                published=item.get("page_age") or item.get("age"), provider=self.id,
            ))
        return results


class GoogleProgrammableSearchProvider(WebSearchProvider):
    id = "google"
    label = "Google Programmable Search"
    endpoint = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, *, api_key: str, engine_id: str, timeout_seconds: float, transport=None):
        super().__init__(api_key=api_key, timeout_seconds=timeout_seconds, transport=transport)
        self.engine_id = engine_id

    def configured(self) -> tuple[bool, str | None]:
        if not self.api_key:
            return False, "Clé API Google (GOOGLE_SEARCH_API_KEY) absente."
        if not self.engine_id:
            return False, "Identifiant du moteur Google (GOOGLE_SEARCH_ENGINE_ID, « cx ») absent."
        return True, None

    def _request(self, client, query, *, max_results, language, country) -> httpx.Response:
        # The key goes in a header, never in the URL: URLs end up in logs.
        params = {"cx": self.engine_id, "q": query, "num": max(1, min(int(max_results), 10))}
        if language:
            params["lr"] = f"lang_{language.lower()}"
            params["hl"] = language.lower()
        if country:
            params["gl"] = country.lower()
        return client.get(self.endpoint, params=params, headers={"X-goog-api-key": self.api_key})

    def _parse(self, response: httpx.Response) -> list[WebResult]:
        status = response.status_code
        if status >= 400:
            reasons: list[str] = []
            message = ""
            try:
                error = (response.json() or {}).get("error") or {}
                reasons += [str(e.get("reason") or "") for e in error.get("errors") or []]
                reasons += [str(d.get("reason") or "") for d in error.get("details") or [] if isinstance(d, dict)]
                reasons.append(str(error.get("status") or ""))
                message = str(error.get("message") or "")
            except (ValueError, AttributeError):
                pass
            reason = redact(" ".join(r for r in reasons if r), self.api_key)
            message = redact(message, self.api_key)
            if status == 429 or any(k in reason for k in ("rateLimitExceeded", "dailyLimitExceeded", "quotaExceeded", "RESOURCE_EXHAUSTED")):
                raise WebSearchError("quota", f"Quota Google Programmable Search dépassé (HTTP {status}).")
            if status == 400 and ("API_KEY_INVALID" in reason or "keyInvalid" in reason or "API key not valid" in message):
                raise WebSearchError("auth", "Clé API Google invalide (HTTP 400).")
            if status in (401, 403):
                hint = (" L'API n'est pas activée pour ce projet, ou elle n'est plus ouverte à ce compte "
                        "(Google l'a fermée aux nouveaux clients et l'arrête le 1er janvier 2027)."
                        if "accessNotConfigured" in reason or "forbidden" in reason.lower() or not reason else "")
                raise WebSearchError("auth", f"Accès refusé par Google (HTTP {status}, {reason or 'sans motif'}).{hint}")
            if status == 400:
                raise WebSearchError("config", f"Requête refusée par Google (HTTP 400, {reason or 'paramètres invalides'}) : vérifiez l'identifiant du moteur (cx).")
            raise WebSearchError("provider", f"Erreur de Google Programmable Search (HTTP {status}).")
        body = self._json(response)
        results = []
        for item in body.get("items") or []:
            url = str(item.get("link") or "")
            if not url.startswith(("http://", "https://")):
                continue
            meta = ((item.get("pagemap") or {}).get("metatags") or [{}])[0]
            published = meta.get("article:published_time") or meta.get("article:modified_time") or meta.get("og:updated_time")
            results.append(WebResult(
                title=str(item.get("title") or url), url=url, domain=domain_of(url),
                snippet=_plain(str(item.get("snippet") or "")), published=published, provider=self.id,
            ))
        return results


class SearXNGSearchProvider(WebSearchProvider):
    id = "searxng"
    label = "SearXNG"

    def __init__(self, *, base_url: str, timeout_seconds: float, transport=None):
        super().__init__(api_key="", timeout_seconds=timeout_seconds, transport=transport)
        self.base_url = base_url.rstrip("/")
        self.unresponsive_engines: list[str] = []
        self._max_results = 5

    def configured(self) -> tuple[bool, str | None]:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return False, "URL SearXNG invalide : indiquez une URL HTTP(S) sans identifiants."
        return True, None

    def _request(self, client, query, *, max_results, language, country) -> httpx.Response:
        self._max_results = max(1, min(int(max_results), 10))
        from app.websearch.fetcher import UnsafeUrl, check_url  # noqa: PLC0415
        parsed = urlparse(self.base_url)
        # Docker's private service name is the one deliberate private-network
        # exception. Any remote instance is HTTPS and checked against the same
        # public-address rules as page fetching before each request.
        if not (parsed.scheme == "http" and parsed.hostname == "searxng" and parsed.port in (None, 8080)):
            try:
                check_url(f"{self.base_url}/search")
            except UnsafeUrl as exc:
                raise WebSearchError("config", f"URL SearXNG refusée : {exc}") from None
        params = {"q": query, "format": "json", "language": language or "all"}
        return client.get(f"{self.base_url}/search", params=params,
                          headers={"Accept": "application/json"})

    def _parse(self, response: httpx.Response) -> list[WebResult]:
        if response.status_code == 429:
            raise WebSearchError("quota", "Limite de requêtes SearXNG atteinte.", retry_after=response.headers.get("Retry-After"))
        if response.status_code == 403:
            raise WebSearchError("config", "SearXNG refuse le format JSON; vérifiez search.formats dans settings.yml.")
        if response.status_code >= 400:
            raise WebSearchError("provider", f"Erreur SearXNG (HTTP {response.status_code}).")
        body = self._json(response)
        if not body:
            raise WebSearchError("bad_response", "Réponse JSON SearXNG invalide.")
        self.unresponsive_engines = []
        for entry in body.get("unresponsive_engines") or []:
            if isinstance(entry, (list, tuple)) and entry:
                self.unresponsive_engines.append(": ".join(str(x)[:120] for x in entry[:2]))
        results, seen = [], set()
        for item in body.get("results") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or url in seen:
                continue
            seen.add(url)
            results.append(WebResult(title=_plain(str(item.get("title") or url)), url=url,
                domain=domain_of(url), snippet=_plain(str(item.get("content") or "")),
                published=item.get("publishedDate"), provider=self.id,
                score=_safe_float(item.get("score")), engines=[str(e)[:80] for e in item.get("engines") or [] if isinstance(e, str)]))
            if len(results) >= self._max_results:
                break
        return results


def _plain(text: str) -> str:
    """Search snippets may carry <strong> highlighting: plain text only."""
    import re  # noqa: PLC0415

    return " ".join(re.sub(r"<[^>]+>", "", text or "").split())[:600]


def _safe_float(value) -> float | None:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


PROVIDERS = {
    BraveSearchProvider.id: BraveSearchProvider.label,
    GoogleProgrammableSearchProvider.id: GoogleProgrammableSearchProvider.label,
    SearXNGSearchProvider.id: "SearXNG (instance locale)",
}
