"""Client for the local Qwen3-30B-A3B-Instruct-2507 GGUF, served by the
`llm` container (llama.cpp's own `llama-server`, see docker-compose.yml).

This is the only place in the codebase that talks to the LLM. It never
receives a raw database session or a raw user identity - only the message
history the caller decided to send. The rest of the application only ever
uses `chat_completion`, `complete_json` and `get_llm_status`, so the engine
behind them can change without touching the agent.

Why a separate llama.cpp server rather than loading the GGUF inside this
process: the model is loaded exactly once, when the `llm` container starts,
stays resident in GPU memory, and is shared by every request - a backend
restart or reload never reloads 4 GB of weights. The GGUF file is mounted
read-only into that container only; its host path lives in the root .env
(MODEL_PATH) and is never sent to the frontend.

The GGUF includes Qwen's chat template, so system, user, assistant, and tool
messages are passed through with their roles intact.
"""

import json
import logging
import time

import httpx

from app.core.config import get_settings
from app.agent.model_provider import OpenAICompatibleProvider

settings = get_settings()
logger = logging.getLogger("orbitia.llm")


def _text_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url=settings.llm_base_url,
        model_name=settings.llm_model_name,
        api_key=settings.llm_api_key,
        timeout_seconds=settings.llm_timeout_seconds,
    )

# Shown to users - the technical cause goes to the logs only.
LLM_UNAVAILABLE_MESSAGE = (
    "Le modèle IA local n'est pas disponible pour le moment. Vérifiez que le modèle IA est correctement chargé."
)
LLM_LOADING_MESSAGE = "Chargement du modèle IA en cours, réessayez dans quelques instants."


class LLMServiceError(RuntimeError):
    """`kind` says what failed (never shown raw to the user):
    unreachable  the server does not answer (stopped, not started, port closed)
    loading      the server answers 503: the model is still loading
    timeout      the model did not finish in LLM_TIMEOUT_SECONDS
    server_error the server answered an HTTP error
    invalid      the answer could not be read"""

    def __init__(self, message: str, kind: str = "unreachable"):
        super().__init__(message)
        self.kind = kind


LLM_TIMEOUT_MESSAGE = (
    "Le modèle IA local n'a pas terminé sa réponse dans le délai prévu. Réessayez, ou posez une question plus courte."
)
LLM_ERROR_MESSAGE = "Le modèle IA local a rencontré une erreur en rédigeant la réponse. Réessayez dans quelques instants."

# Last failure seen by a real request: the status says "erreur d'inférence"
# while /health still answers 200 (the server is up, generation fails).
_last_failure: dict = {}
LAST_FAILURE_WINDOW_SECONDS = 300


def _remember_failure(kind: str) -> None:
    _last_failure.update({"kind": kind, "at": time.monotonic()})


def _clear_failure() -> None:
    _last_failure.clear()


# --- prompt shaping -----------------------------------------------------------


def _approx_tokens(text: str) -> int:
    return len(text) // 3 + 1  # conservative estimate for multilingual text


def to_qwen_messages(messages: list[dict], max_tokens: int | None = None) -> list[dict]:
    """Keep Qwen's native chat roles and trim the oldest exchanges to fit."""
    turns = [dict(message) for message in messages]
    if not any(message.get("role") == "user" for message in turns):
        raise LLMServiceError("No user message to send to the model")
    return _fit_context(turns, max_tokens or settings.llm_max_tokens)


def _fit_context(turns: list[dict], max_tokens: int | None = None) -> list[dict]:
    """Drop old user turns and their replies while preserving system prompts
    and the latest question/context."""
    budget = settings.llm_context_size - (max_tokens or settings.llm_max_tokens) - 64
    def prompt_size() -> int:
        return sum(_approx_tokens(str(turn.get("content") or "")) for turn in turns)

    while prompt_size() > budget:
        user_indexes = [i for i, turn in enumerate(turns) if turn.get("role") == "user"]
        if len(user_indexes) < 2:
            break
        oldest, next_user = user_indexes[:2]
        del turns[oldest:next_user]
    return turns


# --- calls ----------------------------------------------------------------------


