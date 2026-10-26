"""Brave Search wired into the real chat: configuration cycle (key, masks,
statuses), administration settings really applied to the request, routing
(automatic / on explicit request / off), sources kept faithful, and the local
model writing the answer. All HTTP is mocked (tests/test_websearch.py's
harness): no network, no API credit."""

import asyncio
import logging
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.llm_client import LLMServiceError
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.audit import AuditLog
from app.models.runtime_setting import RuntimeSetting
from app.routers.websearch import router as websearch_router
from app.websearch import service
from app.websearch.config import load_config, save_overrides, store_key
from app.websearch.providers import BRAVE_COUNTRIES
from tests.test_websearch import BRAVE_KEY, TWO_RESULTS, Turn, _config, http  # noqa: F401 - fixture

import app.agent.orchestrator as orchestrator


def _admin(db, user):
    app = FastAPI()
    app.include_router(websearch_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _search_params(mock) -> dict:
    request = next(r for r in mock.requests if r.url.host == "api.search.brave.com")
    return {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}


# --- 1-4, 16: the configuration cycle and its statuses -------------------------------------------------------

def test_no_key_is_not_configured_and_sends_nothing(db, make_test_user, http, monkeypatch):
    mock = http()
    director = make_test_user(username="brave_nokey", role="DIRECTOR")
    monkeypatch.setattr("app.websearch.config.get_settings", lambda: _settings(brave=""))
    client = _admin(db, director)
    test = client.post("/api/admin/websearch/test").json()
    assert test["status"] == "not_configured" and test["connection"] == "not_configured"
    assert mock.requests == []
    assert client.get("/api/websearch/status").json()["state"] in ("not_configured", "disabled")


@pytest.mark.parametrize("response,exc,connection", [
    ((200, TWO_RESULTS), None, "connected"),
    ((422, {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "meta": {"component": "authentication"}}}), None, "invalid_key"),
    ((401, {}), None, "invalid_key"),
    ((429, {}), None, "rate_limited"),
    ((500, {}), None, "provider_error"),
    (None, httpx.ReadTimeout("slow"), "timeout"),
    (None, httpx.ConnectError("refused"), "network_error"),
])
def test_connection_test_reports_each_real_outcome_without_the_key(db, make_test_user, http, monkeypatch, caplog,
                                                                     response, exc, connection):
    caplog.set_level(logging.DEBUG)
    mock = http(search_response=response, search_exc=exc) if exc else http(search_response=response)
    director = make_test_user(username=f"brave_t_{connection}", role="DIRECTOR")
    store_key(db, "brave", BRAVE_KEY, "test")
    client = _admin(db, director)
    test = client.post("/api/admin/websearch/test").json()
    assert test["connection"] == connection
    assert len([r for r in mock.requests if r.url.host == "api.search.brave.com"]) == 1  # one call, no retry
    if connection == "connected":
        assert [s["url"] for s in test["sample"]] == ["https://www.energie.gov.tn/loi-2026-12", "https://www.anme.tn/exigences"]
    state = client.get("/api/admin/websearch").json()
    assert state["last_status"]["connection"] == connection
    # the key appears nowhere: response, state, audit, logs (it only travels in the request header)
    everything = str(test) + str(state) + str([a.extra for a in db.query(AuditLog).all()]) + caplog.text
    assert BRAVE_KEY not in everything
    assert mock.requests[0].headers["X-Subscription-Token"] == BRAVE_KEY


def test_masks_and_empty_values_never_replace_a_stored_key(db, make_test_user):
    director = make_test_user(username="brave_mask", role="DIRECTOR")
    client = _admin(db, director)
    assert client.put("/api/admin/websearch/keys", json={"provider": "brave", "api_key": BRAVE_KEY}).status_code == 200
    for mask in ("********", "••••••••••", "****abcd****", "", "   "):
        response = client.put("/api/admin/websearch/keys", json={"provider": "brave", "api_key": mask})
        assert response.status_code == 422 and mask.strip() not in ("", None) or response.status_code == 422
        assert BRAVE_KEY not in response.text
    db.expire_all()
    assert load_config(db).brave_api_key == BRAVE_KEY  # still the real key
    stored = db.get(RuntimeSetting, "web_search_secrets").value["brave_api_key"]
    assert BRAVE_KEY not in stored  # encrypted at rest


def test_env_key_wins_over_the_key_entered_in_the_admin_page(db, monkeypatch):
    store_key(db, "brave", "ADMIN-KEY-0000000000", "test")
    monkeypatch.setattr("app.websearch.config.get_settings", lambda: _settings(brave="ENV-KEY-1111111111"))
    config = load_config(db)
    assert config.brave_api_key == "ENV-KEY-1111111111" and config.brave_key_source == "env"
    assert "ENV-KEY" not in str(config.public()) and "ADMIN-KEY" not in str(config.public())


