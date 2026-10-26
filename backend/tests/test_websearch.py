"""Web search (app/websearch, app/agent/web_answer.py) - all HTTP mocked:
no internet access, no API credit consumed. Every test checks what really
happened: which call went out, with what, and what the user is told."""

import asyncio
import logging
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.agent.orchestrator as orchestrator
from app.agent.web_answer import WEB_REVIEW_PROMPT, WEB_SYSTEM_PROMPT, run_web_turn, sanitize_answer
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.audit import AuditLog
from app.models.runtime_setting import RuntimeSetting
from app.policy.rules import Grant
from app.routers.websearch import router as websearch_router
from app.websearch import fetcher, service
from app.websearch.config import WebSearchConfig, load_config, store_key
from app.websearch.intent import DIRECT_LOCAL, HYBRID, LOCAL, WEB, WEB_DISABLED, SearchDecision, build_web_query, decide_search_mode
from app.websearch.providers import BraveSearchProvider, GoogleProgrammableSearchProvider, SearXNGSearchProvider, WebResult, WebSearchError
from app.websearch.service import WebSearchOutcome, rank_results, targeted_query

BRAVE_KEY = "BRAVE-SECRET-KEY-1234567890"
GOOGLE_KEY = "AIzaGOOGLE-SECRET-KEY-0987654321"
PUBLIC_IP = "93.184.216.34"


def _config(**overrides) -> WebSearchConfig:
    values = dict(
        enabled=True, provider="brave", trigger="auto", max_results=5, fetch_pages=2, timeout_seconds=5,
        language="fr", country="", cache_ttl_seconds=0, max_per_hour=100, fetch_max_bytes=200_000,
        google_engine_id="", brave_api_key=BRAVE_KEY, google_api_key="",
    )
    values.update(overrides)
    return WebSearchConfig(**values)


def _brave_body(*items):
    return {"web": {"results": [
        {"title": t, "url": u, "description": d, "page_age": "2026-09-01T00:00:00"} for t, u, d in items
    ]}}


TWO_RESULTS = _brave_body(
    ("Loi 2026-12 sur l'efficacité énergétique", "https://www.energie.gov.tn/loi-2026-12",
     "La loi 2026-12 impose un audit énergétique tous les 5 ans."),
    ("ANME - nouvelles exigences", "https://www.anme.tn/exigences", "Seuil de consommation abaissé à 500 tep."),
)
PAGES = {
    "https://www.energie.gov.tn/loi-2026-12": (200, "text/html",
        "<html><head><title>Loi 2026-12</title><meta property='article:published_time' content='2026-08-30'></head>"
        "<body><nav>menu</nav><p>La loi 2026-12 impose un audit énergétique obligatoire tous les 5 ans "
        "aux établissements consommant plus de 500 tep.</p><script>steal()</script></body></html>"),
    "https://www.anme.tn/exigences": (503, "text/html", "indisponible"),
}


class Http:
    """Records every outgoing request; answers like the real services."""

    def __init__(self, search_response=None, pages=None, *, search_exc=None):
        self.requests: list[httpx.Request] = []
        self.search_response = search_response if search_response is not None else (200, TWO_RESULTS)
        self.pages = pages if pages is not None else PAGES
        self.search_exc = search_exc

    def search_transport(self):
        def handler(request):
            self.requests.append(request)
            if self.search_exc:
                raise self.search_exc
            status, body = self.search_response
            return httpx.Response(status, json=body, headers={"Retry-After": "60"} if status == 429 else {})
        return httpx.MockTransport(handler)

    def fetch_transport(self):
        def handler(request):
            self.requests.append(request)
            url = str(request.url)
            if url.endswith("/robots.txt"):
                return httpx.Response(404)
            status, ctype, body = self.pages.get(url, (404, "text/html", ""))
            return httpx.Response(status, text=body, headers={"content-type": f"{ctype}; charset=utf-8"})
        return httpx.MockTransport(handler)

    def hosts(self):
        return {r.url.host for r in self.requests}


@pytest.fixture
def http(monkeypatch):
    def install(**kwargs):
        mock = Http(**kwargs)
        monkeypatch.setattr(service, "provider_transport", mock.search_transport())
        monkeypatch.setattr(service, "fetch_transport", mock.fetch_transport())
        monkeypatch.setattr(service, "fetch_resolver", lambda host: [PUBLIC_IP])
        fetcher._robots_cache.clear()
        service.clear_cache()
        return mock
    yield install
    service.clear_cache()


