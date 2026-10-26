"""Web search settings: .env defaults (app/core/config.py) + administrator
overrides (runtime_settings "web_search"), and API keys.

Keys come from .env (BRAVE_SEARCH_API_KEY, GOOGLE_SEARCH_API_KEY) or are
entered in the admin page: then stored in runtime_settings
"web_search_secrets", ENCRYPTED (Fernet, key derived from the server's
JWT secret - a database dump alone does not reveal them). A key is never
returned by the API: only "configured: yes/no" and where it comes from.
"""

import base64
import hashlib
from dataclasses import asdict, dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.runtime_setting import RuntimeSetting
from app.websearch.providers import (
    BRAVE_COUNTRIES,
    PROVIDERS,
    BraveSearchProvider,
    GoogleProgrammableSearchProvider,
    WebSearchProvider,
)

SETTINGS_KEY = "web_search"
SECRETS_KEY = "web_search_secrets"
LANGUAGES = ("fr", "en", "ar")
TRIGGERS = ("auto", "manual")
KEY_PROVIDERS = ("brave", "google")


@dataclass
class WebSearchConfig:
    enabled: bool
    provider: str
    trigger: str
    max_results: int
    fetch_pages: int
    timeout_seconds: float
    language: str
    country: str
    cache_ttl_seconds: int
    max_per_hour: int
    fetch_max_bytes: int
    google_engine_id: str
    searxng_url: str = "http://searxng:8080"
    brave_api_key: str = ""
    google_api_key: str = ""
    brave_key_source: str | None = None  # "env" | "admin" | None
    google_key_source: str | None = None

    def public(self) -> dict:
        """Everything but the keys."""
        data = asdict(self)
        data.pop("brave_api_key")
        data.pop("google_api_key")
        data["keys"] = {
            "brave": {"configured": bool(self.brave_api_key), "source": self.brave_key_source},
            "google": {"configured": bool(self.google_api_key), "source": self.google_key_source},
        }
        data["keys"]["searxng"] = {"configured": bool(self.searxng_url), "source": "local" if self.searxng_url else None}
        return data


def _fernet() -> Fernet:
    secret = get_settings().jwt_secret.encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"orbit-websearch-keys|" + secret).digest()))


def _overrides(db: Session) -> dict:
    row = db.get(RuntimeSetting, SETTINGS_KEY)
    return dict(row.value or {}) if row else {}


def _secrets(db: Session) -> dict:
    row = db.get(RuntimeSetting, SECRETS_KEY)
    return dict(row.value or {}) if row else {}


