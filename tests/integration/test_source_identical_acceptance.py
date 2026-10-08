"""Replay the PCM correction against stale ledger and memory, without APIs."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from localize import translate_localization_files as pipeline
from localize.translation_memory import TranslationMemory, save_translation_memory, load_translation_memory

VALUES = {"mobile.client.topic.private_chat_channels.title": "Private chats", "mobile.community.contacts.reason.privateChat": "Private chat"}


@pytest.mark.asyncio
@pytest.mark.parametrize("dirty", [False, True])
async def test_pcm_correction_survives_stale_memory_and_ledger_repeatedly(
    integration_test_environment, tmp_path, monkeypatch, dirty,
):
    """Accepted corrected labels remain stable on dirty and subsequent clean runs."""
    env = integration_test_environment
    filename = "app_pcm.properties"
    content = "".join(f"{key}={value}\n" for key, value in VALUES.items())
    Path(env["input_folder"], "app.properties").write_text(content)
    Path(env["translation_queue_folder"], filename).write_text(content)
    ledger_path = str(tmp_path / "ledger.json")
    memory_path = str(tmp_path / "memory.json")
    monkeypatch.setattr(pipeline, "ACCEPTED_SOURCE_IDENTICAL_TRANSLATIONS", {"pcm": VALUES})
    monkeypatch.setattr(pipeline, "LANGUAGE_CODES", {"pcm": "Nigerian Pidgin"})
    monkeypatch.setattr(pipeline, "NAME_TO_CODE", {"nigerian pidgin": "pcm"})
    monkeypatch.setattr(pipeline, "PRECOMPUTED_STYLE_RULES_TEXT", {"pcm": ""})
    monkeypatch.setattr(pipeline, "BRAND_GLOSSARY", [])
    monkeypatch.setattr(pipeline, "TRANSLATION_KEY_LEDGER_FILE_PATH", ledger_path)
    monkeypatch.setattr(pipeline, "TRANSLATION_MEMORY_FILE_PATH", memory_path)
    monkeypatch.setattr(pipeline, "TRANSLATION_MEMORY_ENABLED", True)
    monkeypatch.setattr(pipeline, "get_working_tree_changed_keys", lambda *args: set(VALUES) if dirty else set())
    monkeypatch.setattr(pipeline, "DRY_RUN", False)
    provider = MagicMock()
    provider.create_chat_completion = AsyncMock(side_effect=AssertionError("Accepted labels must not call model"))
    monkeypatch.setattr(pipeline, "MODEL_PROVIDER", provider)
    review = AsyncMock(side_effect=AssertionError("Accepted labels must not enter review"))
    monkeypatch.setattr(pipeline, "holistic_review_async", review)
    stale = {k: v.replace("Private", "Privet") for k, v in VALUES.items()}
    pipeline.save_translation_key_ledger(ledger_path, {filename: pipeline.build_file_key_ledger(VALUES, stale)})
    memory = TranslationMemory()
    for key, value in VALUES.items():
        memory.record(value, stale[key], locale="pcm", format_id="java_properties")
    save_translation_memory(memory_path, memory)
    for _ in range(2):
        result = await pipeline.process_translation_queue(
            env["translation_queue_folder"], env["translated_queue_folder"], env["mock_glossary_path_resolved"],
        )
        assert result[3] == 0
        assert Path(env["translation_queue_folder"], filename).read_text() == content
        ledger = pipeline.load_translation_key_ledger(ledger_path)[filename]
        assert all(ledger[k]["target_hash"] == pipeline.compute_ledger_hash(v) for k, v in VALUES.items())
        restored = load_translation_memory(memory_path)
        for value in VALUES.values():
            assert restored.lookup(value, locale="pcm", format_id="java_properties") is None
        assert all(entry["status"] == "conflict" for entry in restored.entries.values())
    provider.create_chat_completion.assert_not_called()
    review.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["changed_source", "unexpected_echo", "failed"])
async def test_acceptance_does_not_skip_source_changes_echoes_or_failed_retries(
    integration_test_environment, tmp_path, monkeypatch, condition,
):
    """Real pipeline selection retries failures and never broadens exact policy."""
    env = integration_test_environment
    key = next(iter(VALUES))
    source = "Private conversations" if condition == "changed_source" else VALUES[key]
    if condition == "unexpected_echo":
        key = "unreviewed.label"
    filename = "app_pcm.properties"
    content = f"{key}={source}\n"
    Path(env["input_folder"], "app.properties").write_text(content)
    Path(env["translation_queue_folder"], filename).write_text(content)
    ledger_path, memory_path = str(tmp_path / "ledger.json"), str(tmp_path / "memory.json")
    for name, value in {
        "ACCEPTED_SOURCE_IDENTICAL_TRANSLATIONS": {"pcm": VALUES},
        "LANGUAGE_CODES": {"pcm": "Nigerian Pidgin"}, "NAME_TO_CODE": {"nigerian pidgin": "pcm"},
        "PRECOMPUTED_STYLE_RULES_TEXT": {"pcm": ""}, "BRAND_GLOSSARY": [],
        "TRANSLATION_KEY_LEDGER_FILE_PATH": ledger_path, "TRANSLATION_MEMORY_FILE_PATH": memory_path,
        "TRANSLATION_MEMORY_ENABLED": True, "DRY_RUN": False,
    }.items():
        monkeypatch.setattr(pipeline, name, value)
    monkeypatch.setattr(pipeline, "get_working_tree_changed_keys", lambda *args: {key})
    ledger = pipeline.build_file_key_ledger({key: VALUES[next(iter(VALUES))]}, {key: "Privet chats"},
                                          failed_keys={key} if condition == "failed" else set())
    pipeline.save_translation_key_ledger(ledger_path, {filename: ledger})
    memory = TranslationMemory()
    if condition == "failed":
        memory.record(source, "Privet chats", locale="pcm", format_id="java_properties")
    save_translation_memory(memory_path, memory)
    provider = MagicMock()
    provider.format_estimate.return_value = "estimate"
    monkeypatch.setattr(pipeline, "MODEL_PROVIDER", provider)
    async def translate(*args):
        return args[8], args[0], True
    translate_mock = AsyncMock(side_effect=translate)
    monkeypatch.setattr(pipeline, "translate_text_async", translate_mock)
    monkeypatch.setattr(pipeline, "holistic_review_async", AsyncMock(return_value={}))
    summaries = {}
    await pipeline.process_translation_queue(env["translation_queue_folder"], env["translated_queue_folder"],
        env["mock_glossary_path_resolved"], validation_summary=summaries)
    translate_mock.assert_awaited_once()
    assert summaries[filename]["source_identical_failures_count"] == (0 if condition == "failed" else 1)
    result_ledger = pipeline.load_translation_key_ledger(ledger_path)[filename][key]
    assert (result_ledger.get("status") == "failed") == (condition != "failed")
    if condition == "failed":
        assert load_translation_memory(memory_path).lookup(source, locale="pcm", format_id="java_properties") is None
