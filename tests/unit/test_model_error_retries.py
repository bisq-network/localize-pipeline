"""Model API failures: when the pipeline waits, retries, and gives up."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from localize import translate_localization_files as pipeline
from localize.run_safeguards import RequestBudgetExceeded


class _NoLimit:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _RateLimited(Exception):
    """Stand-in for a provider error that the provider marks as retryable."""

    def __init__(self, retry_after=None):
        super().__init__("rate limited")
        self.headers = {} if retry_after is None else {"Retry-After": retry_after}


def _response(value):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=value))]
    )


def _provider(side_effect, retryable=True):
    provider = MagicMock()
    provider.count_tokens.return_value = 1
    provider.is_retryable_error.side_effect = (
        lambda exc: retryable and isinstance(exc, _RateLimited)
    )
    provider.create_chat_completion = AsyncMock(side_effect=side_effect)
    return provider


@pytest.fixture
def backoff_sleep():
    """Record the backoff waits instead of sleeping."""
    with patch.object(pipeline.asyncio, "sleep", new_callable=AsyncMock) as sleep:
        yield sleep


# --- Backoff delay ----------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_after_in_milliseconds_is_converted_to_seconds(backoff_sleep):
    should_retry = await pipeline._handle_retry(1, 3, 5, "key", _RateLimited("250ms"))

    assert should_retry is True
    backoff_sleep.assert_awaited_once_with(0.25)


@pytest.mark.asyncio
async def test_retry_after_http_date_waits_until_that_time(backoff_sleep):
    retry_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)

    should_retry = await pipeline._handle_retry(
        1, 3, 5, "key", _RateLimited(format_datetime(retry_at, usegmt=True))
    )

    assert should_retry is True
    (delay,), _kwargs = backoff_sleep.await_args
    assert 25 <= delay <= 30


@pytest.mark.asyncio
async def test_retry_after_http_date_in_the_past_does_not_wait(backoff_sleep):
    retry_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)

    await pipeline._handle_retry(
        1, 3, 5, "key", _RateLimited(format_datetime(retry_at, usegmt=True))
    )

    backoff_sleep.assert_awaited_once_with(0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempt", "expected_delay"), [(1, 5.5), (2, 10.5), (3, 20.5)]
)
async def test_backoff_doubles_per_attempt_without_a_retry_after_header(
    backoff_sleep, attempt, expected_delay
):
    with patch.object(pipeline.random, "uniform", return_value=0.5):
        should_retry = await pipeline._handle_retry(attempt, 4, 5, "key", _RateLimited())

    assert should_retry is True
    backoff_sleep.assert_awaited_once_with(expected_delay)


@pytest.mark.asyncio
async def test_unparseable_retry_after_falls_back_to_backoff(backoff_sleep):
    with patch.object(pipeline.random, "uniform", return_value=0.5):
        should_retry = await pipeline._handle_retry(
            1, 3, 5, "key", _RateLimited("soon")
        )

    assert should_retry is True
    backoff_sleep.assert_awaited_once_with(5.5)


@pytest.mark.asyncio
async def test_last_attempt_gives_up_without_waiting(backoff_sleep):
    should_retry = await pipeline._handle_retry(3, 3, 5, "key", _RateLimited("1"))

    assert should_retry is False
    backoff_sleep.assert_not_awaited()


# --- Draft translation ------------------------------------------------------


async def _translate(monkeypatch, provider):
    monkeypatch.setattr(pipeline, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(pipeline, "DRY_RUN", False)
    return await pipeline.translate_text_async(
        "Open conversation", "conversation.open", {},
        {"conversation.open": "Open conversation"},
        "German", {}, asyncio.Semaphore(1), _NoLimit(), 7,
    )


@pytest.mark.asyncio
async def test_translation_recovers_after_a_retryable_api_error(
    monkeypatch, backoff_sleep
):
    provider = _provider([_RateLimited("2"), _response("Gespräch öffnen")])

    result = await _translate(monkeypatch, provider)

    assert result == (7, "Gespräch öffnen", True)
    assert provider.create_chat_completion.await_count == 2
    backoff_sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_translation_keeps_the_source_when_retries_are_exhausted(
    monkeypatch, backoff_sleep
):
    provider = _provider(_RateLimited("0"))

    result = await _translate(monkeypatch, provider)

    assert result == (7, "Open conversation", False)
    assert provider.create_chat_completion.await_count == 5
    assert backoff_sleep.await_count == 4


@pytest.mark.asyncio
async def test_translation_does_not_retry_an_unexpected_error(
    monkeypatch, backoff_sleep
):
    provider = _provider(ValueError("bad request"))

    result = await _translate(monkeypatch, provider)

    assert result == (7, "Open conversation", False)
    assert provider.create_chat_completion.await_count == 1
    backoff_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_translation_lets_the_request_budget_stop_the_run(
    monkeypatch, backoff_sleep
):
    """The budget error must not be swallowed as a per-key failure."""
    provider = _provider(RequestBudgetExceeded("budget used"))

    with pytest.raises(RequestBudgetExceeded):
        await _translate(monkeypatch, provider)

    assert provider.create_chat_completion.await_count == 1


# --- Holistic review --------------------------------------------------------


async def _review(monkeypatch, provider):
    monkeypatch.setattr(pipeline, "MODEL_PROVIDER", provider)
    monkeypatch.setattr(pipeline, "DRY_RUN", False)
    return await pipeline.holistic_review_async(
        source_content="greeting=Hello",
        translated_content="greeting=Hallo",
        target_language="German",
        keys_to_review=["greeting"],
        semaphore=asyncio.Semaphore(1),
        rate_limiter=_NoLimit(),
        style_rules_text="",
    )


@pytest.mark.asyncio
async def test_review_recovers_after_a_retryable_api_error(monkeypatch, backoff_sleep):
    reviewed = _response(json.dumps({"review_item_0001": "Hallo zusammen"}))
    provider = _provider([_RateLimited("3"), reviewed])

    result = await _review(monkeypatch, provider)

    assert result == {"greeting": "Hallo zusammen"}
    assert provider.create_chat_completion.await_count == 2
    backoff_sleep.assert_awaited_once_with(3.0)


@pytest.mark.asyncio
async def test_review_reports_failure_when_retries_are_exhausted(
    monkeypatch, backoff_sleep
):
    provider = _provider(_RateLimited("0"))

    result = await _review(monkeypatch, provider)

    assert result is None
    assert provider.create_chat_completion.await_count == 3
    assert backoff_sleep.await_count == 2


@pytest.mark.asyncio
async def test_review_does_not_retry_an_unexpected_error(monkeypatch, backoff_sleep):
    provider = _provider(ValueError("bad request"))

    result = await _review(monkeypatch, provider)

    assert result is None
    assert provider.create_chat_completion.await_count == 1
    backoff_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_lets_the_request_budget_stop_the_run(monkeypatch, backoff_sleep):
    provider = _provider(RequestBudgetExceeded("budget used"))

    with pytest.raises(RequestBudgetExceeded):
        await _review(monkeypatch, provider)


# --- Run stop record --------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_error",
    [RequestBudgetExceeded("request budget used"), pipeline.SampleValidationError("sample failed")],
    ids=["budget", "sample"],
)
async def test_budget_or_sample_stop_is_recorded_and_still_fails_the_run(
    monkeypatch, tmp_path, stop_error
):
    """`logs/run_stop.json` tells the operator why a paid run ended early."""
    safeguards = SimpleNamespace(request_count=lambda: 41, max_requests_per_24h=40)
    usage_summary = MagicMock()
    monkeypatch.setattr(pipeline, "PROJECT_ROOT_DIR", str(tmp_path))
    monkeypatch.setattr(pipeline.config, "run_safeguards", safeguards)
    monkeypatch.setattr(pipeline, "write_token_usage_summary", usage_summary)
    monkeypatch.setattr(
        pipeline, "run_translation_pipeline", AsyncMock(side_effect=stop_error)
    )

    with pytest.raises(type(stop_error)):
        await pipeline.main()

    record = json.loads((tmp_path / "logs" / "run_stop.json").read_text(encoding="utf-8"))
    assert record == {
        "reason": str(stop_error),
        "type": type(stop_error).__name__,
        "requests_in_last_24h": 41,
        "limit": 40,
    }
    usage_summary.assert_called_once_with(
        str(tmp_path / "logs" / "token_usage_summary.json")
    )