def _decrypt(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""  # unreadable (server secret changed): treated as absent


def load_config(db: Session) -> WebSearchConfig:
    s = get_settings()
    o = _overrides(db)
    stored = _secrets(db)
    brave_admin = _decrypt(stored.get("brave_api_key"))
    google_admin = _decrypt(stored.get("google_api_key"))
    return WebSearchConfig(
        enabled=bool(o.get("enabled", s.web_search_enabled)),
        provider=o.get("provider", s.web_search_provider) if o.get("provider", s.web_search_provider) in PROVIDERS else "brave",
        trigger=o.get("trigger", s.web_search_trigger) if o.get("trigger", s.web_search_trigger) in TRIGGERS else "auto",
        max_results=max(1, min(int(o.get("max_results", s.web_search_max_results)), 10)),
        fetch_pages=max(0, min(int(o.get("fetch_pages", s.web_search_fetch_pages)), 5)),
        timeout_seconds=max(2.0, min(float(o.get("timeout_seconds", s.web_search_timeout_seconds)), 60.0)),
        language=str(o.get("language", s.web_search_language) or ""),
        country=str(o.get("country", s.web_search_country) or ""),
        cache_ttl_seconds=max(0, int(o.get("cache_ttl_seconds", s.web_search_cache_ttl_seconds))),
        max_per_hour=max(1, int(o.get("max_per_hour", s.web_search_max_per_hour))),
        fetch_max_bytes=s.web_fetch_max_bytes,
        google_engine_id=str(o.get("google_engine_id", s.google_search_engine_id) or ""),
        searxng_url=str(o.get("searxng_url", s.searxng_url) or ""),
        brave_api_key=s.brave_search_api_key or brave_admin,
        google_api_key=s.google_search_api_key or google_admin,
        brave_key_source="env" if s.brave_search_api_key else ("admin" if brave_admin else None),
        google_key_source="env" if s.google_search_api_key else ("admin" if google_admin else None),
    )


EDITABLE = {
    "enabled": bool, "provider": str, "trigger": str, "max_results": int, "fetch_pages": int,
    "timeout_seconds": float, "language": str, "country": str, "cache_ttl_seconds": int,
    "max_per_hour": int, "google_engine_id": str, "searxng_url": str,
}


def save_overrides(db: Session, values: dict, updated_by: str) -> None:
    clean = {}
    for key, kind in EDITABLE.items():
        if key in values and values[key] is not None:
            clean[key] = kind(values[key])
    if "provider" in clean and clean["provider"] not in PROVIDERS:
        raise ValueError("fournisseur inconnu")
    if "trigger" in clean and clean["trigger"] not in TRIGGERS:
        raise ValueError("déclenchement inconnu")
    if "country" in clean and clean["country"]:
        clean["country"] = clean["country"].strip().upper()
        stored = db.get(RuntimeSetting, SETTINGS_KEY)
        provider = clean.get("provider") or ((stored.value or {}).get("provider") if stored else None) \
            or get_settings().web_search_provider
        if provider == "brave" and clean["country"] not in BRAVE_COUNTRIES:
            raise ValueError(
                f"pays « {clean['country']} » non pris en charge par Brave Search (la Tunisie, le Maroc et l'Algérie "
                "n'y figurent pas) : laissez vide ou ALL pour tous les pays, ou choisissez par ex. FR, BE, CA, CH."
            )
        if provider != "brave" and not (len(clean["country"]) == 2 and clean["country"].isalpha()):
            raise ValueError("pays : code ISO à 2 lettres ou vide")
    if "language" in clean and clean["language"] and clean["language"] not in LANGUAGES:
        raise ValueError("langue : fr, en ou ar")
    if "searxng_url" in clean:
        from urllib.parse import urlparse  # noqa: PLC0415
        value = clean["searxng_url"].strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("URL SearXNG invalide : HTTP(S), sans identifiants, paramètres ni fragment requis")
        if parsed.scheme == "http" and not (parsed.hostname == "searxng" and parsed.port in (None, 8080)):
            raise ValueError("Une URL HTTP est autorisée uniquement pour le service Docker local http://searxng:8080")
        clean["searxng_url"] = value
    row = db.get(RuntimeSetting, SETTINGS_KEY) or RuntimeSetting(key=SETTINGS_KEY, value={})
    row.value = {**(row.value or {}), **clean}
    row.updated_by = updated_by
    db.add(row)
    db.commit()


def store_key(db: Session, provider: str, api_key: str | None, updated_by: str) -> None:
    """Encrypted at rest; None/"" removes the admin-entered key."""
    if provider not in KEY_PROVIDERS:
        raise ValueError("fournisseur inconnu")
    row = db.get(RuntimeSetting, SECRETS_KEY) or RuntimeSetting(key=SECRETS_KEY, value={})
    values = dict(row.value or {})
    field = f"{provider}_api_key"
    if api_key and api_key.strip():
        values[field] = _fernet().encrypt(api_key.strip().encode()).decode()
    else:
        values.pop(field, None)
    row.value = values
    row.updated_by = updated_by
    db.add(row)
    db.commit()


def make_provider(config: WebSearchConfig, transport=None) -> WebSearchProvider:
    if config.provider == "google":
        return GoogleProgrammableSearchProvider(
            api_key=config.google_api_key, engine_id=config.google_engine_id,
            timeout_seconds=config.timeout_seconds, transport=transport,
        )
    if config.provider == "searxng":
        from app.websearch.providers import SearXNGSearchProvider  # noqa: PLC0415
        return SearXNGSearchProvider(base_url=config.searxng_url, timeout_seconds=config.timeout_seconds, transport=transport)
    return BraveSearchProvider(api_key=config.brave_api_key, timeout_seconds=config.timeout_seconds, transport=transport)
