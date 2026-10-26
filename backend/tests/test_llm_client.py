"""The LLM layer (app/agent/llm_client.py): Qwen message shaping,
the llama.cpp server calls, and model status - with HTTP mocked."""

import httpx
import pytest

import app.agent.llm_client as llm
from app.agent.llm_client import LLMServiceError, to_qwen_messages


def test_qwen_messages_keep_native_roles():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "question"},
        {"role": "system", "content": "evidence"},
    ]
    assert to_qwen_messages(messages) == messages


def test_qwen_context_drops_old_exchanges_but_keeps_system_and_latest_question(monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_context_size", 1200)
    monkeypatch.setattr(llm.settings, "llm_max_tokens", 300)
    messages = [{"role": "system", "content": "rules"}]
    for i in range(10):
        messages += [{"role": "user", "content": f"q{i} " + "x" * 600}, {"role": "assistant", "content": "r" * 600}]
    messages.append({"role": "user", "content": "last question"})

    turns = to_qwen_messages(messages)

    assert turns[0] == {"role": "system", "content": "rules"}
    assert turns[-1]["content"] == "last question"
    assert len(turns) < len(messages)


def test_qwen_messages_require_a_user_turn():
    with pytest.raises(LLMServiceError):
        to_qwen_messages([{"role": "system", "content": "rules"}])

# --- calls to the llama.cpp server ---------------------------------------------------


class _FakeClient:
    def __init__(self, response=None, error=None, sink=None):
        self.response, self.error, self.sink = response, error, sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json):
        if self.sink is not None:
            self.sink.append((url, json))
        if self.error:
            raise self.error
        return self.response

    async def get(self, url):
        if self.sink is not None:
            self.sink.append((url, None))
        if self.error:
            raise self.error
        return self.response


def _install(monkeypatch, **kwargs):
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **_: _FakeClient(**kwargs))


def _ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})


async def test_chat_completion_calls_the_llama_server_not_ollama(monkeypatch):
    sent = []
    _install(monkeypatch, response=_ok("  Bonjour !  "), sink=sent)
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "salut"}]

    message = await llm.chat_completion(messages, tools=[{"type": "function"}])

    assert message == {"role": "assistant", "content": "Bonjour !", "tool_calls": None}
    url, payload = sent[0]
    assert url == f"{llm.settings.llm_base_url}/v1/chat/completions"
    assert "ollama" not in url and "11434" not in url
    assert payload["model"] == llm.settings.llm_model_name
    assert payload["messages"] == messages
    assert payload["temperature"] == llm.settings.llm_temperature
    assert payload["max_tokens"] == llm.settings.llm_max_tokens
    assert payload["tools"] == [{"type": "function"}]
    assert payload["tool_choice"] == "auto"


async def test_chat_completion_preserves_local_model_tool_calls(monkeypatch):
    tool_call = {"id": "call-1", "type": "function", "function": {"name": "search_emails", "arguments": "{\"subject\":\"invoice\"}"}}
    response = httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                                                                     "tool_calls": [tool_call]}}]})
    _install(monkeypatch, response=response)
    result = await llm.chat_completion([{"role": "user", "content": "Search mail"}],
                                       tools=[{"type": "function", "function": {"name": "search_emails"}}])
    assert result["content"] == ""
    assert result["tool_calls"] == [tool_call]


async def test_complete_json_is_schema_constrained(monkeypatch):
    sent = []
    _install(monkeypatch, response=_ok('{"metric_ids": ["budget_rh"]}'), sink=sent)
    schema = {"type": "object", "properties": {"metric_ids": {"type": "array"}}}

    answer = await llm.complete_json([{"role": "user", "content": "q"}], schema)

    assert answer == {"metric_ids": ["budget_rh"]}
    assert sent[0][1]["response_format"] == {"type": "json_object", "schema": schema}
    assert sent[0][1]["temperature"] == 0.0


async def test_unreachable_server_is_a_user_friendly_error(monkeypatch):
    _install(monkeypatch, error=httpx.ConnectError("refused"))
    with pytest.raises(LLMServiceError) as exc:
        await llm.chat_completion([{"role": "user", "content": "q"}])
    assert str(exc.value) == llm.LLM_UNAVAILABLE_MESSAGE
    assert "refused" not in str(exc.value)  # technical detail goes to the logs only


async def test_model_still_loading_is_reported_as_such(monkeypatch):
    _install(monkeypatch, response=httpx.Response(503, json={"error": {"message": "Loading model"}}))
    with pytest.raises(LLMServiceError) as exc:
        await llm.chat_completion([{"role": "user", "content": "q"}])
    assert str(exc.value) == llm.LLM_LOADING_MESSAGE


# --- model status ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response,error,expected",
    [
        (httpx.Response(200, json={"status": "ok"}), None, ("ready", "Modèle IA prêt")),
        (httpx.Response(503, json={"error": {"message": "Loading model"}}), None, ("loading", "Chargement du modèle IA...")),
        (None, httpx.ConnectError("down"), ("unavailable", llm.LLM_UNAVAILABLE_MESSAGE)),
    ],
)
async def test_status(monkeypatch, response, error, expected):
    sent = []
    _install(monkeypatch, response=response, error=error, sink=sent)

    status = await llm.get_llm_status()

    assert (status["status"], status["message"]) == expected
    assert sent[0][0] == f"{llm.settings.llm_base_url}/health"
    assert status["model"] == llm.settings.llm_display_name
    # Never the model file's location.
    assert not any(marker in str(status) for marker in (".gguf", "\\", "/models", "Downloads"))
