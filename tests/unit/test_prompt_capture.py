import asyncio
import json

import pytest

from localize.placeholder_rules import capture_placeholder_tokens, protect_placeholders
from localize.prompt_capture import PromptCaptureProvider


def test_capture_tokens_are_deterministic_without_changing_production_tokens():
    production_first, _ = protect_placeholders("Amount {0}")
    production_second, _ = protect_placeholders("Amount {0}")
    assert production_first != production_second

    with capture_placeholder_tokens():
        captured_first, _ = protect_placeholders("Amount {0} and {1}")
        captured_second, _ = protect_placeholders("Amount {0} and {1}")

    assert captured_first == "Amount __PH_0001__ and __PH_0002__"
    assert captured_second == captured_first


def test_capture_context_restores_after_nested_exception():
    """Nested failures must restore the caller's placeholder mode."""
    with capture_placeholder_tokens():
        with pytest.raises(RuntimeError, match="capture failed"):
            with capture_placeholder_tokens():
                raise RuntimeError("capture failed")
        assert protect_placeholders("Amount {0}")[0] == "Amount __PH_0001__"

    first, _ = protect_placeholders("Amount {0}")
    second, _ = protect_placeholders("Amount {0}")
    assert first != second
    assert "__PH_0001__" not in first


@pytest.mark.asyncio
async def test_capture_context_does_not_leak_to_another_task():
    """A concurrent normal task retains UUID-backed tokens during capture."""
    capture_ready = asyncio.Event()
    production_done = asyncio.Event()

    async def capture():
        with capture_placeholder_tokens():
            capture_ready.set()
            await production_done.wait()
            return protect_placeholders("Amount {0}")[0]

    async def production():
        await capture_ready.wait()
        try:
            return (
                protect_placeholders("Amount {0}")[0],
                protect_placeholders("Amount {0}")[0],
            )
        finally:
            production_done.set()

    captured, (first, second) = await asyncio.gather(capture(), production())
    assert captured == "Amount __PH_0001__"
    assert first != second
    assert first != captured


@pytest.mark.asyncio
async def test_capture_provider_writes_stage_specific_canonical_payloads(tmp_path):
    draft = PromptCaptureProvider(stage="draft", responses=["Hallo"])
    review = PromptCaptureProvider(stage="review", responses=["{}"])

    await draft.create_chat_completion(
        model="capture-model",
        messages=[
            {"role": "system", "content": "Translate."},
            {"role": "user", "content": "Hello"},
        ],
        temperature=0.3,
    )
    await review.create_chat_completion(
        model="capture-review-model",
        messages=[
            {"role": "system", "content": "Review."},
            {"role": "user", "content": "key=Hallo"},
        ],
        completion_token_limit=1024,
        temperature=0.1,
    )

    draft_path = draft.write_capture(tmp_path)
    review_path = review.write_capture(tmp_path)

    assert draft_path.name == "draft-requests.json"
    assert review_path.name == "review-requests.json"
    assert json.loads(draft_path.read_text(encoding="utf-8")) == draft.canonical_payloads()
    assert json.loads(review_path.read_text(encoding="utf-8")) == review.canonical_payloads()

    repeated = PromptCaptureProvider(stage="draft", responses=["Hallo"])
    await repeated.create_chat_completion(
        model="capture-model",
        messages=[
            {"role": "system", "content": "Translate."},
            {"role": "user", "content": "Hello"},
        ],
        temperature=0.3,
    )
    assert repeated.canonical_json() == draft.canonical_json()
