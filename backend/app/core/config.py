from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql+psycopg://orbitia:orbitia@localhost:5432/orbitia"

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

    # Ollama (local LLM, stays inside the private network)
    ollama_base_url: str = "http://ollama:11434"
    ollama_chat_model: str = "llama3.2:1b"
    ollama_embedding_model: str = "nomic-embed-text"
    embedding_dim: int = 768

    # MinIO (raw document storage)
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "orbitia"
    minio_secret_key: str = "orbitia123"
    minio_bucket: str = "company-documents"
    minio_secure: bool = False

    # Comma-separated, not JSON - trivially safe to interpolate into a
    # docker-compose env value or a plain .env line without quote/escaping
    # gymnastics (e.g. CORS_ORIGINS=https://orbitia.example.com,http://localhost:8081).
    cors_origins: str = "http://localhost:8081,http://localhost:5173"

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
