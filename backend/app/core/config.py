from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database - the app itself (backend/mcp/seed/watcher, anything using
    # app.core.database.SessionLocal) connects as `orbitia_app`, a
    # low-privilege role with no DDL/TRUNCATE rights and only INSERT+SELECT
    # on audit_logs (see app.core.migrations.bootstrap_roles_and_grants).
    # `migration_database_url` is the schema owner (`orbitia`) - used only
    # for schema migrations and backups/restores, never for normal request
    # handling. Keeping the app's day-to-day connection unable to even
    # issue a TRUNCATE is the main lesson from INCIDENTS.md.
    database_url: str = "postgresql+psycopg://orbitia_app:orbitia_app@localhost:5432/orbitia"
    migration_database_url: str = "postgresql+psycopg://orbitia:orbitia@localhost:5432/orbitia"
    app_db_password: str = "orbitia_app"

    # Backups (app.core.backup) - a pg_dump of the whole database, taken
    # automatically before every schema migration and once a day by the
    # `backup` compose service. See README's "Sauvegardes" section for the
    # restore command.
    backup_dir: str = "/backups"
    backup_retention_days: int = 7
    # A restart used to take three full dumps at once (backend + seed
    # pre-migration, plus the `backup` service's daily one), keeping the API
    # down (502) until they finished. A complete dump younger than this is
    # reused instead of taking a new one; 0 = always dump (previous behavior).
    backup_reuse_minutes: int = 360
    backup_daily_reuse_minutes: int = 1200
    backup_timeout_seconds: int = 900

    # User-facing auth (would be replaced by Keycloak/OIDC in production;
    # kept as a pluggable JWT issuer so the rest of the app is provider-agnostic)
    jwt_secret: str = "change-me-dev-secret"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 8

    # Internal service token used ONLY between the backend and the MCP server.
    # Never derived from anything the LLM or the end user supplies directly -
    # minted server-side from the authenticated session on every agent turn.
    internal_auth_secret: str = "change-me-internal-secret"
    internal_auth_expire_seconds: int = 120

    # MCP server (internal network only, never exposed to the host/Internet)
    mcp_server_url: str = "http://mcp:8090/mcp"

    # Local LLM: Qwen3-30B-A3B-Instruct-2507 (GGUF) served by the `llm`
    # container (llama.cpp's llama-server, private network only - see
    # app/agent/llm_client.py). The GGUF file itself is configured ONCE, as
    # MODEL_PATH in the root .env, and mounted read-only into that container
    # only: the backend never needs (nor exposes) its path. Load-time
    # parameters (context size, GPU layers, threads) are set on that
    # container, LLM_* in .env; the generation ones below apply per request.
    llm_base_url: str = "http://host.docker.internal:8096"
    llm_api_key: str = ""
    llm_model_name: str = "Qwen3-30B-A3B-Instruct-2507"
    llm_display_name: str = "Qwen3-30B-A3B-Instruct-2507 (Q4_K_M)"
    llm_context_size: int = 4096  # must match the llm container's --ctx-size
    llm_temperature: float = 0.2  # low: factual, grounded answers
    llm_top_p: float = 0.9
    llm_max_tokens: int = 768
    llm_timeout_seconds: float = 180

    # Optional multimodal provider. It stays separate from the text model so
    # vision can be started, stopped, or disabled without changing Qwen 30B.
    vision_enabled: bool = False
    vision_base_url: str = "http://host.docker.internal:8097"
    vision_api_key: str = ""
    vision_model_name: str = "qwen3-vl-8b"
    vision_display_name: str = "Qwen3-VL-8B-Instruct (Q4_K_M)"
    vision_timeout_seconds: float = 180
    vision_max_image_bytes: int = 8 * 1024 * 1024
    # Idle period after which each llama-server unloads its model
    # (--sleep-idle-seconds, set by start-orbit-ai.ps1 from the same .env
    # values); -1 = always loaded. Read here only to display it.
    llm_sleep_idle_seconds: int = -1
    vision_sleep_idle_seconds: int = 300

    # Web search (app/websearch) - OFF by default. Defaults below; the
    # Director can override them in Admin > Recherche Web (stored in
    # runtime_settings). API keys: here (.env) or entered in the admin page
    # (stored encrypted) - never sent back to a browser, never logged.
    web_search_enabled: bool = False
    web_search_provider: str = "brave"  # searxng | brave | google
    searxng_url: str = "http://searxng:8080"
    web_search_trigger: str = "auto"  # auto: when the question needs it | manual: only when the user picks Web/Hybride
    web_search_max_results: int = 5
    web_search_fetch_pages: int = 3
    web_search_timeout_seconds: float = 10
    web_search_language: str = "fr"
    web_search_country: str = ""  # "" = all countries (Brave only accepts the countries it lists)
    web_search_cache_ttl_seconds: int = 3600
    web_search_max_per_hour: int = 60  # cost guard: searches per hour for the whole server
    web_fetch_max_bytes: int = 1_500_000
    brave_search_api_key: str = ""
    google_search_api_key: str = ""
    google_search_engine_id: str = ""

    # Google Gmail OAuth. Callback must exactly match a URI registered in
    # Google Cloud Console; credentials stay on the backend.
    gmail_oauth_client_id: str = ""
    gmail_oauth_client_secret: str = ""
    gmail_token_encryption_key: str = ""
    gmail_oauth_redirect_uri: str = "http://localhost:8081/api/connectivity/gmail/callback"
    gmail_frontend_redirect_uri: str = "http://localhost:8081/connectivity"

    # Embeddings for the RAG index - still served by Ollama (nomic-embed-text,
    # 768 dims). Qwen3 is a generation model, not an embedding model:
    # swapping it in here would change the vector dimension and require
    # re-indexing every document.
    ollama_base_url: str = "http://ollama:11434"
    ollama_embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768
    # Synced files extracted in parallel in the background (pypdf, OCR).
    # Each one uses a single Tesseract thread, leaving CPU for the local LLM.
    extraction_concurrency: int = 4

    # MinIO (raw document storage)
    minio_endpoint: str = "objectstore:9000"
    minio_access_key: str = "orbitia"
    minio_secret_key: str = "orbitia123"
    minio_bucket: str = "company-documents"
    minio_secure: bool = False

    # Auto-import: a folder mounted into this container (see
    # docker-compose.yml `backend.volumes`) that gets scanned periodically -
    # any new supported file dropped there is ingested automatically,
    # tagged with the department/confidentiality below (same for every file
    # in this folder; for mixed sensitivity, upload the rest by hand).
    watch_folder_enabled: bool = False
    watch_folder_path: str = "/data/watch"
    watch_folder_department: str = "GENERAL"
    watch_folder_confidentiality: str = "INTERNAL"
    watch_folder_interval_seconds: int = 10

    # Server folder sources (app/services/source_roots.py). Up to three host
    # folders, set ONCE in .env (ORBIT_SOURCE_ROOT_1..3) and mounted
    # read-only at /sources/1..3 by docker-compose.yml. Any folder under
    # them can then be chosen from the app without touching Docker. The
    # host path is only ever shown to the user, never used to open a file.
    source_root_1_host: str = ""
    source_root_2_host: str = ""
    source_root_3_host: str = ""
    source_mount_base: str = "/sources"
    # Full rescan of every enabled source (new/changed/deleted files);
    # "Analyser maintenant" triggers one at once.
    source_scan_interval_seconds: int = 600
    # Comma-separated roles allowed to browse the server's folders and
    # connect one: listing folder names of the server PC is itself sensitive.
    source_admin_roles: str = "DIRECTOR"

    # Comma-separated, not JSON - trivially safe to interpolate into a
    # docker-compose env value or a plain .env line without quote/escaping
    # gymnastics (e.g. CORS_ORIGINS=https://orbitia.example.com,http://localhost:8081).
    cors_origins: str = "http://localhost:8081,http://localhost:5173"

    @property
    def source_admin_roles_set(self) -> frozenset[str]:
        return frozenset(role.strip() for role in self.source_admin_roles.split(",") if role.strip())

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
