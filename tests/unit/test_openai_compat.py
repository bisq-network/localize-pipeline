import logging
from types import SimpleNamespace

import pytest
from openai import OpenAIError

from src.openai_compat import create_chat_completion, create_chat_completion_with_fallback


def _response(content="{}"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class _FakeCompletions:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [_response()]
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, *outcomes):
        self.chat = SimpleNamespace(completions=_FakeCompletions(*outcomes))

    @property
    def calls(self):
        return self.chat.completions.calls


@pytest.mark.asyncio
async def test_newer_openai_models_use_max_completion_tokens():
    client = _FakeClient()

    await create_chat_completion(
        client,
        model="gpt-5.4-mini",
        messages=[],
        completion_token_limit=4096,
        temperature=0,
    )

    assert client.calls[0]["max_completion_tokens"] == 4096
    assert "max_tokens" not in client.calls[0]


@pytest.mark.asyncio
async def test_default_models_keep_max_tokens_for_provider_compatibility():
    client = _FakeClient()

    await create_chat_completion(
        client,
        model="gpt-4o",
        messages=[],
        completion_token_limit=4096,
        temperature=0,
    )

    assert client.calls[0]["max_tokens"] == 4096
    assert "max_completion_tokens" not in client.calls[0]


@pytest.mark.asyncio
async def test_retries_with_max_completion_tokens_when_max_tokens_is_rejected(caplog):
    client = _FakeClient(
        OpenAIError("Unsupported parameter: 'max_tokens'"),
        _response(),
    )
    caplog.set_level(logging.DEBUG, logger="src.openai_compat")

    await create_chat_completion(
        client,
        model="custom-provider-model",
        messages=[],
        completion_token_limit=2048,
    )

    assert client.calls[0]["max_tokens"] == 2048
    assert "max_completion_tokens" not in client.calls[0]
    assert client.calls[1]["max_completion_tokens"] == 2048
    assert "max_tokens" not in client.calls[1]
    assert "Retrying chat completion" in caplog.text
    assert "max_completion_tokens" in caplog.text


@pytest.mark.asyncio
async def test_retries_with_max_tokens_when_max_completion_tokens_is_rejected():
    client = _FakeClient(
        OpenAIError("Unsupported parameter: 'max_completion_tokens'"),
        _response(),
    )

    await create_chat_completion(
        client,
        model="gpt-5.4-mini",
        messages=[],
        completion_token_limit=2048,
    )

    assert client.calls[0]["max_completion_tokens"] == 2048
    assert "max_tokens" not in client.calls[0]
    assert client.calls[1]["max_tokens"] == 2048
    assert "max_completion_tokens" not in client.calls[1]


@pytest.mark.asyncio
async def test_retries_when_local_sdk_rejects_unknown_parameter():
    client = _FakeClient(
        TypeError(
            "AsyncCompletions.create() got an unexpected keyword argument "
            "'max_completion_tokens'"
        ),
        _response(),
    )

    await create_chat_completion(
        client,
        model="gpt-5.4-mini",
        messages=[],
        completion_token_limit=2048,
    )

    assert client.calls[0]["max_completion_tokens"] == 2048
    assert "max_tokens" not in client.calls[0]
    assert client.calls[1]["max_tokens"] == 2048
    assert "max_completion_tokens" not in client.calls[1]


@pytest.mark.asyncio
async def test_omits_completion_token_cap_when_no_limit_is_requested():
    client = _FakeClient()

    await create_chat_completion(
        client,
        model="gpt-5.4-mini",
        messages=[],
        temperature=0.3,
    )

    assert "max_tokens" not in client.calls[0]
    assert "max_completion_tokens" not in client.calls[0]


@pytest.mark.asyncio
async def test_callable_helper_accepts_sync_completion_callable():
    calls = []
    response = _response()

    def create(**kwargs):
        calls.append(kwargs)
        return response

    result = await create_chat_completion_with_fallback(
        create,
        model="gpt-4o",
        messages=[],
        completion_token_limit=128,
    )

    assert result is response
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 128
