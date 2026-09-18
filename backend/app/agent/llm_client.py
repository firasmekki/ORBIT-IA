"""Client for the local Ollama chat model.

This is the only place in the codebase that talks to the LLM. It never
receives a raw database session or a raw user identity - only the message
history and the tool catalog the caller decided to expose.
"""

import httpx

from app.core.config import get_settings

settings = get_settings()


class LLMServiceError(RuntimeError):
    pass


async def chat_completion(messages: list[dict], tools: list[dict] | None = None) -> dict:
    payload = {
        "model": settings.ollama_chat_model,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{settings.ollama_base_url}/api/chat", json=payload)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMServiceError(f"Le modèle local (Ollama) est indisponible : {exc}") from exc

    data = response.json()
    message = data.get("message")
    if not message:
        raise LLMServiceError("Réponse inattendue du modèle local (aucun message).")
    return message