class Turn:
    """run_agent_turn with MCP tools and the LOCAL model faked; records what
    the model was given."""

    def __init__(self, monkeypatch, config, *, local_hits=None, answer="Réponse [W1]."):
        self.calls, self.prompts = [], []
        self.local_hits = local_hits or []

        @asynccontextmanager
        async def session(token):
            yield object()

        async def call_tool(_session, name, arguments):
            self.calls.append((name, arguments))
            if name == "search_documents":
                return {"results": self.local_hits}
            if name == "search_database":
                return {"results": []}
            raise AssertionError(f"unexpected tool {name}")

        async def chat_completion(messages, tools=None, *, max_tokens=None):
            self.prompts.append(messages)
            return {"role": "assistant", "content": answer if isinstance(answer, str) else answer(messages)}

        monkeypatch.setattr(orchestrator, "open_mcp_session", session)
        monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: "fake-token")
        monkeypatch.setattr(orchestrator, "call_tool", call_tool)
        monkeypatch.setattr(orchestrator, "chat_completion", chat_completion)
        monkeypatch.setattr(orchestrator, "load_web_config", lambda: config)

    def say(self, message, *, search_mode="auto", history=None):
        grant = Grant(role="DIRECTOR", departments=frozenset({"GENERAL", "FINANCE"}), max_level="SECRET", max_level_rank=3,
                      tools=frozenset({"search_documents", "search_database", "get_document"}))
        return asyncio.run(orchestrator.run_agent_turn(
            user_id="u1", grant=grant, message=message, history=history or [], search_mode=search_mode,
            workspace_name="projets",
        ))


# --- intent: when the web is (not) used --------------------------------------------------------------------


@pytest.mark.parametrize("question, mode", [
    ("Résume cette consultation", LOCAL),
    ("Quelle est la valeur indiquée à la page 12 de mon document ?", LOCAL),
    ("Quelles sont les dernières évolutions de la réglementation énergétique en Tunisie ?", WEB),
    ("Compare les exigences de ma consultation avec les dernières exigences officielles disponibles", HYBRID),
    ("Trouve les informations manquantes concernant ce fournisseur", HYBRID),
    ("Cherche sur internet le prix du kWh en Tunisie", WEB),
])
def test_mode_follows_the_question(question, mode):
    assert decide_search_mode(question, enabled=True).mode == mode


@pytest.mark.parametrize("question", [
    "Bonjour, comment vas-tu ?",
    "Explique la différence entre courant alternatif et continu",
    "Rewrite this paragraph in clear English",
    "اشرح الفرق بين التيار المتناوب والمستمر",
])
def test_general_questions_use_direct_local_mode(question):
    from app.websearch.intent import DIRECT_LOCAL

    assert decide_search_mode(question, enabled=True).mode == DIRECT_LOCAL


def test_direct_local_mode_never_searches_documents_or_web(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config(), answer="Le courant alternatif change périodiquement de sens.")
    result = turn.say("Explique la différence entre courant alternatif et continu")
    assert result.answer.startswith("Le courant alternatif")
    assert turn.calls == []
    assert len(turn.prompts) == 1
    assert mock.requests == []


def test_mode_is_automatic_and_the_admin_switch_wins():
    # nothing the client sends can force the web: only "auto" and the internal "local"
    assert decide_search_mode("Résume cette consultation", "hybrid", enabled=True).mode == LOCAL
    assert decide_search_mode("Quelles sont les dernières lois ?", "web", enabled=True).mode == WEB  # from the question
    assert decide_search_mode("Quelles sont les dernières lois ?", "local", enabled=True).mode == LOCAL
    off = decide_search_mode("Quelles sont les dernières lois ?", enabled=False)
    assert off.mode == WEB_DISABLED and off.needs_web
    manual = decide_search_mode("Quelles sont les dernières lois ?", enabled=True, trigger="manual")
    assert manual.mode == DIRECT_LOCAL  # manual: only when the question asks for it in words
    assert decide_search_mode("Cherche sur internet les dernières lois", enabled=True, trigger="manual").mode == WEB


def test_the_chat_api_no_longer_accepts_a_search_mode():
    from app.schemas.chat import ChatRequest

    request = ChatRequest(message="Résume cette consultation", search_mode="web")
    assert not hasattr(request, "search_mode")


