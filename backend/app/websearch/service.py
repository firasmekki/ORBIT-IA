"""One web search for one chat turn - honest about what happened.

    run_web_search(db, config, query, user)
      -> WebSearchOutcome(status, provider, query, results, pages, error)
         status: ok | no_results | disabled | not_configured | error | rate_limited

At most ONE call to the provider per turn (no automatic retry: a paid API is
never hammered), a per-hour cap for the whole server, an in-memory cache
(config.cache_ttl_seconds), then up to config.fetch_pages result pages read
by fetcher.fetch_page. Each search is audited (WEB_SEARCH: provider, the
query actually sent outside, result count, status) - never a key.
"""

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.audit_logger import log_event
from app.models.user import User
from app.websearch.config import WebSearchConfig, make_provider
from app.websearch.fetcher import FetchedPage, fetch_page
from app.websearch.providers import PROVIDERS, WebResult, WebSearchError
from app.rag.normalize import normalize

logger = logging.getLogger("orbitia.websearch")

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, list[WebResult]]] = {}
_recent_calls: deque[float] = deque()
_last_status: dict = {}
MAX_CACHE_ENTRIES = 200

_RANK_STOPWORDS = set("""derniere dernier latest newest current current version official official source publication published
quelle quel quelle est les la le du des une un of the what is status edition update updated norme standard regulation
reglementation loi rules date recent newer la de en pour with and from about""".split())


