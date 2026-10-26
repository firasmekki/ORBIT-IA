"""Web search administration (Director) and status (everyone).

    GET    /api/websearch/status          is it on? which provider? (no secret)
    GET    /api/admin/websearch           settings + key status (never a key)
    PUT    /api/admin/websearch           non-secret settings
    PUT    /api/admin/websearch/keys      set / replace one API key (write-only)
    DELETE /api/admin/websearch/keys/{p}  remove the key entered here
    POST   /api/admin/websearch/test      one real search, real status

Every change is audited, without the key.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.audit_logger import log_event
from app.core.database import get_db
from app.core.deps import get_current_user, require_role
from app.models.user import User
from app.websearch import service
from app.websearch.config import load_config, make_provider, save_overrides, store_key
from app.websearch.providers import PROVIDERS

router = APIRouter(tags=["websearch"])

PROVIDER_NOTES = {
    "searxng": "Instance SearXNG locale; aucun compte ni clé API commerciale requis.",
    "brave": "Brave Search API : clé à créer sur api-dashboard.search.brave.com (abonnement Search).",
    "google": (
        "Google Programmable Search JSON API : fermée aux nouveaux clients, arrêtée par Google le 1er janvier 2027 "
        "(developers.google.com/custom-search/v1/overview). Utilisable seulement avec une clé et un moteur (cx) existants."
    ),
}


class WebSearchSettingsIn(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    trigger: str | None = None
    max_results: int | None = Field(default=None, ge=1, le=10)
    fetch_pages: int | None = Field(default=None, ge=0, le=5)
    timeout_seconds: float | None = Field(default=None, ge=2, le=60)
    language: str | None = None
    country: str | None = None
    cache_ttl_seconds: int | None = Field(default=None, ge=0, le=86400)
    max_per_hour: int | None = Field(default=None, ge=1, le=1000)
    google_engine_id: str | None = Field(default=None, max_length=200)
    searxng_url: str | None = Field(default=None, max_length=500)


class WebSearchKeyIn(BaseModel):
    provider: str
    # No length constraint here: a 422 from pydantic would echo the value
    # back. Checked in set_key with a message that never contains it.
    api_key: str


def _state(db: Session) -> dict:
    config = load_config(db)
    return {
        **config.public(),
        "providers": [{"id": p, "label": label, "note": PROVIDER_NOTES.get(p, "")} for p, label in PROVIDERS.items()],
        "last_status": service.last_status(),
    }


@router.get("/api/websearch/status")
def websearch_status(db: Session = Depends(get_db), _user: User = Depends(get_current_user)) -> dict:
    config = load_config(db)
    configured, _reason = make_provider(config).configured()
    return {
        "state": "disabled" if not config.enabled else ("configured" if configured else "not_configured"),
        "enabled": config.enabled, "configured": configured, "provider": config.provider,
        "provider_label": PROVIDERS.get(config.provider, config.provider), "trigger": config.trigger,
    }


@router.get("/api/admin/websearch")
def get_settings_(db: Session = Depends(get_db), _director: User = Depends(require_role("DIRECTOR"))) -> dict:
    return _state(db)


@router.put("/api/admin/websearch")
def update_settings(
    payload: WebSearchSettingsIn, db: Session = Depends(get_db), user: User = Depends(require_role("DIRECTOR"))
) -> dict:
    values = payload.model_dump(exclude_none=True)
    try:
        save_overrides(db, values, user.username)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    service.clear_cache()
    log_event(user_id=user.id, username=user.username, role=user.role, action="WEB_SEARCH_SETTINGS_UPDATED",
              decision="ALLOW", resource_type="websearch", resource_id="settings", reason="ok", extra=values)
    return _state(db)


@router.put("/api/admin/websearch/keys")
def set_key(payload: WebSearchKeyIn, db: Session = Depends(get_db), user: User = Depends(require_role("DIRECTOR"))) -> dict:
    key = payload.api_key.strip()
    if not 8 <= len(key) <= 500:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Clé API de longueur invalide.")
    # A display mask sent back by mistake ("********", "••••") never replaces a real key.
    if set(key) <= set("*•●·xX ") or "****" in key or "••" in key:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Ceci est un masque d'affichage, pas une clé : la clé enregistrée est conservée.")
    try:
        store_key(db, payload.provider, payload.api_key, user.username)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    log_event(user_id=user.id, username=user.username, role=user.role, action="WEB_SEARCH_KEY_SET", decision="ALLOW",
              resource_type="websearch", resource_id=payload.provider, reason="ok")
    return _state(db)


@router.delete("/api/admin/websearch/keys/{provider}")
def delete_key(provider: str, db: Session = Depends(get_db), user: User = Depends(require_role("DIRECTOR"))) -> dict:
    try:
        store_key(db, provider, None, user.username)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    log_event(user_id=user.id, username=user.username, role=user.role, action="WEB_SEARCH_KEY_REMOVED", decision="ALLOW",
              resource_type="websearch", resource_id=provider, reason="ok")
    return _state(db)


@router.post("/api/admin/websearch/test")
def test_connection(db: Session = Depends(get_db), user: User = Depends(require_role("DIRECTOR"))) -> dict:
    """One real search (no page read) - even while disabled, to check the
    configuration before switching it on. Reports exactly what happened."""
    config = load_config(db)
    outcome = service.run_web_search(db, config, "ORBIT AI test de connexion", user, fetch=False, test=True)
    return {
        "provider": outcome.provider, "provider_label": outcome.provider_label, "status": outcome.status,
        "connection": service.connection_status(outcome),
        "error_kind": outcome.error_kind, "error": outcome.error, "results": len(outcome.results),
        "sample": [{"title": r.title, "url": r.url} for r in outcome.results[:3]],
        "unresponsive_engines": outcome.unresponsive_engines,
        "at": outcome.searched_at,
    }
