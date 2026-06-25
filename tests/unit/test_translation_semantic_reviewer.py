import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from src.semantic_quality import TranslationChange
from src.translation_semantic_reviewer import (
    _run,
    append_semantic_review_findings,
    build_semantic_review_messages,
    normalize_review_response,
    review_translation_changes,
)


def test_semantic_reviewer_prompt_is_json_only_and_context_rich():
    messages = build_semantic_review_messages(
        target_language="Spanish",
        changes=[
            TranslationChange(
                file="resources/mobile_es.properties",
                locale_code="es",
                key="mobile.tradeHistory.details.networkAddress.clear",
                source_value="Clear network address: {0}",
                old_value="Dirección de red clara: {0}",
                new_value="Borrar dirección de red: {0}",
            )
        ],
        style_rules=["Use formal tone.", "Do not translate clear as delete for clearnet labels."],
        brand_glossary=["Bisq", "Tor"],
    )

    combined = "\n".join(message["content"] for message in messages)

    assert "JSON only" in combined
    assert "source_value" in combined
    assert "old_target_value" in combined
    assert "new_target_value" in combined
    assert '"file": "relative/path/to/file.properties"' in combined
    assert "Clear network address: {0}" in combined
    assert "Borrar dirección de red: {0}" in combined
    assert "Do not translate clear as delete" in combined
    assert "Bisq" in combined


def test_normalize_review_response_accepts_only_in_scope_findings():
    response = json.dumps(
        {
            "findings": [
                {
                    "file": "resources/mobile_es.properties",
                    "key": "mobile.clear",
                    "severity": "error",
                    "reason": "Delete verb used for clearnet label.",
                    "suggested_value": "Dirección de red pública: {0}",
                },
                {
                    "file": "resources/mobile_es.properties",
                    "key": "outside.scope",
                    "severity": "error",
                    "reason": "Should be ignored.",
                },
            ]
        }
    )
    changes = [
        TranslationChange(
            file="resources/mobile_es.properties",
            locale_code="es",
            key="mobile.clear",
            source_value="Clear network address: {0}",
            old_value=None,
            new_value="Borrar dirección de red: {0}",
        )
    ]

    findings = normalize_review_response(response, changes)

    assert len(findings) == 1
    assert findings[0]["file"] == "resources/mobile_es.properties"
    assert findings[0]["key"] == "mobile.clear"
    assert findings[0]["severity"] == "error"
    assert findings[0]["source"] == "ai-review"
    assert findings[0]["suggested_value"] == "Dirección de red pública: {0}"


def test_normalize_review_response_matches_duplicate_keys_by_file_and_key():
    response = json.dumps(
        {
            "findings": [
                {
                    "file": "resources/settings_es.properties",
                    "key": "shared.clear",
                    "severity": "warning",
                    "reason": "Ambiguous clear wording.",
                }
            ]
        }
    )
    changes = [
        TranslationChange(
            file="resources/mobile_es.properties",
            locale_code="es",
            key="shared.clear",
            source_value="Clear",
            old_value=None,
            new_value="Borrar",
        ),
        TranslationChange(
            file="resources/settings_es.properties",
            locale_code="es",
            key="shared.clear",
            source_value="Clear",
            old_value=None,
            new_value="Limpiar",
        ),
    ]

    findings = normalize_review_response(response, changes)

    assert len(findings) == 1
    assert findings[0]["file"] == "resources/settings_es.properties"
    assert findings[0]["key"] == "shared.clear"
    assert findings[0]["value"] == "Limpiar"


def test_append_semantic_review_findings_preserves_existing_summary(tmp_path):
    summary_path = tmp_path / "translation_validation_summary.json"
    summary_path.write_text(
        json.dumps({"files": {"mobile_es.properties": {}}, "pipeline_warnings": []}),
        encoding="utf-8",
    )

    append_semantic_review_findings(
        str(summary_path),
        [
            {
                "file": "mobile_es.properties",
                "key": "mobile.clear",
                "severity": "warning",
                "reason": "Suspicious wording.",
                "source": "ai-review",
            }
        ],
    )

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["files"] == {"mobile_es.properties": {}}
    assert summary["pipeline_warnings"] == []
    assert summary["semantic_review_findings"][0]["key"] == "mobile.clear"


@pytest.mark.asyncio
async def test_semantic_reviewer_is_opt_in(tmp_path):
    config_path = tmp_path / "config.yaml"
    validation_summary_path = tmp_path / "translation_validation_summary.json"
    config_path.write_text("dry_run: false\n", encoding="utf-8")

    exit_code = await _run(
        [
            "--repo-root",
            str(tmp_path),
            "--input-folder",
            str(tmp_path),
            "--config",
            str(config_path),
            "--validation-summary",
            str(validation_summary_path),
            "--changed-files",
            "mobile_es.properties",
        ]
    )

    assert exit_code == 0
    assert not validation_summary_path.exists()


@pytest.mark.asyncio
async def test_semantic_reviewer_uses_compatible_completion_token_limit():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=json.dumps({"findings": []}))
            )
        ]
    )
    create_completion = AsyncMock(return_value=response)
    changes = [
        TranslationChange(
            file="resources/mobile_es.properties",
            locale_code="es",
            key="mobile.clear",
            source_value="Clear network address: {0}",
            old_value=None,
            new_value="Borrar dirección de red: {0}",
        )
    ]

    with patch(
        "src.translation_semantic_reviewer.create_chat_completion",
        create_completion,
    ):
        findings = await review_translation_changes(
            client=object(),
            model="gpt-5.4-mini",
            target_language="Spanish",
            changes=changes,
            style_rules=[],
            brand_glossary=[],
        )

    assert findings == []
    kwargs = create_completion.await_args.kwargs
    assert kwargs["model"] == "gpt-5.4-mini"
    assert kwargs["completion_token_limit"] == 4096
    assert kwargs["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in kwargs
    assert "max_completion_tokens" not in kwargs
