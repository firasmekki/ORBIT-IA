"""Embedding client for the local Ollama instance.

Kept synchronous and dependency-free (plain httpx) on purpose: it is called
both from the (sync) document ingestion path and, via a thread-pool hop,
from the async agent orchestrator - one implementation, no duplication.
"""

import httpx

from app.core.config import get_settings

settings = get_settings()


class EmbeddingServiceError(RuntimeError):
    """Raised when Ollama is unreachable or returns an unexpected payload."""


def embed_text(text: str) -> list[float]:
    try:
        response = httpx.post(
            f"{settings.ollama_base_url}/api/embeddings",
            json={"model": settings.ollama_embedding_model, "prompt": text},
            timeout=60,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise EmbeddingServiceError(
            f"Impossible de contacter le service d'embeddings local (Ollama) : {exc}"
        ) from exc

    data = response.json()
    embedding = data.get("embedding")
    if not embedding or len(embedding) != settings.embedding_dim:
        raise EmbeddingServiceError(
            f"Réponse d'embedding invalide depuis Ollama (dimension attendue: {settings.embedding_dim})."
        )
    return embedding