# --- 5: the administration settings really reach the request ---------------------------------------------------

def test_saved_settings_are_applied_to_the_brave_request(db, http, monkeypatch):
    mock = http()
    save_overrides(db, {"enabled": True, "max_results": 7, "language": "en", "country": "FR", "timeout_seconds": 4,
                        "fetch_pages": 0}, "test")
    store_key(db, "brave", BRAVE_KEY, "test")
    config = load_config(db)
    outcome = service.run_web_search(None, config, "energy efficiency law", None)
    params = _search_params(mock)
    assert outcome.status == "ok"
    assert params["count"] == "7" and params["search_lang"] == "en" and params["country"] == "FR"
    assert params["q"] == "energy efficiency law"
    assert not [r for r in mock.requests if r.url.host != "api.search.brave.com"]  # fetch_pages 0: no page read
    assert config.timeout_seconds == 4


def test_country_brave_does_not_offer_is_refused_and_never_sent(db, http):
    assert "TN" not in BRAVE_COUNTRIES and {"FR", "BE", "CA", "CH", "ALL"} <= BRAVE_COUNTRIES
    with pytest.raises(ValueError, match="non pris en charge par Brave"):
        save_overrides(db, {"provider": "brave", "country": "TN"}, "test")
    save_overrides(db, {"provider": "brave", "country": "all"}, "test")
    assert load_config(db).country == "ALL"
    # a value stored before this check (or from .env) is simply not sent
    mock = http()
    service.run_web_search(None, _config(country="TN"), "loi énergie", None)
    assert "country" not in _search_params(mock)


# --- 6-9: when the chat calls Brave ------------------------------------------------------------------------

def test_disabled_web_never_calls_brave(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config(enabled=False), answer="Réponse locale.")
    for question in ("Quelles sont les dernières nouveautés de Qwen ?", "Cherche sur internet les dernières nouveautés de Qwen"):
        turn.say(question)
    assert mock.requests == []


def test_automatic_trigger_searches_when_the_question_needs_it(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config(trigger="auto"))
    result = turn.say("Quelles sont les dernières nouveautés de Qwen ?")
    assert _search_params(mock)["q"] == "Quelles sont les dernières nouveautés de Qwen"
    assert result.answer_mode == "web" and any(t["tool"] == "web_search" for t in result.tool_trace)


def test_on_request_trigger_does_not_search_unless_asked(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config(trigger="manual"), answer="Réponse locale.")
    implicit = turn.say("Quelles sont les dernières nouveautés de Qwen ?")
    assert mock.requests == [] and implicit.answer.startswith("Réponse locale.")
    assert "sur demande explicite" in implicit.answer
    turn.say("Cherche sur internet les dernières nouveautés de Qwen")
    assert _search_params(mock)["q"].endswith("dernières nouveautés de Qwen")


def test_on_request_trigger_with_a_hybrid_question_answers_from_documents_only(monkeypatch, http):
    mock = http()
    hits = [{"document_id": "d1", "title": "contrat.pdf", "relative_path": "BIC/contrat.pdf", "department": "GENERAL",
             "confidentiality": "INTERNAL", "excerpt": "Le contrat impose un audit tous les 3 ans.", "page": 4}]
    turn = Turn(monkeypatch, _config(trigger="manual"), local_hits=hits, answer="Audit tous les 3 ans.")
    result = turn.say("Compare les exigences de ce contrat à la réglementation actuelle")
    assert mock.requests == [] and "sur demande explicite" in result.answer


def test_greeting_calls_neither_web_nor_documents(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config())
    assert turn.say("Bonjour").answer == "Bonjour ! Comment puis-je vous aider ?"
    assert mock.requests == [] and turn.calls == [] and turn.prompts == []


# --- 10-13: documents, hybrid, empty results, faithful sources -------------------------------------------------

def test_document_question_uses_local_rag_and_sends_nothing_to_brave(monkeypatch, http):
    mock = http()
    hits = [{"document_id": "d1", "title": "contrat.pdf", "relative_path": "BIC/contrat.pdf", "department": "GENERAL",
             "confidentiality": "INTERNAL", "excerpt": "Maintenance trimestrielle.", "score": 0.9, "page": 2, "section": None}]
    turn = Turn(monkeypatch, _config(), local_hits=hits, answer="Maintenance trimestrielle.")
    turn.say("Quel est le délai de maintenance prévu dans le contrat ?")
    assert mock.requests == [] and turn.calls and turn.calls[0][0] == "search_documents"


