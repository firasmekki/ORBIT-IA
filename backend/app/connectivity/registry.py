"""One public registry for connector status, tools and permission summaries."""

from app.connectivity.email import public_config
from app.connectivity.gmail import configured as gmail_configured
from app.websearch.config import load_config, make_provider
from app.websearch.providers import PROVIDERS
from app.websearch.service import last_status as web_last_status


FUTURE_CONNECTORS = [
    {"id": "whatsapp_business", "name": "WhatsApp Business Cloud API", "category": "Messagerie", "protocol": "Meta Graph API + webhook signé", "state": "development",
     "description": "Non activé : identifiants Meta, webhook signé, règles de conversation et coûts à configurer avant usage.", "tools": [], "permissions": []},
    {"id": "microsoft_365", "name": "Microsoft 365", "category": "Email", "protocol": "OAuth 2.0 / Microsoft Graph", "state": "development",
     "description": "Application Entra ID et consentement OAuth requis.", "tools": [], "permissions": []},
    {"id": "calendar", "name": "Calendrier", "category": "Planning", "protocol": "Google Calendar / Microsoft Graph", "state": "development",
     "description": "Recherche en lecture ; création après confirmation à développer.", "tools": [], "permissions": []},
    {"id": "storage", "name": "Stockage documentaire", "category": "Fichiers", "protocol": "Drive / OneDrive / SharePoint", "state": "development",
     "description": "Connexion aux stockages externes à développer avec ACL et RAG.", "tools": [], "permissions": []},
    {"id": "custom_api", "name": "API personnalisées", "category": "API", "protocol": "HTTPS", "state": "development",
     "description": "Validation SSRF et schémas de réponse requis avant activation.", "tools": [], "permissions": []},
    {"id": "automation", "name": "Automatisation", "category": "Workflows", "protocol": "Webhooks / n8n", "state": "development",
     "description": "Déclenchement contrôlé et historique à développer.", "tools": [], "permissions": []},
    {"id": "industrial", "name": "Outils industriels", "category": "Industrie", "protocol": "ORBIT API / MQTT / OPC UA", "state": "development",
     "description": "Connecteur industriel à développer.", "tools": [], "permissions": []},
]


def list_connectors(db):
    web = load_config(db)
    web_ok, reason = make_provider(web).configured()
    web_status = web_last_status()
    connection = web_status.get("connection")
    web_state = (
        "disabled" if not web.enabled else
        "not_configured" if not web_ok else
        "connected" if connection == "connected" else
        "auth_expired" if connection == "invalid_key" else
        "connection_error" if connection in ("timeout", "network_error", "provider_error", "config_error") else
        "configured"
    )
    webtools = ["web_search", "fetch_web_pages"] if web.enabled and web_ok else []
    mail = public_config(db)
    mail_state = mail["state"]
    connectors = [
        {"id": "web_search", "name": "Recherche Web", "category": "Recherche", "protocol": PROVIDERS.get(web.provider, web.provider),
         "description": "Recherche Brave existante, respect des réglages administrateur, du cache et des quotas.",
         "state": web_state, "enabled": web.enabled, "provider": web.provider, "tools": webtools,
         "permissions": ["read"] if webtools else [],
         "error": (reason if not web_ok and web.enabled else web_status.get("error") if connection not in (None, "connected") else None),
         "configured": web_ok, "trigger": web.trigger, "last_status": web_status or None},
        {"id": "email_imap", "name": "Email IMAP / SMTP", "category": "Email", "protocol": "IMAP + SMTP (TLS)",
         "description": "Boîte générique ; recherche et lecture, brouillon. L'envoi passe par une confirmation séparée.",
         "state": mail_state, "enabled": mail["enabled"], "provider": "imap_smtp",
         "tools": [name for name, active in mail["tools"].items() if active] if mail_state in ("configured", "connected") and mail["enabled"] else [],
         "permissions": ([f"{tool}: {r}" for tool, roles in mail["tool_roles"].items() for r in roles]
                         + [f"envoi après confirmation: {r}" for r in mail["send_roles"]]),
         "configured": mail["state"] == "configured", "last_status": mail["last_status"]},
        {"id": "google_gmail", "name": "Gmail / Google Workspace", "category": "Email", "protocol": "OAuth 2.0 / Gmail API",
         "description": "Accès en lecture avec OAuth Google ; chaque utilisateur voit uniquement ses comptes Gmail.",
         "state": "configured" if gmail_configured() else "not_configured", "enabled": gmail_configured(),
         "provider": "gmail_api", "tools": ["gmail_search", "gmail_read", "gmail_read_thread"] if gmail_configured() else [],
         "permissions": ["gmail.readonly"] if gmail_configured() else [], "configured": gmail_configured(), "last_status": None},
        *FUTURE_CONNECTORS,
    ]
    return connectors
