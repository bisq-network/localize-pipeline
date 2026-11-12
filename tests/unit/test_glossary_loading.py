"""A glossary file that cannot be parsed must stop the run, not empty the glossary."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import localize.translate_localization_files as pipeline
from localize import cli
from localize.app_config import validate_config
from localize.glossary import GlossaryLoadError


def test_missing_glossary_file_means_no_glossary(tmp_path):
    """The glossary is optional, so an absent file is an empty glossary."""
    assert pipeline.load_glossary(str(tmp_path / "glossary.json")) == {}


@pytest.mark.parametrize(
    "content",
    [
        b'{"de": {"account": "Konto",}}',  # trailing comma
        b"",  # empty file
        b'["account"]',  # valid JSON, but not an object
        b'{"de": {"account": "Kont\xff"}}',  # not UTF-8
    ],
    ids=["invalid-json", "empty", "not-an-object", "not-utf8"],
)
def test_unreadable_glossary_file_stops_the_load(tmp_path, content):
    """Continuing with an empty glossary would pay for known-wrong output."""
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_bytes(content)

    with pytest.raises(GlossaryLoadError) as error:
        pipeline.load_glossary(str(glossary_path))

    assert str(glossary_path) in str(error.value)
    assert "empty JSON object {}" in str(error.value)


def test_glossary_with_a_byte_order_mark_or_extreme_nesting_is_rejected_cleanly(tmp_path):
    """Unusual parse failures raise the same error, not a raw traceback."""
    bom_path = tmp_path / "bom.json"
    bom_path.write_bytes(b"\xef\xbb\xbf" + b'{"de": {"account": "Konto"}}')
    nested_path = tmp_path / "nested.json"
    nested_path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")

    for path in (bom_path, nested_path, tmp_path):  # tmp_path is a directory
        with pytest.raises(GlossaryLoadError):
            pipeline.load_glossary(str(path))


def test_empty_json_object_is_an_empty_glossary(tmp_path):
    """`{}` is how a project states that it has no required terms."""
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text("{}", encoding="utf-8")

    assert pipeline.load_glossary(str(glossary_path)) == {}


def test_invalid_locale_namespace_is_still_only_skipped(tmp_path):
    """One malformed locale does not discard the other locales."""
    glossary_path = tmp_path / "glossary.json"
    glossary_path.write_text(
        json.dumps({"de": {"account": "Konto"}, "fr": ["compte"]}),
        encoding="utf-8",
    )

    assert pipeline.load_glossary(str(glossary_path)) == {"de": {"account": "Konto"}}


@pytest.mark.asyncio
async def test_unreadable_glossary_stops_the_queue_before_any_model_call(
    integration_test_environment,
):
    """The run fails before it spends a model request."""
    env = integration_test_environment
    with open(f"{env['input_folder']}/app.properties", "w", encoding="utf-8") as handle:
        handle.write("key.one=value one\n")
    with open(
        f"{env['translation_queue_folder']}/app_de.properties", "w", encoding="utf-8"
    ) as handle:
        handle.write("")
    with open(env["mock_glossary_path_resolved"], "w", encoding="utf-8") as handle:
        handle.write('{"de": {"Hello": "Hallo",}}')

    provider = MagicMock()
    provider.create_chat_completion = AsyncMock()

    with patch("localize.translate_localization_files.MODEL_PROVIDER", provider):
        with pytest.raises(GlossaryLoadError):
            await pipeline.process_translation_queue(
                translation_queue_folder=env["translation_queue_folder"],
                translated_queue_folder=env["translated_queue_folder"],
                glossary_file_path=env["mock_glossary_path_resolved"],
            )

    provider.create_chat_completion.assert_not_awaited()


def _glossary_issues(config, config_dir):
    issues = validate_config(
        config,
        path_exists=lambda _path: True,
        api_key_available=True,
        config_base_dir=str(config_dir),
    )
    return [issue for issue in issues if "lossary" in issue.message]


_BASE_CONFIG = {
    "target_project_root": "/repo",
    "input_folder": "i18n",
    "supported_locales": [{"code": "de", "name": "German"}],
}


def test_validation_reports_an_unreadable_default_glossary(tmp_path):
    """`glossary.json` next to the config is the default glossary file."""
    (tmp_path / "glossary.json").write_text("{not json", encoding="utf-8")

    issues = _glossary_issues(dict(_BASE_CONFIG), tmp_path)

    assert [issue.level for issue in issues] == ["error"]
    assert str(tmp_path / "glossary.json") in issues[0].message


def test_validation_reports_an_unreadable_configured_glossary(tmp_path):
    """A relative `glossary_file_path` is resolved against the config file."""
    (tmp_path / "terms.json").write_text("[]", encoding="utf-8")

    issues = _glossary_issues(
        {**_BASE_CONFIG, "glossary_file_path": "terms.json"}, tmp_path
    )

    assert [issue.level for issue in issues] == ["error"]
    assert "must contain a JSON object" in issues[0].message


def test_validation_warns_when_the_configured_glossary_is_missing(tmp_path):
    """A configured path that does not exist is probably a typo."""
    issues = _glossary_issues(
        {**_BASE_CONFIG, "glossary_file_path": "terms.json"}, tmp_path
    )

    assert [issue.level for issue in issues] == ["warning"]


def test_validation_accepts_a_missing_default_and_a_valid_glossary(tmp_path):
    """No glossary at all, and a well-formed glossary, are both fine."""
    assert _glossary_issues(dict(_BASE_CONFIG), tmp_path) == []

    (tmp_path / "glossary.json").write_text(
        json.dumps({"de": {"account": "Konto"}}), encoding="utf-8"
    )
    assert _glossary_issues(dict(_BASE_CONFIG), tmp_path) == []


@pytest.mark.parametrize("command", ["check", "validate", "doctor", "smoke"])
def test_cli_preflight_fails_on_an_unreadable_glossary(tmp_path, capsys, command):
    """Every preflight command reports the problem before a run is started."""
    repo = tmp_path / "repo"
    i18n = repo / "i18n"
    i18n.mkdir(parents=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "target_project_root": str(repo),
            "input_folder": str(i18n),
            "localization_format": "json",
            "localization_layout": "suffix",
            "dry_run": True,
            "supported_locales": [{"code": "de", "name": "German"}],
        }),
        encoding="utf-8",
    )
    (tmp_path / "glossary.json").write_text('{"de": {"a": "b",}}', encoding="utf-8")

    exit_code = cli.main([command, "--config", str(config_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "glossary.json" in captured.err + captured.out
    assert "Preflight OK" not in captured.out
    assert "Configuration OK" not in captured.out

    (tmp_path / "glossary.json").write_text("{}", encoding="utf-8")
    assert cli.main([command, "--config", str(config_path)]) == 0
