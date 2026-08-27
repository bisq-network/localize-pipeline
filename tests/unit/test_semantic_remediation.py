import json

from localize.localization_profiles import load_localization_profiles
from localize.semantic_remediation import apply_semantic_review_suggestions


def test_semantic_remediation_applies_properties_suggestion_with_placeholder_parity(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "messages.properties").write_text("hello=Hello {0}\n", encoding="utf-8")
    target_path = resources / "messages_de.properties"
    target_path.write_text("hello=Hallo\n", encoding="utf-8")

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "messages_de.properties",
                "key": "hello",
                "severity": "error",
                "suggested_value": "Hallo {0}",
            }
        ],
        locale_codes=["de"],
        localization_profiles=load_localization_profiles({"localization_format": "java_properties"}),
        changed_identities={("messages_de.properties", "hello")},
    )

    assert result.applied_count == 1
    assert result.skipped_count == 0
    assert "hello=Hallo {0}" in target_path.read_text(encoding="utf-8")


def test_semantic_remediation_skips_suggestions_that_break_placeholders(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "messages.properties").write_text("hello=Hello {0}\n", encoding="utf-8")
    target_path = resources / "messages_de.properties"
    target_path.write_text("hello=Hallo\n", encoding="utf-8")

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "messages_de.properties",
                "key": "hello",
                "severity": "error",
                "suggested_value": "Hallo",
            }
        ],
        locale_codes=["de"],
        localization_profiles=load_localization_profiles({"localization_format": "java_properties"}),
        changed_identities={("messages_de.properties", "hello")},
    )

    assert result.applied_count == 0
    assert result.skipped_count == 1
    assert "hello=Hallo\n" in target_path.read_text(encoding="utf-8")


def test_semantic_remediation_rejects_source_absent_numeric_constraints(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "messages.properties").write_text(
        "validation.invalidAmount=Invalid amount\n",
        encoding="utf-8",
    )
    target_path = resources / "messages_de.properties"
    target_path.write_text(
        "validation.invalidAmount=Ungültiger Betrag\n",
        encoding="utf-8",
    )

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "messages_de.properties",
                "key": "validation.invalidAmount",
                "severity": "error",
                "suggested_value": "Gib einen Betrag mit höchstens 8 Dezimalstellen ein",
            }
        ],
        locale_codes=["de"],
        localization_profiles=load_localization_profiles({"localization_format": "java_properties"}),
        changed_identities={("messages_de.properties", "validation.invalidAmount")},
    )

    assert result.applied_count == 0
    assert result.skipped[0]["reason"] == "suggestion introduces numeric literals absent from source"
    assert target_path.read_text(encoding="utf-8") == (
        "validation.invalidAmount=Ungültiger Betrag\n"
    )


def test_semantic_remediation_allows_localized_source_numeric_literals(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "messages.properties").write_text(
        "validation.invalidAmount=Use at most 8 decimal places\n",
        encoding="utf-8",
    )
    target_path = resources / "messages_ar.properties"
    target_path.write_text(
        "validation.invalidAmount=قيمة غير صالحة\n",
        encoding="utf-8",
    )

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "messages_ar.properties",
                "key": "validation.invalidAmount",
                "severity": "error",
                "suggested_value": "استخدم ٨ منازل عشرية كحد أقصى",
            }
        ],
        locale_codes=["ar"],
        localization_profiles=load_localization_profiles({"localization_format": "java_properties"}),
        changed_identities={("messages_ar.properties", "validation.invalidAmount")},
    )

    assert result.applied_count == 1
    assert result.skipped_count == 0
    assert "استخدم ٨ منازل عشرية كحد أقصى" in target_path.read_text(encoding="utf-8")


def test_semantic_remediation_skips_suggestions_with_line_breaks(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "messages.properties").write_text("hello=Hello\n", encoding="utf-8")
    target_path = resources / "messages_de.properties"
    target_path.write_text("hello=Hallo\n", encoding="utf-8")

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "messages_de.properties",
                "key": "hello",
                "severity": "error",
                "suggested_value": "Hallo\rinjected.key=value",
            }
        ],
        locale_codes=["de"],
        localization_profiles=load_localization_profiles({"localization_format": "java_properties"}),
        changed_identities={("messages_de.properties", "hello")},
    )

    assert result.applied_count == 0
    assert result.skipped[0]["reason"] == "suggestion contains line break characters"
    assert target_path.read_text(encoding="utf-8") == "hello=Hallo\n"


