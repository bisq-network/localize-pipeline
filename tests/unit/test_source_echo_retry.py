"""Source echoes must use bounded retries rather than report model success."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from localize import translate_localization_files as pipeline


class _NoLimit:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _response(value):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=value))])


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_initial_source_echo_retries_before_reporting_success(monkeypatch, recover):
    source = "Open conversation"
    provider = MagicMock()
    provider.count_tokens.return_value = 1
    provider.is_retryable_error.return_value = False
    provider.create_chat_completion = AsyncMock(side_effect=[
        _response(source), _response("Gespräch öffnen" if recover else source),
    ])
    retry = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(pipeline, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(pipeline, "_handle_retry", retry)
    monkeypatch.setattr(pipeline, "DRY_RUN", False)
    result = await pipeline.translate_text_async(
        source, "conversation.open", {}, {"conversation.open": source},
        "German", {}, asyncio.Semaphore(1), _NoLimit(), 0,
    )
    assert result == (0, "Gespräch öffnen" if recover else source, recover)
    assert provider.create_chat_completion.await_count == 2
    assert retry.await_count == (1 if recover else 2)


@pytest.mark.asyncio
async def test_source_echo_retry_uses_focused_source_without_old_context(monkeypatch):
    """Repeated drafting cannot be anchored by the same large context excerpt."""
    source = "Payment data for trade {0}"
    key = "payment.title"
    provider = MagicMock()
    provider.count_tokens.return_value = 1
    provider.is_retryable_error.return_value = False
    provider.create_chat_completion = AsyncMock(side_effect=[
        _response(source), _response("Data wey concern payment for trade {0}"),
    ])
    monkeypatch.setattr(pipeline, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(pipeline, "_handle_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(pipeline, "DRY_RUN", False)
    monkeypatch.setattr(pipeline, "NAME_TO_CODE", {"nigerian pidgin": "pcm"})
    result = await pipeline.translate_text_async(
        source, key, {"payment.other": "Old example for context"},
        {key: source, "payment.other": "Other payment"},
        "Nigerian Pidgin", {"pcm": {"trade": "Trayd"}},
        asyncio.Semaphore(1), _NoLimit(), 0,
    )
    first, retry = provider.create_chat_completion.await_args_list
    assert "Old example for context" in first.kwargs["messages"][1]["content"]
    repair = retry.kwargs["messages"][1]["content"]
    assert "Old example for context" not in repair
    assert "Trayd" in repair
    assert "Payment data for trade __PH_" in repair
    assert "natural" in repair
    assert result == (0, "Data wey concern payment for trade {0}", True)


@pytest.mark.asyncio
async def test_initial_exact_source_identical_acceptance_does_not_retry(monkeypatch):
    source = "Private chat"
    provider = MagicMock()
    provider.count_tokens.return_value = 1
    provider.is_retryable_error.return_value = False
    provider.create_chat_completion = AsyncMock(return_value=_response(source))
    retry = AsyncMock(return_value=False)
    monkeypatch.setattr(pipeline, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(pipeline, "_handle_retry", retry)
    monkeypatch.setattr(pipeline, "DRY_RUN", False)
    monkeypatch.setattr(pipeline, "ACCEPTED_SOURCE_IDENTICAL_TRANSLATIONS", {
        "de": {"conversation.open": source},
    })
    result = await pipeline.translate_text_async(
        source, "conversation.open", {}, {"conversation.open": source},
        "German", {}, asyncio.Semaphore(1), _NoLimit(), 0,
    )
    assert result == (0, source, True)
    retry.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_review_source_echo_retries_or_keeps_failed_review_evidence(monkeypatch, recover):
    provider = MagicMock()
    provider.is_retryable_error.return_value = False
    provider.create_chat_completion = AsyncMock(side_effect=[
        _response('{"review_item_0001":"Open conversation"}'),
        _response('{"review_item_0001":"Gespräch öffnen"}' if recover else
                  '{"review_item_0001":"Open conversation"}'),
    ])
    retry = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(pipeline, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(pipeline, "_handle_retry", retry)
    monkeypatch.setattr(pipeline, "DRY_RUN", False)
    result = await pipeline.holistic_review_async(
        source_content="conversation.open=Open conversation",
        translated_content="conversation.open=Gespräch öffnen",
        target_language="German", keys_to_review=["conversation.open"],
        semaphore=asyncio.Semaphore(1), rate_limiter=_NoLimit(), style_rules_text="",
        source_translations={"conversation.open": "Open conversation"},
    )
    assert result == ({"conversation.open": "Gespräch öffnen"} if recover else None)
    assert provider.create_chat_completion.await_count == 2
    assert retry.await_count == (1 if recover else 2)


@pytest.mark.asyncio
async def test_review_exact_source_identical_acceptance_does_not_retry(monkeypatch):
    provider = MagicMock()
    provider.is_retryable_error.return_value = False
    provider.create_chat_completion = AsyncMock(return_value=_response(
        '{"review_item_0001":"Private chat"}',
    ))
    retry = AsyncMock(return_value=False)
    monkeypatch.setattr(pipeline, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(pipeline, "_handle_retry", retry)
    monkeypatch.setattr(pipeline, "DRY_RUN", False)
    monkeypatch.setattr(pipeline, "ACCEPTED_SOURCE_IDENTICAL_TRANSLATIONS", {
        "de": {"conversation.open": "Private chat"},
    })
    result = await pipeline.holistic_review_async(
        source_content="conversation.open=Private chat",
        translated_content="conversation.open=Private chat",
        target_language="German", keys_to_review=["conversation.open"],
        semaphore=asyncio.Semaphore(1), rate_limiter=_NoLimit(), style_rules_text="",
        source_translations={"conversation.open": "Private chat"},
    )
    assert result == {"conversation.open": "Private chat"}
    retry.assert_not_awaited()
