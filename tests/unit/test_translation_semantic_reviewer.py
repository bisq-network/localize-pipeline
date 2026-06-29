import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from localize.semantic_quality import TranslationChange
from localize.model_provider import ModelProviderConfigurationError
from localize.translation_semantic_reviewer import (
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
    assert '"file": "relative/path/to/locale-file"' in combined
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


def test_normalize_review_response_recovers_key_written_as_file():
    response = json.dumps(
        {
            "findings": [
                {
                    "file": "mobile.connectivity.reconnecting.client.details.ios",
                    "severity": "error",
                    "reason": "Still in English.",
                    "suggested_value": "Localized value.",
                }
            ]
        }
    )
    changes = [
        TranslationChange(
            file="resources/mobile_id.properties",
            locale_code="id",
            key="mobile.connectivity.reconnecting.client.details.ios",
            source_value="This may occur if the app was in the background.",
            old_value=None,
            new_value="This may occur if the app was in the background.",
        )
    ]

    findings = normalize_review_response(response, changes)

    assert len(findings) == 1
    assert findings[0]["file"] == "resources/mobile_id.properties"
    assert findings[0]["key"] == "mobile.connectivity.reconnecting.client.details.ios"
    assert findings[0]["suggested_value"] == "Localized value."


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
    provider = MagicMock()
    provider.create_chat_completion = AsyncMock(return_value=response)
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

    findings = await review_translation_changes(
        client=object(),
        model="gpt-5.4-mini",
        target_language="Spanish",
        changes=changes,
        style_rules=[],
        brand_glossary=[],
        model_provider=provider,
    )

    assert findings == []
    kwargs = provider.create_chat_completion.await_args.kwargs
    assert kwargs["model"] == "gpt-5.4-mini"
    assert kwargs["completion_token_limit"] == 4096
    assert kwargs["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in kwargs
    assert "max_completion_tokens" not in kwargs


@pytest.mark.asyncio
async def test_semantic_reviewer_defaults_to_aisuite_provider(tmp_path):
    config_path = tmp_path / "config.yaml"
    validation_summary_path = tmp_path / "translation_validation_summary.json"
    config_path.write_text(
        "\n".join(
            [
                "dry_run: false",
                "semantic_review:",
                "  enabled: true",
                "  model: gpt-4o",
                "supported_locales:",
                "  - code: de",
                "    name: German",
            ]
        ),
        encoding="utf-8",
    )
    provider = MagicMock()
    provider.client = object()
    provider.create_chat_completion = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps({"findings": []}))
                )
            ]
        )
    )

    with (
        patch(
            "localize.translation_semantic_reviewer.get_staged_diff",
            return_value="fake diff",
        ),
        patch(
            "localize.translation_semantic_reviewer.iter_translation_changes_from_diff",
            return_value=[
                TranslationChange(
                    file="resources/messages_de.properties",
                    locale_code="de",
                    key="hello",
                    source_value="Hello",
                    old_value=None,
                    new_value="Hallo",
                )
            ],
        ),
        patch(
            "localize.translation_semantic_reviewer.create_model_provider",
            return_value=provider,
        ) as create_provider,
    ):
        exit_code = await _run(
            [
                "--repo-root",
                str(tmp_path),
                "--input-folder",
                "resources",
                "--config",
                str(config_path),
                "--validation-summary",
                str(validation_summary_path),
                "--changed-files",
                "resources/messages_de.properties",
            ]
        )

    assert exit_code == 0
    assert create_provider.call_args.kwargs["provider_name"] == "aisuite"
    assert create_provider.call_args.kwargs["model_names"] == ("gpt-4o",)
    assert json.loads(validation_summary_path.read_text(encoding="utf-8"))[
        "semantic_review_findings"
    ] == []


@pytest.mark.asyncio
async def test_semantic_reviewer_auto_applies_safe_error_suggestions(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    source_path = resources / "messages.properties"
    target_path = resources / "messages_de.properties"
    source_path.write_text("hello=Hello {0}\n", encoding="utf-8")
    target_path.write_text("hello=Hallo\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    validation_summary_path = tmp_path / "translation_validation_summary.json"
    config_path.write_text(
        "\n".join(
            [
                "dry_run: false",
                "semantic_review:",
                "  enabled: true",
                "  auto_apply_error_suggestions: true",
                "supported_locales:",
                "  - code: de",
                "    name: German",
            ]
        ),
        encoding="utf-8",
    )
    provider = MagicMock()
    provider.client = object()
    provider.create_chat_completion = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(
                            {
                                "findings": [
                                    {
                                        "file": "messages_de.properties",
                                        "key": "hello",
                                        "severity": "error",
                                        "reason": "Placeholder missing.",
                                        "suggested_value": "Hallo {0}",
                                    }
                                ]
                            }
                        )
                    )
                )
            ]
        )
    )

    with (
        patch(
            "localize.translation_semantic_reviewer.get_staged_diff",
            return_value="fake diff",
        ),
        patch(
            "localize.translation_semantic_reviewer.iter_translation_changes_from_diff",
            return_value=[
                TranslationChange(
                    file="messages_de.properties",
                    locale_code="de",
                    key="hello",
                    source_value="Hello {0}",
                    old_value=None,
                    new_value="Hallo",
                )
            ],
        ),
        patch(
            "localize.translation_semantic_reviewer.create_model_provider",
            return_value=provider,
        ),
    ):
        exit_code = await _run(
            [
                "--repo-root",
                str(tmp_path),
                "--input-folder",
                str(resources),
                "--config",
                str(config_path),
                "--validation-summary",
                str(validation_summary_path),
                "--changed-files",
                "resources/messages_de.properties",
            ]
        )

    assert exit_code == 0
    assert "hello=Hallo {0}" in target_path.read_text(encoding="utf-8")
    summary = json.loads(validation_summary_path.read_text(encoding="utf-8"))
    assert summary["semantic_review_findings"] == []
    assert summary["semantic_review_remediations"][0]["key"] == "hello"


