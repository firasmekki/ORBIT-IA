from types import SimpleNamespace
from io import BytesIO

import pytest
from PIL import Image

from app.agent import vision_client
from app.agent.llm_client import LLMServiceError
from app.schemas.chat import ChatRequest


class FakeResponse:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "Une vanne rouge est visible."}}], "usage": {"total_tokens": 12}}


class FakeProvider:
    async def chat(self, payload):
        self.payload = payload
        return FakeResponse()


def tiny_png():
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color="red").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def vision_settings(monkeypatch):
    settings = SimpleNamespace(
        vision_enabled=True,
        vision_max_image_bytes=8 * 1024 * 1024,
        vision_base_url="http://vision.test",
        vision_model_name="qwen3-vl-8b",
        vision_api_key="",
        llm_api_key="secret",
        vision_timeout_seconds=2,
        llm_max_tokens=768,
        vision_display_name="Qwen3-VL-8B",
    )
    monkeypatch.setattr(vision_client, "get_settings", lambda: settings)
    return settings


@pytest.mark.asyncio
async def test_describe_image_sends_bounded_data_url_and_returns_observation(monkeypatch, vision_settings):
    provider = FakeProvider()
    monkeypatch.setattr(vision_client, "_provider", lambda: provider)

    result, usage = await vision_client.describe_image(image=tiny_png(), mime_type="image/png", prompt="Que vois-tu ?")

    assert result["description"] == "Une vanne rouge est visible."
    assert result["warnings"]
    assert usage == {"total_tokens": 12}
    image_part = provider.payload["messages"][1]["content"][1]
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_normalizes_structured_observation_and_rejects_invalid_confidence():
    observation = vision_client._normalize_observation(
        '{"description":"Armoire visible","objects":["armoire"],"measurements":[],"text":["230 V"],"warnings":[],"confidence":8}'
    )
    assert observation["objects"] == ["armoire"]
    assert observation["text"] == ["230 V"]
    assert observation["confidence"] is None


@pytest.mark.asyncio
async def test_describe_image_rejects_disabled_unsupported_and_oversized(monkeypatch, vision_settings):
    with pytest.raises(LLMServiceError):
        await vision_client.describe_image(image=b"x", mime_type="image/png", prompt="")

    vision_settings.vision_enabled = True
    with pytest.raises(LLMServiceError):
        await vision_client.describe_image(image=b"x", mime_type="image/svg+xml", prompt="")

    vision_settings.vision_max_image_bytes = 8
    with pytest.raises(LLMServiceError):
        await vision_client.describe_image(image=b"x" * 9, mime_type="image/png", prompt="")


@pytest.mark.asyncio
async def test_real_format_wins_over_the_declared_type_and_invalid_bytes_are_an_attachment_error(monkeypatch, vision_settings):
    provider = FakeProvider()
    monkeypatch.setattr(vision_client, "_provider", lambda: provider)
    # a PNG saved as ".jpeg": read as the PNG it really is, sent with its real type
    await vision_client.describe_image(image=tiny_png(), mime_type="image/jpeg", prompt="")
    image_part = provider.payload["messages"][1]["content"][1]["image_url"]["url"]
    assert image_part.startswith("data:image/png;base64,")
    with pytest.raises(vision_client.InvalidImageError):
        await vision_client.describe_image(image=b"not-an-image", mime_type="image/png", prompt="")


def test_chat_request_accepts_only_image_types_and_bounded_base64():
    assert ChatRequest(message="", image={"mime_type": "image/webp", "data_base64": "eA=="}).image
    with pytest.raises(ValueError):
        ChatRequest(message="", image={"mime_type": "image/svg+xml", "data_base64": "eA=="})