# 1. local only: no web call at all
def test_local_question_never_calls_the_web(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config(), local_hits=[{"document_id": "00000000-0000-0000-0000-000000000001", "title": "CCTP",
                                                     "excerpt": "Le CCTP prévoit 12 sites.", "relative_path": "c/CCTP.pdf"}])
    turn.say("Résume cette consultation")
    assert mock.requests == []
    assert all(name != "web_search" for name, _ in turn.calls)


# 2. web: a real (mocked) search, pages read, local model answers with references
def test_web_question_searches_reads_pages_and_cites(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config(),
                answer="La loi 2026-12 impose un audit énergétique tous les 5 ans [W1]. L'ANME abaisse le seuil à 500 tep [W2].")
    result = turn.say("Quelles sont les dernières évolutions de la réglementation énergétique en Tunisie ?")
    search = next(r for r in mock.requests if r.url.host == "api.search.brave.com")
    assert parse_qs(urlparse(str(search.url)).query)["q"] == [
        "Quelles sont les dernières évolutions de la réglementation énergétique en Tunisie"
    ]
    web = [s for s in result.sources if s["kind"] == "web"]
    assert [s["citation"] for s in web] == ["W1"]
    first = web[0]
    assert first["url"] == "https://www.energie.gov.tn/loi-2026-12" and first["domain"] == "energie.gov.tn"
    assert first["fetch_status"] == "fetched" and first["published"] == "2026-08-30" and first["accessed_at"]
    assert first["provider"] == "Brave Search"
    assert "[W1] [Loi 2026-12](https://www.energie.gov.tn/loi-2026-12)" in result.answer
    assert "requête envoyée : « Quelles sont les dernières" in result.answer
    assert "[W1]" in result.answer and "[W2]" not in result.answer


# 3. hybrid: documents + web, kept apart
def test_hybrid_compares_documents_and_web_sources(monkeypatch, http):
    http()
    hits = [{"document_id": "00000000-0000-0000-0000-000000000002", "title": "Consultation.pdf", "page": 3,
             "relative_path": "noveau consultation/Consultation.pdf", "excerpt": "Audit énergétique tous les 10 ans."}]
    turn = Turn(monkeypatch, _config(), local_hits=hits,
                answer="Votre consultation prévoit un audit tous les 10 ans [D1], la loi tous les 5 ans [W1].")
    result = turn.say("Compare les exigences de ma consultation avec les dernières exigences officielles disponibles")
    assert ("search_documents", {"query": "Compare les exigences de ma consultation avec les dernières exigences officielles disponibles", "top_k": 5}) in turn.calls
    kinds = {s["citation"]: s["kind"] for s in result.sources}
    assert kinds == {"W1": "web", "D1": "local"}  # only the sources the answer cites (W2 was not)
    assert "**Sources Web**" in result.answer and "**Documents locaux**" in result.answer
    assert "- [D1] noveau consultation/Consultation.pdf, page 3" in result.answer


# 4. Google not configured: no call, said so
def test_google_not_configured_makes_no_call_and_uses_the_documents(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config(provider="google", google_api_key="", google_engine_id=""), answer="Réponse locale.")
    result = turn.say("Cherche sur internet le prix du kWh en Tunisie")
    assert mock.requests == []
    # a public question: the local model's general answer, said unverified - no document search
    assert result.answer.startswith("Réponse locale.") and turn.calls == []
    assert "elle n'est pas configurée" in result.answer and "GOOGLE_SEARCH_API_KEY" in result.answer
    assert "n'est pas vérifiée sur le Web" in result.answer
    assert all(WEB_SYSTEM_PROMPT[:40] not in p[0]["content"] for p in turn.prompts)  # no web synthesis


def test_enabled_but_unconfigured_web_answers_current_questions_generally_without_rag(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config(brave_api_key=""), answer="Réponse locale.")
    result = turn.say("Quelles sont les dernières évolutions de la réglementation énergétique en Tunisie ?")
    assert mock.requests == []
    assert result.answer.startswith("Réponse locale.")
    assert "Clé API Brave Search absente." in result.answer and "pas vérifiée sur le Web" in result.answer
    assert turn.calls == []
    assert all(name != "web_search" for name, _ in turn.calls)


def test_a_capability_question_without_subject_needs_no_search(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config(enabled=False))
    result = turn.say("tu peut gérré des information dapres google ?")
    assert mock.requests == [] and turn.calls == [] and turn.prompts == []
    assert "désactivée par l'administrateur" in result.answer