def test_hybrid_keeps_both_provenances_apart_and_sends_no_document_text(monkeypatch, http):
    mock = http()
    hits = [{"document_id": "d1", "title": "contrat.pdf", "relative_path": "BIC/contrat.pdf", "department": "GENERAL",
             "confidentiality": "INTERNAL", "excerpt": "Le client Sotrapil impose un audit tous les 3 ans.", "page": 4}]
    turn = Turn(monkeypatch, _config(), local_hits=hits,
                answer="Le contrat impose un audit tous les 3 ans [D1], la loi tous les 5 ans [W1].")
    result = turn.say("Compare les exigences de ce contrat à la réglementation actuelle")
    query = _search_params(mock)["q"]
    assert "Sotrapil" not in query and "contrat.pdf" not in query and "BIC" not in query
    kinds = {s["kind"] for s in result.sources}
    assert kinds == {"web", "local"} and result.answer_mode == "hybrid"
    assert "**Sources Web**" in result.answer and "**Documents locaux**" in result.answer


def test_empty_results_invent_no_source(monkeypatch, http):
    http(search_response=(200, {"web": {"results": []}}))
    turn = Turn(monkeypatch, _config(), answer="Réponse générale.")
    result = turn.say("Quelles sont les dernières nouveautés de Qwen ?")
    assert "aucun résultat" in result.answer
    assert [s for s in result.sources if s.get("kind") == "web"] == [] and "http" not in result.answer


def test_only_the_cited_sources_are_listed_with_their_real_titles_and_urls(monkeypatch, http):
    http()
    turn = Turn(monkeypatch, _config(), answer="La loi impose un audit tous les 5 ans [W1]. Voir https://faux.example/x")
    result = turn.say("Quelles sont les dernières nouveautés de la loi sur l'efficacité énergétique ?")
    web = [s for s in result.sources if s["kind"] == "web"]
    assert [(s["title"], s["url"]) for s in web] == [
        ("Loi 2026-12", "https://www.energie.gov.tn/loi-2026-12")]
    assert "faux.example" not in result.answer  # a URL the search did not return is removed
    assert "[W1] [Loi 2026-12](https://www.energie.gov.tn/loi-2026-12)" in result.answer
    assert "anme.tn" not in result.answer  # not cited: not listed as a source


def test_the_results_reach_the_local_model_as_untrusted_data(monkeypatch, http):
    http()
    turn = Turn(monkeypatch, _config())
    turn.say("Quelles sont les dernières nouveautés de la loi sur l'efficacité énergétique ?")
    prompt = turn.prompts[0]
    review_prompt = turn.prompts[-1]
    assert "Distingue les faits confirmés" in prompt[0]["content"] and "DONNÉE NON FIABLE" in prompt[0]["content"]
    assert "contenu des pages" in review_prompt[0]["content"] and "n'utilise pas tes connaissances internes" in review_prompt[0]["content"]
    assert "La loi 2026-12 impose un audit énergétique obligatoire" in prompt[1]["content"]  # page really read


# --- 14-15: the local model and the web are two different services ----------------------------------------

def test_local_model_down_during_a_hybrid_turn_leaks_no_document(monkeypatch, http):
    http()
    hits = [{"document_id": "d1", "title": "acces.txt", "relative_path": "BIC/acces.txt", "department": "GENERAL",
             "confidentiality": "INTERNAL", "excerpt": "Le contrat prévoit l'accès au site BIC.", "page": 1}]
    turn = Turn(monkeypatch, _config(), local_hits=hits)

    async def down(*a, **k):
        raise LLMServiceError("Le modèle IA local n'est pas disponible pour le moment.")

    monkeypatch.setattr(orchestrator, "chat_completion", down)
    result = turn.say("Compare les exigences de ce contrat à la réglementation actuelle")
    assert "accès au site BIC" not in result.answer and all(s.get("kind") == "web" for s in result.sources)
    assert "n'est pas disponible" in result.answer and "non analysés" in result.answer
    assert all(s["excerpt"] == "" for s in result.sources)


def test_no_key_general_question_is_still_answered_by_the_local_model(monkeypatch, http):
    mock = http()
    turn = Turn(monkeypatch, _config(brave_api_key=""), answer="Qwen est une famille de modèles.")
    result = turn.say("Quelles sont les dernières nouveautés de Qwen ?")
    assert mock.requests == [] and result.answer.startswith("Qwen est une famille de modèles.")
    assert "Clé API Brave Search absente" in result.answer and "n'est pas vérifiée sur le Web" in result.answer


def _settings(brave=""):
    from app.core.config import get_settings

    real = get_settings()

    class S:
        def __getattr__(self, name):
            return getattr(real, name)

    s = S()
    s.brave_search_api_key = brave
    return s
