"""Start/end logging of background workers and model calls (GPU/RAM
diagnosis: the log must show when work starts, ends and how long it took)."""

import asyncio
import logging

import httpx

from app.agent.model_provider import OpenAICompatibleProvider
from app.core.activity import WorkerActivity


def test_worker_logs_only_busy_and_idle_transitions(caplog):
    caplog.set_level(logging.INFO, logger="orbitia.activity")
    activity = WorkerActivity("vision")
    for worked in (False, False, True, True, True, False, False):
        activity.record(worked)
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0] == "worker vision: busy"
    assert messages[1].startswith("worker vision: idle - queue empty after 3 item(s) in ")
    assert len(messages) == 2  # idle polls are not logged


def _provider(handler):
    provider = OpenAICompatibleProvider(base_url="http://model.test", model_name="qwen-test")
    original = httpx.AsyncClient

    class _Client(original):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return provider, _Client


def test_each_inference_logs_model_duration_and_tokens(caplog, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 120, "completion_tokens": 7}})

    provider, client = _provider(handler)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    caplog.set_level(logging.INFO, logger="orbitia.inference")
    response = asyncio.run(provider.chat({"messages": [{"role": "user", "content": "secret question"}], "max_tokens": 64}))
    assert response.status_code == 200
    start, end = [r.getMessage() for r in caplog.records]
    assert start == "inference start model=qwen-test max_tokens=64"
    assert end.startswith("inference end model=qwen-test status=200 duration=")
    assert end.endswith("prompt_tokens=120 completion_tokens=7")
    assert "secret question" not in caplog.text  # prompts are never logged


def test_failed_inference_is_logged_as_ended(caplog, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("refused")

    provider, client = _provider(handler)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    caplog.set_level(logging.INFO, logger="orbitia.inference")
    try:
        asyncio.run(provider.chat({"messages": []}))
    except httpx.ConnectError:
        pass
    else:
        raise AssertionError("the transport error must propagate")
    assert "inference end model=qwen-test failed after" in caplog.records[-1].getMessage()