async def _post_chat(payload: dict, *, return_message: bool = False) -> str | dict:
    payload = {
        "model": settings.llm_model_name,
        "stream": False,
        "cache_prompt": True,
        "temperature": settings.llm_temperature,
        "top_p": settings.llm_top_p,
        "max_tokens": settings.llm_max_tokens,
        **payload,
    }
    try:
        response = await _text_provider().chat(payload)
    except httpx.TimeoutException as exc:
        if isinstance(exc, httpx.ConnectTimeout):
            logger.error("LLM server unreachable at %s (connect timeout)", settings.llm_base_url)
            _remember_failure("unreachable")
            raise LLMServiceError(LLM_UNAVAILABLE_MESSAGE, "unreachable") from exc
        logger.error("LLM inference timed out after %ss", settings.llm_timeout_seconds)
        _remember_failure("timeout")
        raise LLMServiceError(LLM_TIMEOUT_MESSAGE, "timeout") from exc
    except httpx.HTTPError as exc:
        logger.error("LLM server unreachable at %s: %s", settings.llm_base_url, type(exc).__name__)
        _remember_failure("unreachable")
        raise LLMServiceError(LLM_UNAVAILABLE_MESSAGE, "unreachable") from exc
    if response.status_code == 503:
        _remember_failure("loading")
        raise LLMServiceError(LLM_LOADING_MESSAGE, "loading")
    if response.status_code >= 400:
        # status only: an error body may echo part of the prompt (documents)
        logger.error("LLM server error HTTP %s", response.status_code)
        _remember_failure("server_error")
        raise LLMServiceError(LLM_ERROR_MESSAGE, "server_error")
    try:
        message = response.json()["choices"][0]["message"]
        if return_message:
            if not isinstance(message, dict):
                raise TypeError("message is not an object")
            _clear_failure()
            return message
        content = message["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.error("Unexpected LLM payload (HTTP %s, %d bytes)", response.status_code, len(response.content))
        _remember_failure("invalid")
        raise LLMServiceError(LLM_ERROR_MESSAGE, "invalid") from exc
    _clear_failure()
    return content


async def chat_completion(messages: list[dict], tools: list[dict] | None = None, *, max_tokens: int | None = None) -> dict:
    """Call the local model and preserve constrained function calls.

    `max_tokens` allows longer answers (such as full reports), with a smaller
    prompt budget to fit the model's context window.
    """
    payload = {"messages": to_qwen_messages(messages, max_tokens)}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if max_tokens:
        payload["max_tokens"] = max_tokens
    message = await _post_chat(payload, return_message=True)
    if not isinstance(message, dict):
        raise LLMServiceError(LLM_ERROR_MESSAGE, "invalid")
    return {"role": "assistant", "content": (message.get("content") or "").strip(),
            "tool_calls": message.get("tool_calls") or None}


async def complete_json(messages: list[dict], schema: dict, *, max_tokens: int = 256) -> dict:
    """Answer constrained by llama.cpp's grammar engine to a JSON object
    matching `schema` - the model cannot produce anything else (e.g. an
    `enum` of known ids means it cannot name an id that doesn't exist).
    Callers must still validate the content."""
    content = await _post_chat(
        {
            "messages": to_qwen_messages(messages),
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object", "schema": schema},
        }
    )
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        logger.error("LLM returned invalid JSON despite the schema (%d chars)", len(content))
        raise LLMServiceError(LLM_ERROR_MESSAGE, "invalid") from exc


async def get_llm_status() -> dict:
    """{"status": "ready" | "loading" | "unavailable" | "error" |
    "misconfigured", "model", "message", "detail"} - no path, no key, ever.
    Read from the server itself (/health, /v1/models), never from the
    configuration alone; "error" = the server is up but the last real
    request failed (timeout, HTTP error) less than 5 minutes ago."""
    status, message = "unavailable", LLM_UNAVAILABLE_MESSAGE
    detail = "le serveur du modèle ne répond pas (arrêté ou injoignable)"
    provider = _text_provider()
    try:
        response = await provider.health()
        if response.status_code == 200:
            status, message, detail = "ready", "Modèle IA prêt", None
        elif response.status_code == 503:
            status, message, detail = "loading", "Chargement du modèle IA...", "le serveur démarre et charge le modèle"
        else:
            detail = f"le serveur du modèle répond HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        logger.info("LLM health check failed: %s", type(exc).__name__)
    if status == "ready":
        try:
            body = (await provider.models()).json()
            ids = [str(m.get("id")) for m in body.get("data", []) if isinstance(m, dict)] if isinstance(body, dict) else []
            if ids and settings.llm_model_name not in ids:
                status, message = "misconfigured", "Modèle IA mal configuré"
                detail = "le serveur ne sert pas le modèle attendu (LLM_MODEL_NAME)"
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
    recent = _last_failure and time.monotonic() - _last_failure.get("at", 0) < LAST_FAILURE_WINDOW_SECONDS
    if status == "ready" and recent and _last_failure.get("kind") in ("timeout", "server_error", "invalid"):
        status, message = "error", "Erreur du modèle IA"
        detail = {"timeout": "la dernière réponse a dépassé le délai", "server_error": "la dernière génération a échoué",
                  "invalid": "la dernière réponse était illisible"}[_last_failure["kind"]]
    return {"status": status, "model": settings.llm_display_name, "message": message, "detail": detail}
