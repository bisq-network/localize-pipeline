"""Regression coverage for action direction and warning state in translated text."""

import pytest

from localize.translation_quality_gate import analyze_semantic_qa_changes


@pytest.mark.parametrize(
    ("locale", "source", "target", "rule_id"),
    [
        ("sv", "Lock the bond now.", "Lås upp obligationen nu.", "reversed-bond-lock-action"),
        ("sv", "Unlock the bond now.", "Lås obligationen nu.", "reversed-bond-lock-action"),
        ("lt", "Your unsaved note will be lost.", "Jūsų pastaba bus prarasta.", "missing-unsaved-note-state"),
        ("sk", "Your unsaved note will be lost.", "Vaša poznámka sa stratí.", "missing-unsaved-note-state"),
    ],
)
def test_quality_gate_finds_reversed_action_or_missing_unsaved_state(
    tmp_path, locale, source, target, rule_id,
):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "messages.properties").write_text(f"warning={source}\n", encoding="utf-8")
    diff = f"+++ b/resources/messages_{locale}.properties\n+warning={target}\n"

    stats = analyze_semantic_qa_changes(
        diff_text=diff,
        repo_root=str(tmp_path),
        input_folder=str(resources),
        locale_codes=[locale],
    )

    assert stats.errors_count == 1
    assert stats.examples[0]["rule_id"] == rule_id


@pytest.mark.parametrize(
    ("locale", "source", "target"),
    [
        ("sv", "Lock the bond now.", "Lås obligationen nu."),
        ("sv", "Unlock the bond now.", "Lås upp obligationen nu."),
        ("lt", "Your unsaved note will be lost.", "Jūsų neišsaugota pastaba bus prarasta."),
        ("sk", "Your unsaved note will be lost.", "Vaša neuložená poznámka sa stratí."),
        ("lt", "{note}", "{note}"),
        ("sv", "Bond", "Bond"),
    ],
)
def test_quality_gate_accepts_preserved_meaning_and_source_identical_tokens(
    tmp_path, locale, source, target,
):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "messages.properties").write_text(f"warning={source}\n", encoding="utf-8")
    diff = f"+++ b/resources/messages_{locale}.properties\n+warning={target}\n"

    stats = analyze_semantic_qa_changes(
        diff_text=diff,
        repo_root=str(tmp_path),
        input_folder=str(resources),
        locale_codes=[locale],
    )

    assert stats.findings_count == 0