# 5. invalid key
def test_invalid_keys_are_reported_as_authentication_errors():
    brave = BraveSearchProvider(api_key=BRAVE_KEY, timeout_seconds=5,
                                transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(WebSearchError) as error:
        brave.search("loi énergie", max_results=3, language="fr", country="")
    assert error.value.kind == "auth" and BRAVE_KEY not in error.value.message
    # Brave's real answer to an invalid key (observed live): HTTP 422, not 401
    real_brave = {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "detail": "The provided subscription token is invalid.",
                            "meta": {"component": "authentication"}, "status": 422}, "type": "ErrorResponse"}
    brave = BraveSearchProvider(api_key=BRAVE_KEY, timeout_seconds=5,
                                transport=httpx.MockTransport(lambda r: httpx.Response(422, json=real_brave)))
    with pytest.raises(WebSearchError) as error:
        brave.search("loi énergie", max_results=3, language="fr", country="")
    assert error.value.kind == "auth" and "SUBSCRIPTION_TOKEN_INVALID" in error.value.message
    google = GoogleProgrammableSearchProvider(
        api_key=GOOGLE_KEY, engine_id="cx1", timeout_seconds=5,
        transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"error": {
            "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT",
            "errors": [{"reason": "badRequest"}], "details": [{"reason": "API_KEY_INVALID"}]}})),
    )
    with pytest.raises(WebSearchError) as error:
        google.search("loi énergie", max_results=3, language="fr", country="tn")
    assert error.value.kind == "auth" and GOOGLE_KEY not in error.value.message


# 6. timeout - and a failure is never "not on the internet"
def test_timeout_is_explained_without_claiming_absence(monkeypatch, http):
    http(search_exc=httpx.ReadTimeout("slow"))
    turn = Turn(monkeypatch, _config(), answer="Réponse locale.")
    result = turn.say("Cherche sur internet le prix du kWh en Tunisie")
    assert "n'a pas répondu en 5 s" in result.answer
    assert "ne signifie pas que l'information est absente d'Internet" in result.answer
    assert not result.answer.startswith("Réponse locale.")  # never hide a failed web lookup behind model memory
    assert all(WEB_SYSTEM_PROMPT[:40] not in p[0]["content"] for p in turn.prompts)  # no web synthesis, nothing invented
    assert [t["tool"] for t in result.tool_trace][0] == "web_search"  # the failed attempt stays visible


# 7. quota
def test_quota_errors(monkeypatch, http):
    http(search_response=(429, {}))
    result = Turn(monkeypatch, _config()).say("Cherche sur internet le prix du kWh en Tunisie")
    assert "Quota ou limite de débit Brave Search atteint" in result.answer
    google = GoogleProgrammableSearchProvider(
        api_key=GOOGLE_KEY, engine_id="cx1", timeout_seconds=5,
        transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"error": {"errors": [{"reason": "dailyLimitExceeded"}]}})),
    )
    with pytest.raises(WebSearchError) as error:
        google.search("x y", max_results=1, language="", country="")
    assert error.value.kind == "quota"


def test_hourly_cap_stops_calls(http):
    mock = http()
    config = _config(max_per_hour=1, fetch_pages=0)
    assert service.run_web_search(None, config, "loi énergie Tunisie", None).status == "ok"
    second = service.run_web_search(None, config, "autre sujet précis", None)
    assert second.status == "rate_limited" and len([r for r in mock.requests if r.url.host == "api.search.brave.com"]) == 1


# 8. no result
def test_no_result_is_said_honestly(monkeypatch, http):
    http(search_response=(200, {"web": {"results": []}}))
    turn = Turn(monkeypatch, _config())
    result = turn.say("Cherche sur internet le prix du kWh en Tunisie")
    assert "n'a renvoyé aucun résultat" in result.answer and "ne prouve pas" in result.answer
    assert all(WEB_SYSTEM_PROMPT[:40] not in p[0]["content"] for p in turn.prompts)  # nothing to synthesise from the web


# 9. page that cannot be read: kept as a search snippet, flagged as such
def test_unreadable_page_is_flagged_and_snippet_is_not_sent_as_evidence(monkeypatch, http):
    http()
    turn = Turn(monkeypatch, _config())
    result = turn.say("Quelles sont les dernières évolutions de la réglementation énergétique en Tunisie ?")
    assert len(turn.prompts) == 2
    sources_block = turn.prompts[0][1]["content"]
    assert "[W1] (Web) Loi 2026-12" in sources_block
    assert "ANME - nouvelles exigences" not in sources_block
    assert "steal()" not in sources_block and "menu" not in sources_block  # scripts / navigation stripped