def test_semantic_remediation_applies_json_locale_directory_suggestion(tmp_path):
    resources = tmp_path / "resources"
    source_dir = resources / "en"
    target_dir = resources / "de"
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    (source_dir / "common.json").write_text(
        json.dumps({"hello": "Hello {0}"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    target_path = target_dir / "common.json"
    target_path.write_text(
        json.dumps({"hello": "Hallo"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "de/common.json",
                "key": "/hello",
                "severity": "error",
                "suggested_value": "Hallo {0}",
            }
        ],
        locale_codes=["de"],
        localization_profiles=load_localization_profiles({
            "localization_formats": [
                {
                    "id": "json",
                    "layout": {"id": "locale_directory", "source_locale": "en"},
                }
            ]
        }),
        changed_identities={("de/common.json", "/hello")},
    )

    assert result.applied_count == 1
    assert json.loads(target_path.read_text(encoding="utf-8"))["hello"] == "Hallo {0}"


def test_semantic_remediation_rejects_unchanged_entries(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "messages.properties").write_text("hello=Hello {0}\n", encoding="utf-8")
    target_path = resources / "messages_de.properties"
    target_path.write_text("hello=Hallo\n", encoding="utf-8")

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "messages_de.properties",
                "key": "hello",
                "severity": "error",
                "suggested_value": "Hallo {0}",
            }
        ],
        locale_codes=["de"],
        localization_profiles=load_localization_profiles({"localization_format": "java_properties"}),
        changed_identities=set(),
    )

    assert result.applied_count == 0
    assert result.skipped[0]["reason"] == "finding is not scoped to a changed entry"
    assert "hello=Hallo\n" in target_path.read_text(encoding="utf-8")


def test_semantic_remediation_rejects_out_of_tree_findings(tmp_path):
    resources = tmp_path / "resources"
    outside = tmp_path / "outside"
    resources.mkdir()
    outside.mkdir()
    (resources / "messages.properties").write_text("hello=Hello {0}\n", encoding="utf-8")
    target_path = outside / "messages_de.properties"
    target_path.write_text("hello=Hallo\n", encoding="utf-8")

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "../outside/messages_de.properties",
                "key": "hello",
                "severity": "error",
                "suggested_value": "Hallo {0}",
            }
        ],
        locale_codes=["de"],
        localization_profiles=load_localization_profiles({"localization_format": "java_properties"}),
        changed_identities={("../outside/messages_de.properties", "hello")},
    )

    assert result.applied_count == 0
    assert result.skipped[0]["reason"] == "target file is outside input_folder"
    assert "hello=Hallo\n" in target_path.read_text(encoding="utf-8")


def test_semantic_remediation_skips_duplicate_changed_identity(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "messages.properties").write_text("hello=Hello {0}\n", encoding="utf-8")
    target_path = resources / "messages_de.properties"
    target_path.write_text("hello=Hallo\n", encoding="utf-8")

    result = apply_semantic_review_suggestions(
        repo_root=str(tmp_path),
        input_folder=str(resources),
        findings=[
            {
                "file": "messages_de.properties",
                "key": "hello",
                "severity": "error",
                "reason": "First suggestion.",
                "suggested_value": "Hallo {0}",
            },
            {
                "file": "messages_de.properties",
                "key": "hello",
                "severity": "error",
                "reason": "Second suggestion.",
                "suggested_value": "Guten Tag {0}",
            },
        ],
        locale_codes=["de"],
        localization_profiles=load_localization_profiles({"localization_format": "java_properties"}),
        changed_identities={("messages_de.properties", "hello")},
    )

    assert result.applied_count == 1
    assert result.skipped_count == 1
    assert result.skipped[0]["reason"] == "duplicate finding for changed entry"
    assert "hello=Hallo {0}" in target_path.read_text(encoding="utf-8")
