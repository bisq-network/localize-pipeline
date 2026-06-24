from types import SimpleNamespace

import httpx
import pytest
from openai import BadRequestError

from src.openai_compat import (
    chat_completion_token_limit_kwargs,
    create_chat_completion_with_token_limit,
)


def _bad_request_for_param(
    param: str,
    *,
    message: str | None = None,
    code: str = "unsupported_parameter",
) -> BadRequestError:
    message = message or f"Unsupported parameter: '{param}' is not supported with this model."
    request = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return BadRequestError(
        message,
        response=response,
        body={
            "error": {
                "message": message,
                "param": param,
                "code": code,
            }
        },
    )


def test_chat_completion_token_limit_kwargs_uses_model_appropriate_parameter():
    assert chat_completion_token_limit_kwargs("gpt-5.4-mini", 4096) == {
        "max_completion_tokens": 4096
    }
    assert chat_completion_token_limit_kwargs("openai:o3-mini", 4096) == {
        "max_completion_tokens": 4096
    }
    assert chat_completion_token_limit_kwargs("gpt-4o", 4096) == {"max_tokens": 4096}


@pytest.mark.asyncio
async def test_create_chat_completion_retries_with_alternate_token_limit_parameter():
    class FakeCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise _bad_request_for_param("max_tokens")
            return SimpleNamespace(choices=[])

    completions = FakeCompletions()

    await create_chat_completion_with_token_limit(
        completions,
        model="custom-router-model",
        messages=[{"role": "user", "content": "Return JSON."}],
        max_output_tokens=123,
        temperature=0,
    )

    assert completions.calls[0]["max_tokens"] == 123
    assert "max_completion_tokens" not in completions.calls[0]
    assert completions.calls[1]["max_completion_tokens"] == 123
    assert "max_tokens" not in completions.calls[1]
    assert completions.calls[1]["temperature"] == 0


@pytest.mark.asyncio
async def test_create_chat_completion_uses_max_completion_tokens_first_for_gpt5():
    class FakeCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(choices=[])

    completions = FakeCompletions()

    await create_chat_completion_with_token_limit(
        completions,
        model="gpt-5.4-mini",
        messages=[{"role": "user", "content": "Return JSON."}],
        max_output_tokens=456,
    )

    assert completions.calls == [
        {
            "model": "gpt-5.4-mini",
            "messages": [{"role": "user", "content": "Return JSON."}],
            "max_completion_tokens": 456,
        }
    ]


@pytest.mark.asyncio
async def test_create_chat_completion_falls_back_to_max_tokens_for_compatible_api():
    class FakeCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise _bad_request_for_param("max_completion_tokens")
            return SimpleNamespace(choices=[])

    completions = FakeCompletions()

    await create_chat_completion_with_token_limit(
        completions,
        model="gpt-5.4-mini",
        messages=[{"role": "user", "content": "Return JSON."}],
        max_output_tokens=789,
    )

    assert completions.calls[0]["max_completion_tokens"] == 789
    assert "max_tokens" not in completions.calls[0]
    assert completions.calls[1]["max_tokens"] == 789
    assert "max_completion_tokens" not in completions.calls[1]


@pytest.mark.asyncio
async def test_create_chat_completion_does_not_retry_other_token_limit_errors():
    class FakeCompletions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            raise _bad_request_for_param(
                "max_tokens",
                message="Invalid value for max_tokens.",
                code="invalid_request_error",
            )

    completions = FakeCompletions()

    with pytest.raises(BadRequestError, match="Invalid value for max_tokens"):
        await create_chat_completion_with_token_limit(
            completions,
            model="custom-router-model",
            messages=[{"role": "user", "content": "Return JSON."}],
            max_output_tokens=-1,
        )

    assert len(completions.calls) == 1