# 10. / 11. several sources, citations kept, nothing invented
def test_invented_urls_citations_and_numbers_are_removed(monkeypatch, http):
    http()
    invented = ("Selon la loi [W1], l'audit a lieu tous les 5 ans. Voir https://evil.example/exfil?d=secret et "
                "![x](https://evil.example/p.png) ainsi que [W7]. Le budget national est de 987 654 321 DT [W2].")
    result = Turn(monkeypatch, _config(), answer=invented).say(
        "Quelles sont les dernières évolutions de la réglementation énergétique en Tunisie ?")
    assert "evil.example" not in result.answer and "[W7]" not in result.answer
    assert "[lien non vérifié retiré]" in result.answer
    assert "987 654 321" not in result.answer  # no source backs that number
    assert "[W1]" in result.answer


def test_sanitize_keeps_only_returned_links():
    text = "[Loi](https://ok.example/a) [Faux](https://bad.example/b) https://ok.example/a https://bad.example/c [W3] [D1]"
    clean = sanitize_answer(text, {"https://ok.example/a"}, n_web=2, n_local=1)
    assert clean == "[Loi](https://ok.example/a) Faux https://ok.example/a [lien non vérifié retiré]  [D1]"


# 12. keys never disclosed
def test_keys_never_appear_in_messages_logs_audit_or_api(db, make_test_user, monkeypatch, http, caplog):
    caplog.set_level(logging.DEBUG)
    http(search_response=(403, {"error": {"errors": [{"reason": "accessNotConfigured"}]}}))
    director = make_test_user(username="ws_dir", role="DIRECTOR")
    config = _config(provider="google", google_api_key=GOOGLE_KEY, google_engine_id="cx1")
    outcome = service.run_web_search(db, config, "loi énergie Tunisie", director)
    assert outcome.status == "error" and "1er janvier 2027" in outcome.error
    store_key(db, "brave", BRAVE_KEY, "ws_dir")
    row = db.get(RuntimeSetting, "web_search_secrets")
    assert BRAVE_KEY not in str(row.value)  # encrypted at rest
    assert load_config(db).brave_api_key == BRAVE_KEY  # usable by the server only

    app = FastAPI()
    app.include_router(websearch_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: director
    client = TestClient(app)
    state = client.get("/api/admin/websearch").text
    short = client.put("/api/admin/websearch/keys", json={"provider": "brave", "api_key": "abc"})
    assert short.status_code == 422 and "abc" not in short.text
    db.expire_all()
    audits = " ".join(str(a.extra) for a in db.query(AuditLog).all())
    for secret in (BRAVE_KEY, GOOGLE_KEY):
        assert secret not in outcome.error and secret not in state and secret not in audits
        assert secret not in caplog.text
    assert '"configured":true' in state.replace(" ", "")


# 13. private documents and conversation never sent to the provider
def test_documents_and_history_never_reach_the_provider(monkeypatch, http):
    mock = http()
    hits = [{"document_id": "00000000-0000-0000-0000-000000000003", "title": "Offre", "relative_path": "KSA/offre.pdf",
             "excerpt": "Client SECRET-CLIENT-XYZ, montant confidentiel 777 000 DT."}]
    turn = Turn(monkeypatch, _config(), local_hits=hits)
    question = "Compare les exigences de ma consultation avec les dernières exigences officielles disponibles"
    turn.say(question, history=[{"role": "user", "content": "Notre client CONFIDENTIEL-ABC"}])
    sent = " ".join(str(r.url) + r.content.decode(errors="ignore") for r in mock.requests if r.url.host == "api.search.brave.com")
    for private in ("SECRET-CLIENT-XYZ", "777", "CONFIDENTIEL-ABC", "offre.pdf"):
        assert private not in sent
    assert parse_qs(urlparse(sent.split()[0]).query)["q"] == [build_web_query(question)]


def test_a_question_without_subject_sends_nothing(monkeypatch, http):
    mock = http()
    result = Turn(monkeypatch, _config()).say("Trouve les informations manquantes concernant ce fournisseur")
    assert all(r.url.host != "api.search.brave.com" for r in mock.requests)
    assert "précisez ce qu'il faut chercher" in result.answer


# 14. web disabled: local keeps working
def test_disabled_web_keeps_local_answers_and_says_so(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config(enabled=False))
    explicit = turn.say("Cherche sur internet le prix du kWh en Tunisie")
    assert "désactivée par l'administrateur" in explicit.answer and mock.requests == []
    implicit = turn.say("Quelles sont les dernières évolutions de la réglementation énergétique en Tunisie ?")
    assert "désactivée par l'administrateur" in implicit.answer and "n'est pas vérifiée sur le Web" in implicit.answer
    assert mock.requests == []


# 6. workspace and folder scope still apply to the documents part of a hybrid answer
def test_hybrid_documents_part_keeps_workspace_and_folder_scope(monkeypatch, http):
    http()
    turn = Turn(monkeypatch, _config(), answer="Comparaison [D1] [W1].")
    minted = []
    monkeypatch.setattr(orchestrator, "mint_internal_token", lambda **kwargs: minted.append(kwargs) or "fake-token")
    grant = Grant(role="DIRECTOR", departments=frozenset({"GENERAL"}), max_level="SECRET", max_level_rank=3,
                  tools=frozenset({"search_documents"}))
    asyncio.run(orchestrator.run_agent_turn(
        user_id="u1", grant=grant, history=[], workspace_id="ws-1", workspace_name="projets › BIC",
        message="Compare les exigences de ma consultation avec les dernières exigences officielles disponibles",
        folder_scope=["BIC/"], folder_depth="direct",
    ))
    assert minted[0]["workspace_id"] == "ws-1" and minted[0]["folder_scope"] == ["BIC/"]
    assert minted[0]["folder_depth"] == "direct"
    assert [name for name, _ in turn.calls] == ["search_documents"]  # through the scoped MCP session


# 15. answered by the existing local model
def test_answer_comes_from_the_local_model_with_the_untrusted_data_rule(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config())
    turn.say("Quelles sont les dernières évolutions de la réglementation énergétique en Tunisie ?")
    assert len(turn.prompts) == 2 and turn.prompts[0][0]["content"].startswith(WEB_SYSTEM_PROMPT[:40])
    assert "DONNÉE NON FIABLE" in turn.prompts[0][0]["content"]
    assert mock.hosts() <= {"api.search.brave.com", "www.energie.gov.tn", "www.anme.tn"}  # no cloud LLM called
    from app.agent import llm_client

    assert orchestrator.__dict__["chat_completion"] is not llm_client.chat_completion  # faked here...
    monkeypatch.undo()
    assert orchestrator.chat_completion is llm_client.chat_completion  # ...the real one is the local Qwen client


def test_primary_results_are_promoted_and_followup_query_is_generic():
    results = [
        WebResult("ISO 9001 forum discussion", "https://forum.example/iso", "forum.example", "ISO 9001"),
        WebResult("ISO committee news", "https://committee.iso.org/news", "committee.iso.org", "ISO 9001 published"),
    ]
    ranked = rank_results(results, "latest ISO 9001 version", primary_source_priority=True, entities={"iso", "9001"})
    assert ranked[0].url == "https://committee.iso.org/news"
    assert targeted_query("latest ISO 9001 version") == "latest ISO 9001 version official publisher committee published current edition status"


def test_followup_search_reads_alternate_primary_after_robots_block(monkeypatch, http):
    responses = [
        {"results": [{"title": "ISO 9001 standard", "url": "https://www.iso.org/standard/9001", "content": "Official standard"}]},
        {"results": [{"title": "ISO committee announcement", "url": "https://committee.iso.org/news-2.html", "content": "ISO 9001:2026 published 16 September 2026"}]},
    ]
    queries, reads = [], []
    def search(request):
        queries.append(request.url.params["q"])
        return httpx.Response(200, json=responses.pop(0))
    def fetch(request):
        reads.append(str(request.url))
        if request.url.host == "www.iso.org" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /standard/", headers={"content-type": "text/plain"})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="<title>ISO committee announcement</title><p>ISO 9001:2026 was published on 16 September 2026.</p>", headers={"content-type": "text/html"})
    monkeypatch.setattr(service, "provider_transport", httpx.MockTransport(search))
    monkeypatch.setattr(service, "fetch_transport", httpx.MockTransport(fetch))
    monkeypatch.setattr(service, "fetch_resolver", lambda host: [PUBLIC_IP])
    fetcher._robots_cache.clear()
    outcome = service.run_web_search(None,
        _config(provider="searxng", searxng_url="http://searxng:8080", fetch_pages=1),
        "latest ISO 9001 version", None, primary_source_priority=True, entities={"iso", "9001"})
    assert len(queries) == 2 and "official publisher committee" in queries[1]
    assert len([page for page in outcome.pages.values() if page.status == "fetched"]) == 1
    assert any(page.status == "blocked_robots" for page in outcome.pages.values())
    assert any(page.status == "fetched" and "16 September 2026" in page.text for page in outcome.pages.values())
    assert any("committee.iso.org/news-2.html" in url for url in reads)


def test_review_pass_corrects_stale_claim_using_read_page(monkeypatch, http):
    from app.agent import web_answer as web_answer_module
    from app.websearch.fetcher import FetchedPage
    result_page = WebResult("ISO committee announcement", "https://committee.iso.org/news-2.html",
                            "committee.iso.org", "snippet not evidence")
    page = FetchedPage(url=result_page.url, final_url=result_page.url, status="fetched", title=result_page.title,
        text="ISO 9001:2026 edition 6 was published on 16 September 2026.", fetched_at="2026-10-14T00:00:00+00:00")
    outcome = WebSearchOutcome(status="ok", provider="searxng", provider_label="SearXNG",
        query="ISO 9001 official publication", results=[result_page], pages={result_page.url: page})
    monkeypatch.setattr(web_answer_module, "_search", lambda *args, **kwargs: outcome)
    async def answer(messages, **kwargs):
        if messages[0]["content"] == WEB_REVIEW_PROMPT:
            return {"content": "ISO 9001:2015 est la dernière version publiée [W1]."}
        return {"content": "ISO 9001:2015 est la dernière version publiée [W1]."}
    result = asyncio.run(run_web_turn(session=None, decision=SearchDecision(WEB, True, False, "current"),
        config=_config(provider="searxng"), message="Quelle est la dernière version officielle de la norme ISO 9001 ?",
        user_id="u1", tool_trace=[], call_tool=None, record_tool_call=None, chat_completion=answer))
    assert "ISO 9001:2026" in result.answer and "2015 est la dernière" not in result.answer
    assert "16 September 2026" in result.answer
    assert "contredit le brouillon" in result.answer
    assert "les valeurs absentes des documents" not in result.answer


def test_version_conflict_gate_refuses_older_latest_claim():
    from app.agent.web_answer import _correct_older_latest_claim
    source = {"citation": "W1", "_content": "ISO 9001:2026 was published on 16 September 2026."}
    answer = "ISO 9001:2015 is the latest version [W1]."
    corrected = _correct_older_latest_claim(answer, [source])
    assert "ISO 9001:2026" in corrected and "contredit le brouillon" in corrected


def test_publication_date_day_is_kept_only_when_full_date_is_in_evidence():
    from app.agent.grounding import Evidence, verify_answer
    evidence = Evidence()
    evidence.add("ISO 9001:2026 was published on 16 September 2026.", document_id="web:W1")
    answer = "ISO 9001:2026 was published on 16 September 2026 [W1]."
    verified = verify_answer(answer, evidence, {}, question="Which version was published?").answer
    assert "16 September 2026" in verified


# --- fetcher safety -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin", "http://localhost:8095/health", "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.5/", "http://[::1]/", "file:///etc/passwd", "http://user:pw@example.com/", "http://example.com:8080/",
])
def test_unsafe_urls_are_never_fetched(url):
    page = fetcher.fetch_page(url, timeout_seconds=2, max_bytes=1000,
                              transport=httpx.MockTransport(lambda r: pytest.fail("no request expected")),
                              resolve=lambda host: ["10.0.0.5"] if host == "example.com" and ":8080" not in url else [PUBLIC_IP])
    assert page.status == "unsafe_url"


