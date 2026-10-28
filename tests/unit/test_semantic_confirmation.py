"""Regression coverage for contradictory semantic findings blocking production."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from localize.semantic_quality import TranslationChange
from localize.translation_semantic_reviewer import review_translation_changes


def response(findings, finish_reason="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps({"findings": findings})),
        finish_reason=finish_reason,
    )])


CHANGE = TranslationChange("mobile_af_ZA.properties", "af_ZA", "logFile",
                           "For logs up to 2 MiB, the entire log is included.",
                           None, "Vir loglêers tot 2 MiB word die hele log ingesluit.")
FALSE_FINDING = {"file": CHANGE.file, "key": CHANGE.key, "severity": "error",
                 "reason": "The target preserves the condition and meaning. No semantic issue found."}


async def run(provider, failed):
    return await review_translation_changes(
        client=None, model="test-model", target_language="Afrikaans",
        changes=[CHANGE], style_rules=[], brand_glossary=[],
        model_provider=provider, failed_batches=failed,
    )


@pytest.mark.asyncio
async def test_contradictory_finding_requires_fresh_source_grounded_confirmation():
    provider = MagicMock(create_chat_completion=AsyncMock(
        side_effect=[response([FALSE_FINDING]), response([])]))
    failed = []
    assert await run(provider, failed) == []
    assert failed == []
    assert provider.create_chat_completion.await_count == 2
    payload = json.loads(provider.create_chat_completion.call_args.kwargs["messages"][1]["content"])
    assert payload["candidate_findings"][0]["reason"] == FALSE_FINDING["reason"]
    assert payload["changes"][0]["source_value"] == CHANGE.source_value
    assert payload["changes"][0]["new_target_value"] == CHANGE.new_value


@pytest.mark.asyncio
async def test_confirmed_real_defect_remains_blocking_without_retry_until_pass():
    finding = dict(FALSE_FINDING, reason="The target reverses the threshold.")
    provider = MagicMock(create_chat_completion=AsyncMock(return_value=response([finding])))
    failed = []
    findings = await run(provider, failed)
    assert findings[0]["severity"] == "error"
    assert findings[0]["reason"] == finding["reason"]
    assert failed == []
    assert provider.create_chat_completion.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", [
    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="broken"), finish_reason="stop")]),
    response([], "length"),
    response([dict(FALSE_FINDING, key="unknown")]),
])
async def test_invalid_confirmation_cannot_become_clean(confirmation):
    provider = MagicMock(create_chat_completion=AsyncMock(
        side_effect=[response([FALSE_FINDING]), confirmation]))
    failed = []
    await run(provider, failed)
    assert len(failed) == 1
    assert provider.create_chat_completion.await_count == 2


@pytest.mark.asyncio
async def test_clean_review_needs_no_confirmation():
    provider = MagicMock(create_chat_completion=AsyncMock(return_value=response([])))
    failed = []
    assert await run(provider, failed) == []
    assert not failed
    assert provider.create_chat_completion.await_count == 1


@pytest.mark.asyncio
async def test_failure_cannot_look_clean_without_failure_collector():
    provider = MagicMock(create_chat_completion=AsyncMock(side_effect=ValueError("broken")))
    with pytest.raises(ValueError, match="broken"):
        await run(provider, None)


@pytest.mark.asyncio
async def test_warning_is_confirmed_and_not_silently_downgraded():
    finding = dict(FALSE_FINDING, severity="warning", reason="Ambiguous referent.")
    provider = MagicMock(create_chat_completion=AsyncMock(return_value=response([finding])))
    findings = await run(provider, [])
    assert findings[0]["severity"] == "warning"
    assert provider.create_chat_completion.await_count == 2


@pytest.mark.asyncio
async def test_exact_production_findings_are_confirmed_against_both_original_values():
    from pathlib import Path
    data = json.loads((Path(__file__).parents[1] / "fixtures/semantic_review/mobile_20261028.json").read_text())
    cases = data["cases"]
    provider = MagicMock(create_chat_completion=AsyncMock(side_effect=[
        response([c["original_finding"] for c in cases]), response([]),
    ]))
    changes = [TranslationChange(c["file"], "af_ZA", c["key"], c["source"], None, c["target"]) for c in cases]
    failed = []
    findings = await review_translation_changes(
        client=None, model="test-model", target_language="Afrikaans", changes=changes,
        style_rules=[], brand_glossary=[], model_provider=provider, failed_batches=failed,
    )
    assert findings == [] and failed == []
    payload = json.loads(provider.create_chat_completion.call_args.kwargs["messages"][1]["content"])
    assert len(payload["candidate_findings"]) == 2
    assert {c["key"]: c["source_value"] for c in payload["changes"]} == {c["key"]: c["source"] for c in cases}
