"""Small transport adapter for local OpenAI-compatible model servers.

Providers are configured by the backend only. The frontend never receives
model-server URLs or API keys. Keeping transport here lets the existing text
model and optional vision model share one tested interface.
"""

import logging
import time
from dataclasses import dataclass

import httpx

from app.core import activity

logger = logging.getLogger("orbitia.inference")


@dataclass(frozen=True)
class OpenAICompatibleProvider:
    base_url: str
    model_name: str
    api_key: str = ""
    timeout_seconds: float = 180

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def chat(self, payload: dict) -> httpx.Response:
        """Every inference goes through here: its start, end, duration and
        token counts are logged (never the prompt or the answer)."""
        request = {"model": self.model_name, "stream": False, **payload}
        started = time.monotonic()
        logger.info("inference start model=%s max_tokens=%s", self.model_name, request.get("max_tokens"))
        activity.inference_started(self.model_name)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, headers=self.headers) as client:
                response = await client.post(f"{self.base_url.rstrip('/')}/v1/chat/completions", json=request)
        except httpx.HTTPError as exc:
            logger.info(
                "inference end model=%s failed after %.1fs: %s", self.model_name, time.monotonic() - started,
                type(exc).__name__,
            )
            raise
        finally:
            activity.inference_finished(self.model_name)
        usage = {}
        try:
            usage = response.json().get("usage") or {}
        except (ValueError, AttributeError):
            pass
        logger.info(
            "inference end model=%s status=%s duration=%.1fs prompt_tokens=%s completion_tokens=%s",
            self.model_name, response.status_code, time.monotonic() - started,
            usage.get("prompt_tokens"), usage.get("completion_tokens"),
        )
        return response

    async def health(self) -> httpx.Response:
        async with httpx.AsyncClient(timeout=3, headers=self.headers) as client:
            return await client.get(f"{self.base_url.rstrip('/')}/health")

    async def models(self) -> httpx.Response:
        """Models the server really serves (does not wake a sleeping model)."""
        async with httpx.AsyncClient(timeout=3, headers=self.headers) as client:
            return await client.get(f"{self.base_url.rstrip('/')}/v1/models")

    async def props(self) -> httpx.Response:
        """Server properties, including `is_sleeping`. Like /health it does
        not wake a sleeping model - unlike /slots, never called here."""
        async with httpx.AsyncClient(timeout=3, headers=self.headers) as client:
            return await client.get(f"{self.base_url.rstrip('/')}/props")