def test_redirect_to_internal_address_is_refused():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": "http://192.168.1.10/secret"})
    page = fetcher.fetch_page("https://news.example/a", timeout_seconds=2, max_bytes=1000,
                              transport=httpx.MockTransport(handler), resolve=lambda host: [PUBLIC_IP])
    assert page.status == "unsafe_url"


def test_robots_txt_and_content_type_and_size_are_respected():
    fetcher._robots_cache.clear()

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /prive/")
        if request.url.path == "/doc.pdf":
            return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
        return httpx.Response(200, text="<p>" + "a" * 5000 + "</p>", headers={"content-type": "text/html"})
    kwargs = dict(timeout_seconds=2, max_bytes=1000, transport=httpx.MockTransport(handler), resolve=lambda h: [PUBLIC_IP])
    assert fetcher.fetch_page("https://site.example/prive/x", **kwargs).status == "blocked_robots"
    assert fetcher.fetch_page("https://site.example/doc.pdf", **kwargs).status == "unsupported_type"
    big = fetcher.fetch_page("https://site.example/page", **kwargs)
    assert big.status == "fetched" and big.truncated and len(big.text) < 1000


# --- administration ------------------------------------------------------------------------------------------


def test_only_the_director_administers_and_the_test_reports_the_real_status(db, make_test_user, http):
    http(search_response=(401, {}))
    employee = make_test_user(username="ws_emp", role="EMPLOYEE")
    director = make_test_user(username="ws_dir2", role="DIRECTOR")
    app = FastAPI()
    app.include_router(websearch_router)
    app.dependency_overrides[get_db] = lambda: db
    who = {"user": employee}
    app.dependency_overrides[get_current_user] = lambda: who["user"]
    client = TestClient(app)
    assert client.get("/api/admin/websearch").status_code == 403
    assert client.post("/api/admin/websearch/test").status_code == 403
    assert client.get("/api/websearch/status").json()["enabled"] is False  # off by default

    who["user"] = director
    assert client.put("/api/admin/websearch", json={"country": "TN"}).status_code == 422  # not offered by Brave
    saved = client.put("/api/admin/websearch", json={"enabled": True, "max_results": 3, "country": "FR"}).json()
    assert saved["enabled"] is True and saved["max_results"] == 3 and saved["country"] == "FR"
    assert client.put("/api/admin/websearch", json={"provider": "bing"}).status_code == 422
    client.put("/api/admin/websearch/keys", json={"provider": "brave", "api_key": BRAVE_KEY})
    test = client.post("/api/admin/websearch/test").json()
    assert test["status"] == "error" and test["error_kind"] == "auth" and test["provider"] == "brave"
    assert client.get("/api/admin/websearch").json()["last_status"]["error_kind"] == "auth"