@pytest.mark.asyncio
async def test_semantic_reviewer_collects_changes_from_all_configured_profiles(tmp_path):
    config_path = tmp_path / "config.yaml"
    validation_summary_path = tmp_path / "translation_validation_summary.json"
    config_path.write_text(
        "\n".join(
            [
                "dry_run: false",
                "semantic_review:",
                "  enabled: true",
                "supported_locales:",
                "  - code: de",
                "    name: German",
                "localization_formats:",
                "  - id: java_properties",
                "    layout: suffix",
                "  - id: json",
                "    layout:",
                "      id: locale_directory",
                "      source_locale: en",
            ]
        ),
        encoding="utf-8",
    )
    provider = MagicMock()
    provider.client = object()
    provider.create_chat_completion = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps({"findings": []}))
                )
            ]
        )
    )

    def changes_for_profile(**kwargs):
        if kwargs["localization_format"].id == "java_properties":
            return [
                TranslationChange(
                    file="resources/messages_de.properties",
                    locale_code="de",
                    key="hello",
                    source_value="Hello",
                    old_value=None,
                    new_value="Hallo",
                )
            ]
        return [
            TranslationChange(
                file="resources/de/common.json",
                locale_code="de",
                key="/hello",
                source_value="Hello",
                old_value=None,
                new_value="Hallo",
            )
        ]

    with (
        patch(
            "localize.translation_semantic_reviewer.get_staged_diff",
            return_value="fake diff",
        ),
        patch(
            "localize.translation_semantic_reviewer.iter_translation_changes_from_diff",
            side_effect=changes_for_profile,
        ) as iter_changes,
        patch(
            "localize.translation_semantic_reviewer.create_model_provider",
            return_value=provider,
        ),
    ):
        exit_code = await _run(
            [
                "--repo-root",
                str(tmp_path),
                "--input-folder",
                "resources",
                "--config",
                str(config_path),
                "--validation-summary",
                str(validation_summary_path),
                "--changed-files",
                "resources/messages_de.properties",
                "resources/de/common.json",
            ]
        )

    assert exit_code == 0
    assert iter_changes.call_count == 2
    review_messages = provider.create_chat_completion.await_args.kwargs["messages"]
    combined = "\n".join(message["content"] for message in review_messages)
    assert "resources/messages_de.properties" in combined
    assert "resources/de/common.json" in combined


@pytest.mark.asyncio
async def test_semantic_reviewer_fails_when_provider_configuration_is_invalid(tmp_path, caplog):
    config_path = tmp_path / "config.yaml"
    validation_summary_path = tmp_path / "translation_validation_summary.json"
    config_path.write_text(
        "\n".join(
            [
                "dry_run: false",
                "semantic_review:",
                "  enabled: true",
                "  model: gpt-4o",
                "supported_locales:",
                "  - code: de",
                "    name: German",
            ]
        ),
        encoding="utf-8",
    )

    with (
        patch(
            "localize.translation_semantic_reviewer.get_staged_diff",
            return_value="fake diff",
        ),
        patch(
            "localize.translation_semantic_reviewer.iter_translation_changes_from_diff",
            return_value=[
                TranslationChange(
                    file="resources/messages_de.properties",
                    locale_code="de",
                    key="hello",
                    source_value="Hello",
                    old_value=None,
                    new_value="Hallo",
                )
            ],
        ),
        patch(
            "localize.translation_semantic_reviewer.create_model_provider",
            side_effect=ModelProviderConfigurationError("missing key"),
        ),
    ):
        exit_code = await _run(
            [
                "--repo-root",
                str(tmp_path),
                "--input-folder",
                "resources",
                "--config",
                str(config_path),
                "--validation-summary",
                str(validation_summary_path),
                "--changed-files",
                "resources/messages_de.properties",
            ]
        )

    assert exit_code == 1
    assert "Semantic review model provider configuration failed" in caplog.text
    assert not validation_summary_path.exists()
