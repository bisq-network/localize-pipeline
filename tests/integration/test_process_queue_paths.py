"""Paths of `process_translation_queue` that withhold a file or keep a draft.

Each test drives the whole queue function. The draft and review steps are
replaced by stubs, so a test states exactly what the model stage returned and
then checks what the queue published, skipped and recorded for retry.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import localize.translate_localization_files as pipeline
from localize.ignore_keys import compile_ignore_key_patterns


TARGET = "app_de.properties"
SOURCE_TEXT = "key.one=value one\nkey.two=value two\nkey.three=value three\n"
TARGET_TEXT = "key.one=Wert eins\n"
DRAFTS = {"key.two": "Wert zwei", "key.three": "Wert drei"}


class _Queue:
    """One queue run in the shared integration folders."""

    def __init__(self, env, monkeypatch, tmp_path):
        self.env = env
        self.ledger_path = tmp_path / "ledger.json"
        self.output_path = os.path.join(env["translated_queue_folder"], TARGET)
        self.draft = AsyncMock(side_effect=self._draft)
        self.review = AsyncMock(return_value={})
        provider = MagicMock()
        provider.estimate_run_cost.return_value = MagicMock()
        provider.format_estimate.return_value = "estimate"
        monkeypatch.setattr(pipeline, "MODEL_PROVIDER", provider)
        monkeypatch.setattr(pipeline, "translate_text_async", self.draft)
        monkeypatch.setattr(pipeline, "holistic_review_async", self.review)
        monkeypatch.setattr(pipeline, "TRANSLATION_KEY_LEDGER_FILE_PATH", str(self.ledger_path))
        monkeypatch.setattr(pipeline, "get_working_tree_changed_keys", lambda *a, **k: set())
        self.write(SOURCE_TEXT, TARGET_TEXT)

    def write(self, source_text, target_text):
        with open(os.path.join(self.env["input_folder"], "app.properties"), "w", encoding="utf-8") as handle:
            handle.write(source_text)
        with open(os.path.join(self.env["translation_queue_folder"], TARGET), "w", encoding="utf-8") as handle:
            handle.write(target_text)

    @staticmethod
    async def _draft(text, key, existing, source, language, glossary, semaphore, limiter, index, *rest):
        return index, DRAFTS[key], True

    async def run(self):
        self.summary = {}
        result = await pipeline.process_translation_queue(
            translation_queue_folder=self.env["translation_queue_folder"],
            translated_queue_folder=self.env["translated_queue_folder"],
            glossary_file_path=self.env["mock_glossary_path_resolved"],
            validation_summary=self.summary,
        )
        return tuple(result)

    def output(self):
        with open(self.output_path, encoding="utf-8") as handle:
            return handle.read()

    def ledger_status(self):
        with open(self.ledger_path, encoding="utf-8") as handle:
            entries = json.load(handle)["files"][TARGET]
        return {key: entry.get("status") for key, entry in entries.items()}


@pytest.fixture
def queue(integration_test_environment, monkeypatch, tmp_path):
    return _Queue(integration_test_environment, monkeypatch, tmp_path)


TRANSLATED = "key.one=Wert eins\nkey.two=Wert zwei\nkey.three=Wert drei\n"


# --- Files that are withheld before any model call --------------------------


@pytest.mark.asyncio
async def test_pre_translation_validation_errors_withhold_the_file(queue, monkeypatch):
    monkeypatch.setattr(
        pipeline, "run_pre_translation_validation",
        lambda *args, **kwargs: (["Malformed line 3"], set()),
    )

    result = await queue.run()

    assert result == (0, [], {TARGET: ["Malformed line 3"]}, 0)
    assert not os.path.exists(queue.output_path)
    assert not queue.ledger_path.exists()
    assert queue.summary == {}
    queue.draft.assert_not_awaited()
    queue.review.assert_not_awaited()


@pytest.mark.asyncio
async def test_lint_errors_withhold_the_file(queue, monkeypatch):
    monkeypatch.setattr(
        pipeline, "split_lint_findings",
        lambda findings: (["Unterminated escape"], ["Linter Warning: trailing space"]),
    )

    result = await queue.run()

    assert result == (0, [], {TARGET: ["Unterminated escape"]}, 0)
    assert not os.path.exists(queue.output_path)
    assert not queue.ledger_path.exists()
    queue.draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_lint_warnings_do_not_withhold_the_file(queue, monkeypatch):
    monkeypatch.setattr(
        pipeline, "split_lint_findings",
        lambda findings: ([], ["Linter Warning: trailing space"]),
    )

    result = await queue.run()

    assert result == (1, [TARGET], {}, 2)
    assert queue.output() == TRANSLATED


# --- Files that only copy ignored keys from the source ----------------------


def _only_an_ignored_key_changed(queue, monkeypatch):
    monkeypatch.setattr(
        pipeline, "IGNORE_KEY_PATTERNS", compile_ignore_key_patterns([r"^note\."])
    )
    queue.write("note.one=Note text\nkey.one=value one\n", "key.one=Wert eins\n")


@pytest.mark.asyncio
async def test_ignored_key_copy_is_withheld_when_final_validation_fails(queue, monkeypatch):
    _only_an_ignored_key_changed(queue, monkeypatch)
    monkeypatch.setattr(pipeline, "run_post_translation_validation", lambda *a, **k: False)

    result = await queue.run()

    assert result == (0, [], {TARGET: ["Post-translation validation failed."]}, 0)
    assert not os.path.exists(queue.output_path)
    queue.draft.assert_not_awaited()


@pytest.mark.asyncio
async def test_ignored_key_copy_in_a_dry_run_counts_the_file_but_writes_nothing(queue, monkeypatch):
    _only_an_ignored_key_changed(queue, monkeypatch)
    monkeypatch.setattr(pipeline, "DRY_RUN", True)

    result = await queue.run()

    assert result == (1, [TARGET], {}, 0)
    assert not os.path.exists(queue.output_path)
    assert not queue.ledger_path.exists()
    queue.draft.assert_not_awaited()


# --- Draft and review failures ----------------------------------------------


@pytest.mark.asyncio
async def test_a_crashed_draft_task_keeps_the_source_and_marks_the_key_for_retry(queue):
    async def draft(text, key, existing, source, language, glossary, semaphore, limiter, index, *rest):
        if key == "key.two":
            raise RuntimeError("socket closed")
        return index, DRAFTS[key], True

    queue.draft.side_effect = draft

    result = await queue.run()

    assert result == (1, [TARGET], {}, 2)
    assert queue.output() == "key.one=Wert eins\nkey.two=value two\nkey.three=Wert drei\n"
    assert queue.ledger_status() == {"key.one": None, "key.two": "failed", "key.three": None}
    assert queue.summary[TARGET]["failed_keys"] == ["key.two"]
    assert queue.summary[TARGET]["model_translation_failed_keys"] == ["key.two"]
    # The failed draft is not sent to review.
    assert queue.review.await_args.kwargs["keys_to_review"] == ["key.three"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "review_outcome",
    [RuntimeError("review crashed"), ["Wert zwei"], None],
    ids=["exception", "not-a-mapping", "no-result"],
)
async def test_a_failed_review_publishes_the_drafts_and_marks_them_for_retry(queue, review_outcome):
    if isinstance(review_outcome, Exception):
        queue.review.side_effect = review_outcome
    else:
        queue.review.return_value = review_outcome

    result = await queue.run()

    assert result == (1, [TARGET], {}, 2)
    assert queue.output() == TRANSLATED
    assert queue.ledger_status() == {"key.one": None, "key.two": "failed", "key.three": "failed"}
    assert queue.summary[TARGET]["failed_keys"] == []
    assert queue.summary[TARGET]["model_translation_failed_keys"] == ["key.three", "key.two"]


@pytest.mark.asyncio
async def test_review_corrections_are_limited_to_the_requested_keys(queue):
    queue.review.return_value = {"key.two": "Wert zwei (geprueft)", "key.other": "Fremd"}

    result = await queue.run()

    assert result == (1, [TARGET], {}, 2)
    assert queue.output() == "key.one=Wert eins\nkey.two=Wert zwei (geprueft)\nkey.three=Wert drei\n"
    # The key that the review left out keeps its draft and is retried.
    assert queue.ledger_status() == {"key.one": None, "key.two": None, "key.three": "failed"}
    assert queue.summary[TARGET]["model_translation_failed_keys"] == ["key.three"]


# --- Several files in one run -----------------------------------------------


@pytest.mark.asyncio
async def test_one_broken_file_does_not_stop_the_other_files(queue, monkeypatch):
    """Counters and the skip record stay per file."""
    env = queue.env
    with open(os.path.join(env["input_folder"], "extra.properties"), "w", encoding="utf-8") as handle:
        handle.write("key.two=value two\n")
    with open(os.path.join(env["translation_queue_folder"], "extra_de.properties"), "w", encoding="utf-8") as handle:
        handle.write("")
    real_validation = pipeline.run_pre_translation_validation

    def validation(target_path, *args, **kwargs):
        if target_path.endswith(TARGET):
            raise RuntimeError("disk error")
        return real_validation(target_path, *args, **kwargs)

    monkeypatch.setattr(pipeline, "run_pre_translation_validation", validation)

    result = await queue.run()

    assert result == (
        1,
        ["extra_de.properties"],
        {TARGET: ["Unexpected processing error: disk error"]},
        1,
    )
    assert not os.path.exists(queue.output_path)
    with open(os.path.join(env["translated_queue_folder"], "extra_de.properties"), encoding="utf-8") as handle:
        assert handle.read() == "key.two=Wert zwei\n"