def test_searxng_provider_normalizes_deduplicates_and_reports_failed_engines():
    body = {
        "results": [
            {"title": "<b>Source</b>", "url": "https://example.org/a", "content": "  snippet <b>texte</b> ", "publishedDate": "2026-10-01"},
            {"title": "duplicate", "url": "https://example.org/a"},
            {"title": "bad", "url": "file:///etc/passwd"},
            {"title": "private URL", "url": "https://user:pass@example.org/"},
        ],
        "unresponsive_engines": [["test-engine", "timeout"]],
    }
    provider = SearXNGSearchProvider(base_url="http://searxng:8080", timeout_seconds=5,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    results = provider.search("question", max_results=5, language="fr", country="")
    assert len(results) == 1
    assert results[0].title == "Source"
    assert results[0].snippet == "snippet texte"
    assert results[0].published == "2026-10-01"
    assert provider.unresponsive_engines == ["test-engine: timeout"]


def test_searxng_provider_uses_json_and_classifies_errors():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(403, text="forbidden")
    provider = SearXNGSearchProvider(base_url="http://searxng:8080", timeout_seconds=5,
        transport=httpx.MockTransport(handler))
    with pytest.raises(WebSearchError) as error:
        provider.search("hello", max_results=4, language="fr", country="")
    assert error.value.kind == "config"
    assert requests[0].url.params["format"] == "json"
    assert requests[0].url.params["language"] == "fr"


def test_searxng_provider_empty_response_is_valid_empty_result():
    provider = SearXNGSearchProvider(base_url="http://searxng:8080", timeout_seconds=5,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": [], "query": "x"})))
    assert provider.search("x", max_results=3, language="fr", country="") == []


def test_searxng_remote_private_instance_is_rejected_before_network_request():
    provider = SearXNGSearchProvider(base_url="https://127.0.0.1", timeout_seconds=5,
        transport=httpx.MockTransport(lambda request: pytest.fail("request must not be sent")))
    with pytest.raises(WebSearchError) as error:
        provider.search("query", max_results=3, language="fr", country="")
    assert error.value.kind == "config"


def test_searxng_network_failure_is_reported_without_retry():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("offline", request=request)
    provider = SearXNGSearchProvider(base_url="http://searxng:8080", timeout_seconds=5,
        transport=httpx.MockTransport(handler))
    with pytest.raises(WebSearchError) as error:
        provider.search("query", max_results=3, language="fr", country="")
    assert error.value.kind == "network"
    assert len(calls) == 1
