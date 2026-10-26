"""Authenticated, bounded client for ORBIT's optional local vision model."""

import base64
import json
import logging
import warnings
from io import BytesIO

import httpx
from PIL import Image, UnidentifiedImageError

from app.agent.llm_client import LLMServiceError, LLM_UNAVAILABLE_MESSAGE, LLM_LOADING_MESSAGE
from app.agent.model_provider import OpenAICompatibleProvider
from app.core.config import get_settings

logger = logging.getLogger("orbitia.vision")
MAX_IMAGE_PIXELS = 20_000_000


class VisionUnavailableError(LLMServiceError):
    """The vision server is not there (stopped, loading, connection cut
    mid-request) - nothing is wrong with the image: it must go back to the
    queue without counting as a failed attempt."""


class InvalidImageError(LLMServiceError):
    """The attachment itself is the problem (empty, too big, not an image,
    unsupported format) - not the vision server."""


_FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


async def wait_until_ready(timeout_seconds: float = 90, poll_seconds: float = 3) -> str:
    """Before sending an image: "ready" (loaded, or asleep - the request
    itself wakes it), else waits while the model is loading. Returns the
    last status seen: ready | loading | unavailable | disabled."""
    import asyncio  # noqa: PLC0415
    import time  # noqa: PLC0415

    deadline = time.monotonic() + timeout_seconds
    while True:
        status = (await get_vision_status()).get("status")
        if status != "loading" or time.monotonic() >= deadline:
            return status
        await asyncio.sleep(poll_seconds)


# The server is absent or went away mid-request (stopped, restarted).
# A read timeout is NOT here: the server answered too slowly for this image.
_UNAVAILABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.ReadError)


def _normalize_observation(content: str) -> dict:
    """Return a stable vision shape without trusting malformed fields."""
    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        parsed = None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("description"), str):
        return {
            "description": content.strip(),
            "objects": [],
            "measurements": [],
            "text": [],
            "warnings": ["The vision response was not structured JSON."],
            "confidence": None,
        }
    confidence = parsed.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        confidence = None
    return {
        "description": parsed["description"].strip()[:4000],
        "objects": parsed.get("objects") if isinstance(parsed.get("objects"), list) else [],
        "measurements": parsed.get("measurements") if isinstance(parsed.get("measurements"), list) else [],
        "text": parsed.get("text") if isinstance(parsed.get("text"), list) else [],
        "warnings": parsed.get("warnings") if isinstance(parsed.get("warnings"), list) else [],
        "confidence": confidence,
    }


def _provider() -> OpenAICompatibleProvider:
    settings = get_settings()
    return OpenAICompatibleProvider(
        base_url=settings.vision_base_url,
        model_name=settings.vision_model_name,
        api_key=settings.vision_api_key or settings.llm_api_key,
        timeout_seconds=settings.vision_timeout_seconds,
    )


async def describe_image(*, image: bytes, mime_type: str, prompt: str) -> tuple[dict, dict | None]:
    """Describe one validated image; image contents are never logged or stored."""
    settings = get_settings()
    if not settings.vision_enabled:
        raise VisionUnavailableError("Le moteur d’analyse d’images n’est pas activé sur ce serveur.")
    if not image:
        raise InvalidImageError("la pièce jointe est vide.")
    if len(image) > settings.vision_max_image_bytes:
        raise InvalidImageError("l’image dépasse la taille autorisée (8 Mio).")
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise InvalidImageError("format non pris en charge (JPEG, PNG ou WebP).")
    # The declared type comes from the file name: the real format is read
    # from the bytes (a PNG saved as ".jpeg" is still a valid PNG).
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(image)) as decoded:
                actual = decoded.format
                if actual not in _FORMAT_MIME:
                    raise InvalidImageError(f"format réel {actual or 'inconnu'} non pris en charge (JPEG, PNG ou WebP).")
                if decoded.width * decoded.height > MAX_IMAGE_PIXELS:
                    raise InvalidImageError("l’image dépasse la limite de 20 millions de pixels.")
                decoded.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InvalidImageError("le fichier reçu n’est pas une image lisible.") from None
    mime_type = _FORMAT_MIME[actual]

    data_url = f"data:{mime_type};base64,{base64.b64encode(image).decode('ascii')}"
    safe_prompt = prompt.strip() or "Décris cette image et relève les éléments utiles à la question."
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "Analyze industrial images and documents. Reply only with a JSON object containing description (string), "
                    "objects (array), measurements (array), text (array), warnings (array) and confidence (0 to 1 or null). "
                    "Describe only visible content. Transcribe displayed readings as visible text, never as verified "
                    "instrument measurements. Do not infer measurements from an image. State uncertainty in warnings and "
                    "preserve the user's language."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": safe_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0.1,
        "max_tokens": min(settings.llm_max_tokens, 768),
    }
    try:
        response = await _provider().chat(payload)
    except _UNAVAILABLE_ERRORS as exc:
        logger.error("Vision server unreachable at %s: %s", settings.vision_base_url, exc)
        raise VisionUnavailableError(LLM_UNAVAILABLE_MESSAGE) from exc
    except httpx.HTTPError as exc:
        logger.error("Vision request failed at %s: %s", settings.vision_base_url, type(exc).__name__)
        kind = "délai dépassé" if isinstance(exc, httpx.TimeoutException) else type(exc).__name__
        raise LLMServiceError(f"L'analyse de l'image par le modèle vision a échoué ({kind}).") from exc
    if response.status_code == 503:
        raise VisionUnavailableError(LLM_LOADING_MESSAGE)
    if response.status_code >= 400:
        logger.error("Vision server error %s: %s", response.status_code, response.text[:300])
        raise LLMServiceError(f"Le modèle vision a renvoyé une erreur (HTTP {response.status_code}).")
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty vision response")
        return _normalize_observation(content), body.get("usage")
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.error("Unexpected vision response payload")
        raise LLMServiceError("Le modèle vision a renvoyé une réponse vide ou illisible.") from exc


async def get_vision_status() -> dict:
    settings = get_settings()
    if not settings.vision_enabled:
        return {"status": "disabled", "model": settings.vision_display_name}
    try:
        response = await _provider().health()
        if response.status_code == 200:
            return {"status": "ready", "model": settings.vision_display_name}
        if response.status_code == 503:
            return {"status": "loading", "model": settings.vision_display_name}
    except httpx.HTTPError as exc:
        logger.info("Vision health check failed: %s", exc)
    return {"status": "unavailable", "model": settings.vision_display_name}