def _result_terms(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", normalize(text or "")) if len(word) > 2 and word not in _RANK_STOPWORDS}


def _is_primary_candidate(result: WebResult, entities: set[str]) -> bool:
    host = (result.domain or "").lower()
    if host.endswith((".gov", ".gouv", ".int", ".mil", ".europa.eu")) or ".gov." in host or ".gouv." in host:
        return True
    host_terms = _result_terms(host.replace(".", " "))
    title_terms = _result_terms(result.title)
    if entities and entities & host_terms:
        return True
    return bool(({"official", "officiel"} & (host_terms | title_terms)) and entities & (host_terms | title_terms))


def rank_results(results: list[WebResult], query: str, *, primary_source_priority: bool, entities: set[str] | None = None) -> list[WebResult]:
    """Promote topic-matching primary publishers without treating a domain
    name as proof. The fetched page still has to support the answer."""
    entities = entities or set()
    if not primary_source_priority:
        return list(results)
    query_terms = _result_terms(query)

    def score(result: WebResult) -> tuple[float, int]:
        title = _result_terms(result.title)
        url = _result_terms(result.url)
        snippet = _result_terms(result.snippet)
        terms = query_terms & (title | url | snippet)
        relevance = len(terms) * 2 + len(query_terms & title) + len(query_terms & url)
        entity_match = entities & (_result_terms(result.domain) | title)
        relevance += len(entity_match) * 5
        primary = int(_is_primary_candidate(result, entities) and bool(entity_match or terms))
        return (primary * 100 + relevance + (result.score or 0) * 0.01, -results.index(result))

    return sorted(results, key=score, reverse=True)


def targeted_query(query: str) -> str | None:
    if re.search(r"\b(standard|norme|normes|regulation|reglementation|law|loi|decret)\b", normalize(query or "")):
        suffix = "official publisher committee published current edition status"
    else:
        suffix = "official source publisher current version release date status"
    existing = set(re.findall(r"[a-z0-9]+", normalize(query or "")))
    if {"official", "source", "publisher", "publication", "status"} <= existing:
        return None
    candidate = f"{query} {suffix}".strip()
    return candidate[:200] if candidate.lower() != query.lower() else None

# Test seams: an httpx transport for the provider / the page reader.
provider_transport = None
fetch_transport = None
fetch_resolver = None


@dataclass
class WebSearchOutcome:
    status: str
    provider: str
    provider_label: str
    query: str
    results: list[WebResult] = field(default_factory=list)
    pages: dict[str, FetchedPage] = field(default_factory=dict)
    error_kind: str | None = None
    error: str | None = None
    cached: bool = False
    unresponsive_engines: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    searched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


CONNECTION = {
    "auth": "invalid_key", "quota": "rate_limited", "timeout": "timeout", "network": "network_error",
    "provider": "provider_error", "bad_response": "provider_error", "config": "config_error",
}


def connection_status(outcome: "WebSearchOutcome") -> str:
    """not_configured | connected | invalid_key | rate_limited | timeout |
    provider_error | network_error | config_error | disabled"""
    if outcome.status in ("ok", "no_results"):
        return "connected"
    if outcome.status in ("not_configured", "disabled"):
        return outcome.status
    if outcome.status == "rate_limited":
        return "rate_limited"
    return CONNECTION.get(outcome.error_kind or "", "provider_error")


def last_status() -> dict:
    with _lock:
        return dict(_last_status)


def _remember(outcome: WebSearchOutcome, *, test: bool = False) -> None:
    with _lock:
        _last_status.update({
            "provider": outcome.provider, "status": outcome.status, "connection": connection_status(outcome),
            "error_kind": outcome.error_kind,
            "error": outcome.error, "at": outcome.searched_at, "results": len(outcome.results), "test": test,
            "unresponsive_engines": list(outcome.unresponsive_engines),
        })


def _under_hourly_cap(config: WebSearchConfig) -> bool:
    now = time.monotonic()
    with _lock:
        while _recent_calls and now - _recent_calls[0] > 3600:
            _recent_calls.popleft()
        if len(_recent_calls) >= config.max_per_hour:
            return False
        _recent_calls.append(now)
        return True


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _recent_calls.clear()


def run_web_search(
    db: Session | None, config: WebSearchConfig, query: str, user: User | None, *, fetch: bool = True, test: bool = False,
    primary_source_priority: bool = False, entities: set[str] | None = None,
) -> WebSearchOutcome:
    label = PROVIDERS.get(config.provider, config.provider)
    outcome = WebSearchOutcome(status="ok", provider=config.provider, provider_label=label, query=query, queries=[query])
    if not config.enabled and not test:
        outcome.status, outcome.error = "disabled", "La recherche Web est désactivée par l'administrateur."
        return outcome
    provider = make_provider(config, transport=provider_transport)
    ok, reason = provider.configured()
    if not ok:
        outcome.status, outcome.error_kind, outcome.error = "not_configured", "config", reason
        _remember(outcome, test=test)
        return outcome

    key = (config.provider, " ".join(query.lower().split()), config.language, config.country, config.max_results)
    with _lock:
        hit = _cache.get(key)
    if hit and not test and time.monotonic() - hit[0] < config.cache_ttl_seconds:
        outcome.results, outcome.cached = list(hit[1]), True
    elif not test and not _under_hourly_cap(config):
        outcome.status, outcome.error_kind = "rate_limited", "quota"
        outcome.error = f"Limite locale atteinte ({config.max_per_hour} recherches Web par heure) : aucune recherche lancée."
    else:
        started = time.monotonic()
        try:
            outcome.results = provider.search(query, max_results=config.max_results, language=config.language,
                                              country=config.country)
            outcome.unresponsive_engines = list(getattr(provider, "unresponsive_engines", []))
        except WebSearchError as exc:
            outcome.status, outcome.error_kind, outcome.error = "error", exc.kind, exc.message
        logger.info(
            "web search provider=%s status=%s results=%d duration=%.1fs",
            config.provider, outcome.status, len(outcome.results), time.monotonic() - started,
        )
        if outcome.status == "ok" and config.cache_ttl_seconds > 0 and not test:
            with _lock:
                if len(_cache) >= MAX_CACHE_ENTRIES:
                    _cache.pop(next(iter(_cache)))
                _cache[key] = (time.monotonic(), list(outcome.results))

    if outcome.status == "ok" and not outcome.results:
        outcome.status = "no_results"
    if outcome.status == "ok" and fetch:
        outcome.results = rank_results(outcome.results, query, primary_source_priority=primary_source_priority, entities=entities)
        successful_pages = 0
        primary_urls = {r.url for r in outcome.results if _is_primary_candidate(r, entities or set())}

        def read_candidates(candidates: list[WebResult]) -> None:
            nonlocal successful_pages
            for result in candidates:
                if successful_pages >= config.fetch_pages or result.url in outcome.pages:
                    continue
                outcome.pages[result.url] = fetch_page(
                    result.url, timeout_seconds=config.timeout_seconds, max_bytes=config.fetch_max_bytes,
                    transport=fetch_transport, **({"resolve": fetch_resolver} if fetch_resolver else {}),
                )
                if outcome.pages[result.url].status == "fetched":
                    successful_pages += 1

        # Try topic-owned / authoritative candidates first. If the publisher
        # blocks crawling or no such result appeared, use one budgeted targeted
        # query before spending the page-read limit on secondary coverage.
        if primary_source_priority:
            read_candidates([r for r in outcome.results[:config.max_results] if r.url in primary_urls])
            primary_read = any(outcome.pages.get(url) and outcome.pages[url].status == "fetched" for url in primary_urls)
            followup = targeted_query(query) if not primary_read else None
            followup_key = (config.provider, " ".join((followup or "").lower().split()), config.language, config.country, config.max_results)
            with _lock:
                followup_hit = _cache.get(followup_key) if followup else None
            followup_cached = bool(followup_hit and time.monotonic() - followup_hit[0] < config.cache_ttl_seconds)
            if followup and (followup_cached or _under_hourly_cap(config)):
                try:
                    if followup_cached:
                        extra = list(followup_hit[1])
                    else:
                        extra = provider.search(followup, max_results=config.max_results, language=config.language,
                                                country=config.country)
                        if config.cache_ttl_seconds > 0 and not test:
                            with _lock:
                                _cache[followup_key] = (time.monotonic(), list(extra))
                    outcome.queries.append(followup)
                    outcome.unresponsive_engines = list(getattr(provider, "unresponsive_engines", []))
                    known = {r.url for r in outcome.results}
                    outcome.results.extend(r for r in extra if r.url not in known)
                    outcome.results = rank_results(outcome.results, query, primary_source_priority=True, entities=entities)
                    read_candidates([r for r in extra if _is_primary_candidate(r, entities or set())])
                except WebSearchError as exc:
                    outcome.error_kind, outcome.error = exc.kind, exc.message
            if successful_pages < config.fetch_pages:
                read_candidates([r for r in outcome.results[:config.max_results] if r.url not in primary_urls])
        else:
            read_candidates(outcome.results[:config.max_results])
    _remember(outcome, test=test)
    if db is not None:
        log_event(
            user_id=user.id if user else None, username=user.username if user else None, role=user.role if user else None,
            action="WEB_SEARCH_TEST" if test else "WEB_SEARCH",
            decision="ALLOW" if outcome.status in ("ok", "no_results") else "ERROR",
            resource_type="web", resource_id=config.provider, reason=outcome.error_kind or outcome.status,
            extra={
                "query_sent": query, "provider": config.provider, "results": len(outcome.results),
                "pages_read": sum(1 for p in outcome.pages.values() if p.status == "fetched"),
                "cached": outcome.cached, "status": outcome.status,
            },
        )
    return outcome
