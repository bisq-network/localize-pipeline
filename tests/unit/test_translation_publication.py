"""The publication boundary must fail closed on ambiguous skip evidence."""

import json
from pathlib import Path
import pytest

from localize.translation_publication import plan_publication


@pytest.mark.parametrize("skipped", [None, "settings_pt_PT.properties", ["../outside.properties"], ["/tmp/outside.properties"], ["nested/../settings.properties"], ["a\\b.properties"], ["a\n.properties"], ["./settings.properties"], ["missing.properties"], [23]])
def test_invalid_skip_evidence_blocks_publication(tmp_path, skipped):
    """Malformed and stale skip records cannot silently admit imports."""
    folder = tmp_path / "l10n"
    folder.mkdir()
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"skipped_files": skipped}))
    with pytest.raises((ValueError, FileNotFoundError)):
        plan_publication(summary, tmp_path, folder, ["l10n/settings.properties"])
    assert not list(tmp_path.glob("skipped-inputs-*"))


def test_symlink_skip_evidence_blocks_publication(tmp_path):
    """Do not copy external content or accept aliases as validated skip paths."""
    folder = tmp_path / "l10n"
    folder.mkdir()
    outside = tmp_path / "secret.properties"
    outside.write_text("secret=value")
    (folder / "settings.properties").symlink_to(outside)
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"skipped_files": ["settings.properties"]}))
    with pytest.raises(ValueError, match="without symlinks"):
        plan_publication(summary, tmp_path, folder, ["l10n/settings.properties"])
    assert not list(tmp_path.glob("skipped-inputs-*"))


def test_general_warning_is_not_a_skip_instruction(tmp_path):
    """Only explicit producer skip records may remove publication candidates."""
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"skipped_files": [], "pipeline_warnings": [{"file": "settings.properties"}]}))
    assert plan_publication(summary, tmp_path, tmp_path, ["settings.properties"])["files"] == ["settings.properties"]


def test_validation_writer_records_explicit_skips(tmp_path):
    """The real summary producer exports input-relative skip names and reasons."""
    from localize.translate_localization_files import write_translation_validation_summary

    summary = tmp_path / "summary.json"
    write_translation_validation_summary(str(summary), {}, {"settings_pt_PT.properties": ["U+007F"]})
    result = json.loads(summary.read_text())
    assert result["skipped_files"] == ["settings_pt_PT.properties"]
    assert result["pipeline_warnings"] == [{"file": "settings_pt_PT.properties", "errors": ["U+007F"]}]


@pytest.mark.parametrize("connector", [False, True])
def test_skipped_input_survives_stale_debug_output(tmp_path, monkeypatch, connector):
    """Retained debug output cannot destroy the skipped import's evidence."""
    from localize import translate_localization_files as translator
    from localize.connectors import FilesystemSourceConnector

    source = tmp_path / "output"
    target = tmp_path / "input"
    source.mkdir()
    target.mkdir()
    name = "settings_pt_PT.properties"
    (source / name).write_text("key=stale output\n")
    (target / name).write_text("key=imported English\x7f\n")
    monkeypatch.setattr(translator, "DRY_RUN", False)
    copy = (
        FilesystemSourceConnector(detect_changed_translation_files=lambda *args, **kwargs: []).copy_translated_files_back
        if connector else translator.copy_translated_files_back
    )
    copy(str(source), str(target), skipped_files={name: ["U+007F"]})
    assert (target / name).read_text() == "key=imported English\x7f\n"


def test_nested_input_paths_preserve_layout_and_filter_exactly(tmp_path):
    """Relative skips use the configured input root, including nested layouts."""
    folder = tmp_path / "repo" / "resources"
    path = "values/pt_BR/messages.json"
    source = folder / path
    source.parent.mkdir(parents=True)
    source.write_text('{"label":"English"}')
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"skipped_files": [path]}))
    plan = plan_publication(summary, tmp_path / "repo", folder, ["resources/" + path, "resources/values/de/messages.json"])
    assert plan["files"] == ["resources/values/de/messages.json"]
    assert (Path(plan["evidence_directory"]) / path).read_text() == source.read_text()
