"""Exercise sample/stop/resume through the real parser and queue processor."""

from types import SimpleNamespace

import pytest

from localize import translate_localization_files as runtime
from localize.providers.models import OpenAICompatibleProvider
from localize.run_safeguards import (
    RequestBudgetExceeded, RunSafeguards, SampleValidationError,
)


@pytest.fixture
def guarded_queue(tmp_path, monkeypatch):
    source = tmp_path / "source"
    queue = tmp_path / "queue"
    output = tmp_path / "output"
    for folder in [source, queue, output]:
        folder.mkdir()
    (source / "app.properties").write_text(
        "".join(f"key{i}=English message {i}\n" for i in range(7))
    )
    target = queue / "app_de.properties"
    target.write_text("# Retained comment\n")
    glossary = tmp_path / "glossary.json"
    glossary.write_text("{}")
    ledger = tmp_path / "ledger.json"
    safeguards = RunSafeguards(tmp_path / "state.sqlite3", 100, 1, 2, 10)
    provider = OpenAICompatibleProvider(client=object())
    provider.safeguards = safeguards
    for name, value in {
        "INPUT_FOLDER": str(source), "PROJECT_ROOT_DIR": str(tmp_path),
        "MODEL_PROVIDER": provider, "DRY_RUN": False,
        "TRANSLATION_KEY_LEDGER_FILE_PATH": str(ledger),
        "TRANSLATION_MEMORY_ENABLED": False, "IGNORE_KEY_PATTERNS": [],
        "LANGUAGE_CODES": {"de": "German"}, "NAME_TO_CODE": {"german": "de"},
        "BRAND_GLOSSARY": [], "ACCEPTED_SOURCE_IDENTICAL_TRANSLATIONS": {},
    }.items():
        monkeypatch.setattr(runtime, name, value)
    monkeypatch.setattr(runtime.config, "run_safeguards", safeguards)
    monkeypatch.setattr(runtime.config, "semantic_review_enabled", True)
    monkeypatch.setattr(runtime, "get_working_tree_changed_keys", lambda *a: set())
    calls = []
    fail = {"draft": False, "semantic": False}

    async def translate(text, key, existing, sources, language, glossary,
                        semaphore, limiter, index, *args):
        safeguards.reserve("draft")
        calls.append(("draft", key))
        return index, text if fail["draft"] else f"Deutscher Text {key}", True

    async def review(**kwargs):
        safeguards.reserve("review")
        calls.append(("review", tuple(kwargs["keys_to_review"])))
        return {key: f"Deutscher Text {key}" for key in kwargs["keys_to_review"]} \
            if not fail["draft"] else {
                key: f"English message {key.removeprefix('key')}"
                for key in kwargs["keys_to_review"]
            }

    async def semantic(**kwargs):
        safeguards.reserve("semantic")
        calls.append(("semantic", len(kwargs["changes"])))
        return [{"severity": "error", "message": "Wrong meaning"}] \
            if fail["semantic"] else []

    monkeypatch.setattr(runtime, "translate_text_async", translate)
    monkeypatch.setattr(runtime, "holistic_review_async", review)
    monkeypatch.setattr(
        "localize.translation_semantic_reviewer.review_translation_changes", semantic,
    )

    async def run():
        return await runtime.process_translation_queue(
            str(queue), str(output), str(glossary), validation_summary={},
        )

    return SimpleNamespace(
        run=run, calls=calls, fail=fail, guard=safeguards, target=target,
        ledger=ledger, output=output, source=source,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["draft", "semantic"])
async def test_failed_sample_never_starts_full_queue(guarded_queue, failure):
    q = guarded_queue
    original = q.target.read_bytes()
    q.fail[failure] = True
    with pytest.raises(SampleValidationError):
        await q.run()
    assert sum(kind == "draft" for kind, _ in q.calls) == 2
    assert q.target.read_bytes() == original
    assert not q.ledger.exists()
    assert not list(q.output.iterdir())


@pytest.mark.asyncio
async def test_successful_sample_does_not_hide_remaining_new_keys(guarded_queue):
    q = guarded_queue
    result = await q.run()
    assert len(result.pending_ledger["app_de.properties"]) == 7
    assert not q.ledger.exists()  # Commit follows copyback in core.pipeline.
    content = (q.output / q.target.name).read_text()
    assert all(f"key{i}=Deutscher Text key{i}" in content for i in range(7))
    assert q.calls[3] == ("semantic", 2)
    assert sum(kind == "draft" for kind, _ in q.calls) == 9


@pytest.mark.asyncio
async def test_restart_reuses_accepted_output_without_any_requests(guarded_queue):
    q = guarded_queue
    await q.run()
    before = list(q.calls)
    q.target.write_text("# Retained comment\n")
    result = await q.run()
    assert q.calls == before
    assert len(result.pending_ledger["app_de.properties"]) == 7


@pytest.mark.asyncio
async def test_source_change_invalidates_checkpoint(guarded_queue):
    q = guarded_queue
    await q.run()
    before = len(q.calls)
    q.target.write_text("# Retained comment\n")
    source = q.source / "app.properties"
    source.write_text(source.read_text().replace("message 0", "changed message 0"))
    await q.run()
    assert len(q.calls) > before


@pytest.mark.asyncio
async def test_budget_stop_does_not_publish_partial_file(guarded_queue):
    q = guarded_queue
    q.guard.max_requests_per_24h = 5
    with pytest.raises(RequestBudgetExceeded):
        await q.run()
    assert q.guard.request_count() == 5
    assert not list(q.output.iterdir())
    assert not q.ledger.exists()
