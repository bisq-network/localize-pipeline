"""Exercise sample/stop/resume through the real parser and queue processor."""

import json
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


def partial_file(q, monkeypatch, failure="source"):
    """One valid formatted candidate and one key that withholds the file."""
    sources = {"good": "You don''t have {0}.\\n\\nPlease try another option.", "bad": "Pay {1}"}
    targets = {"good": "Alter Text {0}", "bad": "Alter Betrag {1}"}
    (q.source / "app.properties").write_text("".join(f"{k}={v}\n" for k, v in sources.items()))
    q.target.write_text("".join(f"{k}={v}\n" for k, v in targets.items()))
    runtime.save_translation_key_ledger(str(q.ledger), {q.target.name: runtime.build_file_key_ledger(
        {"good": "Old {0}", "bad": "Old {1}"}, targets,
    )})
    q.guard.sample_keys = 1
    q.fail["bad"] = True
    values = {"good": "Sie haben {0}.\n\nD'Artagnan hilft.", "bad": "Zahlen {1}"}
    monkeypatch.setattr(runtime, "HOLISTIC_REVIEW_CHUNK_SIZE", 1)

    async def draft(text, key, existing, source, language, glossary, semaphore, limiter, index, *args):
        q.guard.reserve("draft")
        q.calls.append(("draft", key))
        if key == "bad" and q.fail["bad"] and failure == "transport":
            return index, sources[key], False
        return index, values[key], True

    async def review(**kwargs):
        keys = kwargs["keys_to_review"]
        q.guard.reserve("review")
        q.calls.append(("review", tuple(keys)))
        result = {key: values[key] for key in keys}
        if "bad" in keys and q.fail["bad"]:
            if failure == "review":
                raise RuntimeError("review unavailable")
            result["bad"] = {
                "source": sources["bad"], "control": "Zahlen\x00 {1}",
                "placeholder": "Zahlen", "empty": "", "transport": sources["bad"],
            }[failure]
        return result

    monkeypatch.setattr(runtime, "translate_text_async", draft)
    monkeypatch.setattr(runtime, "holistic_review_async", review)
    return sources, values


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["source", "control", "placeholder", "empty", "transport", "review"])
async def test_withheld_file_reuses_only_clean_candidates_without_publishing(guarded_queue, monkeypatch, failure):
    q = guarded_queue
    sources, values = partial_file(q, monkeypatch, failure)
    original_target, original_ledger = q.target.read_bytes(), q.ledger.read_bytes()
    result = await q.run()
    # Review failure can leave a valid draft, but it is not accepted or cached.
    assert q.target.read_bytes() == original_target
    assert q.ledger.read_bytes() == original_ledger
    with q.guard.connect() as db:
        rows = db.execute("SELECT fingerprint,result FROM candidates").fetchall()
    assert len(rows) == 1
    fingerprint, payload = rows[0]
    candidates = json.loads(payload)
    assert set(candidates) == {"good"}
    assert q.guard.lookup(fingerprint) is None
    if failure != "review":
        assert q.target.name in result[2]
        assert not list(q.output.iterdir())
        assert all(entry.get("status") == "failed" for entry in result.pending_ledger[q.target.name].values())
    q.fail["bad"] = False
    q.calls.clear()
    # Preserve the same source/target context as the withheld first attempt.
    result = await q.run()
    assert [key for kind, key in q.calls if kind == "draft"] == ["bad"]
    assert [keys for kind, keys in q.calls if kind == "review"] == [("bad",)]
    content = (q.output / q.target.name).read_text()
    assert "good=Sie haben {0}.\\n\\nD''Artagnan hilft.\n" in content
    assert "D''''Artagnan" not in content
    assert "bad=Zahlen {1}\n" in content
    assert q.ledger.read_bytes() == original_ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "source", "target", "policy", "code", "draft_effort", "draft_cap", "semantic_effort",
])
async def test_provisional_candidates_require_identical_context(guarded_queue, monkeypatch, change):
    q = guarded_queue
    partial_file(q, monkeypatch)
    await q.run()
    q.calls.clear()
    q.fail["bad"] = False
    if change == "source":
        path = q.source / "app.properties"
        path.write_text(path.read_text().replace("another option", "a different option"))
    elif change == "target":
        q.target.write_text(q.target.read_text() + "# Edited target context\n")
    elif change == "policy":
        q.guard.policy["style"] = "Different register"
    elif change == "draft_effort":
        monkeypatch.setattr(runtime, "MODEL_REASONING_EFFORT", "low")
    elif change == "draft_cap":
        monkeypatch.setattr(runtime, "MODEL_MAX_COMPLETION_TOKENS", 1024)
    elif change == "semantic_effort":
        monkeypatch.setattr(runtime.config, "semantic_review_reasoning_effort", "low")
    else:
        monkeypatch.setattr("localize.run_safeguards.code_fingerprint", lambda: "changed-code")
    await q.run()
    assert ("draft", "good") in q.calls


@pytest.mark.asyncio
async def test_provisional_candidates_never_bypass_final_validation(guarded_queue, monkeypatch):
    q = guarded_queue
    partial_file(q, monkeypatch)
    await q.run()
    q.fail["bad"] = False
    original_validator = runtime.run_post_translation_validation
    monkeypatch.setattr(runtime, "run_post_translation_validation", lambda *args, **kwargs: False)
    result = await q.run()
    assert q.target.name in result[2]
    assert not list(q.output.iterdir())
    q.calls.clear()
    validations = []
    def validate(*args, **kwargs):
        validations.append(kwargs["changed_keys_for_run"])
        return original_validator(*args, **kwargs)
    monkeypatch.setattr(runtime, "run_post_translation_validation", validate)
    await q.run()
    assert q.calls == []
    assert validations == [{"good", "bad"}]


@pytest.mark.asyncio
async def test_reused_candidates_and_exhausted_drafts_both_skip_review(guarded_queue, monkeypatch):
    q = guarded_queue
    partial_file(q, monkeypatch, "transport")
    await q.run()  # "good" is saved as a candidate; "bad" exhausts its draft.
    q.calls.clear()
    result = await q.run()
    # The candidate was already reviewed, and an exhausted source fallback must
    # not be reviewed either; the file stays withheld until "bad" succeeds.
    assert [key for kind, key in q.calls if kind == "draft"] == ["bad"]
    assert [keys for kind, keys in q.calls if kind == "review"] == []
    assert q.target.name in result[2]
    assert not list(q.output.iterdir())
